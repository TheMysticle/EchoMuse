"""Confirm cold Connect activation before resuming an inherited paused queue."""
import asyncio
from collections.abc import Callable

from .api import SpotifyApi, SpotifyError

# Only a transfer needs this bounded handshake. Normal active-device resume
# stays one PUT. Back off reads so a cold speaker does not flood the account.
POLL_DELAYS = (0, 0.25, 0.5, 1, 1.5, 2)
RESUME_TIMEOUT = 7


async def resume_inactive(api: SpotifyApi, device_id: str, current: Callable[[], bool]) -> dict | None:
    seen_target = False
    seen_content = False
    paused_reads = 0
    resumed = False
    try:
        async with asyncio.timeout(RESUME_TIMEOUT):
            await api.transfer(device_id, play=True)
            for delay in POLL_DELAYS:
                if not current():
                    return
                if delay:
                    await asyncio.sleep(delay)
                if not current():
                    return
                state = await api.playback()
                if not current():
                    return
                device = (state or {}).get("device") or {}
                if device.get("id") != device_id:
                    if seen_target and device.get("id"):
                        # A phone or another speaker took over after activation.
                        # Never transfer back or resume the new active device.
                        raise SpotifyError("Playback moved to another Spotify device while this speaker was starting")
                    paused_reads = 0
                    continue
                seen_target = True
                if state.get("is_playing") is True:
                    return state
                has_content = any(isinstance(state.get(key), dict) and state[key].get("uri")
                    for key in ("item", "context"))
                seen_content = seen_content or has_content
                paused_reads = paused_reads + 1 if has_content and state.get("is_playing") is False else 0
                if paused_reads >= 2 and not resumed:
                    # Transfer acknowledgement is not playback confirmation.
                    # Let normal startup settle first, then send ONE targeted,
                    # payload-free resume, preserving context and position.
                    await api.command("play", device_id)
                    resumed = True
    except TimeoutError:
        pass
    if not current():
        return
    if seen_target and not seen_content:
        raise SpotifyError("Spotify has no music to resume on this speaker; choose a song or playlist")
    raise SpotifyError("Spotify did not confirm playback on this speaker; try again shortly")
