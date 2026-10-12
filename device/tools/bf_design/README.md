# bf_design — filter-and-sum beams for the 7-mic array, measured

Offline measurement tool. It imports the firmware's own beam design
(`internal/beamformer.DesignBeams`, the code behind the default-off `wakeBeam`
setting), so it measures what ships, and nothing in the firmware imports it.
It puts numbers on summing against single-mic selection at the size of
beamformer Radar's stock AFE runs: 8 beams, 4 taps, 4 mics.

```bash
cd device
go run ./tools/bf_design                    # all 7 mics
go run ./tools/bf_design -mics amazon4      # ch0,1,3,4: Radar's wake-path mics
go run ./tools/bf_design -mics perimeter -dump
go test ./tools/bf_design
```

## What it builds

Plain delay-and-sum. Each mic is delayed by its projection on the look
direction over the speed of sound (343 m/s), plus a common offset that makes
every delay causal, and the delays are summed at weight 1/M. A delay is not a
whole number of samples at 16 kHz (the 72mm aperture spans 3.4 samples), so
each mic's delay is an integer shift plus a 4-tap least-squares fractional-delay
FIR fitted over 100-5000 Hz. A beam's longest filter is 7 samples, and every beam
shares one 3-sample (0.19ms) latency, so changing beam never changes delay. Geometry is `internal/beamformer.MicPosition` (r=36mm), so there is one copy.

## Results (7 mics, 8 beams, default scenario)

Beam 0° pattern, horizontal plane. The filter taps are the real ones, not ideal
delays; look-direction error stays within 0.15dB.

| Hz | DI | level at 90° | at 180° | white-noise gain |
|---|---|---|---|---|
| 250 | 0.1 dB | -0.1 | -0.2 | 8.45 dB |
| 500 | 0.3 | -0.4 | -0.8 | 8.45 |
| 1000 | 1.3 | -1.7 | -3.6 | 8.45 |
| 2000 | 4.4 | -7.9 | -24.3 | 8.45 |
| 3000 | 6.7 | -37.6 | -19.7 | 8.45 |
| 4000 | 8.1 | -13.9 | -9.1 | 8.45 |

Output SNR improvement of the nearest beam over the nearest perimeter mic (the
best the onset-ratio selector can do), mean of 16 target azimuths, 22.5° apart
so half fall between beams. Interferer 6dB above the target, diffuse noise 6dB
below, self-noise 30dB below.

| interferer | 100-500 Hz | 500-1500 Hz | 1500-4000 Hz | 100-4000 Hz |
|---|---|---|---|---|
| +90° | +0.2 dB | +1.6 | +9.9 | +1.9 |
| +180° | +0.3 | +2.6 | +13.9 | +2.4 |

The centre mic and the nearest perimeter mic score the same (to 0.01dB):
against plane waves a single omni mic has no direction, so selection by
position cannot help here. Selection's value in the firmware is the room, not
this model.

## What this does and does not show

- **Below 1.5 kHz a sum buys almost nothing**: DI is 0.1-2.8 dB and the gain
  is 0.2-2.6 dB. That is where speech energy and most of a wake word's
  mel-band content sit, and it agrees with the coherence argument in SETUP.md.
- **Above 1.5 kHz it is large** (10-14 dB against a single interferer), but
  that band is also where plane-wave, free-field assumptions are most
  flattering, and the grating lobes start above 4.7 kHz.
- **Limits of the model.** Free-field plane waves, matched mics, no enclosure
  scattering, no reverberation, no AEC residual. The speech and music sources
  are shaped noise, not recordings. A single coherent interferer is the best
  case for a sum; a real TV in a room is partly diffuse.
- **"4 taps" is not the same 4 taps.** Radar's coefficient file holds 32,768
  values, which is exactly 8 beams x 4 mics x 128 sub-bands x complex x 4
  taps: a separate complex weight per frequency band, across frames. Mine is
  28 taps per beam of one delay each. Matching the count says nothing about
  matching the design freedom. Their set can shape a null per band; this one
  cannot.
- **The "-15 dB" in the coefficient filenames is a design parameter, not a
  measured rejection.** The files say `idnr_m15` and "-15dB Idrn: Normal
  noise"; the neighbouring files are named `EVD_*_MVDR`. Nothing in the
  firmware defines IDNR. The reading consistent with the names is an MVDR-type
  design regularised against diffuse noise, which is the superdirective class
  `device/CLAUDE.md` already identifies as the only one that works at this
  aperture. Plain delay-and-sum has no such term, so there is no -15 dB figure
  here to compare against. Its diffuse-noise gain is -DI: about 0 dB at 500 Hz.
