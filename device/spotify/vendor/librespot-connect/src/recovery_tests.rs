//! Session recovery semantics without Spotify access or an audio device.
use super::*;
use crate::protocol::{context_page::ContextPage, context_track::ContextTrack};
use librespot_playback::{
    audio_backend::{Sink, SinkResult},
    config::PlayerConfig,
    convert::Converter,
    decoder::AudioPacket,
    mixer::{MixerConfig, NoOpVolume},
};
use std::{pin::Pin, sync::atomic::AtomicU16};

struct SilentSink;
impl Sink for SilentSink {
    fn write(&mut self, _: AudioPacket, _: &mut Converter) -> SinkResult<()> {
        panic!("state recovery tests must not stream audio");
    }
}

struct TestMixer(AtomicU16);
impl Mixer for TestMixer {
    fn open(_: MixerConfig) -> Result<Self, Error> {
        Ok(Self(AtomicU16::new(10)))
    }
    fn volume(&self) -> u16 {
        self.0.load(Ordering::Relaxed)
    }
    fn set_volume(&self, v: u16) {
        self.0.store(v, Ordering::Relaxed);
    }
}

fn pending<T: Send + 'static>() -> Pin<Box<dyn futures_util::Stream<Item = T> + Send>> {
    Box::pin(futures_util::stream::pending())
}

fn task(playing: bool) -> SpircTask {
    let session = Session::new(Default::default(), None);
    let player = Player::new(
        PlayerConfig::default(),
        session.clone(),
        Box::new(NoOpVolume),
        || Box::new(SilentSink),
    );
    let events = player.get_player_event_channel();
    let (_, commands) = mpsc::unbounded_channel();
    let mut state = ConnectState::new(Default::default(), &session);
    state
        .update_context(
            Context {
                uri: Some("spotify:playlist:recovery".into()),
                pages: vec![ContextPage {
                    tracks: (0..120)
                        .map(|n| ContextTrack {
                            uri: Some(format!("spotify:track:{n:022}")),
                            uid: Some(format!("queue-row-{n}")),
                            ..Default::default()
                        })
                        .collect(),
                    ..Default::default()
                }],
                ..Default::default()
            },
            ContextType::Default,
        )
        .unwrap();
    state.set_current_track(5).unwrap();
    state.reset_playback_to_position(Some(5)).unwrap();
    state.set_active(true);
    let play_status = if playing {
        SpircPlayStatus::Playing {
            nominal_start_time: 0,
            preloading_of_next_track_triggered: true,
        }
    } else {
        SpircPlayStatus::Paused {
            position_ms: 25_000,
            preloading_of_next_track_triggered: true,
        }
    };
    state.set_status(&play_status);
    state.update_duration(180_000);
    SpircTask {
        recovery: Arc::new(Mutex::new(None)),
        player,
        mixer: Arc::new(TestMixer(AtomicU16::new(10))),
        connect_state: state,
        connect_established: true,
        play_request_id: Some(42),
        play_status,
        last_position_ms: Some(25_000),
        connection_id_update: pending(),
        connect_state_update: pending(),
        connect_state_volume_update: pending(),
        connect_state_logout_request: pending(),
        playlist_update: pending(),
        session_update: pending(),
        connect_state_command: pending(),
        user_attributes_update: pending(),
        user_attributes_mutation: pending(),
        commands: Some(commands),
        player_events: Some(events),
        context_resolver: ContextResolver::new(session.clone()),
        session,
        shutdown: false,
        transfer_state: None,
        update_volume: false,
        update_state: false,
        spirc_id: 0,
    }
}

fn capture(task: &mut SpircTask) -> Arc<PlaybackSnapshot> {
    task.save_disconnected_playback();
    task.recovery
        .lock()
        .unwrap()
        .clone()
        .expect("interrupted playback was lost")
}

fn restore(snapshot: &PlaybackSnapshot, account: &str, device: &str) -> Option<(bool, u32)> {
    let session = Session::new(Default::default(), None);
    let mut state = ConnectState::new(Default::default(), &session);
    snapshot.restore_into(
        account,
        device,
        "our-speaker",
        &mut state,
        &mut ContextResolver::new(session),
    )
}

#[tokio::test]
async fn playing_paused_and_midload_recovery_keep_the_play_state_and_position() {
    for playing in [false, true] {
        let mut old = task(playing);
        let snapshot = capture(&mut old);
        assert_eq!(restore(&snapshot, "", ""), Some((playing, 25_000)));
        assert_eq!(
            restore(&snapshot, "", "our-speaker"),
            Some((playing, 25_000))
        );
        old.play_status = if playing {
            SpircPlayStatus::LoadingPlay { position_ms: 9_876 }
        } else {
            SpircPlayStatus::LoadingPause { position_ms: 9_876 }
        };
        let snapshot = capture(&mut old);
        assert_eq!(restore(&snapshot, "", ""), Some((playing, 9_876)));
    }
}

#[tokio::test]
async fn another_device_account_or_new_playback_cannot_be_overridden() {
    let mut old = task(true);
    let snapshot = capture(&mut old);
    assert!(restore(&snapshot, "another-account", "").is_none());
    assert!(restore(&snapshot, "", "phone").is_none());
    snapshot.cancel();
    assert!(restore(&snapshot, "", "").is_none());
}

#[tokio::test]
async fn pause_stop_and_cancel_during_retry_affect_all_snapshot_handles() {
    for stop in [false, true] {
        let mut old = task(true);
        let snapshot = capture(&mut old);
        let pending_login = Arc::clone(&snapshot);
        if stop {
            snapshot.stop();
        } else {
            snapshot.pause();
        }
        assert_eq!(
            restore(&pending_login, "", ""),
            Some((false, if stop { 0 } else { 25_000 }))
        );
        snapshot.cancel();
        assert!(restore(&pending_login, "", "").is_none());
    }
}

#[tokio::test]
async fn pause_and_stop_queued_at_the_instant_of_session_loss_win() {
    for stop in [false, true] {
        let mut old = task(true);
        let (commands, rx) = mpsc::unbounded_channel();
        old.commands = Some(rx);
        commands.send(SpircCommand::Pause).unwrap();
        if stop {
            commands.send(SpircCommand::SetPosition(0)).unwrap();
        }
        old.session.shutdown();
        let saved = Arc::clone(&old.recovery);
        old.run().await;
        let snapshot = saved.lock().unwrap().clone().unwrap();
        assert_eq!(
            restore(&snapshot, "", ""),
            Some((false, if stop { 0 } else { 25_000 }))
        );
    }
}

#[tokio::test]
async fn a_new_track_or_shutdown_queued_at_session_loss_cancels_old_recovery() {
    for command in [
        SpircCommand::Next,
        SpircCommand::Shutdown,
        SpircCommand::Disconnect { pause: true },
    ] {
        let mut old = task(true);
        let (commands, rx) = mpsc::unbounded_channel();
        old.commands = Some(rx);
        commands.send(command).unwrap();
        old.session.shutdown();
        let saved = Arc::clone(&old.recovery);
        old.run().await;
        assert!(saved.lock().unwrap().is_none());
    }
}

#[tokio::test]
async fn a_long_outage_recovers_the_queue_paused_without_losing_position() {
    let mut old = task(true);
    let mut snapshot = capture(&mut old);
    old.recovery.lock().unwrap().take();
    Arc::get_mut(&mut snapshot).unwrap().saved_at =
        std::time::Instant::now() - Duration::from_secs(121);
    assert_eq!(restore(&snapshot, "", ""), Some((false, 25_000)));
}

#[tokio::test]
async fn shutdown_stop_inactivity_and_incomplete_transfers_do_not_autoresume() {
    for reason in ["shutdown", "stop", "inactive", "transfer"] {
        let mut old = task(true);
        // Start with an older saved queue to prove it cannot leak through.
        capture(&mut old);
        match reason {
            "shutdown" => old.shutdown = true,
            "stop" => old.play_status = SpircPlayStatus::Stopped,
            "inactive" => old.connect_state.set_active(false),
            _ => old.transfer_state = Some(Default::default()),
        }
        old.save_disconnected_playback();
        assert!(old.recovery.lock().unwrap().is_none(), "{reason}");
    }
}

#[tokio::test]
async fn another_failed_registration_retains_the_original_snapshot_and_age() {
    let mut old = task(true);
    let snapshot = capture(&mut old);
    let mut replacement = task(false);
    replacement.connect_established = false;
    replacement.connect_state.set_active(false);
    *replacement.recovery.lock().unwrap() = Some(Arc::clone(&snapshot));
    replacement.save_disconnected_playback();
    assert!(Arc::ptr_eq(
        &snapshot,
        replacement.recovery.lock().unwrap().as_ref().unwrap()
    ));
}

#[tokio::test]
async fn latest_audio_position_wins_over_wall_time_and_stale_track_events() {
    let mut old = task(true);
    let uri = SpotifyUri::from_uri("spotify:track:0000000000000000000005").unwrap();
    old.handle_player_event(PlayerEvent::PositionChanged {
        play_request_id: 42,
        track_id: uri.clone(),
        position_ms: 73_000,
    })
    .unwrap();
    old.handle_player_event(PlayerEvent::PositionChanged {
        play_request_id: 41,
        track_id: uri,
        position_ms: 140_000,
    })
    .unwrap();
    assert_eq!(capture(&mut old).position_ms, 73_000);
    old.last_position_ms = Some(900_000);
    assert_eq!(capture(&mut old).position_ms, 179_999);
}

#[tokio::test]
async fn session_loss_retains_snapshot_before_stopping_the_old_player() {
    let old = task(true);
    let saved = Arc::clone(&old.recovery);
    old.session.shutdown();
    tokio::time::timeout(Duration::from_millis(100), old.run())
        .await
        .unwrap();
    let snapshot = saved.lock().unwrap().clone().unwrap();
    assert_eq!(restore(&snapshot, "", ""), Some((true, 25_000)));
}

#[tokio::test]
async fn session_loss_interrupts_a_stalled_handler_and_drops_its_work() {
    struct InFlight(Arc<AtomicBool>);
    impl Drop for InFlight {
        fn drop(&mut self) {
            self.0.store(true, Ordering::Release);
        }
    }
    let session = Session::new(Default::default(), None);
    let dropped = Arc::new(AtomicBool::new(false));
    let guard = InFlight(Arc::clone(&dropped));
    let work = async move {
        let _guard = guard;
        futures_util::future::pending::<()>().await;
    };
    let disconnect = async {
        tokio::time::sleep(Duration::from_millis(10)).await;
        session.shutdown();
    };
    let recovery = until_session_invalid(session.clone(), work);
    tokio::time::timeout(Duration::from_secs(2), async {
        tokio::join!(disconnect, recovery);
    })
    .await
    .unwrap();
    assert!(dropped.load(Ordering::Acquire));
}

#[tokio::test]
async fn paused_idle_event_loop_wakes_on_session_loss_without_another_command() {
    let mut old = task(false);
    old.commands = None;
    let session = old.session.clone();
    let disconnect = async {
        tokio::time::sleep(Duration::from_millis(10)).await;
        session.shutdown();
    };
    tokio::time::timeout(Duration::from_secs(2), async {
        tokio::join!(
            disconnect,
            until_session_invalid(old.session.clone(), old.run_events())
        );
    })
    .await
    .unwrap();
    let snapshot = capture(&mut old);
    assert_eq!(restore(&snapshot, "", ""), Some((false, 25_000)));
}

#[tokio::test]
async fn pending_pages_survive_without_reusing_the_dead_session() {
    let mut old = task(true);
    let pending = ResolveContext::from_uri(
        "spotify:playlist:remaining",
        "",
        ContextType::Default,
        ContextAction::Append,
    );
    old.context_resolver.add(pending.clone());
    let snapshot = capture(&mut old);
    let session = Session::new(Default::default(), None);
    let mut state = ConnectState::new(Default::default(), &session);
    let mut resolver = ContextResolver::new(session);
    assert!(
        snapshot
            .restore_into("", "", "our-speaker", &mut state, &mut resolver)
            .is_some()
    );
    assert_eq!(resolver.pending(), [pending]);
}
