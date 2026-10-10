//! Volume: held here, applied on the device, reported both ways.
//!
//! This is a librespot `Mixer` that **does not touch the audio**. It stores
//! the level Spotify asked for, reports the change, and returns `NoOpVolume`
//! as its soft-volume getter so the player leaves every sample alone.
//!
//! That is the only arrangement that gives one volume model, and the reason
//! is on the device rather than here. The Echo applies volume in its own
//! speaker write loop (`speaker/swvolume.go`, DAC held at unity 127), and on
//! Radar it applies it **ahead of the output chain** — stock FireOS
//! attenuates in AudioFlinger before the AFE's FIR and MBCL see the signal,
//! so the compressors only engage once the user turns it up. If librespot
//! attenuated here as well, the volume would be applied twice: Spotify's
//! slider at 50% and the Echo's level at 50% would play at 25%, and Radar's
//! loudness curve would be selected for the wrong level.
//!
//! So the flow is: Spotify's slider → `set_volume` here → an event to the
//! firmware → the firmware's own volume controller → the codec. And back
//! the other way, a physical button or a Home Assistant volume reaches us
//! as a `volume` command and lands in the same cell, which is what keeps
//! the phone's slider showing the truth. The same rule Sendspin settled on:
//! a server's volume IS the Echo's volume.

use std::sync::atomic::{AtomicU16, Ordering};
use std::sync::mpsc::Sender;
use std::sync::Arc;

use librespot_core::Error;
use librespot_playback::mixer::{Mixer, MixerConfig, NoOpVolume, VolumeGetter};

/// A volume change that came from Spotify's side.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct VolumeNote(pub u16);

/// Stores the volume, attenuates nothing, reports what changed.
pub struct ReportingMixer {
    level: AtomicU16,
    notes: Sender<VolumeNote>,
}

impl ReportingMixer {
    pub fn new(initial: u16, notes: Sender<VolumeNote>) -> Arc<Self> {
        Arc::new(ReportingMixer {
            level: AtomicU16::new(initial),
            notes,
        })
    }

    /// Take a level that came from the DEVICE and store it without
    /// reporting it back.
    ///
    /// The distinction from `set_volume` is the whole point: an echo of the
    /// firmware's own change would come straight back as another volume
    /// event, and with the firmware applying every event it receives, the
    /// two would chase each other. Spirc still learns the new value,
    /// because it reads the mixer when it reports device state.
    pub fn accept_device_level(&self, level: u16) {
        self.level.store(level, Ordering::Relaxed);
    }
}

impl Mixer for ReportingMixer {
    fn open(_config: MixerConfig) -> Result<Self, Error>
    where
        Self: Sized,
    {
        // Never called: librespot's `MIXERS` registry builds mixers from a
        // config, and we construct this one directly with the channel it
        // reports on. Returning an error rather than a half-built mixer
        // with a dead channel, which would silently stop reporting volume.
        Err(Error::unimplemented(
            "ReportingMixer is constructed directly, not through the mixer registry",
        ))
    }

    fn volume(&self) -> u16 {
        self.level.load(Ordering::Relaxed)
    }

    fn set_volume(&self, volume: u16) {
        let previous = self.level.swap(volume, Ordering::Relaxed);
        if previous != volume {
            // Only on a change. Spirc sets the volume on every state
            // report, and forwarding each one would have the firmware
            // write the codec dozens of times a minute for no change —
            // which on a device that runs for years on eMMC is the same
            // class of waste as the console password's unconditional
            // write.
            let _ = self.notes.send(VolumeNote(volume));
        }
    }

    fn get_soft_volume(&self) -> Box<dyn VolumeGetter + Send> {
        // Unity, always. See the module docs: the device attenuates, and on
        // Radar it does so ahead of the output chain, so attenuating here
        // would both halve the level twice and move the loudness curve.
        Box::new(NoOpVolume)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::mpsc::channel;

    #[test]
    fn soft_volume_is_always_unity() {
        // If this ever returns anything else, every stream plays at the
        // square of the intended volume and Radar picks the wrong EQ
        // curve. Pinned across the whole range.
        let (tx, _rx) = channel();
        for level in [0u16, 1, 32768, 65535] {
            let m = ReportingMixer::new(level, tx.clone());
            assert_eq!(m.get_soft_volume().attenuation_factor(), 1.0);
        }
    }

    #[test]
    fn a_change_from_spotify_is_reported() {
        let (tx, rx) = channel();
        let m = ReportingMixer::new(0, tx);
        m.set_volume(40000);
        assert_eq!(rx.recv().unwrap(), VolumeNote(40000));
        assert_eq!(m.volume(), 40000);
    }

    #[test]
    fn setting_the_same_level_reports_nothing() {
        // Spirc writes the volume on every state report. Forwarding the
        // unchanged ones would have the firmware write the codec for no
        // reason, repeatedly, forever.
        let (tx, rx) = channel();
        let m = ReportingMixer::new(1000, tx);
        m.set_volume(1000);
        m.set_volume(1000);
        assert!(rx.try_recv().is_err(), "an unchanged volume was reported");
    }

    #[test]
    fn a_level_from_the_device_does_not_echo_back() {
        // The loop this prevents: device volume → event → firmware applies
        // it → reports it → event → firmware applies it. Both ends would
        // keep acting and neither would be wrong.
        let (tx, rx) = channel();
        let m = ReportingMixer::new(0, tx);
        m.accept_device_level(12345);
        assert_eq!(m.volume(), 12345);
        assert!(rx.try_recv().is_err(), "a device level was echoed back");
    }

    #[test]
    fn the_registry_constructor_refuses_rather_than_half_building() {
        // A mixer built through the registry would have no channel to
        // report on, and the symptom would be a volume slider in the
        // Spotify app that moves and changes nothing.
        let r = <ReportingMixer as Mixer>::open(MixerConfig::default());
        assert!(r.is_err());
    }
}
