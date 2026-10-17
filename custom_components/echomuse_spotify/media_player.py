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

The browse tree borrows Music Assistant's shape — a dispatch on library ids
with one builder each — and deliberately not its depth. MA indexes a whole
library across providers; this is one account's Spotify, so **every level is
exactly one API call** and nothing is fetched merely to decide what to show.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from homeassistant.components.media_player import (
    BrowseError,
    BrowseMedia,
    MediaClass,
    MediaPlayerDeviceClass,
    MediaPlayerEntity,
    MediaPlayerEntityFeature,
    MediaPlayerState,
    MediaType,
    RepeatMode,
    SearchMedia,
    SearchMediaQuery,
)
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import EchoMuseSpotifyEntry
from . import search as ranking
from .const import DOMAIN
from .coordinator import SPOTIFY_ID_KEY, SpotifyCoordinator
from .playback import is_resume_request
from .intents import async_register_local_search
from .resume import resume_inactive

# SEARCH_MEDIA and PLAY_MEDIA must BOTH be present or the search-and-play
# intent cannot match this entity; see the module docstring. BROWSE_MEDIA is
# what gives the entity a media button in the UI at all.
FEATURES = (
    MediaPlayerEntityFeature.SEARCH_MEDIA
    | MediaPlayerEntityFeature.PLAY_MEDIA
    | MediaPlayerEntityFeature.BROWSE_MEDIA
    | MediaPlayerEntityFeature.PLAY
    | MediaPlayerEntityFeature.PAUSE
    | MediaPlayerEntityFeature.STOP
    | MediaPlayerEntityFeature.NEXT_TRACK
    | MediaPlayerEntityFeature.PREVIOUS_TRACK
    | MediaPlayerEntityFeature.VOLUME_SET
    | MediaPlayerEntityFeature.SEEK
    | MediaPlayerEntityFeature.SHUFFLE_SET
    | MediaPlayerEntityFeature.REPEAT_SET
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

# Library ids, named as Music Assistant names them so the two browse trees
# read the same way to anyone who has used both.
LIBRARY_PLAYLISTS = "playlists"
LIBRARY_ALBUMS = "albums"
LIBRARY_TRACKS = "tracks"

_LIBRARY: dict[str, tuple[str, MediaClass, str]] = {
    LIBRARY_PLAYLISTS: ("Playlists", MediaClass.PLAYLIST, MediaType.PLAYLIST),
    LIBRARY_ALBUMS: ("Albums", MediaClass.ALBUM, MediaType.ALBUM),
    LIBRARY_TRACKS: ("Liked Songs", MediaClass.TRACK, MediaType.MUSIC),
}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: EchoMuseSpotifyEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Create an entity per Connect device name; add more as names appear.

    Keys are stable name slugs from the coordinator, not Spotify ids.
    """
    coordinator = entry.runtime_data
    async_register_local_search(hass, entry)
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
    """A Spotify Connect device, played by transferring playback to it.

    Entity identity is a stable slug of the device name. Spotify's Connect
    id is resolved live (`_spotify_id`) so a session drop does not create
    a second Home Assistant entity.
    """

    _attr_has_entity_name = True
    _attr_name = None
    _attr_device_class = MediaPlayerDeviceClass.SPEAKER
    _attr_supported_features = FEATURES

    def __init__(self, coordinator: SpotifyCoordinator, entity_key: str) -> None:
        super().__init__(coordinator)
        # Stable name slug from the coordinator — not Spotify's opaque id.
        self._entity_key = entity_key
        self._attr_unique_id = f"{DOMAIN}_{entity_key}"

    # ── Identity ────────────────────────────────────────────────────────────

    @property
    def _device(self) -> dict:
        if not self.coordinator.data:
            return {}
        return self.coordinator.data.device(self._entity_key) or {}

    @property
    def _spotify_id(self) -> str:
        """Live Spotify Web API device id for play/transfer/volume."""
        return str(self._device.get(SPOTIFY_ID_KEY) or "")

    @property
    def device_info(self) -> DeviceInfo:
        d = self._device
        return DeviceInfo(
            identifiers={(DOMAIN, self._entity_key)},
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
            and bool(self._spotify_id)
        )

    # ── State ───────────────────────────────────────────────────────────────

    @property
    def _active(self) -> bool:
        return bool(self.coordinator.data) and self.coordinator.data.is_active(
            self._entity_key
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

    @property
    def shuffle(self) -> bool | None:
        if not self._active:
            return None
        value = (self.coordinator.data.playback or {}).get("shuffle_state")
        return value if isinstance(value, bool) else None

    @property
    def repeat(self) -> RepeatMode | None:
        if not self._active:
            return None
        return {"off": RepeatMode.OFF, "track": RepeatMode.ONE, "context": RepeatMode.ALL}.get(
            (self.coordinator.data.playback or {}).get("repeat_state"))

    def _item(self) -> dict:
        if not self._active:
            return {}
        item = (self.coordinator.data.playback or {}).get("item")
        return item if isinstance(item, dict) else {}

    @property
    def media_content_type(self) -> str | None:
        return MediaType.MUSIC if self._item() else None

    @property
    def media_content_id(self) -> str | None:
        return self._item().get("uri")

    @property
    def media_title(self) -> str | None:
        return self._item().get("name")

    @property
    def media_artist(self) -> str | None:
        return ", ".join(ranking.artist_names(self._item())) or None

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
    def media_position_updated_at(self) -> datetime | None:
        """
        When `media_position` was read.

        Without this the progress bar does not move: HA extrapolates the
        position from this timestamp between updates, so with it absent the
        one position it was handed is all it ever shows. That is what "the
        time does not sync" was — it genuinely did not. Music Assistant sets
        the same pair, and for a POLLED player it is not optional.
        """
        if not self._active or self.media_position is None:
            return None
        return self.coordinator.last_poll

    @property
    def media_image_url(self) -> str | None:
        return _image_of(self._item())

    # ── Search ──────────────────────────────────────────────────────────────

    async def async_search_media(self, query: SearchMediaQuery) -> SearchMedia:
        """
        Resolve a spoken name to things that can be played.

        The ranking is `search.py`: deterministic, popularity never
        consulted, variants demoted but never dropped. See its docstring for
        why each of those matters more than it sounds.
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
        return SearchMedia(result=[self._result(c) for c in ranked])

    def _result(self, cand: ranking.Candidate) -> BrowseMedia:
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
            can_expand=cand.kind in ("playlist", "album"),
        )

    # ── Browse ──────────────────────────────────────────────────────────────

    async def async_browse_media(
        self,
        media_content_type: MediaType | str | None = None,
        media_content_id: str | None = None,
    ) -> BrowseMedia:
        """
        The account's own library, with a search box at every level.

        The first version returned a root with `can_search` and NO children,
        on the grounds that this integration resolves a name rather than
        holding a catalogue. On a real install that renders as **"No items"**,
        which is worse than no browse button at all: it looks broken rather
        than deliberately empty.

        One API call per level, and none merely to decide what to show — the
        root is built from a constant. That is the difference from Music
        Assistant, whose browser indexes a library across providers; this is
        one account's Spotify and it should feel like three taps.
        """
        node = media_content_id or "library"

        if node == "library":
            return _folder(
                "library",
                "Spotify",
                children=[
                    _folder(key, title, klass=klass)
                    for key, (title, klass, _ct) in _LIBRARY.items()
                ],
                child_class=MediaClass.DIRECTORY,
            )

        if node in _LIBRARY:
            title, klass, content_type = _LIBRARY[node]
            items = await self._library(node)
            return _folder(
                node,
                title,
                klass=klass,
                children=[_node(i, klass, content_type) for i in items],
                child_class=klass,
            )

        if node.startswith(("spotify:playlist:", "spotify:album:")):
            _, kind, spotify_id = node.split(":", 2)
            tracks = await self.coordinator.api.tracks_of(kind, spotify_id)
            return _folder(
                node,
                "Tracks",
                klass=MediaClass.DIRECTORY,
                children=[
                    _node(t, MediaClass.TRACK, MediaType.MUSIC) for t in tracks
                ],
                child_class=MediaClass.TRACK,
                # The container itself stays playable, so "play this album"
                # is one tap from its track list rather than a trip back up.
                playable=True,
            )

        # BrowseError rather than a bare exception: HA renders it as a
        # message in the browser instead of an unexplained failure.
        raise BrowseError(f"Cannot browse {node}")

    async def _library(self, node: str) -> list[dict]:
        api = self.coordinator.api
        if node == LIBRARY_PLAYLISTS:
            return await api.playlists()
        if node == LIBRARY_ALBUMS:
            return await api.saved_albums()
        return await api.saved_tracks()

    # ── Commands ────────────────────────────────────────────────────────────

    async def async_play_media(
        self, media_type: MediaType | str, media_id: str, **kwargs: Any
    ) -> None:
        """
        Play a Spotify URI or plain text song name on this device.

        `media_id` is whatever a search result or browse item carried, which
        is the URI — HA hands the id straight back. A link from the Spotify
        app is accepted too, since pasting one into `play_media` is the
        obvious thing to try.

        If `media_id` is plain text (e.g. from an LLM tool call like Extended
        OpenAI Conversation), it automatically executes a search using the
        integration's ranking engine, extracts the best match URI, and plays it.
        """
        generation = self.coordinator.begin_playback_command()
        uri = (media_id or "").strip()
        # A direct play_media call (e.g. an LLM tool) may contain transport
        # words rather than a title. Native voice aliases take the faster
        # media_play path; keep this fallback from searching for an artist
        # named Music too. Explicit contexts and URIs remain literal.
        if media_type in (MediaType.MUSIC, "", None) and is_resume_request(uri):
            await self.async_media_play()
            return
        if uri.startswith("http"):
            parts = [p for p in uri.split("?")[0].split("/") if p]
            if len(parts) >= 2:
                uri = f"spotify:{parts[-2]}:{parts[-1]}"

        # If not a Spotify URI, assume it is plain text from an LLM and search for it
        if not uri.startswith("spotify:"):
            media_class = {
                MediaType.ARTIST: MediaClass.ARTIST,
                MediaType.ALBUM: MediaClass.ALBUM,
                MediaType.PLAYLIST: MediaClass.PLAYLIST,
            }.get(media_type)
            query = SearchMediaQuery(
                search_query=uri,
                media_filter_classes=[media_class] if media_class else None,
            )
            search_result = await self.async_search_media(query)
            if not search_result.result:
                raise HomeAssistantError(
                    f"Could not find any Spotify results for {media_id!r}"
                )
            uri = search_result.result[0].media_content_id

        if not uri or not uri.startswith("spotify:"):
            raise HomeAssistantError(f"{media_id!r} is not a valid Spotify URI or song")

        if not self.coordinator.is_current_playback_command(generation):
            return
        await self.coordinator.api.play_uri(self._spotify_id, uri)
        await self.coordinator.async_request_refresh()

    async def async_media_play(self) -> None:
        # Transfer when this device is not the active one, because `play`
        # alone resumes wherever the account left off — which may be a
        # different speaker in a different room.
        generation = self.coordinator.begin_playback_command()
        if self._active:
            await self.coordinator.api.command("play", self._spotify_id)
        else:
            playback = await resume_inactive(self.coordinator.api, self._spotify_id,
                lambda: self.coordinator.is_current_playback_command(generation))
            if playback is not None and self.coordinator.is_current_playback_command(generation):
                self.coordinator.note_playback(playback)
        await self.coordinator.async_request_refresh()

    async def async_media_pause(self) -> None:
        self.coordinator.begin_playback_command()
        await self.coordinator.api.command("pause", self._spotify_id)
        await self.coordinator.async_request_refresh()

    async def async_media_stop(self) -> None:
        self.coordinator.begin_playback_command()
        # Spotify has no stop. Pausing and winding to the start is what stop
        # means to a Connect device and it leaves the queue intact — the same
        # choice the on-device engine's command set made.
        await self.coordinator.api.command("pause", self._spotify_id)
        await self.coordinator.api.seek(self._spotify_id, 0)
        await self.coordinator.async_request_refresh()

    async def async_media_next_track(self) -> None:
        self.coordinator.begin_playback_command()
        await self.coordinator.api.command("next", self._spotify_id)
        await self.coordinator.async_request_refresh()

    async def async_media_previous_track(self) -> None:
        self.coordinator.begin_playback_command()
        await self.coordinator.api.command("previous", self._spotify_id)
        await self.coordinator.async_request_refresh()

    async def async_set_volume_level(self, volume: float) -> None:
        percent = round(volume * 100)
        await self.coordinator.api.set_volume(self._spotify_id, percent)
        # Shown at once rather than at the next poll. Spotify's device
        # registry does not reliably reflect a change by the time an
        # immediate re-read lands, so without this the slider snaps back to
        # the old value and corrects a moment later. The poll still has the
        # last word.
        self.coordinator.note_volume(self._spotify_id, percent)
        self.async_write_ha_state()
        await self.coordinator.async_request_refresh()

    async def async_media_seek(self, position: float) -> None:
        self.coordinator.begin_playback_command()
        await self.coordinator.api.seek(self._spotify_id, int(position * 1000))
        await self.coordinator.async_request_refresh()

    async def async_set_shuffle(self, shuffle: bool) -> None:
        await self.coordinator.api.set_shuffle(self._spotify_id, shuffle)
        await self.coordinator.async_request_refresh()

    async def async_set_repeat(self, repeat: RepeatMode) -> None:
        state = {RepeatMode.OFF: "off", RepeatMode.ONE: "track", RepeatMode.ALL: "context"}.get(repeat)
        if state is None:
            raise HomeAssistantError("Repeat must be off, one, or all")
        await self.coordinator.api.set_repeat(self._spotify_id, state)
        await self.coordinator.async_request_refresh()


# ── Browse helpers ──────────────────────────────────────────────────────────


def _image_of(item: dict) -> str | None:
    """The first artwork URL on an item, or on the album it belongs to."""
    images = item.get("images")
    if not images and isinstance(item.get("album"), dict):
        images = item["album"].get("images")
    if isinstance(images, list) and images and isinstance(images[0], dict):
        return images[0].get("url")
    return None


def _folder(
    node: str,
    title: str,
    *,
    klass: MediaClass | str = MediaClass.DIRECTORY,
    children: list[BrowseMedia] | None = None,
    child_class: MediaClass | str | None = None,
    playable: bool = False,
) -> BrowseMedia:
    return BrowseMedia(
        media_class=klass,
        media_content_id=node,
        media_content_type=MediaType.MUSIC,
        title=title,
        can_play=playable,
        can_expand=True,
        # Every level offers search, so it is reachable without walking back
        # to the root.
        can_search=True,
        children=children or [],
        children_media_class=child_class,
    )


def _node(item: dict, klass: MediaClass, content_type: MediaType | str) -> BrowseMedia:
    """One library item. A playlist or an album can be opened or played."""
    subtitle = ", ".join(ranking.artist_names(item))
    name = item.get("name") or "Unknown"
    return BrowseMedia(
        media_class=klass,
        media_content_id=item["uri"],
        media_content_type=content_type,
        title=f"{name} — {subtitle}" if subtitle else name,
        can_play=True,
        can_expand=klass in (MediaClass.PLAYLIST, MediaClass.ALBUM),
        thumbnail=_image_of(item),
    )
