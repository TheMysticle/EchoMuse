"""
One poll of the account, shared by every entity.

Spotify's device registry is a cloud resource with a rate limit, so the
devices and the current playback are fetched ONCE per interval and every
entity reads the result — rather than each entity polling for itself, which
with several Echoes would multiply the request rate by the number of
speakers for data that is identical.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import SpotifyApi, SpotifyAuthExpired, SpotifyError
from .const import DOMAIN, SCAN_INTERVAL_SECONDS

_LOGGER = logging.getLogger(__name__)


@dataclass
class AccountState:
    """Everything one poll learned."""

    devices: dict[str, dict] = field(default_factory=dict)
    playback: dict | None = None

    def device(self, device_id: str) -> dict | None:
        return self.devices.get(device_id)

    def is_active(self, device_id: str) -> bool:
        """Whether this device is the one the account is playing through."""
        if not self.playback:
            return False
        d = self.playback.get("device")
        return isinstance(d, dict) and d.get("id") == device_id


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
        # Devices are remembered once seen, so an entity is created for an
        # Echo that was online at setup and does not vanish from the registry
        # the moment it drops off. Entities for absent devices report
        # unavailable; removing and recreating them would churn HA's registry
        # and break any automation naming them.
        self._seen: dict[str, dict] = {}

    async def _async_update_data(self) -> AccountState:
        try:
            devices = await self.api.devices()
            playback = await self.api.playback()
        except SpotifyAuthExpired:
            # Let the entry's reauth flow handle it rather than logging an
            # error every interval for something only the user can fix.
            raise
        except SpotifyError as err:
            raise UpdateFailed(str(err)) from err

        present = {d["id"]: d for d in devices}
        for device_id, d in present.items():
            self._seen[device_id] = d
        # Keep a remembered entry but mark it absent, so `available` is false
        # rather than the entity reporting the last state it ever had.
        merged = {
            device_id: present.get(device_id, {**d, "_absent": True})
            for device_id, d in self._seen.items()
        }
        return AccountState(devices=merged, playback=playback)

    @property
    def device_ids(self) -> list[str]:
        return list((self.data.devices if self.data else {}).keys())
