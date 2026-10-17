"""
What to PUT to `/me/player/play` for a given URI.

Pure, and separate from `api.py`, so it can be tested where this repo runs
Python tests — `api.py` imports Home Assistant and cannot be.

THE RULE SPOTIFY ENFORCES. `/me/player/play` takes a playable item in one of
two mutually exclusive fields, and the choice is decided by the URI's kind:

  `uris`        a list of TRACKS or EPISODES
  `context_uri` one ALBUM, ARTIST, PLAYLIST or SHOW

Sending a track as a `context_uri`, or an album in `uris`, fails the whole
request. There is no field that takes either.

WHY A TRACK STILL GETS A CONTEXT. A bare track in `uris` is legal and plays,
but it leaves the Connect session with no context — and a librespot device in
that state stops working as a Connect device (see `api.play_uri` for the
measurement). So a track is sent as its album with an `offset` pointing at
it, which is what a phone does when you tap a track inside an album. The
album URI has to be looked up, which is the caller's job because it needs the
network; this module only decides the shape.

`offset` selects where to start inside a context. Valid for an album,
playlist or show; NOT for an artist, so no artist context carries one.
"""

from __future__ import annotations

from typing import Any

# Kinds that go in `uris`, and the ones that go in `context_uri`. Both lists
# are closed: a kind in neither is refused rather than guessed at, because
# guessing produces a request Spotify rejects whole.
ITEM_KINDS = frozenset({"track", "episode"})
CONTEXT_KINDS = frozenset({"album", "artist", "playlist", "show"})


def is_resume_request(text: str) -> bool:
    """Generic transport requests are not catalogue names.

    Exact phrases only: 'Music by Madonna' and explicit Spotify URIs remain
    searchable/playable. Explicit artist/album requests bypass this helper.
    """
    return " ".join(text.casefold().strip(" .!?").split()) in {
        "music", "the music", "my music", "some music", "play music",
        "resume", "resume music", "continue", "continue music",
        "continue playing", "continue playing music",
    }


def uri_kind(uri: str) -> str | None:
    """The kind in `spotify:<kind>:<id>`, or None if it is not one of those.

    Deliberately strict about the shape: a URI with no id is not playable,
    and accepting it here would send Spotify a request it refuses with a
    message about the wrong thing.
    """
    parts = uri.split(":")
    if len(parts) < 3 or parts[0] != "spotify" or not parts[-1]:
        return None
    return parts[1] or None


def play_payload(uri: str, album_uri: str | None = None) -> dict[str, Any]:
    """The body for `/me/player/play`.

    `album_uri` is the album a TRACK belongs to, when the caller could look
    it up. It is ignored for every other kind.

    Raises ValueError for a URI this endpoint cannot play, which the caller
    turns into something the user can read.
    """
    kind = uri_kind(uri)
    if kind is None:
        raise ValueError(f"{uri!r} is not a Spotify URI")

    if kind == "track":
        if album_uri:
            # The album, starting at this track: a real context, so the
            # device gets a queue and next/previous mean something.
            return {"context_uri": album_uri, "offset": {"uri": uri}}
        # No album known. Legal, and the degraded case the docstring above
        # is about — the caller logs it.
        return {"uris": [uri]}

    if kind == "episode":
        # An episode has a show it belongs to, and giving it that context
        # would be the same improvement as the album above. Not done because
        # nothing in this integration produces an episode URI yet: search
        # and browse return tracks, albums, artists and playlists only, so
        # the only way here is a hand-pasted link. Left as the plain form
        # rather than an untested lookup.
        return {"uris": [uri]}

    if kind in CONTEXT_KINDS:
        return {"context_uri": uri}

    raise ValueError(
        f"{uri!r} is a Spotify {kind} URI, which cannot be played directly"
    )
