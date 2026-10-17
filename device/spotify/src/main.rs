//! `em-spotify` — the Echo's Spotify Connect engine.
//!
//! Runs on the device, fetches its own audio from Spotify, and hands PCM to
//! the firmware. Started and supervised by the Go server, which owns the two
//! unix sockets it talks over and decides whether it should be running at
//! all.
//!
//! ```text
//!   Spotify app ──zeroconf──> em-spotify ──audio socket──> PcmSpeaker
//!                                  │                       (mix, duck,
//!   Go firmware <─control socket───┘                        output chain,
//!                                                           volume)
//! ```
//!
//! ## What this process decides, and what it does not
//!
//! It decides nothing about policy. Whether Spotify is enabled, which
//! volume the codec is at, when to duck under a voice turn, and what to
//! search for are all somebody else's business — the firmware's or the
//! controller's. This process logs in, holds a Connect session, plays what
//! it is told to play, and reports what is happening.
//!
//! ## Credentials
//!
//! Linking happens on the device, in the user's own Spotify app: with no
//! cached credentials librespot advertises over zeroconf, the user picks
//! the Echo, and Spotify hands over a reusable blob which librespot writes
//! into its cache. No password ever reaches us, the controller never sees
//! the blob, and nothing here logs it.
//!
//! The cache holds credentials and the last volume and **no audio**. An
//! audio cache would cut network use on a repeated track and spend eMMC
//! writes to do it, on a device that runs for years on flash that cannot be
//! replaced — the same reasoning that makes the console-password file a
//! write-on-change.
//!
//! ## Premium
//!
//! librespot refuses non-Premium accounts by calling `exit(1)` from inside
//! `Session::check_catalogue`, which runs when the access point pushes user
//! attributes — so it can happen a second after a successful login and
//! there is no hook to intercept. Two things cover it, and both are needed:
//! this process checks the account type itself as soon as it is known and
//! reports `premium_required` cleanly, and the firmware matches librespot's
//! own stderr line as a backstop for the race where librespot exits first.

use std::path::PathBuf;
use std::sync::mpsc::{Receiver as StdReceiver, TryRecvError};
use std::sync::Arc;
use std::time::Duration;

use futures_util::StreamExt;
use librespot_connect::{ConnectConfig, LoadRequest, LoadRequestOptions, Spirc};
use librespot_core::authentication::Credentials;
use librespot_core::cache::Cache;
use librespot_core::config::SessionConfig;
use librespot_core::error::ErrorKind;
use librespot_core::session::Session;
use librespot_discovery::Discovery;
use librespot_metadata::audio::UniqueFields;
use librespot_playback::config::{Bitrate, PlayerConfig};
// In scope for `get_soft_volume`, which is a trait method rather than an
// inherent one on ReportingMixer.
use librespot_playback::mixer::Mixer;
use librespot_playback::player::{Player, PlayerEvent};
use tokio::io::{AsyncBufReadExt, AsyncWriteExt, BufReader};
use tokio::net::UnixStream;
use tokio::sync::{broadcast, mpsc, oneshot, Mutex};

use em_spotify::mixer::{ReportingMixer, VolumeNote};
use em_spotify::proto::{Command, Event, LinkStatus, PlayState, Reply, Request};
use em_spotify::sink::{PeriodSink, SinkNote};
use em_spotify::volume::VolumeGate;

/// How many events to hold for a control connection that is not reading.
///
/// Small on purpose: the firmware reads promptly, and a backlog of stale
/// playback states is worth less than the snapshot that is replayed on
/// every reconnect anyway.
const EVENT_BUFFER: usize = 64;

/// Backoff between attempts to reach Spotify, and its ceiling.
///
/// The ceiling matters more than the start: an Echo whose WiFi is down
/// should keep trying for as long as it is switched on, without becoming a
/// once-a-minute retry that takes a minute to notice the network came back.
const RETRY_START: Duration = Duration::from_secs(2);
const RETRY_MAX: Duration = Duration::from_secs(60);

/// How long to wait for the account type after login before giving up on
/// learning it. Not a failure if it never arrives — a session that works is
/// evidence enough that the account can stream.
const ACCOUNT_TYPE_WAIT: Duration = Duration::from_secs(10);

fn main() {
    let args = match Args::parse(std::env::args().skip(1)) {
        Ok(a) => a,
        Err(e) => {
            eprintln!("em-spotify: {e}\n\n{}", Args::usage());
            std::process::exit(2);
        }
    };

    // Logging to stderr, which the firmware captures. The default is `info`
    // because this process is normally invisible and a device that will not
    // play needs to be explicable from a support bundle without somebody
    // first turning logging up and waiting for it to happen again.
    env_logger::Builder::from_env(env_logger::Env::default().default_filter_or(&args.log_filter))
        .format_timestamp_millis()
        .init();

    // Two worker threads. librespot's player takes a thread and a runtime
    // of its own regardless, so this runtime only carries the session, the
    // Connect state machine and two sockets. Four workers on a four-core
    // SoC that is also meeting a 160ms microphone deadline would be
    // claiming the whole device for the part of the work that is waiting on
    // the network.
    let rt = match tokio::runtime::Builder::new_multi_thread()
        .worker_threads(2)
        .enable_all()
        .build()
    {
        Ok(rt) => rt,
        Err(e) => {
            eprintln!("em-spotify: could not start a runtime: {e}");
            std::process::exit(1);
        }
    };

    if let Err(e) = rt.block_on(run(args)) {
        log::error!("{e}");
        std::process::exit(1);
    }
}

struct Args {
    /// The name shown in the Spotify app. The device's label, passed in by
    /// the firmware, so an Echo called "Kitchen" is "Kitchen" in Spotify.
    name: String,
    cache_dir: PathBuf,
    control_socket: PathBuf,
    audio_socket: PathBuf,
    zeroconf_port: u16,
    /// Where the codec is right now, in librespot's 0..=65535 range, so the
    /// app's slider starts out telling the truth.
    initial_volume: u16,
    bitrate: Bitrate,
    /// Stable per-device id. Passed in rather than generated, because
    /// librespot's default is a fresh UUID per process and that makes the
    /// Echo a NEW device in the Spotify app on every restart.
    device_id: String,
    log_filter: String,
}

impl Args {
    fn usage() -> &'static str {
        "usage: em-spotify --name <label> --device-id <id> --cache-dir <path>\n\
         \x20                 --control-socket <path> --audio-socket <path>\n\
         \x20                 [--zeroconf-port N] [--initial-volume 0-65535]\n\
         \x20                 [--bitrate 96|160|320] [--log-filter <spec>]"
    }

    fn parse(args: impl Iterator<Item = String>) -> Result<Args, String> {
        let mut name = None;
        let mut device_id = None;
        let mut cache_dir = None;
        let mut control_socket = None;
        let mut audio_socket = None;
        let mut zeroconf_port = 0u16;
        let mut initial_volume = u16::MAX / 2;
        let mut bitrate = Bitrate::Bitrate160;
        let mut log_filter = "info".to_string();

        let mut it = args.peekable();
        while let Some(flag) = it.next() {
            let mut value = || it.next().ok_or_else(|| format!("{flag} needs a value"));
            match flag.as_str() {
                "--name" => name = Some(value()?),
                "--device-id" => device_id = Some(value()?),
                "--cache-dir" => cache_dir = Some(PathBuf::from(value()?)),
                "--control-socket" => control_socket = Some(PathBuf::from(value()?)),
                "--audio-socket" => audio_socket = Some(PathBuf::from(value()?)),
                "--zeroconf-port" => {
                    let v = value()?;
                    zeroconf_port = v
                        .parse()
                        .map_err(|_| format!("--zeroconf-port {v} is not a port number"))?;
                }
                "--initial-volume" => {
                    let v = value()?;
                    initial_volume = v
                        .parse()
                        .map_err(|_| format!("--initial-volume {v} is not 0-65535"))?;
                }
                "--bitrate" => {
                    let v = value()?;
                    bitrate = match v.as_str() {
                        "96" => Bitrate::Bitrate96,
                        "160" => Bitrate::Bitrate160,
                        "320" => Bitrate::Bitrate320,
                        _ => return Err(format!("--bitrate {v} is not 96, 160 or 320")),
                    };
                }
                "--log-filter" => log_filter = value()?,
                other => return Err(format!("unknown argument {other}")),
            }
        }

        Ok(Args {
            name: name.ok_or("--name is required")?,
            device_id: device_id.ok_or("--device-id is required")?,
            cache_dir: cache_dir.ok_or("--cache-dir is required")?,
            control_socket: control_socket.ok_or("--control-socket is required")?,
            audio_socket: audio_socket.ok_or("--audio-socket is required")?,
            zeroconf_port,
            initial_volume,
            bitrate,
            log_filter,
        })
    }
}

/// A command from the firmware with somewhere to put the answer.
type Job = (Request, oneshot::Sender<Reply>);

/// The last thing we said about each kind of state.
///
/// Replayed whenever a control connection arrives, because the firmware
/// restarting must not leave the dashboard showing whatever it believed
/// before. Same reason `Status` exists as a command.
#[derive(Default)]
struct Snapshot {
    link: Option<Event>,
    session: Option<Event>,
    state: Option<Event>,
    track: Option<Event>,
    volume: Option<Event>,
}

impl Snapshot {
    fn record(&mut self, e: &Event) {
        let slot = match e {
            Event::Link { .. } => &mut self.link,
            Event::Session { .. } => &mut self.session,
            Event::State { .. } => &mut self.state,
            Event::Track { .. } => &mut self.track,
            Event::Volume { .. } => &mut self.volume,
            // Shuffle, repeat and errors are moments rather than states.
            // Replaying an old error on reconnect would report a fault that
            // is no longer happening.
            _ => return,
        };
        *slot = Some(e.clone());
    }

    fn replay(&self) -> Vec<Event> {
        [
            &self.link,
            &self.session,
            &self.state,
            &self.track,
            &self.volume,
        ]
        .into_iter()
        .flatten()
        .cloned()
        .collect()
    }
}

/// Everything the tasks share.
struct Shared {
    events: broadcast::Sender<Event>,
    snapshot: Mutex<Snapshot>,
    /// Which volume changes the firmware is told about. See `volume.rs` —
    /// the rule is "unchanged is not news", and it is what stops a level the
    /// device set coming back as a Spotify event and looping.
    volume: VolumeGate,
}

impl Shared {
    async fn emit(&self, e: Event) {
        if let Event::Volume { level } = e {
            // Unchanged is not news, and acting on it is what loops.
            if !self.volume.should_report(level) {
                return;
            }
        }
        self.snapshot.lock().await.record(&e);
        // A send failure means nothing is subscribed, which is the ordinary
        // state while the firmware is not connected. Not an error.
        let _ = self.events.send(e);
    }

    /// Record a level that came FROM the firmware, so the echo of it is
    /// dropped rather than applied.
    ///
    /// The snapshot is written too: the firmware already has this value, so
    /// a later `Status` replay must report it rather than the one before.
    async fn note_device_volume(&self, level: u16) {
        self.volume.note_device(level);
        self.snapshot.lock().await.record(&Event::Volume { level });
    }
}

async fn run(args: Args) -> Result<(), String> {
    let (events_tx, _) = broadcast::channel(EVENT_BUFFER);
    let shared = Arc::new(Shared {
        events: events_tx,
        snapshot: Mutex::new(Snapshot::default()),
        volume: VolumeGate::default(),
    });

    let (job_tx, job_rx) = mpsc::channel::<Job>(32);

    tokio::spawn(control_plane(
        args.control_socket.clone(),
        Arc::clone(&shared),
        job_tx,
    ));

    session_loop(args, shared, job_rx).await
}

/// Dial the firmware's control socket, carry commands in and events out,
/// and redial for as long as the process lives.
///
/// The daemon dials rather than listens. That is deliberate: emOS's posture
/// is that nothing listens, and while librespot must open a Connect port to
/// be a speaker at all, there is no reason for the local control channel to
/// add a second one.
async fn control_plane(path: PathBuf, shared: Arc<Shared>, jobs: mpsc::Sender<Job>) {
    let mut backoff = RETRY_START;
    loop {
        match UnixStream::connect(&path).await {
            Ok(stream) => {
                backoff = RETRY_START;
                log::info!("control plane connected on {}", path.display());
                if let Err(e) = serve_control(stream, &shared, &jobs).await {
                    log::warn!("control plane closed: {e}");
                } else {
                    log::info!("control plane closed");
                }
            }
            Err(e) => {
                log::debug!("control socket {} unavailable: {e}", path.display());
                tokio::time::sleep(backoff).await;
                backoff = (backoff * 2).min(RETRY_MAX);
                continue;
            }
        }
        tokio::time::sleep(RETRY_START).await;
    }
}

async fn serve_control(
    stream: UnixStream,
    shared: &Arc<Shared>,
    jobs: &mpsc::Sender<Job>,
) -> Result<(), String> {
    let mut rx = shared.events.subscribe();
    let (read_half, mut write_half) = stream.into_split();
    let mut lines = BufReader::new(read_half).lines();

    // Replay first, so a firmware that has just restarted knows where it
    // stands without having to ask.
    for e in shared.snapshot.lock().await.replay() {
        write_event(&mut write_half, &e).await?;
    }

    loop {
        tokio::select! {
            line = lines.next_line() => {
                let line = match line {
                    Ok(Some(l)) => l,
                    Ok(None) => return Ok(()),
                    Err(e) => return Err(format!("reading: {e}")),
                };
                if line.trim().is_empty() {
                    continue;
                }
                let reply = match serde_json::from_str::<Request>(&line) {
                    Ok(req) => {
                        let id = req.id;
                        let (tx, rx) = oneshot::channel();
                        if jobs.send((req, tx)).await.is_err() {
                            Reply::Error { id, message: "player is not running".into() }
                        } else {
                            match rx.await {
                                Ok(r) => r,
                                Err(_) => Reply::Error {
                                    id,
                                    message: "player dropped the command".into(),
                                },
                            }
                        }
                    }
                    // Named rather than ignored: the sender is our own
                    // firmware, so a line we cannot parse is a bug, and
                    // silence here would present as a hang.
                    Err(e) => Reply::Error { id: None, message: format!("bad request: {e}") },
                };
                let mut out = serde_json::to_string(&reply)
                    .map_err(|e| format!("serialising a reply: {e}"))?;
                out.push('\n');
                write_half.write_all(out.as_bytes()).await
                    .map_err(|e| format!("writing a reply: {e}"))?;
            }
            event = rx.recv() => {
                match event {
                    Ok(e) => write_event(&mut write_half, &e).await?,
                    // Lagged: the firmware was not reading. The snapshot is
                    // the recovery, and it is already correct, so say so and
                    // carry on rather than dropping the connection.
                    Err(broadcast::error::RecvError::Lagged(n)) => {
                        log::warn!("control plane fell {n} events behind");
                        for e in shared.snapshot.lock().await.replay() {
                            write_event(&mut write_half, &e).await?;
                        }
                    }
                    Err(broadcast::error::RecvError::Closed) => return Ok(()),
                }
            }
        }
    }
}

async fn write_event(out: &mut tokio::net::unix::OwnedWriteHalf, e: &Event) -> Result<(), String> {
    let mut line = serde_json::to_string(e).map_err(|err| format!("serialising {e:?}: {err}"))?;
    line.push('\n');
    out.write_all(line.as_bytes())
        .await
        .map_err(|err| format!("writing an event: {err}"))
}

/// Hold a Spotify session for as long as the process lives, relinking and
/// reconnecting as needed.
async fn session_loop(
    args: Args,
    shared: Arc<Shared>,
    mut jobs: mpsc::Receiver<Job>,
) -> Result<(), String> {
    let session_config = SessionConfig {
        device_id: args.device_id.clone(),
        ..SessionConfig::default()
    };

    // Credentials and volume persist; audio does not. See the module docs
    // on eMMC wear.
    let cache = Cache::new(
        Some(args.cache_dir.as_path()),
        Some(args.cache_dir.as_path()),
        None,
        None,
    )
    .map_err(|e| {
        format!(
            "could not open the credential cache at {}: {e}",
            args.cache_dir.display()
        )
    })?;

    let mut discovery =
        Discovery::builder(args.device_id.clone(), session_config.client_id.clone())
            .name(args.name.clone())
            .port(args.zeroconf_port)
            .launch()
            .map_err(|e| format!("could not advertise over zeroconf: {e}"))?;

    let mut backoff = RETRY_START;
    let mut credentials = cache.credentials();
    if credentials.is_none() {
        shared
            .emit(Event::Link {
                status: LinkStatus::AwaitingLink,
                detail: Some(format!(
                    "pick \"{}\" in the Spotify app to link this Echo",
                    args.name
                )),
            })
            .await;
    }

    loop {
        // ── get something to log in with ───────────────────────────────
        let creds = match credentials.take() {
            Some(c) => c,
            None => {
                // Nothing cached. Wait to be picked in somebody's Spotify
                // app, while still answering control commands so the
                // dashboard is not dealing with a silent process.
                match await_link(&mut discovery, &mut jobs, &shared).await {
                    Some(c) => c,
                    None => return Err("zeroconf discovery stopped".into()),
                }
            }
        };

        // ── hold a session ─────────────────────────────────────────────
        match hold_session(
            &args,
            &session_config,
            &cache,
            &creds,
            &shared,
            &mut jobs,
            &mut discovery,
        )
        .await
        {
            SessionEnd::Relink(new_creds) => {
                log::info!("relinking: a Spotify client handed over new credentials");
                credentials = Some(new_creds);
                backoff = RETRY_START;
            }
            SessionEnd::Refused(detail) => {
                // Positive evidence that the stored credential is no good.
                // Dropping it is what turns a device that will never work
                // again into one the user can re-link, and the status says
                // which of those happened.
                log::warn!("Spotify refused this device's credentials: {detail}");
                if let Err(e) = clear_credentials(&args.cache_dir) {
                    log::warn!("could not remove the stale credentials: {e}");
                }
                shared
                    .emit(Event::Link {
                        status: LinkStatus::AuthFailed,
                        detail: Some(detail),
                    })
                    .await;
                credentials = None;
            }
            SessionEnd::PremiumRequired => {
                shared
                    .emit(Event::Link {
                        status: LinkStatus::PremiumRequired,
                        detail: Some(
                            "this Spotify account cannot stream to a Connect speaker; \
                             Spotify Premium is required"
                                .into(),
                        ),
                    })
                    .await;
                // Not retried. A free account will not become Premium by
                // being asked again, and the retry would be a login every
                // two seconds for as long as the Echo is switched on.
                return Ok(());
            }
            SessionEnd::Dropped(detail) => {
                log::warn!("Spotify session ended: {detail}");
                shared
                    .emit(Event::Link {
                        status: LinkStatus::Offline,
                        detail: Some(detail),
                    })
                    .await;
                // The credential is still believed good, so keep it and
                // retry. This is the WiFi-came-back case.
                credentials = cache.credentials();
                tokio::time::sleep(backoff).await;
                backoff = (backoff * 2).min(RETRY_MAX);
            }
            SessionEnd::LoggedOut => {
                log::info!("logged out at the firmware's request");
                if let Err(e) = clear_credentials(&args.cache_dir) {
                    log::warn!("could not remove the credentials: {e}");
                }
                shared
                    .emit(Event::Link {
                        status: LinkStatus::AwaitingLink,
                        detail: Some("disconnected".into()),
                    })
                    .await;
                credentials = None;
                backoff = RETRY_START;
            }
        }
    }
}

/// Remove librespot's cached credential blob.
///
/// By path rather than through `Cache`, which has no remove-credentials
/// call. The filename is librespot's (`Cache::new` joins `credentials.json`
/// onto the credentials directory), so it is pinned by a test rather than
/// remembered.
fn clear_credentials(cache_dir: &std::path::Path) -> std::io::Result<()> {
    let path = cache_dir.join(CREDENTIALS_FILE);
    match std::fs::remove_file(&path) {
        Ok(()) => Ok(()),
        // Already gone is the desired state, not a failure.
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => Ok(()),
        Err(e) => Err(e),
    }
}

/// librespot's own name for the credential file inside the cache directory.
const CREDENTIALS_FILE: &str = "credentials.json";

/// Wait for a Spotify client to hand over credentials, answering control
/// commands meanwhile.
async fn await_link(
    discovery: &mut Discovery,
    jobs: &mut mpsc::Receiver<Job>,
    shared: &Arc<Shared>,
) -> Option<Credentials> {
    loop {
        tokio::select! {
            creds = discovery.next() => return creds,
            job = jobs.recv() => {
                let (req, reply) = job?;
                // Nothing can be played before there is an account. Answer
                // honestly rather than accepting a command that will never
                // happen — the controller surfaces this verbatim.
                let r = match req.command {
                    Command::Status => {
                        for e in shared.snapshot.lock().await.replay() {
                            let _ = shared.events.send(e);
                        }
                        Reply::Ok { id: req.id }
                    }
                    Command::Logout => Reply::Ok { id: req.id },
                    Command::Volume { level } => {
                        // The Echo's volume exists before Spotify is linked.
                        // Keep it for the first session instead of reverting
                        // to the process's (possibly boot-time) argument.
                        shared.note_device_volume(level).await;
                        Reply::Ok { id: req.id }
                    }
                    _ => Reply::Error {
                        id: req.id,
                        message: "this Echo is not linked to a Spotify account yet".into(),
                    },
                };
                let _ = reply.send(r);
            }
        }
    }
}

/// Why a session stopped.
enum SessionEnd {
    /// A Spotify client linked this device while a session was running.
    Relink(Credentials),
    /// Spotify refused the credentials we hold.
    Refused(String),
    /// Logged in, but the account cannot stream.
    PremiumRequired,
    /// Lost for some other reason — network, access point, a dealer close.
    Dropped(String),
    /// The firmware asked us to forget the account.
    LoggedOut,
}

#[allow(clippy::too_many_arguments)]
async fn hold_session(
    args: &Args,
    session_config: &SessionConfig,
    cache: &Cache,
    creds: &Credentials,
    shared: &Arc<Shared>,
    jobs: &mut mpsc::Receiver<Job>,
    discovery: &mut Discovery,
) -> SessionEnd {
    let session = Session::new(session_config.clone(), Some(cache.clone()));

    // Channels the sink and mixer report on. std::sync::mpsc because both
    // run on librespot's own threads, outside this runtime.
    let (sink_notes_tx, sink_notes) = std::sync::mpsc::channel::<SinkNote>();
    let (volume_notes_tx, volume_notes) = std::sync::mpsc::channel::<VolumeNote>();

    // A login/relink may happen hours after process startup. The CLI value
    // is only a fallback; the device's latest level must seed BOTH halves.
    let connect_config = session_connect_config(args, shared);
    let mixer = ReportingMixer::new(connect_config.initial_volume, volume_notes_tx);

    let player_config = PlayerConfig {
        bitrate: args.bitrate,
        // Spotify's loudness normalisation, left OFF. The Echo already has
        // a measured output chain — EQ, bass guard and a limiter with
        // stock's own MBCL dynamics — and normalisation ahead of it would
        // move the level the compressors see, which on Radar is the thing
        // that selects the loudness curve. One gain law at a time.
        normalisation: false,
        // Position updates drive the Home Assistant progress reporting.
        // One a second: enough for a seek bar, nowhere near the per-frame
        // cost class the project refuses.
        position_update_interval: Some(Duration::from_secs(1)),
        ..PlayerConfig::default()
    };

    let audio_socket = args.audio_socket.clone();
    let player = Player::new(
        player_config,
        session.clone(),
        mixer.get_soft_volume(),
        move || Box::new(PeriodSink::new(audio_socket, sink_notes_tx)),
    );
    let mut player_events = player.get_player_event_channel();

    // Spirc::new calls session.connect(credentials, true) itself, so this
    // is where a login failure surfaces — and where the credential gets
    // written into the cache on success.
    let (spirc, spirc_task) = match Spirc::new(
        connect_config,
        session.clone(),
        creds.clone(),
        player.clone(),
        mixer.clone(),
    )
    .await
    {
        Ok(pair) => pair,
        Err(e) => return classify_login_error(&e),
    };

    shared
        .emit(Event::Link {
            status: LinkStatus::Linked,
            detail: None,
        })
        .await;
    shared
        .emit(Event::Session {
            connected: true,
            user: Some(session.username()),
        })
        .await;

    let spirc = Arc::new(spirc);
    let mut spirc_task = Box::pin(spirc_task);

    // Learn the account type once, so a free account is reported rather
    // than only killing the process from inside librespot.
    let mut account_check = Box::pin(tokio::time::sleep(ACCOUNT_TYPE_WAIT));
    let mut account_checked = false;
    let mut activation_volume_pending = false;

    let outcome = loop {
        tokio::select! {
            // The Connect state machine. When this returns the session is
            // over, whatever the reason.
            _ = &mut spirc_task => {
                break SessionEnd::Dropped("the Connect session ended".into());
            }

            event = player_events.recv() => {
                match event {
                    Some(e) => {
                        if let Some(level) = handle_player_event(
                            e, shared, &mixer, &mut activation_volume_pending,
                        ).await {
                            if let Err(e) = spirc.set_volume(level) {
                                log::warn!("could not synchronize activation volume: {e}");
                            }
                        }
                    }
                    None => break SessionEnd::Dropped("the player stopped".into()),
                }
            }

            job = jobs.recv() => {
                let Some((req, reply)) = job else {
                    break SessionEnd::Dropped("the control plane went away".into());
                };
                let id = req.id;
                match apply(&spirc, &mixer, shared, req, cache).await {
                    Ok(Some(end)) => {
                        let _ = reply.send(Reply::Ok { id });
                        break end;
                    }
                    Ok(None) => { let _ = reply.send(Reply::Ok { id }); }
                    Err(message) => { let _ = reply.send(Reply::Error { id, message }); }
                }
            }

            creds = discovery.next() => {
                match creds {
                    Some(c) => break SessionEnd::Relink(c),
                    // Discovery stopping is not fatal to a session that is
                    // already working; it only means nobody new can link
                    // until the process restarts.
                    None => log::warn!("zeroconf discovery stopped; relinking will need a restart"),
                }
            }

            _ = &mut account_check, if !account_checked => {
                account_checked = true;
                if let Some(kind) = session.get_user_attribute("type") {
                    if kind != "premium" {
                        log::error!("this account is {kind:?}, which cannot stream to a Connect speaker");
                        break SessionEnd::PremiumRequired;
                    }
                    log::info!("account type: {kind}");
                }
            }
        }

        // Drain what the sink and mixer have said since the last turn of
        // the loop. Both run on librespot's threads and post rather than
        // await, so this is where their notes become events.
        if drain_notes(&sink_notes, &volume_notes, shared).await {
            break SessionEnd::Dropped("the audio sink reported a fatal error".into());
        }
    };

    // Whatever happened, stop being a Connect device cleanly. Leaving the
    // Spirc up would keep this Echo in everybody's Spotify app while it is
    // unable to play.
    if let Err(e) = spirc.shutdown() {
        log::debug!("spirc shutdown: {e}");
    }
    shared
        .emit(Event::Session {
            connected: false,
            user: None,
        })
        .await;
    shared
        .emit(Event::State {
            state: PlayState::Stopped,
            position_ms: None,
            track_uri: None,
        })
        .await;

    outcome
}

/// Turn a login failure into a status the user can act on.
///
/// The split that matters is "your credentials are no good" against "we
/// could not reach Spotify": one needs re-linking and the other needs
/// waiting, and telling the user the wrong one costs them a re-link they
/// did not need or leaves them waiting for a device that will never
/// recover.
fn classify_login_error(e: &librespot_core::Error) -> SessionEnd {
    match e.kind {
        ErrorKind::Unauthenticated | ErrorKind::PermissionDenied => {
            SessionEnd::Refused(e.to_string())
        }
        _ => SessionEnd::Dropped(e.to_string()),
    }
}

/// Carry out one command. `Ok(Some(end))` means the session is over.
async fn apply(
    spirc: &Spirc,
    mixer: &Arc<ReportingMixer>,
    shared: &Arc<Shared>,
    req: Request,
    _cache: &Cache,
) -> Result<Option<SessionEnd>, String> {
    let r = match req.command {
        Command::Load {
            uri,
            start_playing,
            seek_to_ms,
        } => {
            // A voice "play X" arrives at a device nothing has transferred
            // playback to, so it has to take the active role first or the
            // load lands on whatever device Spotify thinks is current.
            spirc.activate().map_err(|e| e.to_string())?;
            spirc.load(LoadRequest::from_context_uri(
                uri,
                LoadRequestOptions {
                    start_playing,
                    seek_to: seek_to_ms,
                    ..LoadRequestOptions::default()
                },
            ))
        }
        Command::LoadTracks {
            uris,
            start_playing,
        } => {
            spirc.activate().map_err(|e| e.to_string())?;
            spirc.load(LoadRequest::from_tracks(
                uris,
                LoadRequestOptions {
                    start_playing,
                    ..LoadRequestOptions::default()
                },
            ))
        }
        Command::Play => spirc.play(),
        Command::Pause => spirc.pause(),
        Command::PlayPause => spirc.play_pause(),
        // There is no Spirc "stop". Pausing and winding to the start is
        // what a stop means to a Connect speaker, and it leaves the queue
        // intact so a later play does something sensible.
        Command::Stop => spirc.pause().and_then(|()| spirc.set_position_ms(0)),
        Command::Next => spirc.next(),
        Command::Prev => spirc.prev(),
        Command::Seek { position_ms } => spirc.set_position_ms(position_ms),
        Command::Volume { level } => {
            // Stored without reporting back: this level CAME from the
            // device, and echoing it would have the firmware apply its own
            // change a second time. BOTH of these are needed — the mixer
            // suppresses its own note, and `note_device_volume` suppresses
            // the player event that librespot's spirc emits from inside
            // `set_volume` below, which is the route that actually looped.
            shared.note_device_volume(level).await;
            mixer.accept_device_level(level);
            spirc.set_volume(level)
        }
        Command::Activate => spirc.activate(),
        Command::Disconnect { pause } => spirc.disconnect(pause),
        Command::Shuffle { on } => spirc.shuffle(on),
        Command::Repeat { on } => spirc.repeat(on),
        Command::RepeatTrack { on } => spirc.repeat_track(on),
        Command::Logout => return Ok(Some(SessionEnd::LoggedOut)),
        Command::Status => {
            for e in shared.snapshot.lock().await.replay() {
                let _ = shared.events.send(e);
            }
            Ok(())
        }
    };
    r.map(|()| None).map_err(|e| e.to_string())
}

fn session_connect_config(args: &Args, shared: &Shared) -> ConnectConfig {
    ConnectConfig {
        name: args.name.clone(),
        initial_volume: shared.volume.last().unwrap_or(args.initial_volume),
        ..ConnectConfig::default()
    }
}

// Returns a device volume to synchronize now that Connect is active.
async fn handle_player_event(
    e: PlayerEvent,
    shared: &Arc<Shared>,
    mixer: &ReportingMixer,
    activation_volume_pending: &mut bool,
) -> Option<u16> {
    match e {
        PlayerEvent::Playing {
            position_ms,
            track_id,
            ..
        } => {
            shared
                .emit(Event::State {
                    state: PlayState::Playing,
                    position_ms: Some(position_ms),
                    track_uri: Some(track_id.to_uri().unwrap_or_default()),
                })
                .await
        }
        PlayerEvent::Paused {
            position_ms,
            track_id,
            ..
        } => {
            shared
                .emit(Event::State {
                    state: PlayState::Paused,
                    position_ms: Some(position_ms),
                    track_uri: Some(track_id.to_uri().unwrap_or_default()),
                })
                .await
        }
        PlayerEvent::Loading {
            position_ms,
            track_id,
            ..
        } => {
            shared
                .emit(Event::State {
                    state: PlayState::Loading,
                    position_ms: Some(position_ms),
                    track_uri: Some(track_id.to_uri().unwrap_or_default()),
                })
                .await
        }
        PlayerEvent::Stopped { track_id, .. } => {
            shared
                .emit(Event::State {
                    state: PlayState::Stopped,
                    position_ms: None,
                    track_uri: Some(track_id.to_uri().unwrap_or_default()),
                })
                .await
        }
        // A track the account cannot play — market restriction, or pulled
        // from the catalogue. A state rather than an error: the right
        // response is to say so and let the queue move on.
        PlayerEvent::Unavailable { track_id, .. } => {
            shared
                .emit(Event::State {
                    state: PlayState::Unavailable,
                    position_ms: None,
                    track_uri: Some(track_id.to_uri().unwrap_or_default()),
                })
                .await
        }
        PlayerEvent::PositionChanged {
            position_ms,
            track_id,
            ..
        }
        | PlayerEvent::PositionCorrection {
            position_ms,
            track_id,
            ..
        }
        | PlayerEvent::Seeked {
            position_ms,
            track_id,
            ..
        } => {
            shared
                .emit(Event::State {
                    state: PlayState::Playing,
                    position_ms: Some(position_ms),
                    track_uri: Some(track_id.to_uri().unwrap_or_default()),
                })
                .await
        }
        PlayerEvent::TrackChanged { audio_item } => {
            let (artists, album) = match &audio_item.unique_fields {
                UniqueFields::Track { artists, album, .. } => (
                    artists.0.iter().map(|a| a.name.clone()).collect(),
                    Some(album.clone()),
                ),
                // Episodes and the rest have no album; the show's name is
                // not an album and claiming otherwise would put the wrong
                // string on a Home Assistant card.
                _ => (Vec::new(), None),
            };
            // Largest cover available. The firmware passes the URL on
            // untouched and nothing fetches the image on the device.
            let cover_url = audio_item
                .covers
                .iter()
                .max_by_key(|c| c.width.saturating_mul(c.height))
                .map(|c| c.url.clone());
            shared
                .emit(Event::Track {
                    uri: audio_item.uri.clone(),
                    name: audio_item.name.clone(),
                    artists,
                    album,
                    duration_ms: audio_item.duration_ms,
                    explicit: audio_item.is_explicit,
                    cover_url,
                })
                .await
        }
        PlayerEvent::VolumeChanged { volume } => {
            // librespot 0.8's handle_activate emits its stored Connect
            // volume immediately after SessionConnected/SessionClientChanged.
            // That is a snapshot, not a slider command. While inactive,
            // Spirc ignores SetVolume even though our mixer tracks it, so
            // applying this snapshot would restore an old (often boot) level.
            if std::mem::take(activation_volume_pending) {
                let current = mixer.volume();
                if current != volume {
                    return Some(current);
                }
            } else {
                shared.emit(Event::Volume { level: volume }).await;
            }
        }
        PlayerEvent::ShuffleChanged { shuffle } => {
            shared.emit(Event::Shuffle { on: shuffle }).await
        }
        PlayerEvent::RepeatChanged { context, track } => {
            shared.emit(Event::Repeat { context, track }).await
        }
        PlayerEvent::SessionDisconnected { .. } => {
            shared
                .emit(Event::Session {
                    connected: false,
                    user: None,
                })
                .await
        }
        PlayerEvent::SessionConnected { user_name, .. } => {
            *activation_volume_pending = true;
            shared
                .emit(Event::Session {
                    connected: true,
                    user: Some(user_name),
                })
                .await;
        }
        // Preload, play-request ids, end-of-track and the rest are
        // librespot's own bookkeeping. Forwarding them would give the
        // firmware things to react to that mean nothing to it.
        _ => {}
    }
    None
}

/// Move whatever the sink and mixer have reported onto the event stream.
///
/// Returns true if the sink reported something the session cannot continue
/// through.
async fn drain_notes(
    sink_notes: &StdReceiver<SinkNote>,
    volume_notes: &StdReceiver<VolumeNote>,
    shared: &Arc<Shared>,
) -> bool {
    let mut fatal = false;
    loop {
        match sink_notes.try_recv() {
            Ok(SinkNote::Connected) => log::debug!("audio socket connected"),
            Ok(SinkNote::Disconnected { reason }) => {
                log::warn!("audio socket dropped: {reason}")
            }
            Ok(SinkNote::UnsupportedPacket) => {
                shared
                    .emit(Event::Error {
                        kind: "unsupported_audio".into(),
                        message: "the player produced audio this build cannot send".into(),
                    })
                    .await;
                fatal = true;
            }
            Err(TryRecvError::Empty) => break,
            // The sink is gone, which means the player is gone. The
            // player-event branch will have noticed; nothing to add.
            Err(TryRecvError::Disconnected) => break,
        }
    }
    while let Ok(VolumeNote(level)) = volume_notes.try_recv() {
        shared.emit(Event::Volume { level }).await;
    }
    fatal
}

#[cfg(test)]
mod tests {
    use super::*;

    fn args(v: &[&str]) -> Result<Args, String> {
        Args::parse(v.iter().map(|s| s.to_string()))
    }

    const REQUIRED: &[&str] = &[
        "--name",
        "Kitchen",
        "--device-id",
        "abc123",
        "--cache-dir",
        "/data/spotify",
        "--control-socket",
        "/tmp/ctl",
        "--audio-socket",
        "/tmp/pcm",
    ];

    fn shared_state() -> Arc<Shared> {
        let (events, _) = broadcast::channel(EVENT_BUFFER);
        Arc::new(Shared {
            events,
            snapshot: Mutex::new(Snapshot::default()),
            volume: VolumeGate::default(),
        })
    }

    #[tokio::test]
    async fn a_new_session_uses_the_latest_volume_including_silence() {
        let a = args(REQUIRED).unwrap();
        let shared = shared_state();
        assert_eq!(
            session_connect_config(&a, &shared).initial_volume,
            a.initial_volume
        );
        // These may arrive before linking or between Spotify sessions.
        for level in [18000, 0, 42000] {
            shared.note_device_volume(level).await;
            let config = session_connect_config(&a, &shared);
            let (tx, rx) = std::sync::mpsc::channel();
            let mixer = ReportingMixer::new(config.initial_volume, tx);
            assert_eq!(config.initial_volume, level);
            // Spirc::new sets the mixer to ConnectConfig's initial volume.
            mixer.set_volume(config.initial_volume);
            assert_eq!(mixer.volume(), level);
            assert!(rx.try_recv().is_err());
        }
    }

    #[tokio::test]
    async fn activation_keeps_device_volume_and_later_slider_changes_work() {
        for device_level in [0, 18000, 47000, u16::MAX] {
            let shared = shared_state();
            let mut events = shared.events.subscribe();
            let (tx, _rx) = std::sync::mpsc::channel();
            let boot_level = 47000;
            let mixer = ReportingMixer::new(boot_level, tx);
            let mut pending = false;
            // While inactive, our mixer accepts this but Spirc ignores
            // SetVolume, leaving its Connect state at the boot level.
            mixer.accept_device_level(device_level);
            shared.note_device_volume(device_level).await;

            // Repeat to cover transferring away and back to this Echo.
            for _ in 0..2 {
                let sync = handle_player_event(
                    PlayerEvent::SessionConnected {
                        connection_id: "test".into(),
                        user_name: "listener".into(),
                    },
                    &shared,
                    &mixer,
                    &mut pending,
                )
                .await;
                assert_eq!(sync, None);
                assert!(matches!(
                    events.try_recv(),
                    Ok(Event::Session {
                        connected: true,
                        ..
                    })
                ));
                handle_player_event(
                    PlayerEvent::SessionClientChanged {
                        client_id: "test".into(),
                        client_name: "phone".into(),
                        client_brand_name: String::new(),
                        client_model_name: String::new(),
                    },
                    &shared,
                    &mixer,
                    &mut pending,
                )
                .await;
                // handle_activate's automatic snapshot must not reach Go.
                let sync = handle_player_event(
                    PlayerEvent::VolumeChanged { volume: boot_level },
                    &shared,
                    &mixer,
                    &mut pending,
                )
                .await;
                assert_eq!(sync, (device_level != boot_level).then_some(device_level));
                assert!(!pending);
                assert!(events.try_recv().is_err());
                // The resynchronization's own echo must also stay local.
                handle_player_event(
                    PlayerEvent::VolumeChanged {
                        volume: device_level,
                    },
                    &shared,
                    &mixer,
                    &mut pending,
                )
                .await;
                assert!(events.try_recv().is_err());
                assert_eq!(shared.volume.last(), Some(device_level));
            }

            // Genuine phone changes still arrive on BOTH paths. Report
            // once, including a later deliberate selection of the old level.
            for phone_level in [25000, boot_level, 0] {
                mixer.set_volume(phone_level);
                shared.emit(Event::Volume { level: phone_level }).await;
                handle_player_event(
                    PlayerEvent::VolumeChanged {
                        volume: phone_level,
                    },
                    &shared,
                    &mixer,
                    &mut pending,
                )
                .await;
                assert!(
                    matches!(events.try_recv(), Ok(Event::Volume { level }) if level == phone_level)
                );
                assert!(events.try_recv().is_err());
            }
        }
    }

    #[test]
    fn the_required_arguments_are_required() {
        // Every one of these is something the firmware must decide. A
        // default would be this process inventing a device identity, a
        // cache location or a socket path, and the symptom of guessing the
        // device id is an Echo that appears in the Spotify app as a new
        // speaker after every restart.
        for drop_flag in [
            "--name",
            "--device-id",
            "--cache-dir",
            "--control-socket",
            "--audio-socket",
        ] {
            let kept: Vec<&str> = REQUIRED
                .chunks(2)
                .filter(|c| c[0] != drop_flag)
                .flat_map(|c| c.iter().copied())
                .collect();
            let r = args(&kept);
            assert!(r.is_err(), "{drop_flag} was not required");
            let msg = r.err().unwrap();
            assert!(msg.contains(drop_flag), "{msg} should name {drop_flag}");
        }
    }

    #[test]
    fn defaults_are_the_safe_ones() {
        let a = args(REQUIRED).expect("should parse");
        assert_eq!(a.name, "Kitchen");
        assert_eq!(a.device_id, "abc123");
        // Port 0 means "let the kernel choose", which is right as a
        // default: the firmware passes a fixed port because it has to open
        // a firewall rule for it, but a bare run should not collide.
        assert_eq!(a.zeroconf_port, 0);
        assert_eq!(a.initial_volume, u16::MAX / 2);
        assert!(matches!(a.bitrate, Bitrate::Bitrate160));
    }

    #[test]
    fn bitrate_takes_only_spotifys_three() {
        for (v, want) in [
            ("96", Bitrate::Bitrate96),
            ("160", Bitrate::Bitrate160),
            ("320", Bitrate::Bitrate320),
        ] {
            let mut v2: Vec<&str> = REQUIRED.to_vec();
            v2.extend(["--bitrate", v]);
            let a = args(&v2).expect("should parse");
            assert_eq!(
                std::mem::discriminant(&a.bitrate),
                std::mem::discriminant(&want)
            );
        }
        let mut bad: Vec<&str> = REQUIRED.to_vec();
        bad.extend(["--bitrate", "256"]);
        assert!(args(&bad).is_err(), "256kbps is not a Spotify bitrate");
    }

    #[test]
    fn an_unknown_flag_is_refused_rather_than_ignored() {
        // The firmware builds this command line. A flag it passes and we
        // ignore is a setting that silently does nothing.
        let mut v: Vec<&str> = REQUIRED.to_vec();
        v.push("--enable-everything");
        assert!(args(&v).is_err());
    }

    #[test]
    fn a_flag_without_its_value_is_refused() {
        assert!(args(&["--name"]).is_err());
    }

    #[test]
    fn the_snapshot_replays_states_and_not_moments() {
        // A reconnecting firmware needs to know where things stand. It must
        // NOT be told about an error that happened an hour ago, which would
        // report a fault that is not happening.
        let mut s = Snapshot::default();
        s.record(&Event::Link {
            status: LinkStatus::Linked,
            detail: None,
        });
        s.record(&Event::Error {
            kind: "transient".into(),
            message: "something".into(),
        });
        s.record(&Event::Shuffle { on: true });
        let replayed = s.replay();
        assert_eq!(replayed.len(), 1);
        assert!(matches!(replayed[0], Event::Link { .. }));
    }

    #[test]
    fn the_snapshot_keeps_only_the_latest_of_each_kind() {
        let mut s = Snapshot::default();
        s.record(&Event::Volume { level: 1 });
        s.record(&Event::Volume { level: 2 });
        let replayed = s.replay();
        assert_eq!(replayed.len(), 1);
        assert_eq!(replayed[0], Event::Volume { level: 2 });
    }

    #[test]
    fn clearing_absent_credentials_is_not_an_error() {
        // Reached whenever a device is unlinked twice, or unlinked having
        // never been linked. Failing there would turn Disconnect into a
        // button that reports an error for doing exactly what was asked.
        let dir = std::env::temp_dir().join(format!("em-spotify-cred-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        assert!(clear_credentials(&dir).is_ok());
        // And with a file present.
        std::fs::write(dir.join(CREDENTIALS_FILE), b"{}").unwrap();
        assert!(clear_credentials(&dir).is_ok());
        assert!(!dir.join(CREDENTIALS_FILE).exists());
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn the_credential_filename_matches_librespots_own() {
        // `clear_credentials` works by path because Cache has no
        // remove-credentials call. If librespot ever renames this file,
        // Disconnect would silently leave the account linked — so the name
        // is checked against a Cache built over a temp directory rather
        // than remembered.
        let dir = std::env::temp_dir().join(format!("em-spotify-name-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        let cache = Cache::new(Some(dir.as_path()), None, None, None).expect("cache");
        let creds = Credentials::with_access_token("not-a-real-token");
        cache.save_credentials(&creds);
        assert!(
            dir.join(CREDENTIALS_FILE).exists(),
            "librespot did not write {CREDENTIALS_FILE}; the name has changed"
        );
        let _ = std::fs::remove_dir_all(&dir);
    }
}
