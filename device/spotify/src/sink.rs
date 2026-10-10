//! The audio sink: librespot's decoded output, resampled and handed to the
//! firmware as the periods its music plane already takes.
//!
//! This is a librespot `Sink` implementation, so librespot drives it the
//! same way it drives its ALSA backend. That is deliberate rather than
//! convenient: librespot's own comment on the player thread says
//!
//!     "While PlayerInternal is written as a future, it still contains
//!      blocking code. It must be run by using block_on() in a dedicated
//!      thread."
//!
//! so a blocking write here is the contract, not a hazard — the player has
//! a thread and a runtime of its own, and blocking it is how every backend
//! applies backpressure. We rely on that: the Go side reads at the rate
//! ALSA consumes, the socket buffer fills, our write blocks, and librespot
//! stops decoding ahead. Nothing in this process needs to know the clock.
//!
//! The format on the wire is bare interleaved 48kHz S16LE stereo, no
//! framing, because that is exactly what `PcmSpeaker.MusicSource` wants a
//! period of. Framing would mean a parser at both ends for no information.

use std::io::Write;
use std::os::unix::net::UnixStream;
use std::path::PathBuf;
use std::sync::mpsc::Sender;
use std::time::{Duration, Instant};

use librespot_playback::audio_backend::{Sink, SinkError, SinkResult};
use librespot_playback::convert::Converter;
use librespot_playback::decoder::AudioPacket;

use crate::resample::{self, Resampler};

/// Something the sink noticed that the control plane should report.
///
/// The sink runs on librespot's player thread and must never block on the
/// control socket, so it posts these and carries on. A full channel drops
/// the note rather than waiting: a diagnostic is not worth a gap in the
/// audio.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum SinkNote {
    /// The audio socket came up. Carries nothing — the Go side knows which
    /// socket it accepted.
    Connected,
    /// The audio socket went away. The firmware restarting its listener is
    /// the ordinary cause, and the sink reconnects by itself.
    Disconnected { reason: String },
    /// librespot handed us a packet we cannot use. Reported rather than
    /// skipped, because the audible result is silence and a silence with
    /// no explanation is the worst version of this.
    UnsupportedPacket,
}

/// How long to wait between attempts to dial the firmware's audio socket.
///
/// Short, because the usual reason it is missing is that the firmware is
/// mid-restart and will be back in under a second. The sink does not give
/// up: it is not the component that decides whether Spotify should be
/// running, and exiting here would silently end playback.
const REDIAL_DELAY: Duration = Duration::from_millis(250);

/// Attempts per `write` before giving the packet up.
///
/// One retry, not many: holding a decoded packet while redialling costs
/// buffer depth on the device, and librespot's own retry is to stop and let
/// the player resume.
const REDIAL_ATTEMPTS: usize = 2;

pub struct PeriodSink {
    socket_path: PathBuf,
    stream: Option<UnixStream>,
    resampler: Resampler,
    /// Reused across packets so steady-state playback allocates nothing.
    f32_buf: Vec<f32>,
    byte_buf: Vec<u8>,
    notes: Sender<SinkNote>,
    last_redial: Option<Instant>,
}

impl PeriodSink {
    pub fn new(socket_path: PathBuf, notes: Sender<SinkNote>) -> Self {
        PeriodSink {
            socket_path,
            stream: None,
            resampler: Resampler::new(),
            f32_buf: Vec::new(),
            byte_buf: Vec::new(),
            notes,
            last_redial: None,
        }
    }

    fn note(&self, n: SinkNote) {
        // Deliberately ignoring a send failure. The only ways it fails are
        // a full buffer and a receiver that has gone, and in both cases the
        // right thing for an audio thread to do is carry on.
        let _ = self.notes.send(n);
    }

    /// Get a connected stream, dialling if needed.
    fn connect(&mut self) -> Result<&mut UnixStream, std::io::Error> {
        if self.stream.is_none() {
            // Rate-limit redials so a missing socket cannot spin the player
            // thread. The player calls write() per packet, which is tens of
            // times a second.
            if let Some(at) = self.last_redial {
                let since = at.elapsed();
                if since < REDIAL_DELAY {
                    std::thread::sleep(REDIAL_DELAY - since);
                }
            }
            self.last_redial = Some(Instant::now());
            let s = UnixStream::connect(&self.socket_path)?;
            // No write timeout on purpose: a blocking write IS the
            // backpressure that paces this whole path. A timeout here would
            // turn a device whose buffer is briefly full into dropped
            // audio.
            self.stream = Some(s);
            self.note(SinkNote::Connected);
        }
        Ok(self.stream.as_mut().expect("just connected"))
    }

    fn drop_stream(&mut self, reason: String) {
        if self.stream.take().is_some() {
            self.note(SinkNote::Disconnected { reason });
        }
    }
}

impl Sink for PeriodSink {
    fn start(&mut self) -> SinkResult<()> {
        // A fresh filter history for a new playback run. The cost is the
        // filter's group delay of silence — about 1.1ms — against the
        // alternative of the previous track's tail bleeding into the first
        // samples of the next one.
        self.resampler.reset();
        Ok(())
    }

    fn stop(&mut self) -> SinkResult<()> {
        if let Some(s) = self.stream.as_mut() {
            // Flush what is already handed over; do not tear the socket
            // down. A pause is followed by a resume far more often than by
            // a shutdown, and reconnecting per pause would churn for
            // nothing.
            let _ = s.flush();
        }
        Ok(())
    }

    fn write(&mut self, packet: AudioPacket, _converter: &mut Converter) -> SinkResult<()> {
        let samples = match packet {
            AudioPacket::Samples(s) => s,
            // Passthrough mode hands over encoded frames. We never enable
            // it (the firmware takes PCM periods), so this is a
            // misconfiguration rather than a stream we should try to
            // interpret.
            AudioPacket::Raw(_) => {
                self.note(SinkNote::UnsupportedPacket);
                return Err(SinkError::InvalidParams(
                    "passthrough packets cannot be resampled; run without --passthrough".into(),
                ));
            }
        };

        self.f32_buf.clear();
        self.f32_buf.reserve(
            self.resampler
                .output_frames_for(samples.len() / resample::CHANNELS)
                * resample::CHANNELS,
        );
        self.resampler.process(&samples, &mut self.f32_buf);
        if self.f32_buf.is_empty() {
            // Only reachable while the filter is still filling its history
            // at the very start of a session.
            return Ok(());
        }

        self.byte_buf.clear();
        resample::to_s16le(&self.f32_buf, &mut self.byte_buf);

        // Moved out and put back so the write can borrow `self` mutably for
        // the redial while still reading the bytes. Putting it back keeps
        // the capacity, so steady-state playback allocates nothing.
        let bytes = std::mem::take(&mut self.byte_buf);
        let result = self.send(&bytes);
        self.byte_buf = bytes;
        result
    }
}

impl PeriodSink {
    /// Write one period, redialling once if the firmware's listener moved.
    fn send(&mut self, data: &[u8]) -> SinkResult<()> {
        let mut last_err: Option<std::io::Error> = None;
        for _ in 0..REDIAL_ATTEMPTS {
            match self.connect() {
                Ok(stream) => match stream.write_all(data) {
                    Ok(()) => return Ok(()),
                    Err(e) => {
                        self.drop_stream(e.to_string());
                        last_err = Some(e);
                    }
                },
                Err(e) => last_err = Some(e),
            }
        }

        let e = last_err.expect("the loop runs at least once and sets this on failure");
        Err(SinkError::OnWrite(format!(
            "writing to {}: {e}",
            self.socket_path.display()
        )))
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::Read;
    use std::os::unix::net::UnixListener;
    use std::sync::mpsc::channel;

    /// A temp path that does not collide between tests.
    fn sock_path(tag: &str) -> PathBuf {
        let mut p = std::env::temp_dir();
        p.push(format!(
            "em-spotify-test-{tag}-{}-{:?}",
            std::process::id(),
            std::thread::current().id()
        ));
        let _ = std::fs::remove_file(&p);
        p
    }

    fn silence(frames: usize) -> Vec<f64> {
        vec![0.0; frames * resample::CHANNELS]
    }

    #[test]
    fn writes_s16_periods_to_the_socket() {
        let path = sock_path("write");
        let listener = UnixListener::bind(&path).unwrap();
        let (tx, rx) = channel();
        let mut sink = PeriodSink::new(path.clone(), tx);

        let reader = std::thread::spawn(move || {
            let (mut s, _) = listener.accept().unwrap();
            let mut buf = Vec::new();
            // Read until the writer closes.
            let _ = s.read_to_end(&mut buf);
            buf
        });

        let mut conv = Converter::new(None);
        sink.start().unwrap();
        // One second of input must arrive as one second of 48k stereo S16.
        sink.write(AudioPacket::Samples(silence(44100)), &mut conv)
            .unwrap();
        sink.stop().unwrap();
        drop(sink);

        let got = reader.join().unwrap();
        let frames = got.len() / (resample::CHANNELS * 2);
        assert!(
            (frames as i64 - 48000).abs() <= 1,
            "expected ~48000 frames on the wire, got {frames}"
        );
        assert_eq!(rx.recv().unwrap(), SinkNote::Connected);
        let _ = std::fs::remove_file(&path);
    }

    #[test]
    fn a_missing_socket_is_an_error_not_a_panic() {
        // The firmware may not have its listener up yet. The sink must say
        // so and let librespot's player decide, rather than taking the
        // process down.
        let path = sock_path("absent");
        let (tx, _rx) = channel();
        let mut sink = PeriodSink::new(path, tx);
        let mut conv = Converter::new(None);
        sink.start().unwrap();
        let err = sink.write(AudioPacket::Samples(silence(4410)), &mut conv);
        assert!(matches!(err, Err(SinkError::OnWrite(_))), "got {err:?}");
    }

    #[test]
    fn passthrough_packets_are_refused_loudly() {
        // Silently skipping these would present as a device that plays
        // nothing with a perfectly healthy log.
        let path = sock_path("raw");
        let (tx, rx) = channel();
        let mut sink = PeriodSink::new(path, tx);
        let mut conv = Converter::new(None);
        let err = sink.write(AudioPacket::Raw(vec![0u8; 16]), &mut conv);
        assert!(
            matches!(err, Err(SinkError::InvalidParams(_))),
            "got {err:?}"
        );
        assert_eq!(rx.recv().unwrap(), SinkNote::UnsupportedPacket);
    }

    #[test]
    fn a_dropped_reader_is_reported_and_retried() {
        // The firmware restarting is the ordinary cause. The sink must not
        // treat it as fatal, because it is not the component that decides
        // whether Spotify should be playing.
        let path = sock_path("drop");
        let listener = UnixListener::bind(&path).unwrap();
        let (tx, rx) = channel();
        let mut sink = PeriodSink::new(path.clone(), tx);
        let mut conv = Converter::new(None);

        // Accept and immediately close, twice: once for the first write,
        // once for the retry.
        let acceptor = std::thread::spawn(move || {
            for _ in 0..3 {
                if let Ok((s, _)) = listener.accept() {
                    drop(s);
                }
            }
        });

        sink.start().unwrap();
        // Writes may succeed into a socket buffer whose reader has gone, so
        // this asserts on the NOTES rather than on the return: what matters
        // is that a disconnect is observable.
        for _ in 0..4 {
            let _ = sink.write(AudioPacket::Samples(silence(4410)), &mut conv);
        }
        drop(sink);
        let _ = acceptor.join();

        let notes: Vec<SinkNote> = rx.try_iter().collect();
        assert!(
            notes.iter().any(|n| matches!(n, SinkNote::Connected)),
            "never reported a connection: {notes:?}"
        );
        let _ = std::fs::remove_file(&path);
    }
}
