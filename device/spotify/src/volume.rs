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
//! Two rules: "unchanged is not news", and "the echo of a level the device
//! set is not news". A device-originated level is recorded BEFORE it is
//! handed to spirc, so its echo is already old by the time it comes back.
//!
//! THE SECOND RULE NEEDS A QUEUE, NOT A SLOT, and a single slot is why this
//! was only half fixed. spirc emits one `volume_changed` per `set_volume`,
//! and a person holding a volume button changes the level faster than that
//! round trip completes — so several are legitimately in flight. With the
//! echoes arriving 38, 27, 17 while one slot held 17, the first two no
//! longer matched, read as news, went to the firmware, which applied them
//! and sent three more. The button sequence then recirculated for as long as
//! the queue took to drain.
//!
//! Reproduced on a Radar 2026-10-11 and visible once the firmware's log
//! named the writer of each change: ten presses, then six seconds of
//! `Volume set to … (spotify)` replaying 38, 27, 17, 11, 7, 3 in order,
//! over and over. The log line said only "changed" before that, which is
//! why this took five attempts.

use std::collections::VecDeque;
use std::sync::Mutex;

/// Nothing has been reported yet. Deliberately not a valid `u16`, so a real
/// volume of 0 is reportable — the Echo's level 0 is true silence, and
/// swallowing it would leave a muted device looking like it ignored the
/// slider.
const NONE: u32 = u32::MAX;

/// How many device-originated levels can be awaiting their echo.
///
/// A human holding a volume button generates a change every ~150ms and the
/// round trip through spirc is slower than that, so several are legitimately
/// in flight. Measured on a Radar 2026-10-11: ten presses produced a queue
/// the engine replayed for six seconds, and `control plane fell 30 events
/// behind`. Sixteen is far more than a person can get ahead by, and the
/// queue is bounded so a lost echo cannot grow it without limit.
const PENDING_CAP: usize = 16;

/// The volumes the firmware has been told, and the decision to tell it again.
#[derive(Debug)]
pub struct VolumeGate {
    last: Mutex<u32>,
    /// Device-originated levels not yet seen coming back, oldest first.
    ///
    /// A single slot was not enough, and that is the whole bug. spirc emits
    /// one `volume_changed` per `set_volume`, so with three commands in
    /// flight the echoes arrive 38, 27, 17 while the slot holds only 17 —
    /// the first two no longer match, read as news, and go to the firmware,
    /// which applies them, which sends three more. The button sequence
    /// recirculates for as long as the queue takes to drain.
    ///
    /// FIFO because the echoes arrive in the order the commands were sent,
    /// so the oldest match is the right one to consume.
    pending: Mutex<VecDeque<u16>>,
}

impl Default for VolumeGate {
    fn default() -> Self {
        Self::new()
    }
}

impl VolumeGate {
    pub fn new() -> Self {
        Self {
            last: Mutex::new(NONE),
            pending: Mutex::new(VecDeque::new()),
        }
    }

    /// Whether `level` is news for the firmware, recording it either way.
    ///
    /// Two reasons it is not: it is the echo of a level the firmware itself
    /// set (consumed from `pending`), or it is the level the firmware was
    /// last told. Either way acting on it is what loops.
    pub fn should_report(&self, level: u16) -> bool {
        let mut pending = self.pending.lock().unwrap();
        if let Some(i) = pending.iter().position(|&l| l == level) {
            pending.remove(i);
            drop(pending);
            *self.last.lock().unwrap() = level as u32;
            return false;
        }
        drop(pending);
        let mut last = self.last.lock().unwrap();
        if *last == level as u32 {
            return false;
        }
        *last = level as u32;
        true
    }

    /// Record a level that came FROM the firmware, without reporting it.
    ///
    /// Called before the level is handed to spirc, so the event librespot
    /// emits from inside `set_volume` arrives as old news — and queued, so
    /// a burst of changes is still old news when its echoes arrive late.
    pub fn note_device(&self, level: u16) {
        let mut pending = self.pending.lock().unwrap();
        if pending.len() >= PENDING_CAP {
            pending.pop_front();
        }
        pending.push_back(level);
        drop(pending);
        *self.last.lock().unwrap() = level as u32;
    }

    /// The last level reported, or `None` if nothing has been.
    pub fn last(&self) -> Option<u16> {
        match *self.last.lock().unwrap() {
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
    fn a_burst_of_device_changes_is_all_old_news_when_it_echoes_back() {
        // THE BUG, as reproduced on hardware. A person holding the volume
        // button changes the level faster than spirc's round trip, so the
        // echoes arrive late and in order. With one slot the earlier ones no
        // longer matched and were handed back to the firmware, which applied
        // them and sent the whole sequence round again.
        let g = VolumeGate::new();
        let burst = [38u16, 27, 17, 11, 7, 3];
        for l in burst {
            g.note_device(l);
        }
        for l in burst {
            assert!(!g.should_report(l), "{l} echoed back to the device");
        }
    }

    #[test]
    fn a_phone_change_during_a_burst_still_gets_through() {
        // The queue must not swallow everything while it drains, or touching
        // the Echo would deafen it to the phone for as long as the echoes
        // take to arrive.
        let g = VolumeGate::new();
        g.note_device(38);
        g.note_device(27);
        assert!(g.should_report(50000), "a genuine change was swallowed");
        assert!(!g.should_report(38));
        assert!(!g.should_report(27));
    }

    #[test]
    fn an_echo_is_consumed_once_not_forever() {
        // Each command produces exactly one echo. If a matching level
        // suppressed indefinitely, the phone could never set that value
        // again.
        let g = VolumeGate::new();
        g.note_device(1234);
        assert!(!g.should_report(1234));
        g.should_report(9999);
        assert!(g.should_report(1234), "the level became permanently blocked");
    }

    #[test]
    fn the_queue_is_bounded() {
        // A lost echo must not grow the queue without limit. Oldest out
        // first, so the entries that survive are the ones still plausibly in
        // flight.
        let g = VolumeGate::new();
        for i in 0..(PENDING_CAP as u16 + 8) {
            g.note_device(i + 1);
        }
        // The earliest are gone, so they report as news again.
        assert!(g.should_report(1));
        // The most recent are still pending.
        assert!(!g.should_report(PENDING_CAP as u16 + 8));
    }

    #[test]
    fn nothing_reported_reads_as_nothing_reported() {
        let g = VolumeGate::new();
        assert_eq!(g.last(), None);
        g.note_device(7);
        assert_eq!(g.last(), Some(7));
    }
}
