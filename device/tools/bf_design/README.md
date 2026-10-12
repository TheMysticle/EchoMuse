# bf_design — beams for the mic array, measured

Offline measurement tool. It imports the firmware's own beam designs
(`internal/beamformer`: `DesignBeams`, a delay-and-sum baseline, and
`DesignSuperdirective`, the design behind the default-off `wakeBeam` setting)
and runs the superdirective beams through the firmware's own stage, so it
measures what ships. Nothing in the firmware imports it. It puts numbers on
summing against single-mic selection, on the shipped beams against Radar's stock
ones, and on the interference canceller after them.

```bash
cd device
go run ./tools/bf_design                         # delay-and-sum baseline, all 7 mics
go run ./tools/bf_design -sd -mics amazon4       # the shipped beams: ch0,1,3,4
go run ./tools/bf_design -sd -mics amazon4 -ara -reverb      # with the canceller, in a room
go run ./tools/bf_design -sd -wng -3 -ring 0     # shift the floor, turn the ring off
go run ./tools/bf_design -amazon /path/to/coefs_FBFV2_4micsSq_8beams_idnr_m15_lowLatency.cfg -sd -mics amazon4
go test ./tools/bf_design
```

`-mics` is `all`, `perimeter` or `amazon4` (ch0, ch1, ch3, ch4, the four Radar's
AFE sums for its wake path: a 36 x 62mm rectangle on this board). `-wng` shifts
the white-noise-gain floor profile in dB; `-ring`, `-sector`, `-ring.low` and
`-azimuths` set the noise model and the look directions.

## What it builds

**Delay-and-sum** (`DesignBeams`). Each mic is delayed by its projection on the
look direction over the speed of sound (343 m/s), plus a common offset that
makes every delay causal, and the delays are summed at weight 1/M. A delay is
not a whole number of samples at 16 kHz (the 72mm aperture spans 3.4), so each is
an integer shift plus a 4-tap least-squares fractional-delay FIR. This is the
baseline; it is not what the firmware runs.

**Superdirective** (`DesignSuperdirective`, what the firmware runs). One complex
weight per mic per 62.5Hz bin, applied to the mics' spectra inside the canceller
stage (`internal/beamformer/ara.go`), which is also the domain Radar's stock
beamformer works in:

- At each bin, minimum-variance weights against a noise field: an isotropic
  diffuse field (coherence `sin(kd)/kd`) plus a ring of equal interferers round
  the horizontal plane outside a ±35° look sector, at three times the diffuse
  power. The ring is what lets a beam suppress one loud source (a TV on a shelf)
  that a diffuse model does not know about. The weights pass the look direction
  at exactly unity.
- Loaded just enough to keep the white-noise gain above a floor that follows
  stock's measured profile: -7dB at 250Hz, -4 at 500, -1.5 at 1kHz, 0 at 2kHz and
  above. Below 0dB a beam makes a mic's own noise louder than a single mic's,
  which is the price of differencing mics 36mm apart.
- Eight beams looking at stock's directions for these four mics: 322, 289, 235,
  205, 142, 110, 55 and 25 degrees, in stock's index order. They sit in four pairs
  about 22 degrees apart (stock's peaks are at 316/292, 236/215, 136/112 and
  56/35), not on a 45° grid. These are design angles that make ours peak where
  stock's do, since a beam's peak sits a few degrees off its look direction.
- No time-domain filters. A first version fitted 32 and then 64-tap FIRs to the
  same weights. At 32 taps (2ms) the directivity at 250Hz was 2.8dB against
  stock's 4.5; at 64 it matched and the wake path cost 2.8ms a batch. Forming
  the beams from the spectra the canceller already takes costs 0.6ms, and the
  look direction is exact, with no fit to correct.

Geometry is `internal/beamformer.MicPosition` (r=36mm), so there is one copy.

## Against Radar's stock beams

```bash
go run ./tools/bf_design -sd -mics amazon4 -amazon /path/to/coefs_FBFV2_4micsSq_8beams_idnr_m15_lowLatency.cfg
```

The table is read from the path given and is not in this repository. It is
32,768 values, laid out `[beam 8][mic 4][band 128][tap 4][re, im]` (found by
experiment: every beam's block opens with eight zeros for DC, and read this way
the eight beams have unity gain at their peak, a peak azimuth that holds within a
degree across 1-2.5kHz, and eight distinct directions). Mics are the AFE's
channel map `[0,1,3,4]`, bands are the 256-point filter bank's, and tap t is t
hops of 128 samples. The sign conventions cannot be told apart from magnitudes;
all eight give the same beams up to a half turn, which choosing the best beam per
target does not see. Every design is measured by the same code, analytically
(sums over bands), and the time-domain beams are checked against that analysis
by a test.

Directivity index, mean over the eight beams, four stock mics:

| Hz | 250 | 500 | 1000 | 2000 | 3000 |
|---|---|---|---|---|---|
| stock | 4.49 dB | 5.64 | 6.75 | 7.52 | 6.59 |
| **ours** | 4.40 | 5.57 | 6.78 | 7.53 | 6.44 |
| delay-and-sum | 0.10 | 0.39 | 1.50 | 4.41 | 5.32 |

White-noise gain per beam, 250 / 500 / 1000 / 2000 Hz: stock -7.7 to -7.0, -5.3 to
-2.7, -2.4 to +0.5, +2.0 to +3.7 dB; ours -7.5 to -6.9, -4.3 to -4.0, -1.5 to
-1.4, +2.8 to +3.8 dB.

Output SNR of the beams alone, dB (analytic, best beam per target, 16 target
azimuths; interferer 6dB above the talker, diffuse noise 6dB below, self-noise
30dB below):

| interferer | design | 100-500 Hz | 500-1500 | 1500-4000 | 100-4000 |
|---|---|---|---|---|---|
| +90° | single mic | -5.5 | -5.2 | -9.6 | -6.3 |
| | stock | +0.3 | +5.7 | +0.1 | +0.4 |
| | **ours** | -0.4 | +5.2 | +0.0 | +0.1 |
| | delay-and-sum | -5.3 | -3.5 | -3.1 | -4.6 |
| +180° | stock | +4.5 | +4.2 | +2.4 | +3.5 |
| | **ours** | +4.2 | +7.9 | +4.3 | +4.9 |
| | delay-and-sum | -5.2 | -2.1 | -1.0 | -4.1 |

What stock turned out to be, and what the match took:

- **It is superdirective.** 4.5-7.5dB of directivity from the four mics where
  delay-and-sum gets 0.1-5.3, with negative white-noise gain up to 2kHz.
- **It calibrates the mics first.** `asp.cfg` enables a per-unit level read from
  `/proc/idme/miccal.*` (default 16384). Seven values read off a Radar unit are
  15483 to 17018: every capsule within ±0.5dB of unity as a Q14 gain.
- **Its "4 taps" are per band.** 32,768 = 8 beams x 4 mics x 128 sub-bands x
  complex x 4 taps.
- **The "-15 dB" in the filenames is a design parameter, not a rejection
  figure.** The files say `idnr_m15` and "-15dB Idrn: Normal noise"; the
  neighbouring files are `EVD_*_MVDR`. Nothing in the firmware defines IDNR; the
  reading consistent with the names and the measurements is an MVDR design with
  a directional-interference term.
- **Look directions mattered**: with stock's pairs instead of an even grid the
  directivity at 2kHz went from 6.1 to 7.2dB and the +180° SNR from +0.5 to +1.5dB.
- **Filter length mattered**, until the filters went. 250Hz directivity was
  2.8dB at 32 taps, 4.0 at 48, 4.6 at 64, and is 4.4 as per-bin weights.
- **The ring costs about 1dB of directivity at 1-3kHz and buys the side
  interferer**: ring weight 3 and ±35° gave the closest overall match; weights
  0 and 5, and sectors 50 and 65, each moved something the wrong way.

## The interference canceller (ARA)

```bash
go run ./tools/bf_design -sd -mics amazon4 -ara -reverb      # a room: direct path plus four reflections
go run ./tools/bf_design -sd -mics amazon4 -ara -ara.offsets 0,45,90,180
```

After the beams, each beam's interference is predicted from the two beams 135
degrees round the ring (stock's table: beam i uses i+3 and i+5) and subtracted,
per 62.5Hz band, with a complex filter over six 8ms frames. The code is
`internal/beamformer/ara.go` and this tool runs that code. The canceller adapts,
so signal and noise cannot be separated by linearity: each scene is run with and
without the talker and the talker's power is the difference, exact while the
filters are frozen during the talk. A steady interferer is on for 9s so the
filters can learn, then the talker joins; 8 talker azimuths between beams, the
best beam for each, wideband 100-4000Hz, interferer 6dB above the talker at a
mic unless stated, diffuse noise 6dB below.

| scene | single mic | beams, no ARA | beams + ARA | ARA gain | talker through it |
|---|---|---|---|---|---|
| free field, +90° | -6.4 dB | +1.4 | +11.4 | +10.1 | -0.7 dB |
| free field, +180° | -6.3 | +5.7 | +10.8 | +5.1 | -0.8 |
| room, +90° | -8.6 | -2.9 | +8.7 | +11.6 | -0.9 |
| room, +180° | -8.6 | -1.0 | +9.5 | +10.5 | -0.5 |
| room, interferer AT the talker | -8.6 | -6.5 | -1.0 | +5.5 | -0.8 |
| room, interferer 0dB re talker, +90° | -3.0 | +2.7 | +9.9 | +7.2 | -1.0 |
| room, interferer 0dB re talker, +180° | -3.0 | +4.3 | +10.3 | +6.0 | -0.7 |
| room, interferer -6dB re talker, +90° | +1.6 | +7.1 | +10.4 | +3.3 | -1.2 |
| room, interferer -6dB re talker, +180° | +1.6 | +8.2 | +10.0 | +1.8 | -1.6 |
| no interferer (-40dB) | +6.0 | +11.1 | +11.9 | +0.8 | -1.0 |

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
  which takes that to -0.5dB. A residual that stays high for 3.2s is taken as the
  room having changed, and learnt again.
- **Step size.** 0.5 gave 30% extra noise on unrelated beams (NLMS
  misadjustment). 0.2 while young and 0.05 after leaves 3% (-15dB) and the same
  cancellation; steady steps from 0.02 to 0.1 are within 0.4dB, adaptation
  ratios 0.25 to 1 within 0.1dB, and seeds within 0.05dB.
- **The suppression floor** (the output is never below this fraction of the
  beam): 0.2 (-14dB) was limiting; 0.1 (-20dB) gains about 1dB and costs the
  talker nothing; below 0.1 gains nothing.
- **Learning time.** From a cold start the cut is 7dB after one second and 15dB
  after two (a single clean source; a room will be slower). Have the interferer
  on for several seconds before speaking.
- **Cost.** On a desktop core one 160ms batch of the whole wake path takes 0.6ms
  with the canceller off and 1.1ms with it on; the previous design with 64-tap
  filters took 2.8 and 3.6ms. Extrapolated to the A53, about 4-7% of one core.
  Not measured on a device.

## What this does and does not show

- **Aperture is not what limits a sum below 1.5kHz; the design is.** On the same
  four mics delay-and-sum reaches -4.6 to -4.1dB wideband against a 6dB
  interferer, and the shipped beams +0.1 to +4.9, within 0.3dB of stock's at +90°
  and 1.3dB above it at +180°.
- **This is the beams, not the whole of stock's chain.** Stock follows them with
  its own canceller and a beam merger whose numbers cannot be read out of the
  firmware, so the match above is beam against beam, and the ARA table is ours.
  Putting stock's beams through our canceller is a possible comparison; it needs
  their weights applied in a spectral stage and has not been done.
- **Seven mics beat the four** for a sum-and-cancel design, and were measured at
  +1.1 to +1.4dB wideband for an earlier version of the beams. The four are used
  to follow the stock choice; change `fbfChannels` to use more.
- **Mismatch is where a superdirective sum fails on hardware.** These figures
  assume matched mics. The firmware applies the `miccal` levels (as a Q14
  multiplier, the direction confirmed on a device by the level check it logs) and
  the white-noise-gain floor is what bounds the cost of what is left.
- **Between bin centres** the weights of neighbouring bins blend. A test passes a
  tone at 312, 1031, 2093 and 3500Hz through the shipped stage: the look
  direction stays within half a dB of unity, and an off-axis direction, where the
  response changes quickly from one bin to the next, within 4dB of the analytic
  number.
- **Limits of the model.** Free-field plane waves (plus four reflections for
  "room"), matched mics, no enclosure, no AEC residual. Sources are shaped noise,
  not recordings. A single coherent interferer is the best case for any sum.
