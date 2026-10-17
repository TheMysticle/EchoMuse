"""
One poll of the account, shared by every entity.

Spotify's device registry is a cloud resource with a rate limit, so the
devices and the current playback are fetched ONCE per interval and every
entity reads the result — rather than each entity polling for itself, which
with several Echoes would multiply the request rate by the number of
speakers for data that is identical.

Entity identity is keyed by a *stable* slug of the device name, not by
Spotify's opaque Connect id. Spotify's id can change across session drops
(especially under Jam / host switching); the HA entity must not. The live
Spotify id is stored on each device dict as `_spotify_id` and used only for
API calls (play, transfer, volume).
"""

from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import SpotifyApi, SpotifyAuthExpired, SpotifyError
from .const import DOMAIN, SCAN_INTERVAL_SECONDS

_LOGGER = logging.getLogger(__name__)

# Spotify id stashed on each device row for command routing.
SPOTIFY_ID_KEY = "_spotify_id"


def normalize_device_name(name: str) -> str:
    """Casefold and collapse whitespace for name matching."""
    return " ".join((name or "").casefold().split())


def entity_slug(name: str) -> str:
    """
    HA-safe unique_id fragment from a Connect device name.

    Stable across Spotify id changes as long as the name is unchanged. Letters
    of any script are kept ("Küche", "キッチン"); a name with none at all ("🔊")
    falls back to a hash of the NAME, never of Spotify's id, which is the very
    thing that changes.
    """
    s = normalize_device_name(name)
    slug = re.sub(r"[\W_]+", "_", s).strip("_")
    if slug:
        return slug
    if not s:
        return ""
    return "device_" + hashlib.sha1(s.encode()).hexdigest()[:8]


@dataclass
class AccountState:
    """Everything one poll learned.

    `devices` is keyed by *stable entity key* (name slug), not Spotify id.
    Each value carries `_spotify_id` for Web API calls.
    """

    devices: dict[str, dict] = field(default_factory=dict)
    playback: dict | None = None

    def device(self, entity_key: str) -> dict | None:
        return self.devices.get(entity_key)

    def is_active(self, entity_key: str) -> bool:
        """Whether this entity's live Spotify id is the account's active device."""
        if not self.playback:
            return False
        d = self.playback.get("device")
        if not isinstance(d, dict):
            return False
        row = self.devices.get(entity_key) or {}
        sid = row.get(SPOTIFY_ID_KEY)
        return bool(sid) and d.get("id") == sid


class SpotifyCoordinator(DataUpdateCoordinator[AccountState]):
    """Polls the account's devices and current playback."""

    def __init__(
        self, hass: HomeAssistant, entry: ConfigEntry, api: SpotifyApi
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=SCAN_INTERVAL_SECONDS),
            config_entry=entry,
        )
        self.api = api
        self._playback_generation = 0
        # When the last poll landed. `media_position_updated_at` needs it: HA
        # extrapolates the progress bar from that instant, and without one
        # the position never moves.
        self.last_poll: datetime | None = None
        # Optimistic volume keyed by *Spotify* id (what the API just accepted).
        self._optimistic_volume: dict[str, int] = {}
        # Stable identity maps. Survive Spotify id churn for the same name.
        self._key_for_id: dict[str, str] = {}  # spotify_id → entity_key
        self._id_for_key: dict[str, str] = {}  # entity_key → current spotify_id
        self._name_for_key: dict[str, str] = {}  # entity_key → last display name
        # Last non-absent row per entity key (for offline reporting).
        self._seen_by_key: dict[str, dict] = {}

    async def _async_update_data(self) -> AccountState:
        try:
            devices = await self.api.devices()
            playback = await self.api.playback()
        except SpotifyAuthExpired as err:
            # MUST be ConfigEntryAuthFailed: DataUpdateCoordinator only starts
            # the reauth flow for that exact type. Our own SpotifyAuthExpired
            # was just logged as "unexpected error" every poll and, during
            # first refresh, turned into endless setup retries.
            raise ConfigEntryAuthFailed(str(err)) from err
        except SpotifyError as err:
            raise UpdateFailed(str(err)) from err

        self.last_poll = dt_util.utcnow()
        present = {d["id"]: d for d in devices if isinstance(d, dict) and d.get("id")}

        for device_id, pct in list(self._optimistic_volume.items()):
            if (d := present.get(device_id)) is None:
                continue
            if d.get("volume_percent") == pct:
                self._optimistic_volume.pop(device_id, None)
            else:
                d["volume_percent"] = pct

        merged = self._bind_devices(present)
        return AccountState(devices=merged, playback=playback)

    def _bind_devices(self, present: dict[str, dict]) -> dict[str, dict]:
        """
        Map Spotify's present devices onto stable entity keys.

        Rebinding: if a *new* Spotify id appears with a name that matches a
        key whose previous id is no longer online, adopt that key. That is
        what stops a session drop from creating media_player.living_room_2.
        """
        present_ids = set(present)

        # ── Rebind new ids onto absent keys with the same name ───────────
        for sid, d in present.items():
            if sid in self._key_for_id:
                continue
            norm = normalize_device_name(d.get("name") or "")
            if not norm:
                continue
            for key, old_sid in list(self._id_for_key.items()):
                if old_sid in present_ids:
                    continue
                if normalize_device_name(self._name_for_key.get(key, "")) != norm:
                    continue
                _LOGGER.info(
                    "Spotify device id changed for %r: %s → %s (keeping entity key %s)",
                    self._name_for_key.get(key, key),
                    old_sid,
                    sid,
                    key,
                )
                self._key_for_id.pop(old_sid, None)
                self._key_for_id[sid] = key
                self._id_for_key[key] = sid
                self._name_for_key[key] = (d.get("name") or "").strip()
                break

        # ── Assign keys for anything still unbound ───────────────────────
        for sid, d in present.items():
            name = (d.get("name") or "").strip()
            if sid in self._key_for_id:
                key = self._key_for_id[sid]
                self._id_for_key[key] = sid
                if name:
                    self._name_for_key[key] = name
                continue
            # A device with no name at all has nothing stable to derive from;
            # its id is the last resort and it will not survive an id change.
            base = entity_slug(name) or f"device_{sid[:8]}"
            key = base
            n = 2
            while key in self._id_for_key and self._id_for_key[key] != sid:
                key = f"{base}_{n}"
                n += 1
            self._key_for_id[sid] = key
            self._id_for_key[key] = sid
            self._name_for_key[key] = name or key
            _LOGGER.debug("Bound entity key %s for Spotify id %s (%r)", key, sid, name)

        # ── Build rows keyed by entity key ───────────────────────────────
        result: dict[str, dict] = {}
        for key, sid in self._id_for_key.items():
            if sid in present:
                row = {**present[sid], SPOTIFY_ID_KEY: sid}
                result[key] = row
                self._seen_by_key[key] = dict(row)
            else:
                prev = self._seen_by_key.get(key, {})
                result[key] = {
                    **prev,
                    SPOTIFY_ID_KEY: sid,
                    "name": self._name_for_key.get(key) or prev.get("name") or key,
                    "_absent": True,
                }
        return result

    def begin_playback_command(self) -> int:
        """Invalidate a pending cold resume when newer transport intent arrives."""
        self._playback_generation += 1
        return self._playback_generation

    def is_current_playback_command(self, generation: int) -> bool:
        return generation == self._playback_generation

    def note_playback(self, playback: dict) -> None:
        """Publish a live confirmed playback read without waiting on poll debounce."""
        if self.data is not None:
            self.last_poll = dt_util.utcnow()
            self.async_set_updated_data(AccountState(devices=self.data.devices, playback=playback))

    def note_volume(self, spotify_id: str, percent: int) -> None:
        """Remember a volume we just set, until Spotify reports it back."""
        self._optimistic_volume[spotify_id] = percent
        if not self.data:
            return
        for row in self.data.devices.values():
            if row.get(SPOTIFY_ID_KEY) == spotify_id:
                row["volume_percent"] = percent
                break

    @property
    def device_ids(self) -> list[str]:
        """Stable entity keys (not Spotify ids)."""
        return list((self.data.devices if self.data else {}).keys())
