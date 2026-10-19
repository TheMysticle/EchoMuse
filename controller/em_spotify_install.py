"""
Installing the on-device Spotify Connect engine (device/spotify/).

The binaries built by device/spotify/compile.sh reach the controller by
upload and each device over the shell plane, md5-verified, the same path the
wake word runtime takes, and for the same reason: ~4MB folded into the
firmware would double every OTA's payload across both A/B slots whether or
not Spotify changed.

Shared by the routes (em_routes_spotify) and the connect-time reconcile
(em_reconcile), which is why it is not a route module: logic both need
belongs below them.
"""

from __future__ import annotations

import asyncio
import logging
import posixpath
import shlex

import em_config_types
import em_db as db
import em_dbwriter
import em_ota
import em_shell
import em_spotify_assets
import em_state

log = logging.getLogger("echomuse.api")


def stored_artefacts() -> dict[str, em_spotify_assets.Artefact]:
    """Artefacts in the data volume, keyed by name."""
    directory = em_spotify_assets.assets_dir()
    found: dict[str, em_spotify_assets.Artefact] = {}
    for name in em_spotify_assets.ARTEFACTS:
        path = directory / name
        if not path.is_file():
            continue
        data = path.read_bytes()
        found[name] = em_spotify_assets.Artefact(
            name=name, size=len(data), md5=em_spotify_assets.md5_hex(data)
        )
    return found


async def device_md5s(live) -> tuple[dict[str, str], bool]:
    """
    md5 of each artefact on the device, and whether the shell answered.

    The second value is load-bearing: `_shell_run` swallows every exception
    and returns "", so an absent file and a device that never answered are
    the same string — and read as "out of date" either way. Seconds after a
    connect the shell plane is very likely not up yet, so a push on that
    evidence is a pointless transfer and a user-visible claim that is
    untrue. Same `em_shell.OK_MARK` sentinel as `_sync_start_script`, for the same
    reason.
    """
    names = " ".join(
        shlex.quote(em_spotify_assets.device_path(n))
        for n in em_spotify_assets.ARTEFACTS
    )
    out = await em_shell.run(
        live, f"busybox md5sum {names} 2>/dev/null; echo {em_shell.OK_MARK}"
    )
    if em_shell.OK_MARK not in out:
        return {}, False

    found: dict[str, str] = {}
    for line in out.splitlines():
        parts = line.split()
        # busybox prints "<hash>  <path>". No `cut`: the branch where
        # busybox is absent is exactly the branch where `busybox cut` is
        # absent too.
        if len(parts) >= 2 and len(parts[0]) == 32:
            base = posixpath.basename(parts[1])
            if base in em_spotify_assets.ARTEFACTS:
                found[base] = parts[0].lower()
    return found, True


async def install_engine(live, device_id: str, plan: list[str],
                                  stored: dict) -> None:
    """
    Push the planned artefacts, serialised behind `em_ota.queue`.

    Takes the same lock as a firmware OTA and a wake word asset install, for
    the reason documented there: these all move megabytes over one device's
    shell plane on the event loop that also sends speaker periods and LED
    frames, so three at once stalled that loop for 11.1 seconds. A device
    answering somebody must not pay for a device being set up.
    """
    em_ota.queued.add(device_id)
    try:
        await em_ota.queue.acquire()
    finally:
        em_ota.queued.discard(device_id)

    try:
        await asyncio.wait_for(
            _install_locked(live, device_id, plan, stored),
            timeout=em_ota.MAX_HOLD_S,
        )
    except asyncio.TimeoutError:
        log.error(f"[{device_id}] Spotify engine install abandoned after "
                  f"{em_ota.MAX_HOLD_S:.0f}s so the queue could continue")
        em_dbwriter.submit(db.log_device, device_id, "error", "controller",
                           "Spotify engine install timed out")
    finally:
        em_ota.queue.release()


async def _install_locked(live, device_id: str, plan: list[str],
                                         stored: dict) -> None:
    installed: list[str] = []
    # The destination directory first, and this is not belt-and-braces.
    # `_stream_file_to_device` probes that the directory EXISTS before it
    # sends, because the heredoc writes with `>` and a write into a missing
    # directory fails with `TRANSFER_OK` never arriving — which used to cost
    # the transfer its whole timeout while holding the device's shell lock.
    # The probe makes that fail fast instead of hanging, but nothing creates
    # the directory, so without this every first install on a device fails:
    # /data/local/share/echomuse exists for the wake word assets, and
    # `spotify` under it does not. `reconcile_oww_assets` does the same thing
    # for the same reason.
    await em_shell.run(live, f"mkdir -p {shlex.quote(em_spotify_assets.DEVICE_DIR)}")

    directory = em_spotify_assets.assets_dir()
    for name in plan:
        dest = em_spotify_assets.device_path(name)
        data = await asyncio.to_thread((directory / name).read_bytes)
        # require_verify=True: this is a binary we are about to execute, so
        # an unverifiable transfer is refused rather than accepted with a
        # warning. Firmware passes True for the same reason — a corrupt
        # binary and a broken one produce the same observable, and the
        # mismatch has to be caught before anything runs it.
        res = await em_shell.stream_file(live, data, dest, mode="755",
                                           require_verify=True)
        if not res:
            log.error(f"[{device_id}] Spotify {name} transfer failed at "
                      f"{getattr(res, 'stage', 'unknown')}: {res}")
            em_dbwriter.submit(db.log_device, device_id, "error", "controller",
                               f"Spotify {name} install failed: {res}")
            # Carry on with the rest. The probe failing must not stop the
            # engine, and vice versa — unrelated payloads, the rule the
            # connect-time reconcile already follows.
            continue
        log.info(f"[{device_id}] Spotify {name} installed "
                 f"({len(data):,} bytes)")
        em_dbwriter.submit(db.log_device, device_id, "info", "controller",
                           f"Spotify {name} installed")
        installed.append(name)

    # The firmware refuses the start when the engine is absent and leaves
    # itself NOT waiting, so nothing retries it: `applySpotifyConfig` only
    # ever starts the engine from a config push. Without this the install
    # succeeds and the device goes on reporting the engine as missing for
    # good — the control that appears to work. Same mechanism and the same
    # reason as reconcile_oww_assets re-pushing a repaired wake word. Only
    # the ENGINE earns it: the probe is a diagnostic the firmware never
    # loads.
    if em_spotify_assets.ENGINE not in installed:
        return
    live = em_state.devices.get(device_id)
    if live is None:
        return
    loop = asyncio.get_event_loop()
    effective = await loop.run_in_executor(
        None, db.get_effective_device_config, device_id
    )
    if not effective.get("spotifyEnabled"):
        return
    await live.send_control(
        {"type": "config", **em_config_types.well_typed(device_id, effective)})
    log.info(f"[{device_id}] Spotify engine installed — config re-pushed so "
             f"the device starts it")


async def sync_engine(live, device_id: str) -> None:
    """
    On connect: install the stored Spotify engine when this device wants it.

    emOS only (the caller gates it), and only when `spotifyEnabled` is on and
    the controller holds an engine. Failure to LOOK is not evidence of
    absence: a shell plane that is not up yet changes nothing.
    """
    loop = asyncio.get_event_loop()
    effective = await loop.run_in_executor(
        None, db.get_effective_device_config, device_id
    )
    if not effective.get("spotifyEnabled"):
        return
    # In a thread, and read ONCE: this reads a ~4MB binary and md5s it, and
    # unlike the request handlers that share the helper, this path runs on
    # every emOS connect — on the loop that sends speaker periods and LED
    # frames. Same reason no coroutine here calls db.* directly.
    artefacts = await asyncio.to_thread(stored_artefacts)
    stored = {n: a.md5 for n, a in artefacts.items()}
    # The probe is a diagnostic; only the engine is installed unattended.
    wanted = (em_spotify_assets.ENGINE,)
    if not any(n in stored for n in wanted):
        return
    on_device, answered = await device_md5s(live)
    if not answered:
        return
    plan = em_spotify_assets.plan_install(stored, on_device, wanted)
    if not plan:
        return
    log.info(f"[api] [{device_id}] Spotify engine out of date — installing")
    await install_engine(live, device_id, plan, artefacts)
