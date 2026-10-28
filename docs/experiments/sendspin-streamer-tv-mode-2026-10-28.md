# Streamer TV mode: latency measured at the speakers (2026-10-28)

TV mode (`tvdsp.py` in the NAS streamer, `sendspin-streamer-tv-mode.patch`)
processes the TV audio before it is encoded:
- dialogue lift: a 2 kHz peak on the centre of the stereo image only;
- bass trim: a first-order low shelf at 120 Hz;
- loudness: a slow leveller and a peak compressor, with gains decided per
  1 ms block.

Every filter is a causal IIR with its state carried across chunks. The chunk's
play time is fixed (`OwnTimeline.next`) before the DSP runs, so the DSP cannot
move when the chunk plays. The processing time comes out of the lead's network
margin.

## Off the device (inside the image, on the NAS's Ryzen R2514)

- Test suites: `test_tvdsp.py` 10 and `test_volume_sync.py` 13, all passing.
- Cost per 10 ms chunk at the strongest settings: median 0.67 ms, p99
  0.93 ms, max 2.3 ms. With TV mode off the chunk is returned untouched, at
  0.02 ms.
- Lag at the strongest settings (night, +9 dialogue, −12 bass): broadband
  noise correlates at lag 0.
- Group delay at the strongest settings: under 0.25 ms from 60 Hz up and
  0.1 ms at 2 kHz. Some bands show negative group delay, which is expected of
  a shelf cut.
- Output with 10 ms and 20 ms chunks is identical to the last LSB.
- Full-scale input at every setting gave no NaN and no int16 wrap.

At −12 dB, the bass trim gives −5.3 dB at 60 Hz and −2.7 dB at 100 Hz, less
than its label suggests.

Added on deploy: if the DSP raises, the chunk passes through unprocessed and
the error is logged at most once a minute. Without this, the stream loop's
handler would restart the capture every second.

## At the speakers (dac_probe, both Echos)

The method was the same as in `sendspin-group-desync-2026-10-27.md`.
- Source: `test:clicks` (one 1 kHz burst a second).
- Captures: Living Room and Lounge captured for 40 s on the host clock.
- TV mode changed every ~10 s: off, then night +9/−12, then off, then level
  out +4/−12.
- Each click's onset was placed on a 1 s grid fitted to the clicks played
  with TV mode off.

| TV mode | Living Room | Lounge |
|---|---|---|
| off (grid) | ±0.02 ms | ±0.00 ms |
| night, +9 dialogue, −12 bass | −0.20 ms | −0.21 ms |
| level out, +4 dialogue, −12 bass | −0.23 ms | −0.19 ms |

With TV mode on, the onset is about 0.2 ms EARLIER, and it returns to the grid
as soon as TV mode is switched off. The compressor and the presence boost
shape the burst's envelope, so the 25 % threshold is crossed sooner. Nothing
was delayed.

The grid period was 1000.000 ms on both Echos, with no timeline steering
triggered by TV mode.

Living Room showed a 40 ms step between its 3rd and 4th clicks, about 3 s
after the source switch restarted the stream, and before TV mode was first
changed. This is how the player settles after a stream restart; it is
unrelated to TV mode.

During the click run Living Room played 2.3 ms ahead of Lounge, steady through
every TV-mode setting. After switching back to the TV source, two 8 s
captures of TV audio gave +0.06 and +0.05 ms (Living Room after Lounge).

## Added after the measurement: an on/off switch and a fast mode

- **Switch (`tv_on`, on by default).** Off means the input is passed through
  bit for bit. The DSP keeps running while TV mode is off and its output is
  discarded, so switching back on is instant and already levelled. The change
  is a 20 ms linear crossfade: the processed and unprocessed audio are
  sample-aligned, so the fade cannot comb-filter.
- **New installs default to** +4 dB dialogue, −12 dB bass and level out.
  Saved settings are kept.
- **`game` loudness ("fast (gaming)").** The same leveller made fast:
  - 0.1 s integration;
  - gain raised at up to 10 dB/s and lowered at up to 20 dB/s (level out: 1.5
    and 4);
  - ±9 dB range;
  - peak compressor at −14 dB, 3:1, 60 ms release.

  This brings quiet cues such as footsteps back right after a loud passage.
  The cost is pumping on music and ambience, which is why level out stays
  slow.
- **Tests** (28 in all, in the image): switched off is bit-exact; switching on
  after 10 s off is already lifted by more than 8 dB; toggling three times on a
  −10 dBFS tone gives no step larger than the programme's own at full gain;
  one second after a loud passage, game has recovered more than 5 dB further
  than normal; game holds a −1 dBFS burst under −3 dBFS within 2 ms.
