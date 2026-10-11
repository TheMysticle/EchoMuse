"""
The Spotify Web API calls this integration makes, and nothing else.

Thin on purpose: every method is one request, the ranking lives in
`search.py` (pure), and the entity decides what to do with the answers. That
split is what lets the ranking be tested without a Spotify account.

Errors are translated here rather than at the call sites, because Spotify's
interesting failures are all about what the USER has to do — a free account,
an app in development mode that has not listed them, a device that went away
— and a bare "403" in a voice response is useless to them.
"""

from __future__ import annotations

import logging
from typing import Any

from aiohttp import ClientError, ClientResponseError, ClientSession
from homeassistant.exceptions import HomeAssistantError

from .const import API_BASE
from .playback import play_payload

_LOGGER = logging.getLogger(__name__)


class SpotifyError(HomeAssistantError):
    """Something the user can act on. The message is shown as written."""


class SpotifyAuthExpired(SpotifyError):
    """The token is no longer accepted; the config entry needs reauth."""


def _describe(status: int, body: Any) -> str:
    """
    An actionable reason for a failed call.

    Mirrors `em_spotify_web.describe_api_error` on the controller. The two
    are deliberately separate copies rather than a shared import — they live
    in different installs — but they must say the same things, because a user
    hitting the development-mode user list should not get two different
    explanations depending on which half of the project asked.
    """
    err = body.get("error") if isinstance(body, dict) else None
    msg = err.get("message", "") if isinstance(err, dict) else ""
    reason = err.get("reason") if isinstance(err, dict) else None
    if status == 429:
        if reason == "QUOTA_EXCEEDED":
            return "this Spotify app has used its development-mode quota; try later"
        return "Spotify is rate limiting this app; wait a minute"
    if status == 401:
        return "Spotify no longer accepts this connection; reconnect the integration"
    if status == 403:
        low = msg.lower()
        if "premium" in low:
            return "Spotify Premium is required to control playback"
        if "not registered" in low or "user not" in low:
            return (
                "this Spotify account is not on the app's User Management list; "
                "add it on developer.spotify.com/dashboard (development mode "
                "allows 5 users)"
            )
        return "Spotify refused the request" + (f" ({msg})" if msg else "")
    if status == 404:
        # The playback endpoints answer 404 for "no such active device",
        # which is a device that dropped off rather than a bad request.
        return "that Spotify device is no longer available"
    if status >= 500:
        return "Spotify's service had an error; try again shortly"
    return f"Spotify returned {status}" + (f" ({msg})" if msg else "")


class SpotifyApi:
    """One account's Web API, behind a token the caller keeps fresh."""

    def __init__(self, session: ClientSession, token: "TokenSource") -> None:
        self._session = session
        self._token = token

    async def _request(self, method: str, path: str, **kw) -> Any:
        token = await self._token()
        try:
            async with self._session.request(
                method,
                API_BASE + path,
                headers={"Authorization": f"Bearer {token}"},
                **kw,
            ) as resp:
                # 204 is success with no body, which every playback command
                # returns. Reading it as a failed parse would report an error
                # for something that worked.
                if resp.status == 204:
                    return None
                try:
                    body = await resp.json(content_type=None)
                except (ValueError, ClientResponseError):
                    body = None
                if resp.status == 401:
                    raise SpotifyAuthExpired(_describe(401, body))
                if resp.status >= 400:
                    raise SpotifyError(_describe(resp.status, body))
                return body
        except ClientError as err:
            raise SpotifyError(
                f"could not reach Spotify ({type(err).__name__})"
            ) from err

    # ── Reads ───────────────────────────────────────────────────────────────

    async def devices(self) -> list[dict]:
        """
        The Connect devices the account can currently see.

        Only devices that are ONLINE appear, which is the only way to learn
        that an Echo has gone away — there is no event for it.
        """
        body = await self._request("GET", "/me/player/devices")
        items = body.get("devices") if isinstance(body, dict) else None
        return [d for d in items or [] if isinstance(d, dict) and d.get("id")]

    async def playback(self) -> dict | None:
        """What the account is playing, or None when nothing is."""
        body = await self._request("GET", "/me/player")
        return body if isinstance(body, dict) else None

    async def search(self, params: dict) -> dict:
        """Raw search results; `search.search_params` builds the params."""
        body = await self._request("GET", "/search", params=params)
        return body if isinstance(body, dict) else {}

    async def playlists(self, limit: int = 50) -> list[dict]:
        """The account's own playlists."""
        body = await self._request(
            "GET", "/me/playlists", params={"limit": str(limit)}
        )
        items = body.get("items") if isinstance(body, dict) else None
        return [i for i in items or [] if isinstance(i, dict)]

    async def saved_albums(self, limit: int = 50) -> list[dict]:
        """Albums in the account's library. Wrapped one level deeper."""
        body = await self._request(
            "GET", "/me/albums", params={"limit": str(limit)}
        )
        items = body.get("items") if isinstance(body, dict) else None
        out = []
        for i in items or []:
            album = i.get("album") if isinstance(i, dict) else None
            if isinstance(album, dict):
                out.append(album)
        return out

    async def saved_tracks(self, limit: int = 50) -> list[dict]:
        """Liked Songs. Wrapped one level deeper, like saved albums."""
        body = await self._request("GET", "/me/tracks", params={"limit": str(limit)})
        items = body.get("items") if isinstance(body, dict) else None
        out = []
        for i in items or []:
            track = i.get("track") if isinstance(i, dict) else None
            if isinstance(track, dict) and track.get("uri"):
                out.append(track)
        return out

    async def tracks_of(self, kind: str, spotify_id: str, limit: int = 50) -> list[dict]:
        """
        The tracks of a playlist or an album.

        A playlist's items are wrapped in a `track` key and an album's are
        not, and a playlist can contain episodes — which come back with a
        null track. Both are flattened here so the caller sees tracks.
        """
        path = f"/{kind}s/{spotify_id}/tracks"
        body = await self._request("GET", path, params={"limit": str(limit)})
        items = body.get("items") if isinstance(body, dict) else None
        out = []
        for i in items or []:
            if not isinstance(i, dict):
                continue
            track = i.get("track", i) if kind == "playlist" else i
            if isinstance(track, dict) and track.get("uri"):
                out.append(track)
        return out

    # ── Playback ────────────────────────────────────────────────────────────

    async def track(self, spotify_id: str) -> dict:
        """One track, for the album it belongs to. See `play_uri`."""
        body = await self._request("GET", f"/tracks/{spotify_id}")
        return body if isinstance(body, dict) else {}

    async def play_uri(self, device_id: str, uri: str) -> None:
        """
        Start `uri` on `device_id`, transferring playback if needed.

        A SINGLE TRACK IS PLAYED IN ITS ALBUM'S CONTEXT, not on its own, and
        that is the point of the extra request this makes.

        Spotify accepts a bare track through `uris` with no context, and it
        plays — but it leaves the Connect session with no context at all, and
        a librespot device then cannot function as one. Observed on an Echo,
        2026-10-11: `couldn't load context info because: context is not
        available. type: Default`, then the context resolver's request
        refused with a 400 (`librespot_connect::spirc`, which logs it and
        marks the context unavailable), and at the end of the track `Not
        playing next track because there are no more tracks left in queue`.
        Playing again into that state wedged the device until em-spotify was
        killed. The Spotify app never does this to it, because the app always
        sends a context.

        So a track is sent as its album plus an `offset`, which is what a
        phone does when you tap a track in an album. The visible difference
        is that playback CONTINUES with the rest of the album rather than
        stopping after one track, and next/previous work.

        `offset` is valid for an album, playlist or show context; it is not
        for an artist, which is why only the album case sends one.
        """
        album_uri: str | None = None
        if uri.startswith("spotify:track:"):
            try:
                album = (await self.track(uri.split(":")[-1])).get("album")
                if isinstance(album, dict) and isinstance(album.get("uri"), str):
                    album_uri = album["uri"]
            except SpotifyError as err:
                # The lookup is an improvement on the request, not a
                # precondition for it. Losing it costs the queue and risks
                # the state above, so it is said out loud and play still
                # goes ahead.
                _LOGGER.warning(
                    "could not look up the album for %s (%s); playing it "
                    "without a context, which leaves the device with an "
                    "empty queue",
                    uri,
                    err,
                )
        try:
            payload = play_payload(uri, album_uri)
        except ValueError as err:
            raise SpotifyError(str(err)) from err
        await self._request(
            "PUT", "/me/player/play", params={"device_id": device_id}, json=payload
        )

    async def transfer(self, device_id: str, play: bool = True) -> None:
        await self._request(
            "PUT", "/me/player", json={"device_ids": [device_id], "play": play}
        )

    async def command(self, verb: str, device_id: str) -> None:
        """pause / next / previous, which differ only in method and path."""
        method, path = {
            "pause": ("PUT", "/me/player/pause"),
            "play": ("PUT", "/me/player/play"),
            "next": ("POST", "/me/player/next"),
            "previous": ("POST", "/me/player/previous"),
        }[verb]
        await self._request(method, path, params={"device_id": device_id})

    async def set_volume(self, device_id: str, percent: int) -> None:
        await self._request(
            "PUT",
            "/me/player/volume",
            params={"volume_percent": str(max(0, min(100, percent))),
                    "device_id": device_id},
        )

    async def seek(self, device_id: str, position_ms: int) -> None:
        await self._request(
            "PUT",
            "/me/player/seek",
            params={"position_ms": str(max(0, position_ms)), "device_id": device_id},
        )


class TokenSource:  # pragma: no cover - a typing alias in practice
    """Anything that returns a currently valid access token when awaited."""

    async def __call__(self) -> str:  # noqa: D102
        raise NotImplementedError
