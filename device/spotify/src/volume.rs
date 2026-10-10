//! Which volume changes the firmware is told about, as pure logic.
//!
//! A volume reaches the firmware by TWO routes, and only one of them can be
//! suppressed where it is produced. `mixer::ReportingMixer::accept_device_level`
//! stops a level that came FROM the device going back out through the mixer.
//! But librespot's spirc also calls `emit_volume_changed_event` from inside
//! its own `set_volume`, gated on `old_volume != new_volume` — a field
//! `accept_device_level` does not touch. So a level the device set arrived
//! back as a Spotify volume event, the firmware applied it, reported it, and
//! round it went.
//!
//! On hardware that is the volume walking up and down on its own during
//! playback, with the reported level jumping with it. It does not settle,
//! because the conversion between Spotify's u16 and the Echo's level is not
//! a fixed point: on Radar the level table is stock's 30 coarse steps, so
//! each lap lands somewhere new and starts another one.
//!
//! The gate therefore sits at the ONE place every event leaves by, rather
//! than at either producer. Two reasons that is the right altitude: it
//! covers both routes by construction, so a third cannot reintroduce the
//! loop; and it also stops a genuine Spotify change being reported twice,
//! since the mixer note and the player event carry the same level and the
//! firmware would write the codec for both.
//!
//! The rule is just "unchanged is not news". What makes it sufficient is
//! that a device-originated level is recorded BEFORE it is handed to spirc,
//! so the echo is already not news by the time it comes back.

use std::sync::atomic::{AtomicU32, Ordering};

/// Nothing has been reported yet. Deliberately not a valid `u16`, so a real
/// volume of 0 is reportable — the Echo's level 0 is true silence, and
/// swallowing it would leave a muted device looking like it ignored the
/// slider.
const NONE: u32 = u32::MAX;

/// The last volume the firmware has been told, and the decision to tell it
/// again.
#[derive(Debug)]
pub struct VolumeGate(AtomicU32);

impl Default for VolumeGate {
    fn default() -> Self {
        VolumeGate(AtomicU32::new(NONE))
    }
}

impl VolumeGate {
    pub fn new() -> Self {
        Self::default()
    }

    /// Whether `level` is news, recording it either way.
    ///
    /// One atomic swap, so two threads reporting the same level cannot both
    /// decide it is news.
    pub fn should_report(&self, level: u16) -> bool {
        self.0.swap(level as u32, Ordering::Relaxed) != level as u32
    }

    /// Record a level that came FROM the firmware, without reporting it.
    ///
    /// Called before the level is handed to spirc, which is what makes the
    /// event librespot emits from inside `set_volume` arrive as old news.
    pub fn note_device(&self, level: u16) {
        self.0.store(level as u32, Ordering::Relaxed);
    }

    /// The last level reported, or `None` if nothing has been.
    pub fn last(&self) -> Option<u16> {
        match self.0.load(Ordering::Relaxed) {
            NONE => None,
            v => Some(v as u16),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_level_the_device_set_is_not_reported_back_to_it() {
        // The loop this closes. Recorded first, so librespot's own
        // volume_changed event for the same level is already old news.
        let g = VolumeGate::new();
        g.note_device(30000);
        assert!(!g.should_report(30000), "the device's own level echoed back");
    }

    #[test]
    fn a_genuine_spotify_change_still_reaches_the_firmware() {
        // The dedup must not cost the feature it protects: a change made on
        // the phone has to reach the device.
        let g = VolumeGate::new();
        g.note_device(30000);
        assert!(g.should_report(40000));
    }

    #[test]
    fn one_change_is_reported_once_not_twice() {
        // A Spotify-originated change arrives on BOTH routes carrying the
        // same level — the mixer's note and the player event. Applying it
        // twice is a redundant codec write on hardware that runs for years
        // on eMMC.
        let g = VolumeGate::new();
        assert!(g.should_report(12345));
        assert!(!g.should_report(12345));
    }

    #[test]
    fn the_first_report_is_always_news_including_zero() {
        // `NONE` is not a valid u16 precisely so this holds. Level 0 is true
        // silence on this hardware, so swallowing it would leave a muted
        // Echo looking like it ignored the slider.
        assert!(VolumeGate::new().should_report(0));
        assert!(VolumeGate::new().should_report(u16::MAX));
    }

    #[test]
    fn moving_away_and_back_is_news_both_times() {
        // Only the IMMEDIATELY preceding level is suppressed. A slider taken
        // down and back up must arrive, or the second move is lost.
        let g = VolumeGate::new();
        assert!(g.should_report(1000));
        assert!(g.should_report(2000));
        assert!(g.should_report(1000));
    }

    #[test]
    fn nothing_reported_reads_as_nothing_reported() {
        let g = VolumeGate::new();
        assert_eq!(g.last(), None);
        g.note_device(7);
        assert_eq!(g.last(), Some(7));
    }
}
