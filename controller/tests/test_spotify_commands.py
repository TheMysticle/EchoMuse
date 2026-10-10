"""em_spotify: the command allowlist and the Spotify URI grammar."""

import pytest

import em_spotify as sp

ID = "4uLU6hMCjMI75M1A2tKUQC"


def test_a_uri_passes_through():
    assert sp.normalise_uri(f"spotify:track:{ID}") == f"spotify:track:{ID}"


def test_a_shared_link_becomes_a_uri_and_loses_its_query():
    got = sp.normalise_uri(f"https://open.spotify.com/track/{ID}?si=abcdef")
    assert got == f"spotify:track:{ID}"


def test_a_localised_link_is_understood():
    assert sp.normalise_uri(f"https://open.spotify.com/intl-de/album/{ID}") == f"spotify:album:{ID}"


@pytest.mark.parametrize("bad", [
    "", "spotify:track:short", f"spotify:user:{ID}", f"spotify:track:{ID}x",
    f"https://evil.example/track/{ID}", f"https://open.spotify.com.evil.example/track/{ID}",
    f"ftp://open.spotify.com/track/{ID}", f"spotify:track:{ID}\nspotify:track:{ID}",
    f"spotify:track:{ID}!",
])
def test_things_that_are_not_spotify_links_are_refused(bad):
    with pytest.raises(sp.BadCommand):
        sp.normalise_uri(bad)


def test_a_non_string_is_refused():
    with pytest.raises(sp.BadCommand):
        sp.normalise_uri(123)  # type: ignore[arg-type]


def test_transport_commands_carry_nothing():
    assert sp.validate("pause", {"junk": 1}) == {}


def test_load_needs_a_uri():
    with pytest.raises(sp.BadCommand):
        sp.validate("load", {})
    assert sp.validate("load", {"uri": f"spotify:playlist:{ID}"}) == {"uri": f"spotify:playlist:{ID}"}


def test_load_tracks_takes_tracks_only_and_is_bounded():
    t = f"spotify:track:{ID}"
    assert sp.validate("load_tracks", {"uris": [t], "start_playing": False}) == {"uris": [t], "start_playing": False}
    with pytest.raises(sp.BadCommand):
        sp.validate("load_tracks", {"uris": [f"spotify:album:{ID}"]})
    with pytest.raises(sp.BadCommand):
        sp.validate("load_tracks", {"uris": [t] * (sp.MAX_TRACKS + 1)})
    with pytest.raises(sp.BadCommand):
        sp.validate("load_tracks", {"uris": []})


def test_volume_is_a_u16_and_not_a_bool():
    assert sp.validate("volume", {"level": 65535}) == {"level": 65535}
    for bad in (-1, 65536, True, "10", 1.5):
        with pytest.raises(sp.BadCommand):
            sp.validate("volume", {"level": bad})


def test_a_flag_must_be_a_real_boolean():
    assert sp.validate("shuffle", {"on": True}) == {"on": True}
    with pytest.raises(sp.BadCommand):
        sp.validate("shuffle", {"on": "false"})  # "false" is truthy


def test_an_unknown_command_is_refused():
    with pytest.raises(sp.BadCommand):
        sp.validate("rm", {})
