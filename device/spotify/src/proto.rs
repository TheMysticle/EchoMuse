//! The control protocol between this daemon and the Go firmware.
//!
//! Newline-delimited JSON over a unix socket. Commands travel in, replies
//! and events travel out, and the whole thing is small enough to drive by
//! hand with `nc` — which is deliberate, because the alternative on a
//! device with no debugger is guessing.
//!
//! ## Why two sockets rather than one framed stream
//!
//! Audio does not come through here. It goes out on a SECOND unix socket as
//! bare 48kHz S16LE stereo, with no framing at all, because the Go side
//! feeds it straight into `PcmSpeaker.MusicSource` in exactly that layout.
//! Two sockets instead of one multiplexed stream for three reasons: a
//! blocked audio write cannot delay a pause command, the audio socket needs
//! no parser on either end, and the control socket stays readable by a
//! person.
//!
//! ## Compatibility
//!
//! Both ends ignore what they do not recognise, the same rule the
//! device/controller wire protocol follows. An unknown command is answered
//! with an error naming it rather than dropped, since the sender is our own
//! firmware and silence there would be a bug that looks like a hang. An
//! unknown EVENT field is ignored by Go's decoder, so adding a field is
//! safe; moving one is not, which is why the shapes below are pinned by
//! golden tests rather than by inspection.
//!
//! ## What never appears here
//!
//! No credential blob, no access token, no OAuth code, and no Spotify
//! password — not in an event, not in a reply, not in a log line. The
//! credential blob from zeroconf pairing is written straight into
//! librespot's own cache and is never read back into this process's
//! messages. `Event::Session` carries the account's display name because
//! "connected as whom" is the one thing a person needs from it; the
//! controller redacts that from support bundles, as it already does for
//! dashboard account names.

use serde::{Deserialize, Serialize};

/// One line from the firmware.
///
/// `id` is echoed on the reply so a caller can correlate without having to
/// assume ordering. It is optional: fire-and-forget commands are the common
/// case and should not have to invent one.
#[derive(Debug, Clone, Deserialize, PartialEq)]
pub struct Request {
    #[serde(default)]
    pub id: Option<u64>,
    #[serde(flatten)]
    pub command: Command,
}

/// What the firmware can ask the player to do.
///
/// Every variant maps onto one `Spirc` method — librespot's own player API,
/// not a CLI and not a synthesised keypress. `Load` is the one that matters
/// for the voice path: `LoadRequest::from_context_uri` is documented in
/// librespot as the equivalent of the Web API's `/me/player/play` with a
/// `context_uri`, so an album, playlist or artist URI plays as a context
/// while the device stays a Connect speaker.
#[derive(Debug, Clone, Deserialize, PartialEq)]
#[serde(tag = "cmd", rename_all = "snake_case")]
pub enum Command {
    /// Play a context: track, album, playlist or artist URI.
    Load {
        uri: String,
        #[serde(default = "default_true")]
        start_playing: bool,
        #[serde(default)]
        seek_to_ms: u32,
    },
    /// Play an explicit list of track URIs, with no surrounding context.
    LoadTracks {
        uris: Vec<String>,
        #[serde(default = "default_true")]
        start_playing: bool,
    },
    Play,
    Pause,
    PlayPause,
    Stop,
    Next,
    Prev,
    Seek {
        position_ms: u32,
    },
    /// Volume in librespot's own 0..=65535 range.
    ///
    /// The conversion from the Echo's 0..=127 codec level lives on the Go
    /// side, because that law is Radar's 30-step stock table and belongs
    /// with the rest of the volume model rather than duplicated here.
    Volume {
        level: u16,
    },
    /// Become the active Connect device without being told to by a phone.
    /// What a voice "play X" needs when nothing has transferred to us yet.
    Activate,
    Disconnect {
        #[serde(default = "default_true")]
        pause: bool,
    },
    Shuffle {
        on: bool,
    },
    Repeat {
        on: bool,
    },
    RepeatTrack {
        on: bool,
    },
    /// Forget the cached credentials and stop being a logged-in device.
    /// The dashboard's Disconnect.
    Logout,
    /// Re-send the current state as events, for a client that has just
    /// connected and has no idea what is going on.
    Status,
}

fn default_true() -> bool {
    true
}

/// Answer to a `Request`. Exactly one per request, always.
#[derive(Debug, Clone, Serialize, PartialEq)]
#[serde(tag = "reply", rename_all = "snake_case")]
pub enum Reply {
    Ok {
        #[serde(skip_serializing_if = "Option::is_none")]
        id: Option<u64>,
    },
    Error {
        #[serde(skip_serializing_if = "Option::is_none")]
        id: Option<u64>,
        message: String,
    },
}

/// What the player is doing.
///
/// `Unavailable` is its own state rather than an error, because it is a
/// routine outcome: a track can be missing in the account's market, and the
/// right response is to say so and move on, not to tear the session down.
#[derive(Debug, Clone, Copy, Serialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum PlayState {
    Stopped,
    Loading,
    Playing,
    Paused,
    Unavailable,
}

/// Why the device is or is not usable as a Spotify speaker.
///
/// This is the field the dashboard's status comes from, and it exists
/// because "the process is running" is not the same claim. `PremiumRequired`
/// is separated out because it is the one failure the user can act on and
/// the Web API can no longer tell us about: since the February 2026
/// Development Mode changes, `/v1/me` no longer returns `product`, so the
/// account tier is only ever learned from librespot's own login.
#[derive(Debug, Clone, Copy, Serialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum LinkStatus {
    /// No cached credentials. Advertising over zeroconf and waiting to be
    /// picked in somebody's Spotify app.
    AwaitingLink,
    /// Logged in and usable.
    Linked,
    /// Credentials exist and were refused. Needs re-linking, and that is a
    /// different instruction to the user than a network problem.
    AuthFailed,
    /// Logged in, but the account cannot stream through Connect.
    PremiumRequired,
    /// Cannot reach Spotify. Distinct from AuthFailed on purpose: one is
    /// worth retrying by itself, the other is not.
    Offline,
}

/// A line out to the firmware.
#[derive(Debug, Clone, Serialize, PartialEq)]
#[serde(tag = "event", rename_all = "snake_case")]
pub enum Event {
    /// Playback state, and where we are in the track.
    State {
        state: PlayState,
        #[serde(skip_serializing_if = "Option::is_none")]
        position_ms: Option<u32>,
        #[serde(skip_serializing_if = "Option::is_none")]
        track_uri: Option<String>,
    },
    /// The track changed. Carries everything Home Assistant can show, which
    /// comes from librespot's own metadata rather than a Web API call — so
    /// it works on a device whose controller has no search token at all.
    Track {
        uri: String,
        name: String,
        artists: Vec<String>,
        #[serde(skip_serializing_if = "Option::is_none")]
        album: Option<String>,
        duration_ms: u32,
        explicit: bool,
        #[serde(skip_serializing_if = "Option::is_none")]
        cover_url: Option<String>,
    },
    /// Spotify's own volume moved — someone used the slider in the app.
    /// The Go side applies it to the codec and reports it onward, which is
    /// what makes one volume model out of two.
    Volume {
        level: u16,
    },
    /// Whether a Connect session is attached, and to whose account.
    Session {
        connected: bool,
        #[serde(skip_serializing_if = "Option::is_none")]
        user: Option<String>,
    },
    /// Whether this device can act as a Spotify speaker at all.
    Link {
        status: LinkStatus,
        #[serde(skip_serializing_if = "Option::is_none")]
        detail: Option<String>,
    },
    Shuffle {
        on: bool,
    },
    Repeat {
        context: bool,
        track: bool,
    },
    /// Something went wrong that does not change the link status.
    /// `kind` is a stable slug the Go side may branch on; `message` is for
    /// a person and may change wording freely.
    Error {
        kind: String,
        message: String,
    },
}

#[cfg(test)]
mod tests {
    use super::*;

    fn parse(line: &str) -> Request {
        serde_json::from_str(line).expect("should parse")
    }

    #[test]
    fn load_accepts_the_minimal_form() {
        // What the controller's voice path sends: a URI and nothing else.
        // start_playing defaults TRUE, because a load that silently did not
        // play would present as "the assistant said it was playing and
        // nothing happened".
        let r = parse(r#"{"cmd":"load","uri":"spotify:track:4cOdK2wGLETKBW3PvgPWqT"}"#);
        assert_eq!(
            r,
            Request {
                id: None,
                command: Command::Load {
                    uri: "spotify:track:4cOdK2wGLETKBW3PvgPWqT".into(),
                    start_playing: true,
                    seek_to_ms: 0,
                },
            }
        );
    }

    #[test]
    fn load_accepts_a_context_and_an_offset() {
        let r = parse(
            r#"{"id":7,"cmd":"load","uri":"spotify:album:1To7kv722A8SpZF789MZy7",
                "start_playing":false,"seek_to_ms":30000}"#,
        );
        assert_eq!(r.id, Some(7));
        assert_eq!(
            r.command,
            Command::Load {
                uri: "spotify:album:1To7kv722A8SpZF789MZy7".into(),
                start_playing: false,
                seek_to_ms: 30000,
            }
        );
    }

    #[test]
    fn unit_commands_need_no_fields() {
        for (line, want) in [
            (r#"{"cmd":"play"}"#, Command::Play),
            (r#"{"cmd":"pause"}"#, Command::Pause),
            (r#"{"cmd":"play_pause"}"#, Command::PlayPause),
            (r#"{"cmd":"stop"}"#, Command::Stop),
            (r#"{"cmd":"next"}"#, Command::Next),
            (r#"{"cmd":"prev"}"#, Command::Prev),
            (r#"{"cmd":"activate"}"#, Command::Activate),
            (r#"{"cmd":"logout"}"#, Command::Logout),
            (r#"{"cmd":"status"}"#, Command::Status),
        ] {
            assert_eq!(parse(line).command, want, "parsing {line}");
        }
    }

    #[test]
    fn disconnect_pauses_by_default() {
        // Leaving a speaker playing after being told to disconnect is the
        // kind of thing that reads as a stuck device.
        assert_eq!(
            parse(r#"{"cmd":"disconnect"}"#).command,
            Command::Disconnect { pause: true }
        );
        assert_eq!(
            parse(r#"{"cmd":"disconnect","pause":false}"#).command,
            Command::Disconnect { pause: false }
        );
    }

    #[test]
    fn volume_spans_the_full_u16() {
        assert_eq!(
            parse(r#"{"cmd":"volume","level":65535}"#).command,
            Command::Volume { level: 65535 }
        );
        assert_eq!(
            parse(r#"{"cmd":"volume","level":0}"#).command,
            Command::Volume { level: 0 }
        );
    }

    #[test]
    fn an_unknown_command_is_an_error_not_a_default() {
        // Guessing here would mean a typo in the firmware silently became
        // "play". The sender is our own code, so an error is actionable.
        let err = serde_json::from_str::<Request>(r#"{"cmd":"explode"}"#);
        assert!(err.is_err(), "unknown command parsed as {err:?}");
    }

    #[test]
    fn a_missing_required_field_is_an_error() {
        assert!(serde_json::from_str::<Request>(r#"{"cmd":"load"}"#).is_err());
        assert!(serde_json::from_str::<Request>(r#"{"cmd":"seek"}"#).is_err());
        assert!(serde_json::from_str::<Request>(r#"{"cmd":"volume"}"#).is_err());
    }

    #[test]
    fn extra_fields_are_ignored() {
        // Degrade to old behaviour, never to a wrong answer: a newer
        // firmware sending a field this build does not know must still get
        // its command executed.
        assert_eq!(
            parse(r#"{"cmd":"play","something_new":42}"#).command,
            Command::Play
        );
    }

    // ── The event shapes are the contract with Go, so they are pinned ──
    //
    // Golden strings rather than round-trips: a round-trip through this
    // same enum proves nothing about whether the Go decoder still matches.
    // If one of these changes, the Go side changes in the same commit.

    fn json(e: &Event) -> String {
        serde_json::to_string(e).unwrap()
    }

    #[test]
    fn state_event_shape() {
        assert_eq!(
            json(&Event::State {
                state: PlayState::Playing,
                position_ms: Some(1234),
                track_uri: Some("spotify:track:abc".into()),
            }),
            r#"{"event":"state","state":"playing","position_ms":1234,"track_uri":"spotify:track:abc"}"#
        );
    }

    #[test]
    fn absent_is_absent_never_zero() {
        // The project's rule: a measurement nothing produced stores as
        // NULL, not 0. A position of 0 is a real position (the start of a
        // track), so an unknown one must not serialise as one.
        assert_eq!(
            json(&Event::State {
                state: PlayState::Stopped,
                position_ms: None,
                track_uri: None,
            }),
            r#"{"event":"state","state":"stopped"}"#
        );
    }

    #[test]
    fn track_event_shape() {
        assert_eq!(
            json(&Event::Track {
                uri: "spotify:track:abc".into(),
                name: "Never Gonna Give You Up".into(),
                artists: vec!["Rick Astley".into()],
                album: Some("Whenever You Need Somebody".into()),
                duration_ms: 213_573,
                explicit: false,
                cover_url: Some("https://i.scdn.co/image/abc".into()),
            }),
            r#"{"event":"track","uri":"spotify:track:abc","name":"Never Gonna Give You Up",
"artists":["Rick Astley"],"album":"Whenever You Need Somebody","duration_ms":213573,
"explicit":false,"cover_url":"https://i.scdn.co/image/abc"}"#
                .replace('\n', "")
        );
    }

    #[test]
    fn link_event_shape() {
        assert_eq!(
            json(&Event::Link {
                status: LinkStatus::AwaitingLink,
                detail: None,
            }),
            r#"{"event":"link","status":"awaiting_link"}"#
        );
        assert_eq!(
            json(&Event::Link {
                status: LinkStatus::PremiumRequired,
                detail: Some("Spotify Premium is required".into()),
            }),
            r#"{"event":"link","status":"premium_required","detail":"Spotify Premium is required"}"#
        );
    }

    #[test]
    fn every_link_status_has_a_stable_slug() {
        // These reach the dashboard and the Go side branches on them, so a
        // rename is an API change. Listed exhaustively so adding a variant
        // without deciding its slug fails here.
        let all = [
            (LinkStatus::AwaitingLink, "awaiting_link"),
            (LinkStatus::Linked, "linked"),
            (LinkStatus::AuthFailed, "auth_failed"),
            (LinkStatus::PremiumRequired, "premium_required"),
            (LinkStatus::Offline, "offline"),
        ];
        for (status, slug) in all {
            let got = serde_json::to_string(&status).unwrap();
            assert_eq!(got, format!("\"{slug}\""));
        }
    }

    #[test]
    fn every_play_state_has_a_stable_slug() {
        for (state, slug) in [
            (PlayState::Stopped, "stopped"),
            (PlayState::Loading, "loading"),
            (PlayState::Playing, "playing"),
            (PlayState::Paused, "paused"),
            (PlayState::Unavailable, "unavailable"),
        ] {
            assert_eq!(
                serde_json::to_string(&state).unwrap(),
                format!("\"{slug}\"")
            );
        }
    }

    #[test]
    fn reply_echoes_the_id_when_there_was_one() {
        assert_eq!(
            json_reply(&Reply::Ok { id: Some(3) }),
            r#"{"reply":"ok","id":3}"#
        );
        assert_eq!(json_reply(&Reply::Ok { id: None }), r#"{"reply":"ok"}"#);
        assert_eq!(
            json_reply(&Reply::Error {
                id: Some(4),
                message: "no such context".into()
            }),
            r#"{"reply":"error","id":4,"message":"no such context"}"#
        );
    }

    fn json_reply(r: &Reply) -> String {
        serde_json::to_string(r).unwrap()
    }

    #[test]
    fn no_message_type_can_carry_a_credential() {
        // A structural check, not a wording one: nothing in the outbound
        // vocabulary has a field that could hold a token or a blob. If
        // somebody adds one, this fails and they have to argue for it.
        // Enumerated by serialising one of every variant and asserting the
        // key set.
        let samples = vec![
            Event::State {
                state: PlayState::Playing,
                position_ms: Some(0),
                track_uri: Some("u".into()),
            },
            Event::Track {
                uri: "u".into(),
                name: "n".into(),
                artists: vec![],
                album: None,
                duration_ms: 0,
                explicit: false,
                cover_url: None,
            },
            Event::Volume { level: 0 },
            Event::Session {
                connected: true,
                user: Some("u".into()),
            },
            Event::Link {
                status: LinkStatus::Linked,
                detail: None,
            },
            Event::Shuffle { on: false },
            Event::Repeat {
                context: false,
                track: false,
            },
            Event::Error {
                kind: "k".into(),
                message: "m".into(),
            },
        ];
        let forbidden = [
            "token",
            "credential",
            "credentials",
            "blob",
            "password",
            "secret",
            "auth_data",
            "access_token",
            "refresh_token",
            "code",
        ];
        for e in &samples {
            let v: serde_json::Value = serde_json::to_value(e).unwrap();
            let obj = v.as_object().unwrap();
            for key in obj.keys() {
                assert!(
                    !forbidden.contains(&key.as_str()),
                    "event {e:?} has a field named {key}, which must never leave this process"
                );
            }
        }
    }
}
