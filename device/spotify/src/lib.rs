//! On-device Spotify Connect for the Echo.
//!
//! This crate is the Rust half of the feature: librespot as a library,
//! wrapped so the Go firmware can drive it and take its audio. The audio
//! does NOT go out over a pipe to be re-decoded — it is resampled here and
//! handed to the firmware as 48kHz S16LE periods, which is the format the
//! Echo's music plane already takes (`PcmSpeaker.MusicSource`).
//!
//! Why on the device at all: the controller-fed path pays for every link
//! stall. The fleet measures 4.6–7.1% packet loss, TCP's ordering turns one
//! lost segment into a multi-second application stall, and `em_player`'s 4s
//! pacing lead is documented in its own source as "MITIGATION, not a cure".
//! An Echo that fetches its own stream has librespot's whole prefetch
//! between it and the network instead of a 4s window.
//!
//! The split with the controller is deliberate and narrow: catalogue search,
//! OAuth tokens and the dashboard stay there, because they need HTTPS to
//! Spotify's Web API, a browser and durable storage. The device is handed a
//! URI to load and reports what it is doing.

pub mod proto;
pub mod resample;
pub mod volume;

// The librespot-facing halves. Behind the feature so the pure modules above
// can be tested on the host without cross-compiling 300 crates.
#[cfg(feature = "librespot")]
pub mod mixer;
#[cfg(feature = "librespot")]
pub mod sink;
