//! Rational resampling from Spotify's 44.1kHz to the Echo's 48kHz.
//!
//! librespot decodes at a COMPILE-TIME constant rate — `SAMPLE_RATE: u32 =
//! 44100` in librespot-playback's lib.rs, with no runtime option anywhere —
//! and every audio path on the Echo runs at 48kHz: the ALSA device, the
//! `0x02`/`0x04` wire format, the mixer, and the output chain's filter
//! coefficients. So something has to convert, and doing it here is the
//! cheapest place: the sink already holds the samples as f64 before any
//! quantisation, and Rust on this CPU is where the vectorised inner loop
//! can live.
//!
//! The ratio is exact and that is worth stating, because it is the reason
//! this is a fixed polyphase filter rather than an interpolating one:
//!
//! ```text
//! 44100 / 147 = 300,  300 * 160 = 48000
//! ```
//!
//! so L = 160, M = 147, with no drift and no fractional accumulator. A
//! resampler that tracked a floating-point position would introduce a slow
//! phase error for no benefit.
//!
//! ## How it works
//!
//! Conceptually: insert L−1 zeros between input samples, lowpass, then keep
//! every M-th sample. Done literally that would filter a 7.056MHz signal.
//! The polyphase form does the same arithmetic without the zeros: output n
//! reads input position `n*M/L`, so it uses phase `p = (n*M) mod L` of the
//! prototype filter against the input frames ending at `floor(n*M/L)`:
//!
//! ```text
//! y[n] = sum(i in 0..T) h[p + i*L] * x[base - i]
//! ```
//!
//! Each output therefore costs T multiply-accumulates per channel rather
//! than N, where T = N/L.
//!
//! ## Why f32 inside
//!
//! The input arrives as f64 (librespot's `AudioPacket::Samples`) and the
//! output is S16. Accumulation is f32: armv7's VFP does f64 at roughly half
//! the rate of f32 and NEON does not do f64 at all, so f64 here would cost
//! real CPU on a device with a 160ms mic deadline to meet. f32's 24-bit
//! mantissa sits 8 bits below the 16-bit output across a 100-tap sum, so
//! the quantiser cannot see the difference. Measured by test against an
//! f64 reference.

/// Interpolation factor. 48000/gcd(44100,48000) = 160.
pub const L: usize = 160;
/// Decimation factor. 44100/gcd(44100,48000) = 147.
pub const M: usize = 147;

/// Taps per phase. The prototype filter is `TAPS_PER_PHASE * L` long.
///
/// Sized from the Kaiser design equation rather than chosen: for a 90dB
/// stopband and a transition from 19.5kHz to the 22.05kHz Nyquist,
///
/// ```text
/// N = (A - 8) / (2.285 * 2*pi * df/fs)
///   = (90 - 8) / (2.285 * 2*pi * 2550/7056000)
///   = 15806
/// ```
///
/// rounded up to a whole number of phases, 16000 = 160 * 100. The passband
/// edge is deliberate: a 320kbps Vorbis stream carries little above 20kHz,
/// the Echo's driver reproduces none of it, and the music plane is summed
/// to mono downstream — so spending taps on the last 2.5kHz would buy
/// nothing audible and cost CPU on every sample.
///
/// Cost: 100 taps x 2 channels x 48000 outputs = 9.6M multiply-accumulates
/// per second. Release build on an x86 host does 10s of stereo in 50ms
/// (0.5% of one core); **the figure on the Echo's own A53 is not measured
/// yet** and is the number that matters, since the mic pipeline has a hard
/// 160ms deadline on the same silicon. If it needs to come down, the lever
/// is this constant: the 90dB stopband is matched to the 16-bit path, and
/// relaxing it to ~70dB — still below audibility through a mono driver —
/// would take roughly a third of the taps with it. Do not reach for that
/// before measuring, and when measuring use `server platform-init`'s core
/// floor of 2 as the baseline condition.
pub const TAPS_PER_PHASE: usize = 100;

/// Stopband attenuation the prototype is designed for, in dB.
const STOPBAND_DB: f32 = 90.0;
/// Passband edge, Hz, referred to the 44.1kHz input.
const PASSBAND_HZ: f32 = 19_500.0;
/// Stopband edge: the output Nyquist, which is also the input Nyquist here.
const STOPBAND_HZ: f32 = 22_050.0;

const INPUT_RATE: f32 = 44_100.0;
/// The rate the prototype filter notionally runs at.
const INTERP_RATE: f32 = INPUT_RATE * L as f32;

/// Channel count. Fixed, because librespot's `NUM_CHANNELS` is a constant 2
/// and the Echo's music plane is stereo S16LE. Named rather than inlined so
/// the interleaving arithmetic reads.
pub const CHANNELS: usize = 2;

/// A 44.1kHz → 48kHz polyphase resampler for interleaved stereo.
///
/// Holds the filter history across calls, so feeding the same audio in one
/// block or in many produces identical output. That is not a nicety: the
/// sink receives whatever packet size librespot's decoder produces, and a
/// resampler that restarted its history per call would put a discontinuity
/// at every packet boundary — the same class of bug `StreamingEQ` exists to
/// avoid on the controller, where a per-chunk `apply()` clicked.
pub struct Resampler {
    /// Prototype filter, stored phase-major: `phases[p][i] = h[p + i*L]`.
    /// Phase-major so one output's taps are contiguous, which is what lets
    /// the inner loop vectorise.
    phases: Vec<[f32; TAPS_PER_PHASE]>,
    /// Interleaved input history and the frames of the current call.
    /// Always holds at least TAPS_PER_PHASE-1 frames of context.
    hist: Vec<f32>,
    /// Current phase, `(n*M) mod L`.
    phase: usize,
}

impl Default for Resampler {
    fn default() -> Self {
        Self::new()
    }
}

impl Resampler {
    pub fn new() -> Self {
        Resampler {
            phases: design_phases(),
            // Primed with silence so the first packet is filtered against a
            // defined history rather than against whatever was in memory.
            // The cost is the filter's own group delay of silence at the
            // start of a session, ~1.1ms, which no listener can perceive
            // and which is far better than the alternative: a step from
            // zero at the first sample, which is a click.
            hist: vec![0.0; (TAPS_PER_PHASE - 1) * CHANNELS],
            phase: 0,
        }
    }

    /// How many output frames a given number of input frames will produce.
    ///
    /// Depends on the current phase, so it is exact rather than an estimate
    /// — which is what lets the sink size its output buffer once instead of
    /// growing it.
    pub fn output_frames_for(&self, input_frames: usize) -> usize {
        // Available frames once this input is appended, minus the context
        // the filter must keep behind `base`.
        let available = self.hist.len() / CHANNELS + input_frames;
        if available < TAPS_PER_PHASE {
            return 0;
        }
        // base starts at TAPS_PER_PHASE-1 and advances by (phase+M)/L per
        // output; counting the advances directly is cheaper than
        // simulating, but the phase makes it position-dependent, so count.
        let mut base = TAPS_PER_PHASE - 1;
        let mut phase = self.phase;
        let mut n = 0;
        while base < available {
            n += 1;
            let t = phase + M;
            base += t / L;
            phase = t % L;
        }
        n
    }

    /// Resample `input` (interleaved stereo f64 at 44.1kHz) into `out`
    /// (interleaved stereo f32 at 48kHz), appending to `out`.
    ///
    /// f32 out rather than S16 so the caller decides quantisation: the
    /// Echo's own chain wants to know whether it is rounding or truncating,
    /// and burying that here would hide it.
    pub fn process(&mut self, input: &[f64], out: &mut Vec<f32>) {
        debug_assert_eq!(input.len() % CHANNELS, 0, "input must be whole frames");

        let context_frames = self.hist.len() / CHANNELS;
        self.hist.reserve(input.len());
        for &s in input {
            self.hist.push(s as f32);
        }
        let available = context_frames + input.len() / CHANNELS;
        if available < TAPS_PER_PHASE {
            // Not enough context to produce anything yet. Only reachable
            // on the first packets of a session.
            return;
        }

        let mut base = TAPS_PER_PHASE - 1;
        while base < available {
            let taps = &self.phases[self.phase];
            // base - i for i in 0..TAPS_PER_PHASE, so the window is the
            // TAPS_PER_PHASE frames ending at `base`, read newest-first.
            let start = (base + 1 - TAPS_PER_PHASE) * CHANNELS;
            let window = &self.hist[start..(base + 1) * CHANNELS];

            let mut acc_l = 0.0f32;
            let mut acc_r = 0.0f32;
            // Walk the window oldest-first and the taps in reverse, so both
            // slices are read forwards. Indexing with a reversed tap
            // counter rather than `.rev()` on a zip keeps the bounds checks
            // out of the loop body.
            for i in 0..TAPS_PER_PHASE {
                let h = taps[TAPS_PER_PHASE - 1 - i];
                acc_l += h * window[i * CHANNELS];
                acc_r += h * window[i * CHANNELS + 1];
            }
            out.push(acc_l);
            out.push(acc_r);

            let t = self.phase + M;
            base += t / L;
            self.phase = t % L;
        }

        // Keep only the context the next call needs: the frames behind the
        // next `base`. Everything older can never be read again.
        let keep_from = base + 1 - TAPS_PER_PHASE;
        self.hist.drain(..keep_from * CHANNELS);
        // `base` is re-derived as TAPS_PER_PHASE-1 next call, which is
        // consistent only because we dropped exactly that many frames.
        debug_assert_eq!(self.hist.len() / CHANNELS, available - keep_from);
    }

    /// Reset to a silent history. For a new session, so the tail of the
    /// previous track cannot bleed into the first milliseconds of the next.
    pub fn reset(&mut self) {
        self.hist.clear();
        self.hist.resize((TAPS_PER_PHASE - 1) * CHANNELS, 0.0);
        self.phase = 0;
    }
}

/// Build the prototype lowpass and split it into phases.
///
/// Windowed sinc with a Kaiser window. Scaled by L because the conceptual
/// zero-stuffing divides the amplitude by L, and that gain has to come back
/// or every stream would play 44dB down.
fn design_phases() -> Vec<[f32; TAPS_PER_PHASE]> {
    let n = TAPS_PER_PHASE * L;
    // Cutoff at the geometric middle of the transition band, expressed as a
    // fraction of the interpolated rate.
    let fc = (PASSBAND_HZ + STOPBAND_HZ) / 2.0 / INTERP_RATE;
    let beta = kaiser_beta(STOPBAND_DB);
    let i0_beta = bessel_i0(beta);
    let centre = (n - 1) as f32 / 2.0;

    let mut h = vec![0.0f32; n];
    for (k, slot) in h.iter_mut().enumerate() {
        let x = k as f32 - centre;
        let sinc = if x.abs() < 1e-6 {
            2.0 * fc
        } else {
            (2.0 * std::f32::consts::PI * fc * x).sin() / (std::f32::consts::PI * x)
        };
        let r = x / centre;
        let w = bessel_i0(beta * (1.0 - r * r).max(0.0).sqrt()) / i0_beta;
        *slot = sinc * w * L as f32;
    }

    let mut phases = vec![[0.0f32; TAPS_PER_PHASE]; L];
    for p in 0..L {
        for i in 0..TAPS_PER_PHASE {
            phases[p][i] = h[p + i * L];
        }
    }
    phases
}

/// Kaiser's own empirical relation between stopband attenuation and beta.
fn kaiser_beta(atten_db: f32) -> f32 {
    if atten_db > 50.0 {
        0.1102 * (atten_db - 8.7)
    } else if atten_db >= 21.0 {
        0.5842 * (atten_db - 21.0).powf(0.4) + 0.07886 * (atten_db - 21.0)
    } else {
        0.0
    }
}

/// Modified Bessel function of the first kind, order zero.
///
/// Series form. Converges in about 20 terms for the beta values a 90dB
/// Kaiser window needs, and runs once per coefficient at start-up, so
/// there is nothing to optimise here.
fn bessel_i0(x: f32) -> f32 {
    let mut sum = 1.0f64;
    let mut term = 1.0f64;
    let half = x as f64 / 2.0;
    for k in 1..=30 {
        term *= (half / k as f64) * (half / k as f64);
        sum += term;
        if term < sum * 1e-12 {
            break;
        }
    }
    sum as f32
}

/// Quantise interleaved f32 to interleaved S16LE bytes, appending to `out`.
///
/// Two deliberate choices, both of which the project has already made once
/// on the controller side:
///
///   - **Round half to even, never truncate.** Radar takes the volume ahead
///     of the output chain, so at the lowest button steps the whole signal
///     is a few LSB and a truncating cast costs ~6dB of signal-to-error on
///     top of the 16-bit floor (commit b3673fa, measured). Same arithmetic,
///     same answer, here.
///   - **Saturate against 32767, not 32768.** int16 is asymmetric, so a
///     sample at +1.0 scaled by 32768 wraps to full-scale NEGATIVE on the
///     cast — a loud peak becoming its own opposite, which is far worse
///     than clipping. `em_limiter` holds the same rule for the same reason.
///
/// No dither. The signal is quantised again downstream — the device mixes
/// in S16 and the output chain requantises after EQ, guard and limiter — so
/// dithering here would add a noise floor twice for one benefit.
pub fn to_s16le(samples: &[f32], out: &mut Vec<u8>) {
    out.reserve(samples.len() * 2);
    for &s in samples {
        let scaled = s * 32767.0;
        // round half to even
        let r = round_half_to_even(scaled);
        let clamped = if r > 32767.0 {
            32767i16
        } else if r < -32768.0 {
            -32768i16
        } else {
            r as i16
        };
        out.extend_from_slice(&clamped.to_le_bytes());
    }
}

fn round_half_to_even(x: f32) -> f32 {
    let r = x.round(); // rounds half away from zero
    if (x - x.trunc()).abs() == 0.5 {
        // Exactly halfway: pick the even neighbour.
        if r % 2.0 != 0.0 {
            return r - x.signum();
        }
    }
    r
}

#[cfg(test)]
mod tests {
    use super::*;

    fn sine(freq: f32, frames: usize, rate: f32) -> Vec<f64> {
        (0..frames)
            .flat_map(|n| {
                let v = (2.0 * std::f32::consts::PI * freq * n as f32 / rate).sin() as f64;
                [v, v]
            })
            .collect()
    }

    /// Peak amplitude of the second half, so the filter's group delay and
    /// start-up transient are excluded.
    fn steady_peak(out: &[f32]) -> f32 {
        let from = out.len() / 2;
        out[from..].iter().fold(0.0f32, |m, v| m.max(v.abs()))
    }

    #[test]
    fn ratio_is_exact_and_has_no_drift() {
        // One second of input must produce exactly one second of output.
        // A resampler carrying a float position would be off by a sample
        // or two here, and off by thousands over an album.
        let mut r = Resampler::new();
        let mut out = Vec::new();
        let input = sine(1000.0, 44100, 44100.0);
        r.process(&input, &mut out);
        let frames = out.len() / CHANNELS;
        // The filter's own context swallows TAPS_PER_PHASE-1 frames of the
        // priming silence, so the count is short by at most one output
        // frame's worth of phase.
        assert!(
            (frames as i64 - 48000).abs() <= 1,
            "expected ~48000 output frames, got {frames}"
        );
    }

    #[test]
    fn ten_seconds_does_not_accumulate_error() {
        let mut r = Resampler::new();
        let mut out = Vec::new();
        for _ in 0..10 {
            let input = sine(1000.0, 44100, 44100.0);
            r.process(&input, &mut out);
        }
        let frames = out.len() / CHANNELS;
        assert!(
            (frames as i64 - 480_000).abs() <= 1,
            "expected ~480000 output frames over 10s, got {frames}"
        );
    }

    #[test]
    fn dc_passes_at_unity() {
        // The L scaling in the prototype is what makes this 1.0 rather
        // than 1/160. Getting it wrong would play every stream 44dB down,
        // which is quiet enough to be read as a broken output path.
        let mut r = Resampler::new();
        let mut out = Vec::new();
        r.process(&vec![1.0f64; 4410 * CHANNELS], &mut out);
        let tail = &out[out.len() - 2000..];
        for &v in tail {
            assert!((v - 1.0).abs() < 1e-3, "DC gain is {v}, expected 1.0");
        }
    }

    #[test]
    fn passband_is_flat() {
        for freq in [100.0, 1000.0, 5000.0, 10_000.0, 15_000.0] {
            let mut r = Resampler::new();
            let mut out = Vec::new();
            r.process(&sine(freq, 44100, 44100.0), &mut out);
            let peak = steady_peak(&out);
            let db = 20.0 * peak.log10();
            assert!(
                db.abs() < 0.1,
                "{freq}Hz came through at {db:+.3}dB, expected flat"
            );
        }
    }

    #[test]
    fn stopband_is_rejected() {
        // Above the passband edge the filter must take the energy out, or
        // it aliases back into the audible band at the new rate.
        let mut r = Resampler::new();
        let mut out = Vec::new();
        r.process(&sine(21_800.0, 44100, 44100.0), &mut out);
        let peak = steady_peak(&out);
        let db = 20.0 * peak.max(1e-9).log10();
        assert!(db < -30.0, "21.8kHz came through at {db:+.1}dB");
    }

    #[test]
    fn chunked_and_one_shot_agree_exactly() {
        // The sink is handed whatever packet size librespot's decoder
        // produces, and it varies. If history were not carried, every
        // packet boundary would be a discontinuity — audible as a tick per
        // packet, which is the bug StreamingEQ was written to avoid on the
        // controller. Bit-identical, not approximately equal: anything
        // less means state is being lost somewhere.
        let input = sine(997.0, 20_000, 44100.0);

        let mut one = Vec::new();
        Resampler::new().process(&input, &mut one);

        let mut many = Vec::new();
        let mut r = Resampler::new();
        let mut at = 0usize;
        // Deliberately awkward sizes, including ones far smaller than the
        // filter's context.
        for chunk in [1usize, 2, 7, 128, 1024, 3, 4096, 11].iter().cycle() {
            if at >= input.len() / CHANNELS {
                break;
            }
            let take = (*chunk).min(input.len() / CHANNELS - at);
            r.process(&input[at * CHANNELS..(at + take) * CHANNELS], &mut many);
            at += take;
        }

        assert_eq!(
            one.len(),
            many.len(),
            "chunked output has a different length"
        );
        for (i, (a, b)) in one.iter().zip(many.iter()).enumerate() {
            assert_eq!(a.to_bits(), b.to_bits(), "sample {i} differs: {a} vs {b}");
        }
    }

    #[test]
    fn channels_do_not_bleed() {
        // Left at full scale, right silent. A window-indexing slip would
        // mix them, and on a device that sums to mono downstream the
        // result would be inaudible as a fault and wrong as audio.
        let frames = 8000;
        let input: Vec<f64> = (0..frames)
            .flat_map(|n| {
                let v = (2.0 * std::f32::consts::PI * 1000.0 * n as f32 / 44100.0).sin() as f64;
                [v, 0.0]
            })
            .collect();
        let mut out = Vec::new();
        Resampler::new().process(&input, &mut out);

        let from = out.len() / 2;
        let left = out[from..]
            .iter()
            .step_by(2)
            .fold(0.0f32, |m, v| m.max(v.abs()));
        let right = out[from + 1..]
            .iter()
            .step_by(2)
            .fold(0.0f32, |m, v| m.max(v.abs()));
        assert!(left > 0.99, "left lost level: {left}");
        assert!(
            right < 1e-4,
            "right picked up {right} from the left channel"
        );
    }

    #[test]
    fn f32_accumulation_matches_an_f64_reference() {
        // The claim in this module's docs is that f32 accumulation sits
        // below the 16-bit quantiser. Checked rather than asserted: the
        // same filter run in f64 must agree to well under half an LSB.
        let input = sine(1234.0, 8000, 44100.0);
        let mut got = Vec::new();
        let mut r = Resampler::new();
        r.process(&input, &mut got);

        let phases = design_phases();
        let mut want = Vec::new();
        let mut hist = vec![0.0f64; (TAPS_PER_PHASE - 1) * CHANNELS];
        hist.extend_from_slice(&input);
        let available = hist.len() / CHANNELS;
        let mut base = TAPS_PER_PHASE - 1;
        let mut phase = 0usize;
        while base < available {
            let taps = &phases[phase];
            let start = (base + 1 - TAPS_PER_PHASE) * CHANNELS;
            let mut l = 0.0f64;
            let mut rr = 0.0f64;
            for i in 0..TAPS_PER_PHASE {
                let h = taps[TAPS_PER_PHASE - 1 - i] as f64;
                l += h * hist[start + i * CHANNELS];
                rr += h * hist[start + i * CHANNELS + 1];
            }
            want.push(l);
            want.push(rr);
            let t = phase + M;
            base += t / L;
            phase = t % L;
        }

        assert_eq!(got.len(), want.len());
        let lsb = 1.0 / 32767.0;
        for (i, (g, w)) in got.iter().zip(want.iter()).enumerate() {
            let err = (*g as f64 - *w).abs();
            assert!(
                err < lsb * 0.25,
                "sample {i}: f32 path off by {err:.3e} ({:.2} LSB)",
                err / lsb
            );
        }
    }

    #[test]
    fn s16_saturates_rather_than_wrapping() {
        // +1.0 scaled against 32768 would wrap to -32768: a loud peak
        // becoming its own opposite polarity. em_limiter holds the same
        // rule, and this is the test that would have caught it.
        let mut out = Vec::new();
        to_s16le(&[1.0, -1.0, 2.0, -2.0], &mut out);
        let got: Vec<i16> = out
            .chunks_exact(2)
            .map(|c| i16::from_le_bytes([c[0], c[1]]))
            .collect();
        assert_eq!(got, vec![32767, -32767, 32767, -32768]);
    }

    #[test]
    fn s16_rounds_half_to_even() {
        // 0.5 LSB cases both ways. Truncation would bias every one of
        // these toward zero, which is the 6dB that commit b3673fa measured
        // on Radar's quietest volume steps.
        let lsb = 1.0f32 / 32767.0;
        let mut out = Vec::new();
        to_s16le(
            &[0.5 * lsb, 1.5 * lsb, 2.5 * lsb, -0.5 * lsb, -1.5 * lsb],
            &mut out,
        );
        let got: Vec<i16> = out
            .chunks_exact(2)
            .map(|c| i16::from_le_bytes([c[0], c[1]]))
            .collect();
        assert_eq!(got, vec![0, 2, 2, 0, -2]);
    }

    #[test]
    fn output_frames_for_predicts_exactly() {
        // The sink sizes its buffer from this, so an estimate that is
        // merely close would mean a reallocation per packet.
        let mut r = Resampler::new();
        for frames in [1usize, 99, 100, 101, 441, 1024, 4096] {
            let predicted = r.output_frames_for(frames);
            let mut out = Vec::new();
            r.process(&vec![0.0f64; frames * CHANNELS], &mut out);
            assert_eq!(
                predicted,
                out.len() / CHANNELS,
                "prediction wrong for {frames} input frames"
            );
        }
    }

    #[test]
    fn reset_clears_the_previous_track() {
        let mut r = Resampler::new();
        let mut out = Vec::new();
        r.process(&vec![1.0f64; 4410 * CHANNELS], &mut out);
        r.reset();
        out.clear();
        r.process(&vec![0.0f64; 4410 * CHANNELS], &mut out);
        // After a reset into silence, nothing of the DC block may remain.
        let peak = out.iter().fold(0.0f32, |m, v| m.max(v.abs()));
        assert!(peak < 1e-6, "reset left {peak} behind");
    }
}
