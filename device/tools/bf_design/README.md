# bf_design — beams for the mic array, measured

Offline measurement tool. It imports the firmware's own beam designs
(`internal/beamformer`: `DesignBeams`, a delay-and-sum baseline, and
`DesignSuperdirective`, the design behind the default-off `wakeBeam` setting),
so it measures what ships, and nothing in the firmware imports it. It puts
numbers on summing against single-mic selection, and on both against Radar's
stock beams.

```bash
cd device
go run ./tools/bf_design                         # delay-and-sum, all 7 mics
go run ./tools/bf_design -sd -mics amazon4       # the shipped design: ch0,1,3,4
go run ./tools/bf_design -sd -wng -3 -ring 0     # shift the floor, turn the ring off
go run ./tools/bf_design -amazon /path/to/coefs_FBFV2_4micsSq_8beams_idnr_m15_lowLatency.cfg
go test ./tools/bf_design
```

`-mics` is `all`, `perimeter` or `amazon4` (ch0, ch1, ch3, ch4, the four Radar's
AFE sums for its wake path: a 36 x 62mm rectangle on this board). `-wng` shifts
the white-noise-gain floor profile in dB, `-ring` and `-sector` set the
directional-interference model.

## What it builds

**Delay-and-sum** (`DesignBeams`). Each mic is delayed by its projection on the
look direction over the speed of sound (343 m/s), plus a common offset that
makes every delay causal, and the delays are summed at weight 1/M. A delay is
not a whole number of samples at 16 kHz (the 72mm aperture spans 3.4), so each is
an integer shift plus a 4-tap least-squares fractional-delay FIR. This is the
baseline; it is not what the firmware runs.

**Superdirective** (`DesignSuperdirective`, what the firmware runs). Minimum-
variance weights per frequency against a noise field, fitted to a 32-tap FIR per
mic (2ms, the latency stock quotes):

- The noise field is an isotropic diffuse field (coherence `sin(kd)/kd`) plus a
  ring of equal interferers round the horizontal plane outside a ±50° look
  sector, at three times the diffuse power. The ring is what lets a beam
  suppress one loud source (a TV on a shelf) that a diffuse model does not know
  about.
- Weights are loaded just enough to keep the white-noise gain above a floor that
  follows stock's measured profile: -7dB at 250Hz, -4 at 500, -1.5 at 1kHz, 0 at
  2kHz and above. Below 0dB a beam makes a mic's own noise louder than a single
  mic's, which is the price of differencing mics 36mm apart.
- The FIRs are fitted over 62.5Hz to Nyquist (the bin spacing of stock's filter
  bank), with bins above 5.2kHz counting for 0.15 as much. The first version
  fitted only to 5.2kHz and gained 66dB above it. The fit is then corrected three
  times for its own look-direction gain error: measure what the beam passes from
  its look direction, scale the targets by the reciprocal, fit again.
- Eight beams at 45° steps, all with a 16-sample latency, so changing beam never
  changes delay and the centre mic delayed 16 samples stands in for "no beam".

Geometry is `internal/beamformer.MicPosition` (r=36mm), so there is one copy.

## Results

Free-field plane waves, matched mics, a single coherent interferer 6dB above the
target and diffuse noise 6dB below it, mean of 16 target azimuths, the best beam
per target. SNR gain is over one mic.

Directivity index, mean over the eight beams, on the four stock mics:

| Hz | 250 | 500 | 1000 | 2000 | 3000 |
|---|---|---|---|---|---|
| stock | 4.5 dB | 5.6 | 6.8 | 7.5 | 6.6 |
| superdirective (ours) | 2.8 | 5.7 | 5.9 | 6.1 | 5.6 |
| delay-and-sum | ~0.1 | 0.4 | 1.5 | 4.4 | 5.3 |

SNR gain over one mic, by band:

| interferer | design | 100-500 Hz | 500-1500 | 1500-4000 | 100-4000 |
|---|---|---|---|---|---|
| +90° | stock, 4 mics | +5.8 dB | +10.9 | +9.7 | +6.7 |
| | **ours, 4 mics** | +3.4 | +10.1 | +9.6 | +5.5 |
| | delay-and-sum, 4 mics | +0.2 | +1.7 | +6.5 | +1.7 |
| | ours, 7 mics | +3.9 | +13.4 | +18.1 | +6.9 |
| +180° | stock, 4 mics | +10.0 | +9.4 | +12.0 | +9.8 |
| | **ours, 4 mics** | +5.6 | +9.2 | +8.2 | +6.8 |
| | delay-and-sum, 4 mics | +0.3 | +3.1 | +8.7 | +2.2 |
| | ours, 7 mics | +6.2 | +8.7 | +12.0 | +7.9 |

The wideband figure is dominated by 100-500Hz, where speech energy is largest
and the aperture smallest.

## Against Radar's stock beams

The table is read from the path given and is not in this repository. It is
32,768 values, laid out `[beam 8][mic 4][band 128][tap 4][re, im]` (found by
experiment: every beam's block opens with eight zeros for DC, and read this way
the eight beams have unity gain at their peak, a peak azimuth that holds within a
degree across 1-2.5kHz, and eight distinct directions). Mics are the AFE's
channel map `[0,1,3,4]`, bands are the 256-point filter bank's, and tap t is t
hops of 128 samples. The sign conventions cannot be told apart from magnitudes;
all eight give the same beams up to a half turn, which choosing the best beam per
target does not see. The same code measures every design, with the sums
evaluated analytically (a filter bank has no time-domain form here); for
delay-and-sum that agrees with the time-domain simulation to about 1dB.

What the measurement says about stock:

- **It is superdirective.** 5.6-7.5dB of directivity from the same four mics
  where delay-and-sum gets 0.4-4.4, with negative white-noise gain: mean -1.3dB
  at 1kHz, -4 at 500 and -7.4 at 250, positive from 2kHz.
- **Its beams are not on a 45° grid.** The peaks sit near 35°, 56°, 112°, 136°,
  215°, 236°, 292° and 316°, in four pairs, and are wide.
- **It calibrates the mics first.** `asp.cfg` enables a per-unit level read from
  `/proc/idme/miccal.*` (default 16384). Seven values read off a Radar unit are
  15483 to 17018: every capsule within ±0.5dB of unity as a Q14 gain.
- **Its "4 taps" are per band.** 32,768 = 8 beams x 4 mics x 128 sub-bands x
  complex x 4 taps: a separate complex weight per band and frame delay.
- **The "-15 dB" in the filenames is a design parameter, not a rejection
  figure.** The files say `idnr_m15` and "-15dB Idrn: Normal noise"; the
  neighbouring files are `EVD_*_MVDR`. Nothing in the firmware defines IDNR; the
  reading consistent with the names and the measurements is an MVDR design with
  a directional-interference term.

## The interference canceller (ARA)

```bash
go run ./tools/bf_design -sd -mics amazon4 -ara -reverb      # a room: direct path plus four reflections
go run ./tools/bf_design -sd -mics amazon4 -ara -ara.offsets 0,45,90,180
```

After the beams, each beam's interference is predicted from the two beams 135
degrees round the ring (stock's table: beam i uses i+3 and i+5) and subtracted,
per 62.5Hz band, with a complex filter over six 8ms frames. The code is
`internal/beamformer/ara.go` and this tool measures that code. The canceller
adapts, so signal and noise cannot be separated by linearity: each scene is
run with and without the talker and the talker's power is the difference,
exact while the filters are frozen during the talk. A steady interferer is on
for 9s so the filters can learn, then the talker joins; 8 talker azimuths between
beams, the best beam for each, wideband 100-4000Hz, interferer 6dB above the
talker at a mic, diffuse noise 6dB below.

| scene | single mic | beams, no ARA | beams + ARA | ARA gain | talker through it |
|---|---|---|---|---|---|
| free field, +90° | -6.4 dB | -1.2 | +9.9 | +11.1 | -1.1 dB |
| free field, +180° | -6.3 | +0.3 | +10.4 | +10.0 | -1.1 |
| room, +90° | -8.6 | -4.2 | +8.2 | +12.3 | -1.2 |
| room, +180° | -8.6 | -3.1 | +8.5 | +11.6 | -1.1 |
| room, interferer AT the talker | -8.6 | -6.9 | -1.0 | +5.9 | -2.1 |
| room, interferer 0dB re talker, +90° | -3.0 | +1.4 | +9.4 | +8.0 | -1.3 |
| room, interferer 0dB re talker, +180° | -3.0 | +2.4 | +9.6 | +7.3 | -1.2 |
| room, interferer -6dB re talker, +90° | +1.6 | +5.9 | +9.4 | +3.4 | -2.1 |
| room, interferer -6dB re talker, +180° | +1.6 | +6.6 | +8.9 | +2.3 | -2.7 |
| no interferer (-40dB) | +6.0 | +10.0 | +11.4 | +1.4 | -1.5 |

("room" is a direct path and four coherent reflections, 2-21ms, -4 to -10dB, from
other directions.) The gain grows with the interferer, which is what it is for.
It does not hurt when there is no interferer or when the interferer is where the
talker is: the beams cannot separate those by direction, and the reflections,
which come from elsewhere, are still cancelled.

What was checked, and what moved the result:

- **The adaptation gate.** Letting the filters learn whenever the references
  are louder than the target beam is the obvious rule, and it is wrong in the
  common case: a TV louder than the talker leaves the references louder while the
  talker speaks, so the filters kept learning during speech and cost the talker
  4.1dB in a clean tone scene. The filters are now also held when their own
  residual jumps (energy the references cannot predict, which is the talker),
  which takes that to -0.5dB and improved the room scenes by 0.3-0.4dB. A
  residual that stays high for 3.2s is taken as the room having changed, and
  learnt again.
- **Step size.** 0.5 gave 30% extra noise on unrelated beams (NLMS
  misadjustment). 0.2 while young and 0.05 after leaves 3% (-15dB) and the same
  cancellation; steady steps from 0.02 to 0.1 are within 0.4dB, adaptation
  ratios 0.25 to 1 within 0.1dB, and seeds within 0.05dB.
- **The suppression floor** (the output is never below this fraction of the
  beam): 0.2 (-14dB) was limiting; 0.1 (-20dB) gains about 1dB and costs the talker
  nothing; below 0.1 gains nothing.
- **Learning time.** From a cold start the cut is 7dB after one second and
  15dB after two (a single clean source; a room will be slower). Have the
  interferer on for several seconds before speaking.
- **Cost.** About 1.0ms per 160ms batch for the canceller, and the spectral stage
  it needs about 0.5ms, on a desktop core; the whole wake path was 1.2-1.9ms.
  Extrapolated to the A53, 5-15% of one core. Not measured on a device.

## What this does and does not show

- **Aperture is not what limits a sum below 1.5kHz; the design is.** On the same
  four mics delay-and-sum gains +0.2 to +3.1dB there and the superdirective
  design +3.4 to +10.1.
- **The shipped design is behind stock, not level with it.** About 1dB of
  directivity at 1-3kHz and 1.7dB at 250Hz, and 1.1dB (90°) and 3.0dB (180°) of
  wideband SNR. The ring costs about 1dB of directivity at 1-3kHz and buys the
  gain against a side interferer. Its white-noise gain at 250Hz is -2 to -3.6dB,
  not the -7 the profile allows, because the ring already regularises the low
  end; stock spends more there. That is the first thing to tune.
- **Seven mics beat the four** for this design: +1.1 to +1.4dB wideband and up to
  8.5dB in 1.5-4kHz against a side interferer, and at 90° they exceed stock's
  wideband figure. The four are used to follow the stock choice and cost 57% of
  the seven; change `fbfChannels` to use more.
- **Mismatch is where a superdirective sum fails on hardware.** These figures
  assume matched mics. The firmware applies the `miccal` levels (as a Q14
  multiplier, the direction being inferred) and logs the ambient level of every
  mic raw, with the calibration and with it reversed, so a device shows which
  direction flattens them.
- **Limits of the model.** Free-field plane waves, matched mics, no enclosure, no
  reverberation, no AEC residual, and nothing after the beams: stock follows them
  with an adaptive stage that subtracts beams about 135° away from the target.
  Sources are shaped noise, not recordings. A single coherent interferer is the
  best case for any sum.
