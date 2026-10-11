"""
One media player per Spotify Connect device.

The feature set is the point: `HassMediaSearchAndPlay` matches on
`SEARCH_MEDIA | PLAY_MEDIA` together (`single_target=True`), so an entity
advertising only one of them is invisible to "play <something> on <a
speaker>". Read off HA 2026.2.3's own intent handler rather than inferred.

The round trip HA performs, which is why `media_content_id` is the Spotify
URI and nothing cleverer: it calls `media_player.search_media`, takes
`result[0]`, and calls `media_player.play_media` with that result's
`media_content_id` and `media_content_type`. So whatever this entity puts in
a search result is handed straight back to it to play, and the URI is the one
thing both ends already agree on.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.media_player import (
    BrowseMedia,
    MediaClass,
    MediaPlayerDeviceClass,
    MediaPlayerEntity,
    MediaPlayerEntityFeature,
    MediaPlayerState,
    MediaType,
    SearchMedia,
    SearchMediaQuery,
)
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import EchoMuseSpotifyEntry
from .const import DOMAIN
from .coordinator import SpotifyCoordinator
from . import search as ranking

# SEARCH_MEDIA and PLAY_MEDIA must BOTH be present or the search-and-play
# intent cannot match this entity; see the module docstring.
FEATURES = (
    MediaPlayerEntityFeature.SEARCH_MEDIA
    | MediaPlayerEntityFeature.PLAY_MEDIA
    # BROWSE_MEDIA is what puts the entity's media button in the Home
    # Assistant UI at all, and the browse ROOT is where the frontend finds
    # `can_search` and offers a search box. Without it the entity could be
    # searched only from Developer Tools, which is where this was left
    # after the first pass — `search_media` worked and there was no way to
    # reach it by hand.
    | MediaPlayerEntityFeature.BROWSE_MEDIA
    | MediaPlayerEntityFeature.PLAY
    | MediaPlayerEntityFeature.PAUSE
    | MediaPlayerEntityFeature.NEXT_TRACK
    | MediaPlayerEntityFeature.PREVIOUS_TRACK
    | MediaPlayerEntityFeature.VOLUME_SET
    | MediaPlayerEntityFeature.SEEK
)

# How a Spotify kind is described to HA, and what a result's content type
# says. `MediaType.MUSIC` for a track because that is what HA's media classes
# expect to be playable; the others are contexts.
_CLASS = {
    "track": (MediaClass.TRACK, MediaType.MUSIC),
    "album": (MediaClass.ALBUM, MediaType.ALBUM),
    "artist": (MediaClass.ARTIST, MediaType.ARTIST),
    "playlist": (MediaClass.PLAYLIST, MediaType.PLAYLIST),
}

# A media_class slot on the intent restricts what the user asked for
# ("play the album X"). Mapped back onto the kinds the search understands.
_KIND_OF_CLASS = {
    MediaClass.TRACK: "track",
    MediaClass.MUSIC: "track",
    MediaClass.ALBUM: "album",
    MediaClass.ARTIST: "artist",
    MediaClass.PLAYLIST: "playlist",
}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: EchoMuseSpotifyEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Create an entity per Connect device, and for any that appear later."""
    coordinator = entry.runtime_data
    known: set[str] = set()

    def _sync() -> None:
        new = [d for d in coordinator.device_ids if d not in known]
        if not new:
            return
        known.update(new)
        async_add_entities(SpotifyConnectPlayer(coordinator, d) for d in new)

    _sync()
    entry.async_on_unload(coordinator.async_add_listener(_sync))


class SpotifyConnectPlayer(CoordinatorEntity[SpotifyCoordinator], MediaPlayerEntity):
    """A Spotify Connect device, played by transferring playback to it."""

    _attr_has_entity_name = True
    _attr_name = None
    _attr_device_class = MediaPlayerDeviceClass.SPEAKER
    _attr_supported_features = FEATURES
    _attr_media_content_type = MediaType.MUSIC

    def __init__(self, coordinator: SpotifyCoordinator, device_id: str) -> None:
        super().__init__(coordinator)
        self._device_id = device_id
        self._attr_unique_id = f"{DOMAIN}_{device_id}"

    # ── Identity ────────────────────────────────────────────────────────────

    @property
    def _device(self) -> dict:
        return (self.coordinator.data.device(self._device_id) or {}) if self.coordinator.data else {}

    @property
    def device_info(self) -> DeviceInfo:
        d = self._device
        return DeviceInfo(
            identifiers={(DOMAIN, self._device_id)},
            name=d.get("name") or "Spotify device",
            manufacturer="Spotify Connect",
            model=d.get("type") or None,
        )

    @property
    def available(self) -> bool:
        # A device absent from the registry is off or asleep. Reported
        # unavailable rather than removed: removing it would churn HA's
        # registry and break automations that name it.
        return (
            super().available
            and bool(self._device)
            and not self._device.get("_absent")
        )

    # ── State ───────────────────────────────────────────────────────────────

    @property
    def _active(self) -> bool:
        return bool(self.coordinator.data) and self.coordinator.data.is_active(
            self._device_id
        )

    @property
    def state(self) -> MediaPlayerState:
        if not self.available:
            return MediaPlayerState.OFF
        if not self._active:
            # The device is there but the account is playing elsewhere. IDLE
            # rather than OFF: it can be played to, which OFF would imply it
            # cannot.
            return MediaPlayerState.IDLE
        playback = self.coordinator.data.playback or {}
        return (
            MediaPlayerState.PLAYING
            if playback.get("is_playing")
            else MediaPlayerState.PAUSED
        )

    @property
    def volume_level(self) -> float | None:
        v = self._device.get("volume_percent")
        return v / 100 if isinstance(v, int) else None

    def _item(self) -> dict:
        if not self._active:
            return {}
        item = (self.coordinator.data.playback or {}).get("item")
        return item if isinstance(item, dict) else {}

    @property
    def media_title(self) -> str | None:
        return self._item().get("name")

    @property
    def media_artist(self) -> str | None:
        names = ranking.artist_names(self._item())
        return ", ".join(names) or None

    @property
    def media_album_name(self) -> str | None:
        album = self._item().get("album")
        return album.get("name") if isinstance(album, dict) else None

    @property
    def media_duration(self) -> int | None:
        ms = self._item().get("duration_ms")
        return ms // 1000 if isinstance(ms, int) else None

    @property
    def media_position(self) -> int | None:
        if not self._active:
            return None
        ms = (self.coordinator.data.playback or {}).get("progress_ms")
        return ms // 1000 if isinstance(ms, int) else None

    @property
    def media_image_url(self) -> str | None:
        album = self._item().get("album")
        images = album.get("images") if isinstance(album, dict) else None
        if isinstance(images, list) and images and isinstance(images[0], dict):
            return images[0].get("url")
        return None

    # ── Search ──────────────────────────────────────────────────────────────

    async def async_search_media(self, query: SearchMediaQuery) -> SearchMedia:
        """
        Resolve a spoken name to things that can be played.

        The ranking is `search.py`, a byte-identical copy of the controller's
        `em_spotify_search.py`: deterministic, popularity never consulted,
        variants demoted but never dropped. See its docstring for why each of
        those matters more than it sounds.
        """
        kinds = ranking.KINDS
        if query.media_filter_classes:
            wanted = [
                _KIND_OF_CLASS[c]
                for c in query.media_filter_classes
                if c in _KIND_OF_CLASS
            ]
            # An unrecognised filter must not silently become "everything":
            # asking for a podcast and getting a track is a wrong answer.
            if not wanted:
                return SearchMedia(result=[])
            kinds = tuple(dict.fromkeys(wanted))

        try:
            params = ranking.search_params(query.search_query, kinds=kinds)
        except ValueError as err:
            raise HomeAssistantError(str(err)) from err

        body = await self.coordinator.api.search(params)
        ranked = ranking.rank(
            ranking.candidates(body, query.search_query, kinds=kinds), prefer=kinds
        )
        return SearchMedia(result=[self._browse(c) for c in ranked])

    def _browse(self, cand: ranking.Candidate) -> BrowseMedia:
        media_class, content_type = _CLASS.get(
            cand.kind, (MediaClass.MUSIC, MediaType.MUSIC)
        )
        subtitle = ", ".join(cand.artists)
        return BrowseMedia(
            media_class=media_class,
            media_content_id=cand.uri,
            media_content_type=content_type,
            title=f"{cand.name} — {subtitle}" if subtitle else cand.name,
            can_play=True,
            # Nothing here can be drilled into: this integration resolves a
            # name to something playable and stops. A browse tree is a
            # different feature and claiming it would give an empty one.
            can_expand=False,
        )

    # ── Browse ──────────────────────────────────────────────────────────────

    async def async_browse_media(
        self,
        media_content_type: MediaType | str | None = None,
        media_content_id: str | None = None,
    ) -> BrowseMedia:
        """
        A root that can be SEARCHED, with nothing to drill into.

        This integration resolves a name to something playable; it does not
        hold a catalogue. So the root advertises `can_search` — which is what
        the frontend reads to offer a search box — and has no children,
        rather than claiming a tree and presenting an empty one.

        Listing the account's own playlists and saved albums here would need
        `playlist-read-private` and `user-library-read`, and every existing
        connection would have to be re-authorised to pick them up. Worth
        doing deliberately rather than as a side effect of wanting a search
        box.
        """
        return BrowseMedia(
            media_class=MediaClass.DIRECTORY,
            media_content_id="search",
            media_content_type=MediaType.MUSIC,
            title="Spotify",
            can_play=False,
            can_expand=True,
            can_search=True,
            children=[],
            children_media_class=MediaClass.TRACK,
        )

    # ── Commands ────────────────────────────────────────────────────────────

    async def async_play_media(
        self, media_type: str, media_id: str, **kwargs: Any
    ) -> None:
        """
        Play a Spotify URI on this device.

        `media_id` is whatever a search result carried, which is the URI —
        HA hands `result[0].media_content_id` straight back. A URL from the
        Spotify app is accepted too, since pasting one into `play_media` is
        the obvious thing to try.
        """
        uri = media_id.strip()
        if uri.startswith("http"):
            # open.spotify.com/track/<id>?si=... — the share-link form.
            parts = [p for p in uri.split("?")[0].split("/") if p]
            if len(parts) >= 2:
                uri = f"spotify:{parts[-2]}:{parts[-1]}"
        if not uri.startswith("spotify:"):
            raise HomeAssistantError(
                f"{media_id!r} is not a Spotify URI or link"
            )
        await self.coordinator.api.play_uri(self._device_id, uri)
        await self.coordinator.async_request_refresh()

    async def async_media_play(self) -> None:
        # Transfer when this device is not the active one, because `play`
        # alone resumes wherever the account left off — which may be a
        # different speaker in a different room.
        if self._active:
            await self.coordinator.api.command("play", self._device_id)
        else:
            await self.coordinator.api.transfer(self._device_id, play=True)
        await self.coordinator.async_request_refresh()

    async def async_media_pause(self) -> None:
        await self.coordinator.api.command("pause", self._device_id)
        await self.coordinator.async_request_refresh()

    async def async_media_next_track(self) -> None:
        await self.coordinator.api.command("next", self._device_id)
        await self.coordinator.async_request_refresh()

    async def async_media_previous_track(self) -> None:
        await self.coordinator.api.command("previous", self._device_id)
        await self.coordinator.async_request_refresh()

    async def async_set_volume_level(self, volume: float) -> None:
        await self.coordinator.api.set_volume(self._device_id, round(volume * 100))
        await self.coordinator.async_request_refresh()

    async def async_media_seek(self, position: float) -> None:
        await self.coordinator.api.seek(self._device_id, int(position * 1000))
        await self.coordinator.async_request_refresh()
