"""
Spotify commands the controller will send an Echo, and the URIs they carry.

Pure, so the allowlist and the URI grammar are tested without aiohttp. The
engine (device/spotify) is the real authority on what a command means; this
is the boundary check, so a dashboard or voice request cannot put anything
else on the wire and a bad URI is refused with a reason before it is sent.
"""

from __future__ import annotations

import re
from urllib.parse import urlparse

# Spotify IDs are 22 base-62 characters.
_ID = r"[A-Za-z0-9]{22}"
_KINDS = ("track", "album", "playlist", "artist", "show", "episode")
_URI = re.compile(rf"^spotify:({'|'.join(_KINDS)}):({_ID})$")
_WEB_PATH = re.compile(rf"^/(?:intl-[a-z]{{2,5}}/)?({'|'.join(_KINDS)})/({_ID})/?$")

# What load and load_tracks may be handed. A podcast is a valid Connect
# context but not music the voice flow looks for.
MAX_TRACKS = 50


class BadCommand(ValueError):
    pass


def normalise_uri(text: str) -> str:
    """
    A spotify: URI, from either a URI or an open.spotify.com link. Anything
    else raises BadCommand; the query string on a shared link is dropped.
    """
    if not isinstance(text, str):
        raise BadCommand("a Spotify URI is text")
    text = text.strip()
    m = _URI.match(text)
    if m:
        return text
    if text.startswith("http"):
        u = urlparse(text)
        if u.scheme in ("http", "https") and u.hostname == "open.spotify.com":
            w = _WEB_PATH.match(u.path)
            if w:
                return f"spotify:{w.group(1)}:{w.group(2)}"
    raise BadCommand("not a Spotify track, album, playlist, artist or show link")


def _bool(fields: dict, key: str) -> bool:
    v = fields.get(key)
    if not isinstance(v, bool):
        raise BadCommand(f"{key} must be true or false")
    return v


def _uint(fields: dict, key: str, hi: int) -> int:
    v = fields.get(key)
    if isinstance(v, bool) or not isinstance(v, int) or not 0 <= v <= hi:
        raise BadCommand(f"{key} must be a whole number from 0 to {hi}")
    return v


def validate(cmd: str, fields: dict | None) -> dict:
    """The fields to send for `cmd`, or BadCommand. Unknown fields are dropped."""
    fields = fields or {}
    if not isinstance(fields, dict):
        raise BadCommand("fields must be an object")
    if cmd in ("play", "pause", "play_pause", "stop", "next", "prev",
               "activate", "status", "logout"):
        return {}
    if cmd == "load":
        out: dict = {"uri": normalise_uri(fields.get("uri", ""))}
        if "start_playing" in fields:
            out["start_playing"] = _bool(fields, "start_playing")
        return out
    if cmd == "load_tracks":
        uris = fields.get("uris")
        if not isinstance(uris, list) or not 1 <= len(uris) <= MAX_TRACKS:
            raise BadCommand(f"uris must be a list of 1 to {MAX_TRACKS} tracks")
        norm = [normalise_uri(u) for u in uris]
        if any(not u.startswith("spotify:track:") for u in norm):
            raise BadCommand("load_tracks takes tracks only")
        out = {"uris": norm}
        if "start_playing" in fields:
            out["start_playing"] = _bool(fields, "start_playing")
        return out
    if cmd == "seek":
        return {"position_ms": _uint(fields, "position_ms", 0xFFFFFFFF)}
    if cmd == "volume":
        return {"level": _uint(fields, "level", 65535)}
    if cmd == "disconnect":
        return {"pause": _bool(fields, "pause")} if "pause" in fields else {}
    if cmd in ("shuffle", "repeat", "repeat_track"):
        return {"on": _bool(fields, "on")}
    raise BadCommand(f"unknown Spotify command: {cmd}")
