# Echoes that stop hearing broadcast and multicast (2026-10-26)

Both living-room Radars under emOS (Living Room `G2A0XXXXXXXXXXXX`, Lounge
`G090XXXXXXXXXXXX`, firmware 20261026-0207-dev), on the router's 5 GHz-1
radio (eth6, channel 44/80), were unreachable from the LAN in the morning
after a night in a speaker group. Each kept its controller link and Pipecat
worked, because the Echo opens those connections itself. Nothing could open
a connection to them: a host's ARP request (broadcast) got no answer, and
the Sendspin streamer could not dial either.

## What it is not

| Test | Result |
|---|---|
| Router per-station view (`wl sta_info`) | Both AUTHORIZED, AES-CCM, 0 failures. The BSS-wide `tx mcast/bcast pkts` counter was identical for both (~100 frames per 15 s): the router sends the same group frames to both. |
| Router multicast settings | WMF on, `proxy_arp=0`, `mcast_regen_bss_enable=0` on the radio, DTIM 1, GTK rekey 3600 s. Nothing per-station that drops broadcast. |
| Lounge after `wpa_cli reassociate` | Reachable at once on the same radio, same settings: the frames reach a healthy Echo. |
| Living Room WiFi power save off (`iwpriv wlan0 set_power_mode 0`, logged as CAM) | ARP from a host still unanswered over the next ~10 s. |
| Driver RX filter (`driver RXFILTER-STOP`) | Still unanswered. |
| Kernel suspend (the MTK driver filters broadcast in its suspend mode) | Not reached: the firmware holds the `EchoMuse` wakelock. |
| `/sys/class/net/wlan0/statistics/multicast` | Always 0 on this driver, healthy or not; useless as a probe. |

Supplicant (`/run/net.log`, hostap 2.10): every hourly group rekey since the
02:26 association logged `Group rekeying completed`. That shows the
supplicant took the key, not that the chip uses it.

## Living Room recovered without a reassociate

Between 13:48 and 13:58 Living Room began answering a fresh-cache mDNS query
(it can only hear one by multicast). In that window it had been put in CAM
and had its RSN/SEC/OID driver logging and supplicant log level raised;
nothing else was done to it. Its power mode has stayed CAM (a second
`set_power_mode 2` was also logged as CAM).

## Running A/B

From 13:59, both Echoes are asked the same mDNS question every 2 minutes
(scratchpad `hearing_ab.log`): Living Room in CAM, Lounge in the driver's
default. Lounge going deaf while Living Room stays healthy points at power
save; both going deaf at a rekey (hh:11) points at the group key.

## 14:11: both went deaf at the group rekey

The A/B answered the power-save question in one step: at the 14:11:50 rekey
Living Room (CAM) and Lounge (default) went deaf together, first missed
query 14:13:48 for both. Power save is not it. Setting CAM again on the deaf
Living Room did not bring it back (6 queries over 70 s).

Both logs show a clean rekey: the supplicant installed the GTK with
`keyidx=1 tx=0`, `KEY_SEQ 00 00 00 00 00 00` (the router's key RSC was 0:
it starts the packet number of each new group key from zero), and the driver
logged `_wlanoidSetAddKey ... Key Index 1, ucAlgorithmId=4 (CCMP)`.

Working hypothesis: the chip keeps its group-frame replay counter across a
group key install. Every group frame under the new key carries a packet
number below the last one it accepted, so it drops each as a replay until
the router's new count passes the old one. That fits:

- deafness that starts at a rekey and ends by itself later (Living Room
  this morning: deaf by 13:4x, healthy by 13:58, with nothing done that
  could have changed key state);
- Lounge, reassociated at ~13:30, going deaf at the next rekey too: a
  reassociate resets the counter to the current key's number, which then
  climbs for the rest of the hour;
- an Echo that sometimes reconnects fine and sometimes cannot, depending on
  the hour and on how much broadcast traffic the previous key carried.

Prediction to test: both Echoes recover together, when the number of group
frames since 14:11:50 passes the number sent under the 13:11 key. The
router's BSS group-frame counter (`wl sta_info`, `tx mcast/bcast pkts`) is
logged every minute alongside the hearing log to check it.

## 14:48–15:04: reproduced on demand, and the deafness is measurable

The Bedroom Radar (`G090XXXXXXXXXXXX`, on USB) ran a test hostap 2.10 built
with `CONFIG_TESTING_OPTIONS`, so `wpa_cli raw KEY_REQUEST 0 0` asks the
router for a new group key. The router honours it and rekeys the whole BSS,
so Living Room and Lounge act as untouched controls on every run. Probe:
a fresh-cache mDNS question (multicast only) to each Echo.

| Time | Rekey | Previous key lived | Result |
|---|---|---|---|
| 14:48:38 | KEY_REQUEST | ~37 min | Bedroom deaf at once |
| 14:49:45 | KEY_REQUEST | 67 s | Living Room and Lounge (deaf since 14:11) hear again between 14:50:07 and 14:51:07 |
| 14:56:10 | A | ~6 min | All three deaf within 5 s |
| 14:56:31 | B | 21 s | All three hear again within 20 s, Bedroom included, which a clean reassociate at 14:53 had NOT fixed |
| 15:00:45 | C | 4 min 14 s | All three deaf; Living Room and Lounge hear again between 15:03:34 and 15:04:34 |

Run C against the router's BSS group-frame counter (`router_bcmc.log`):
key B carried about 1,600 group frames (938,656 at 14:56:47, 940,259 at
15:00:49). Key C passed 1,600 frames at about 15:04:00 (941,767 at 15:03:50,
942,208 at 15:04:50). The Echoes came back inside that minute.

**Mechanism.** The router starts each new group key's packet number at 0
(key RSC 0 in every install). The Echo's WiFi chip does not reset its
group-frame replay counter when the new key is installed: it keeps the
number the previous key reached, and drops every group frame under the new
key as a replay until the new key's count passes it. So:

- after an hourly rekey the Echo is deaf for about as long as the previous
  hour's broadcast traffic takes to repeat, i.e. most of the next hour, and
  often until the next rekey, which starts it again;
- a second rekey right after the first cures it, because the key it follows
  carried almost nothing;
- unicast never goes through the group key, which is why the controller
  link, Pipecat and running Sendspin streams are untouched.

The supplicant does its part correctly (KEY_SEQ 0, the router's RSC). The
driver (MediaTek gen2, `wlanoidSetAddKey`) has no replay check of its own
in kallsyms, so the counter is in the chip's firmware.

Deleting both key slots before the install (test patch, mode 4) did not
cure it on Bedroom, though that run is confounded: Bedroom moved to the
2.4 GHz BSS (`…:45:00`) around it.

**The router timer restarts on a requested rekey**: no rekey happened at
15:11:50; the next one is due an hour after 15:00:45.

## 15:25–15:31: two rekeys that did no harm

Rekeys D (15:25:34, previous key 25 min old) and E (15:31:01, previous key
5½ min old) deafened nobody, checked by mDNS and by a broadcast ARP from the
router (`arping -I br0`, whose first probe is broadcast and so is never
turned into unicast by WMF). Living Room and Lounge had been playing the TV
over Sendspin continuously since ~14:10, through the rekeys that deafened
them and the ones that did not, so streaming is not the variable.

Packet numbers, from this PC's mac80211 (`/sys/kernel/debug/ieee80211/
phy0/keys/*/rx_spec`, same BSS, logged every 2 s to scratchpad `pn.log`):
the router starts every new group key at PN 0 (D: idx 1; C ended at
11,018 = 0x2b0a), and stops using the old key within ~5 s of the request.
In every EAPOL install, including an association 25 min into a key, the
router reports RSC 0, so a client is never told the real number.

The driver discards the RSC anyway. MediaTek's `mtk_cfg80211_add_key`
never reads `params->seq`, and `wlanoidSetAddKey` zeroes `CMD_802_11_KEY`
(which has an `aucKeyRsc[16]` field) and fills it only for WAPI. Same in
the MT8163 `conn_soc/drv_wlan/mt_wifi` tree that LibreEcho builds, whose
driver commits are all bring-up diagnostics. Whatever replay counter the
firmware starts a key with, it chooses it itself.

## Model: the firmware starts a new key's counter from the last PN it saw

Every observation fits this, D and E included: at install, the firmware
initialises the new key's replay counter from the last group PN it
received, whichever key that was under.

- The router switches to the new key quickly (D, E): the Echo has already
  seen the new key's first frames (PN 1, 2, …) when it installs, the counter
  is small, nothing is lost.
- The router keeps sending under the old key until after the Echo has
  installed (it waits for every station to answer group message 1, and a
  dozing phone delays that): the counter is the old key's last PN, and the
  Echo drops group traffic until the new key's count passes it, which takes
  about as long as the old key lived. A, C and the hourly rekeys.
- B (21 s after A): the old key carried ~140 frames, so ~20 s of deafness.

If that is right, re-installing the same GTK after the router has switched
cures it without a disconnect, since the last PN seen is then the new
key's. Test build: scratchpad `wpabuild`, `wpa_cli raw GTK_REINSTALL`, and
`gtkfix=5` re-installs at +1, +3 and +10 s after every install. Running on
Bedroom from 15:24; test runs in `cycle.log`.

## Confirmed, and the fix

GTK re-install on Bedroom alone, Living Room and Lounge untouched as
controls (`cycle.log`): rekeys at 15:42 and 15:54 deafened all three;
`GTK_REINSTALL` on Bedroom brought it back within ~15 s each time, while the
controls stayed deaf until the new key's PN passed the old key's last (after
15:42: old key ended at 4,713, controls hearing again at 15:51:42 with the
new key at 4,759). Timed re-installs (+1/+3/+10 s) on the next rekey: Bedroom
hearing at the first probe, controls deaf.

The fix is patch 4 in `emos/tools/build-wpa-supplicant.sh`: every GTK
install is followed by re-installs of the same key at +1, +3, +10, +30 and
+60 s (cancelled on disassociation and deinit). The driver logs each one.
Neither Echo's Sendspin counters (underruns, late drops, wait timeouts)
moved across a rekey and its five re-installs.

With all three running it:

- requested rekeys G, H1, H2 (Living Room and Lounge with the binary in
  /sbin) and J1–J3 (Bedroom on payload `emos-v0.10-gtk.1`, triggered from
  Lounge on the test build): no Echo deaf, against 3–7 min expected without;
- the router's own hourly rekey at ~17:38:33, all three on the payload, the
  previous key an hour old: hearing on every probe through it.

Installed as local emOS payload `emos-v0.10-gtk.1` through the controller on
Bedroom (16:28), Living Room (16:42) and Lounge (16:45); each confirmed.
Stock FireOS uses Android's supplicant and is not covered.
