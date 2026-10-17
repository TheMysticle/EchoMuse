# EchoMuse playback preservation

This is the crates.io `librespot-connect` **0.8.0** source, package checksum
`4b2a21e9e19b906c6676b1a515fc5529d5414f69049f5a167dfb2ea3206e5a27`,
with its original MIT license. The application still pins the other librespot
crates through `device/spotify/Cargo.lock`.

Snapshot copying, pending context pages, and the exact-queue tests are adapted
from crmne's proposed [upstream PR #1765](https://github.com/librespot-org/librespot/pull/1765).
The lifecycle also takes inspiration from
[#1692](https://github.com/librespot-org/librespot/pull/1692) and
[#1789](https://github.com/librespot-org/librespot/pull/1789), but does not import
those branches wholesale or change their merge/release status.

Changes to the released source:

- `ConnectState`, its contexts and shuffle state can be cloned. Restoration
  copies the full playback state, manual queue, previous/next tracks, repeat,
  autoplay context, exact shuffle order and unresolved pages. It preserves the
  replacement session's identity, volume and device fields.
- `Spirc::disconnected_playback()` returns an account-scoped RAM-only snapshot
  after an unexpected exit. `Spirc::with_playback()` accepts that snapshot for
  a **new player**, restoring and reloading only after registration reports
  that no other device has taken over. This differs from #1789's same-player
  continuation API despite sharing its method name.
- Paused playback stays paused. A snapshot older than two minutes restores
  paused, retaining its queue and position. The age is measured from detected
  session loss, not the first dropped network packet; retries before successful
  registration retain the original snapshot and age.
- Pause and stop can suppress automatic resumption through shared snapshot
  handles during login/backoff. Stop restores paused at position zero. New
  replacement playback, logout and relinking cancel the old snapshot. Commands
  queued just as a session dies are checked before the snapshot is captured.
- An independent one-second AP validity guard drives the whole event loop,
  including awaits inside handlers. Invalidating the session wakes an idle or
  paused device and cancels stalled cloud work. Dead-session cleanup is skipped;
  other cleanup and Dealer closure have five-second bounds.
- Current audio position is recorded from player events, including 0.8.0's
  periodic `PositionChanged` event (omitted by its `get_play_request_id`). Stale
  track events cannot replace it. Recovery clamps the position to the known
  duration rather than extrapolating through a network stall.
- A dead-session preload failure is not processed as an unavailable song before
  recovery can retain the queue.

No audio or snapshots are written to flash. A complete `em-spotify` process
crash or device reboot loses this in-memory snapshot. During full AP session
replacement the old player stops and the new player reloads at the saved
position, so uninterrupted audio is not promised. Ordinary Dealer-only
reconnections keep the existing player under the separate core patch.

The host tests in `src/recovery_tests.rs` use real Connect/player state with a
silent test sink, without Spotify credentials or audio hardware. State tests
advance hundreds of tracks through restored shuffled/manual/autoplay queues.

```sh
CARGO_TARGET_DIR=/tmp/echomuse-connect-preservation-tests cargo test --offline \
  --manifest-path device/spotify/vendor/librespot-connect/Cargo.toml \
  --no-default-features --features rustls-tls-webpki-roots --lib
cargo test --manifest-path device/spotify/Cargo.toml --locked --lib --bins
```
