"""
The body sent to Spotify's `/me/player/play`.

WHY THIS IS TESTED AT ALL, given it is a dict with two shapes: getting it
wrong does not fail loudly. A bare track in `uris` is perfectly legal and
plays — and leaves a librespot Connect device with no context, which is what
wedged an Echo on 2026-10-11 (`couldn't load context info`, the context
resolver refused with a 400, then `no more tracks left in queue`, and the
engine had to be killed). The request succeeded every time. So the thing
worth pinning is that a track carries a context, and these are the edges
Spotify's own rule has.

Lives here because `controller/tests` is where this repo runs Python tests.
"""

import importlib.util
import sys
from pathlib import Path

import pytest

_PLAYBACK = (Path(__file__).resolve().parents[2]
             / "custom_components" / "echomuse_spotify" / "playback.py")


def _load():
    spec = importlib.util.spec_from_file_location("echomuse_spotify_playback", _PLAYBACK)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


assert _PLAYBACK.exists(), f"the playback module is missing at {_PLAYBACK}"
p = _load()

TRACK = "spotify:track:2EBCVPNAG46nbgs6jXPGvv"  # the one from the crash log
ALBUM = "spotify:album:5GWoXPsTQ1g7qKNchhbQiI"


# --- the regression this module exists for --------------------------------


def test_a_track_with_a_known_album_is_played_in_that_context():
    # THE FIX. Not `uris`, which is what left the device without a context.
    assert p.play_payload(TRACK, ALBUM) == {
        "context_uri": ALBUM,
        "offset": {"uri": TRACK},
    }


def test_a_track_in_a_context_never_also_sends_uris():
    # The two fields are mutually exclusive; sending both fails the request.
    payload = p.play_payload(TRACK, ALBUM)
    assert "uris" not in payload


def test_a_track_with_no_album_still_plays():
    # The degraded case. Worse than the above and logged by the caller, but
    # refusing to play at all would be a worse answer than an empty queue.
    assert p.play_payload(TRACK, None) == {"uris": [TRACK]}
    assert p.play_payload(TRACK) == {"uris": [TRACK]}


def test_an_empty_album_uri_is_treated_as_unknown():
    # An empty string is what a missing field reads as, and it must not be
    # sent as a context — Spotify refuses it and nothing plays.
    assert p.play_payload(TRACK, "") == {"uris": [TRACK]}


# --- Spotify's field rule, in both directions -----------------------------


@pytest.mark.parametrize("kind", ["album", "artist", "playlist", "show"])
def test_a_context_kind_goes_in_context_uri(kind):
    uri = f"spotify:{kind}:4uLU6hMCjMI75M1A2tKUQC"
    assert p.play_payload(uri) == {"context_uri": uri}


@pytest.mark.parametrize("kind", ["album", "artist", "playlist", "show"])
def test_a_context_kind_ignores_an_album_hint(kind):
    uri = f"spotify:{kind}:4uLU6hMCjMI75M1A2tKUQC"
    assert p.play_payload(uri, ALBUM) == {"context_uri": uri}


def test_an_artist_context_carries_no_offset():
    # Spotify does not accept `offset` with an artist context. Pinned
    # because an artist is a context kind and the offset lives on the other
    # branch, so a refactor could easily hand it one.
    uri = "spotify:artist:0hCNtLu0JehylgoiP8L4Gh"
    assert "offset" not in p.play_payload(uri, ALBUM)


def test_an_episode_goes_in_uris_like_a_track():
    uri = "spotify:episode:512ojhOuo1ktJprKbVcKyQ"
    assert p.play_payload(uri) == {"uris": [uri]}


def test_an_episode_is_not_given_an_album_context():
    # An album is not an episode's container; a show is. Passing one must
    # not produce a request that mixes them.
    uri = "spotify:episode:512ojhOuo1ktJprKbVcKyQ"
    assert p.play_payload(uri, ALBUM) == {"uris": [uri]}


# --- what is refused rather than guessed ----------------------------------


@pytest.mark.parametrize(
    "uri",
    [
        "spotify:user:someone",
        "spotify:collection:tracks",
        "spotify:local:a:b:c:1",
    ],
)
def test_an_unplayable_kind_is_refused(uri):
    # Refused, not guessed into one of the two fields: a guess produces a
    # request Spotify rejects whole, with a message about the field rather
    # than about the URI.
    with pytest.raises(ValueError):
        p.play_payload(uri)


@pytest.mark.parametrize(
    "uri",
    [
        "",
        "spotify",
        "spotify:",
        "spotify:track",
        "spotify:track:",
        "https://open.spotify.com/track/2EBCVPNAG46nbgs6jXPGvv",
        "2EBCVPNAG46nbgs6jXPGvv",
        ":track:abc",
    ],
)
def test_a_malformed_uri_is_refused(uri):
    with pytest.raises(ValueError):
        p.play_payload(uri)


# --- uri_kind, which the above rests on -----------------------------------


def test_uri_kind_reads_the_kind():
    assert p.uri_kind(TRACK) == "track"
    assert p.uri_kind(ALBUM) == "album"


def test_uri_kind_rejects_what_is_not_a_playable_uri():
    for bad in ("", "spotify", "spotify:", "spotify:track:", "track:abc"):
        assert p.uri_kind(bad) is None, bad


def test_uri_kind_tolerates_a_longer_uri():
    # Spotify's own URIs are not all three parts — a playlist used to carry
    # the owner. The kind is still the second field.
    assert p.uri_kind("spotify:user:someone:playlist:37i9dQZF1DX") == "user"


def test_the_two_kind_sets_do_not_overlap():
    # If a kind were in both, which field it lands in would depend on branch
    # order rather than on Spotify's rule.
    assert not (p.ITEM_KINDS & p.CONTEXT_KINDS)
