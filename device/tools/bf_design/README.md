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

## Against Radar's stock beams

```bash
go run ./tools/bf_design -amazon /path/to/vendor/etc/audio-algorithms/coefs_FBFV2_4micsSq_8beams_idnr_m15_lowLatency.cfg
```

The table is read from the path given and is not in this repository. It is
32,768 values, laid out `[beam 8][mic 4][band 128][tap 4][re, im]` (found by
experiment: every beam's block opens with eight zeros for DC, and read this
way the eight beams have unity gain at their peak, a peak azimuth that holds
within a degree across 1-2.5kHz, and eight distinct directions). Mics are the
AFE's channel map `[0,1,3,4]`, bands are the 256-point filter bank's, and tap t
is t hops of 128 samples. The sign conventions cannot be told apart from
magnitudes; all eight give the same beams up to a half turn, which choosing the
best beam per target does not see. The same code measures both designs, with
the sums evaluated analytically (a filter bank has no time-domain form here);
for the shipped design that agrees with the time-domain simulation above to
about 1dB.

Directivity at 500 / 1000 / 2000 / 3000 Hz, mean over the eight beams:

| | 500 | 1000 | 2000 | 3000 | white-noise gain at 1kHz |
|---|---|---|---|---|---|
| stock, 4 mics | 5.6 dB | 6.8 | 7.5 | 6.6 | -2.4 to +0.5 dB (mean -1.3) |
| ours, same 4 mics | 0.4 | 1.5 | 4.4 | 5.3 | +6.0 |

Output SNR gain over one mic, 6dB interferer, -6dB diffuse noise, best beam per
target, mean of 16 azimuths:

| interferer | design | 100-500 | 500-1500 | 1500-4000 | 100-4000 |
|---|---|---|---|---|---|
| +90° | stock | +5.8 dB | +10.9 | +9.7 | +6.7 |
| | ours, 4 mics | +0.2 | +1.7 | +6.5 | +1.7 |
| | ours, 7 mics | +0.1 | +1.3 | +8.7 | +1.7 |
| +180° | stock | +10.0 | +9.4 | +12.0 | +9.8 |
| | ours, 4 mics | +0.3 | +3.1 | +8.7 | +2.2 |
| | ours, 7 mics | +0.3 | +2.5 | +13.2 | +2.4 |

## What this does and does not show

- **The aperture is not what limits a sum below 1.5kHz.** Stock gets 5.6-7.5dB
  of directivity from the same four mics at 36 x 62mm where delay-and-sum
  gets 0.4-4.4. The beams are superdirective: the 1kHz white-noise gain is
  negative on six of the eight beams (mean -1.3dB), meaning they spend sensor self-noise to buy
  directivity, which is the trade `device/CLAUDE.md` names as the only class
  that works at this aperture. The shipped design is plain delay-and-sum and
  gets none of that; its gain is above 1.5kHz.
- **Self-noise is where that trade is paid, and stock prepares for it.**
  `asp.cfg` turns on a per-unit mic calibration read from `/proc/idme/miccal.*`
  (default value 16384). A superdirective sum on unmatched capsules across four
  ADCs amplifies the mismatch; nothing here reads those values.
- **Beam directions are not a 45° grid.** The stock peaks sit near 35°, 56°,
  112°, 136°, 215°, 236°, 292° and 316°, in four pairs, and the beams are wide
  (7dB directivity), so a talker is always within one beam's reach. Ours are
  eight at 45° with narrower low-frequency patterns.
- **Limits of the model.** Free-field plane waves, matched mics, no
  enclosure, no reverberation, no AEC residual, and nothing after the beams:
  stock follows them with an adaptive stage that subtracts beams about 135° away
  from the target, which this does not model. Sources are shaped noise, not
  recordings. A single coherent interferer is the best case for any sum.
- **Their "4 taps" are per band.** 32,768 = 8 beams x 4 mics x 128 sub-bands x
  complex x 4 taps: a separate complex weight per frequency band and frame
  delay. Ours is 28 delay taps per beam. Matching the count says nothing about
  matching the design freedom.
- **The "-15 dB" in the filenames is a design parameter, not a measured
  rejection.** The files say `idnr_m15` and "-15dB Idrn: Normal noise"; the
  neighbouring files are `EVD_*_MVDR`. Nothing in the firmware defines IDNR.
  The reading consistent with the names and with the measurements above is an
  MVDR-type design with a diffuse-noise term, which is what gives the 5.6dB at
  500Hz. Delay-and-sum has no such term, so there is no -15 dB figure for it;
  its diffuse-noise gain is -DI, about 0 dB at 500Hz.
