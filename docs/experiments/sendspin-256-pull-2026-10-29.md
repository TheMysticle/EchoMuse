# Low-latency Sendspin: 256-frame pulls, and the router's channel scanner (2026-10-29)

TV audio from the NAS streamer to the living-room pair (Living Room
G2A0QU05747105HU, Lounge G090P30874710Q0R, both on firmware 20261027-1859-dev
with 512-frame pulls) dropped out while Plex streamed 4K HDR to the TV.

## The network

- The streamer sent every chunk on time throughout (frame lead 57-60 ms at a
  60 ms lead, no late chunks or capture overruns). The loss was downstream.
- The TV (192.168.1.119, then .120) and both Echos were on the same radio, the
  router's 5 GHz "NightHawk" on 44/80. Pinging both Echos at 20 Hz from the NAS
  while Plex played: replies over 30 ms 13 times a minute each, worst 89 and
  96 ms, 7 of Living Room's 8 spikes within 100 ms of one on Lounge. The TV's
  link was at -71 dBm, and the NAS had 4 MB queued to it.
- The router (Netgear RAX70 on SWRT firmware, which reports itself as an
  RT-AX95Q; tri-band) had its third
  radio "Peregrine" (5GHz-2) disabled (`wl2_radio=0`). The user enabled it and
  moved the TV to it: replies over 30 ms fell to 3-5 a minute, none over 50.
- Spikes still hit both Echos at once with channel 44 96% idle, router CPU 95%
  idle and no NightHawk client bursting (per-client byte counters each second
  for 90 s; the busiest, a phone, averaged 2.7 Mbit/s in spike seconds).
  Peregrine was on an Auto channel, which restarts `acsd2`, the channel
  scanner already found to stall every wireless client (2026-10-24). With
  Peregrine fixed at 100/80, `acsd2` exited and replies over 31 ms fell from
  4-11 to 1.0 and 2.4 a minute, over 51 ms from up to 1.5 to 0.3 and 0.
- Peregrine offers only DFS channels (100-140, EU). 100/80 was chosen: 97%
  idle and already past its radar check; 116/80 reaches the 120-128 weather
  radar channels (10-minute check), 132-140 have no 80 MHz, and 160 MHz from
  100 spans 100-128 too, for a TV that links at 433 Mbit/s single-stream.

## Where the lead goes on the Echo

The Echo's `buffered` figure (lead left after the ring and the period being
built) read 0-10 ms at a 50 ms lead, which first looked like a 45 ms playback
cost. It is not the requirement. A late packet must beat the play time of the
period holding its first sample less the fill wait's 8 ms reserve, and that
period starts up to one pull before the sample, so the Echo needs reserve +
pull: 18.7 ms with 512-frame pulls. At 50 ms that tolerates 31 ms of network
delay. Raising `maxFillWait` (40 ms) buys nothing: with the ring held at 48 ms
the cap and `playAt - reserve` are the same instant, and every logged timeout
stopped at 40.1-40.8 ms.

Living Room's own counters by lead, the TV still on NightHawk:

| Lead | Underruns per minute |
|---|---|
| 50 ms | ~12 |
| 60 ms | ~4 |
| 80 ms | ~0.4 |
| 50 ms, TV on Peregrine (scanner still running) | ~6 |

## 256-frame pulls

Test build 20261029-0341-dev on Bedroom (Radar G090QU067393015G, emOS, same
NightHawk radio), joined to the same stream, muted at the player (the pull,
fill wait and write all still run).

At a 50 ms lead, 03:55-04:11, after the scanner fix:

| | Bedroom (256) | Living Room (512) | Lounge (512) |
|---|---|---|---|
| Underruns | 26 | 49 | 50 |
| Late drops | 56 | 102 | 106 |
| `I2S0dl1_pointer underflow` | 0 | 0 | 0 |

All of them came in one spell, 03:58-04:06, on all three at once; the minutes
either side were clean on all three. Over 6 minutes at 60 ms before that,
Bedroom held 14-15 ms `buffered` against 2-12 ms on the pair.

`[speaker] write margin:` (new) logs once a minute the work from a pull's
Fill returning to its write completing, and how far ahead of its play time it
was written. With music alone the work peaked at 5.9-9.3 ms a minute, so the
8 ms reserve stays. Server CPU on Bedroom ~93% of one core against Living
Room's ~108% on 512 (different feature sets; four cores).

Voice over the stream, which is now sliced to 256 too: an 8 s announcement
(a -50 dBFS tone, at 04:14) played 188 periods with 0 underruns; in that
minute the stream gained no underrun and no snap, syncErr stayed 80 us, the
work peaked at 15.7 ms and every period was still written 15.6 ms ahead.

## Recommendation

60 ms on 256-frame pulls for no audible gaps in the network's bad spells; 50 ms
plays clean between them.
