# The Rust cross-compiler for the on-device Spotify Connect engine.
#
# Pinned by DIGEST, not by tag, for the reason `device/compiler/Dockerfile`
# is: this image produces a binary that runs on hardware we cannot upgrade,
# and a floating tag lets a third party change the compiler under us with no
# PR and nothing in CI to notice. The first symptom would be a binary the
# Echo refuses to execute.
#
# WHY MUSL AND NOT THE NDK. Every unit is on emOS, where bionic has no
# working resolver — it goes through Android's property service, which emOS
# deliberately does not provide (emos/init/init.c, and the NTP entry in
# emos/README.md's known gaps). emOS's init DOES write /etc/resolv.conf
# pointing at the default gateway (`write_resolv_conf`), and musl reads that
# file, so a static musl binary resolves where an NDK one cannot. This is
# inverted on FireOS, which has no /etc/resolv.conf at all and whose /etc is
# a symlink into read-only /system/etc — hence the feature is gated on
# `base_os: emos` and a FireOS device keeps controller-fed music.
#
# WHY A C CROSS-COMPILER IS STILL NEEDED even though nothing here is our own
# C: rustls resolves to `ring`, which compiles C and hand-written assembly.
# A pure `rust-lld` link cannot build it, so the toolchain has to be real.
#
# To move the pin: pull the new digest deliberately, build, and run the
# result on a real Radar before the pin changes. The host tests prove nothing
# about this image — they run on amd64 against the host toolchain, and this
# image is only ever exercised by compile.sh and the release workflow.
# Resolved and frozen 2026-10-10. The tag is left in a comment so the next
# person knows which tag this digest WAS, which `docker buildx imagetools
# inspect` cannot tell you after the fact.
# messense/rust-musl-cross:armv7-musleabihf
FROM messense/rust-musl-cross@sha256:714d7529ed9098699cc13abade83c565e6a946e53975c108089a8cd3b43cb871

# Pinned toolchain, for the same reason as the image. Kept as its own layer
# so a toolchain bump does not re-download the musl sysroot.
#
# 1.84 (the base image's own toolchain) cannot resolve this dependency tree:
# librespot-core 0.8.0 declares 1.85, but transitive crates have moved past
# it — uuid 1.28 wants 1.89 and cpufeatures 0.3.1 needs edition 2024, which
# fails as "feature `edition2024` is required" rather than as a version
# complaint. Pinned to a specific release rather than `stable`, so a release
# built next month uses the compiler this one was tested with.
ARG RUST_VERSION=1.95.0
RUN rustup toolchain install ${RUST_VERSION} --profile minimal \
        --target armv7-unknown-linux-musleabihf \
    && rustup default ${RUST_VERSION} \
    && rustup component add rustfmt clippy \
    && rustup target add armv7-unknown-linux-musleabihf

# protoc, for librespot-protocol. The crate generates its Rust from Spotify's
# .proto files at build time, so this is a build dependency rather than a
# runtime one — nothing protobuf-related is dynamically linked.
RUN apt-get update \
    && apt-get install -y --no-install-recommends protobuf-compiler \
    && rm -rf /var/lib/apt/lists/*

# Where the musl target's own C compiler lives, so `ring`'s build script and
# cc-rs find the cross toolchain rather than the host's gcc.
ENV CC_armv7_unknown_linux_musleabihf=armv7-unknown-linux-musleabihf-gcc
ENV AR_armv7_unknown_linux_musleabihf=armv7-unknown-linux-musleabihf-ar
ENV CARGO_TARGET_ARMV7_UNKNOWN_LINUX_MUSLEABIHF_LINKER=armv7-unknown-linux-musleabihf-gcc

WORKDIR /src
