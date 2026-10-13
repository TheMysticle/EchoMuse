#!/bin/sh
# Run probe.c against a firmware dump on a PC, under qemu-user, with no root.
#
#   run.sh <system-dir> <qemu-arm-static> <file.raw> [more.raw ...]
#
# <system-dir> is a device's /system as a directory (it must contain bin/linker
# and lib/libpryon.so). <qemu-arm-static> is the static ARM user-mode emulator
# (Debian's qemu-user-static package carries it). Needs bwrap and the Android NDK
# (ANDROID_NDK, default /opt/android-ndk). Each .raw is 16kHz mono int16.
#
# What it does: builds probe.c for armv7a API 22, then runs it inside a bubblewrap
# namespace whose /system is the dump, so the library's own loader and bionic run
# unmodified. Two things the namespace fakes: the CPU identification the neural
# runtime reads (it refuses to start on the emulated x86 host's), and nothing else.
set -eu
SYS=$(readlink -f "$1"); QEMU=$(readlink -f "$2"); shift 2
HERE=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
NDK=${ANDROID_NDK:-/opt/android-ndk}
CC=$(ls "$NDK"/toolchains/llvm/prebuilt/*/bin/armv7a-linux-androideabi22-clang | head -1)
W=$(mktemp -d); trap 'rm -rf "$W"' EXIT
"$CC" -O1 -fPIE -pie -Wl,--hash-style=both "$HERE/probe.c" -o "$W/probe" -ldl
mkdir -p "$W/cpu"
i=0; : > "$W/cpuinfo"
while [ $i -lt 4 ]; do
  printf 'processor\t: %s\nCPU implementer\t: 0x41\nCPU architecture: 7\nCPU variant\t: 0x0\nCPU part\t: 0xd03\nCPU revision\t: 4\n\n' $i >> "$W/cpuinfo"
  mkdir -p "$W/cpu/cpu$i/cpufreq" "$W/cpu/cpu$i/topology"
  echo 1300000 > "$W/cpu/cpu$i/cpufreq/cpuinfo_max_freq"; echo 598000 > "$W/cpu/cpu$i/cpufreq/cpuinfo_min_freq"
  echo 0-3 > "$W/cpu/cpu$i/topology/core_siblings_list"; echo 0 > "$W/cpu/cpu$i/topology/physical_package_id"
  i=$((i+1))
done
echo 0-3 > "$W/cpu/possible"; echo 0-3 > "$W/cpu/present"; echo 0-3 > "$W/cpu/online"; echo 3 > "$W/cpu/kernel_max"
for f in "$@"; do
  d=$(CDPATH= cd -- "$(dirname -- "$f")" && pwd); b=$(basename "$f")
  bwrap --ro-bind "$QEMU" /qemu --ro-bind "$SYS" /system --proc /proc --dev /dev --tmpfs /tmp \
    --ro-bind "$W/cpuinfo" /proc/cpuinfo --ro-bind "$W/cpu" /sys/devices/system/cpu \
    --ro-bind "$W/probe" /probe --ro-bind "$d" /in --setenv LD_LIBRARY_PATH /system/lib:/system/vendor/lib \
    --unshare-all /qemu /probe "/in/$b" 2>&1 | grep -E '^(DET|decoder|modelset)'
done
