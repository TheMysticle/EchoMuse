# EchoMuse Dealer recovery patch

This directory is `librespot-core` **0.8.0** from crates.io, with its original
MIT license. Other librespot crates and all application dependency versions
remain pinned by `device/spotify/Cargo.lock`.

The upstream source is https://github.com/librespot-org/librespot/tree/v0.8.0/core.
The package checksum is
`168bbe1c416980ddd9a969ebd6b50fb6c924eb1a3ded194285fa8ec0e2b1c68b`.

Only `src/dealer/mod.rs` changes upstream runtime behavior:

- Startup and reconnect use the same background retry loop. A resolver/token
  error during reconnection used to return from the Dealer task without closing its subscribers;
  Spirc could then keep the process alive indefinitely without a connection.
- A 30-second attempt deadline covers URL/token retrieval, TCP and TLS/WebSocket
  establishment. A peer that accepts TCP but never finishes the handshake
  previously blocked reconnection indefinitely.
- Direct connections use Tokio's asynchronous hostname resolution so the
  deadline is not blocked by synchronous DNS resolution on the runtime thread.
- Shutdown cancels a pending attempt or the existing ten-second retry delay.

The normal WebSocket heartbeat, subscription dispatch, registration, playback,
volume, account credentials, and reconnection interval retain upstream behavior.
Healthy idle sessions are not periodically restarted. Test builds shorten the
networking intervals to exercise the same paths against local sockets.

`src/dealer/recovery_tests.rs` injects missing pong replies, dropped connections,
resolver/token errors, stalled lookups and stalled WebSocket handshakes, during
startup and reconnection. It verifies message delivery after recovery through
the original subscription, healthy idle sessions, and cancellation on shutdown.

Run these offline without Spotify credentials (local sockets must be allowed):

```sh
CARGO_TARGET_DIR=/tmp/echomuse-dealer-target cargo test \
  --manifest-path device/spotify/vendor/librespot-core/Cargo.toml \
  --no-default-features --features rustls-tls-webpki-roots --lib
cargo test --manifest-path device/spotify/Cargo.toml --locked --lib --bins
```

Use separate target directories when comparing this crate with a standalone
copy of the same upstream package, to avoid Cargo reusing root-package build
artifacts between those checkouts.
