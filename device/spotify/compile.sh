#!/bin/bash
# Build the on-device Spotify Connect binaries for the Echo's armv7 target.
#
# Mirrors device/compile.sh: the build happens inside a pinned container so
# the toolchain cannot drift between a developer's machine and a release.
#
#   ./compile.sh            build everything this crate defines
#   ./compile.sh probe      build only the feasibility probe
#
# Output lands in build/, which is gitignored.
set -euo pipefail

cd "$(dirname "$0")"
TARGET=armv7-unknown-linux-musleabihf
IMAGE=echomuse-rust

# Git Bash on Windows hands docker an MSYS path (/c/Users/...) that the
# daemon cannot resolve, and the symptom is a mount that succeeds and is
# empty — "could not find Cargo.toml in /src". `pwd -W` gives the Windows
# form and does not exist elsewhere, so this falls back to plain pwd.
HOSTDIR=$(pwd -W 2>/dev/null || pwd)

if ! docker image inspect "$IMAGE" >/dev/null 2>&1; then
    echo "The $IMAGE image is missing. Build it first:"
    echo "    docker build -t $IMAGE device/spotify/compiler/"
    exit 1
fi

BIN_ARGS=()
case "${1:-all}" in
    all)   ;;
    probe) BIN_ARGS=(--bin em-spotify-probe) ;;
    *)     echo "usage: $0 [all|probe]"; exit 2 ;;
esac

mkdir -p build

# The cargo registry and the target directory are cached in named volumes:
# librespot's dependency tree is large, and a cold build costs minutes that
# nobody should pay twice. They are per-image rather than per-checkout so a
# toolchain change invalidates them.
docker run --rm \
    -v "$HOSTDIR":/src \
    -v echomuse-rust-registry:/root/.cargo/registry \
    -v echomuse-rust-target:/src/target \
    "$IMAGE" \
    cargo build --release --target "$TARGET" "${BIN_ARGS[@]}"

# Copy out of the cached target volume so build/ holds the artefacts even
# though the volume is where cargo wrote them. `-type f -perm -u+x` picks
# the binaries and leaves cargo's .d dependency files behind.
docker run --rm \
    -v "$HOSTDIR":/src \
    -v echomuse-rust-target:/src/target \
    "$IMAGE" \
    sh -c "find /src/target/$TARGET/release -maxdepth 1 -type f -perm -u+x \
             -name 'em-spotify*' -exec cp {} /src/build/ \;"

# A bind mount from Windows does not carry the execute bit, so the copies
# arrive 0644. Restored here because the verification below used to test
# `-x` and `continue` past anything that failed it — so on Windows every
# check silently passed by never running. That is the project's own rule
# from the emOS flow (a check that cannot run must not read as a pass),
# met from the other direction.
chmod +x build/em-spotify* 2>/dev/null || true

echo
found=0
for f in build/em-spotify*; do
    case "$f" in *.d) continue ;; esac
    if [ ! -f "$f" ]; then
        echo "✗ expected a binary at $f and found nothing"
        exit 1
    fi
    found=$((found + 1))
    size=$(du -h "$f" | cut -f1)

    # The same three checks emos/tools/build-busybox.sh makes, for the same
    # reason: each of these fails SILENTLY on hardware. Read with readelf
    # rather than `file`, which describes rather than asserts.
    #
    #  - no INTERP: a dynamically linked binary finds no loader under emOS
    #    and dies saying nothing useful.
    #  - ET_EXEC: Rust's musl targets can emit a static PIE, which this
    #    kernel would have to self-relocate. ET_EXEC is the shape already
    #    proven on these devices by emOS's own busybox.
    #  - ARM: an amd64 host build is otherwise indistinguishable here.
    hdr=$(docker run --rm -v "$HOSTDIR":/src "$IMAGE" \
            sh -c "readelf -hl /src/$f" 2>/dev/null || true)
    if [ -z "$hdr" ]; then
        echo "✗ could not read $f with readelf — refusing to vouch for it"
        exit 1
    fi
    case "$hdr" in
        *INTERP*) echo "✗ $f is dynamically linked — emOS has no loader for it"; exit 1 ;;
    esac
    case "$hdr" in
        *"EXEC (Executable file)"*) ;;
        *"DYN ("*) echo "✗ $f is a PIE — this kernel wants ET_EXEC (see build-busybox.sh)"; exit 1 ;;
        *) echo "✗ $f has an ELF type this script does not recognise"; exit 1 ;;
    esac
    case "$hdr" in
        *"Machine:"*ARM*) ;;
        *) echo "✗ $f is not an ARM binary"; exit 1 ;;
    esac

    echo "✓ $f  ($size, static ET_EXEC ARM)"
done

if [ "$found" -eq 0 ]; then
    echo "✗ the build produced no binaries"
    exit 1
fi
echo
