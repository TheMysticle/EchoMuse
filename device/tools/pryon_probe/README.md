# pryon_probe

Drives the firmware's own wake word decoder (Radar's `libpryon.so` and the shipped
ALEXA model package) on a PC, to score recordings against it. Nothing of Amazon's is
in this directory; the library and model are read from a copy of a device's `/system`
(the Echo has them already; `/mnt/Development/alexa-firm/out/system/system` is one).

```bash
# one-time: a static ARM emulator, no root (Debian's qemu-user-static package)
curl -LO https://deb.debian.org/debian/pool/main/q/qemu/qemu-user-static_7.2+dfsg-7+deb12u18+b3_amd64.deb
ar x qemu-user-static_*.deb && tar -xf data.tar.xz ./usr/bin/qemu-arm-static

# score 16kHz mono int16 files: one DET line per detection
./run.sh /path/to/system  ./usr/bin/qemu-arm-static  clip.raw
```

Needs `bwrap` (bubblewrap) and the Android NDK. It runs Android's own bionic and
linker under `qemu-user`, in a namespace whose `/system` is the dump, and fakes only
the CPU identification files the neural runtime reads.

Output is `DET begin=<sample> end=<sample> classifier_score=<s>,active_threshold=0.923,detection_type=Accept`.
Speed is about real time on a desktop, which is not the Echo's: use this for what the
detector does, not for how much CPU it costs.

`probe.c`'s header has the calling convention. The one trap: the decoder drops audio
when its backlog grows, so the probe waits on `PryonDecoder_BacklogMsec`.

`bf_design -sweep ... -dump.dir DIR` writes the wake path's output as raw
16kHz int16, which is what this expects.
