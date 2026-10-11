"""
Choosing what to play from a Spotify search.

Written from the edges rather than from typical input: the catalogue is full
of live versions, karaoke, accents and artists whose name is also a word, and
those are what a ranking gets wrong. No Spotify account, token or network —
the module is pure for exactly this reason.

THE MODULE UNDER TEST IS THE HA INTEGRATION'S, not the controller's. The
integration talks only to Spotify, so the controller has no search of its own
and there is exactly ONE copy of the ranking, where it is used.

The test lives here because `controller/tests` is where this repo runs
Python tests (`.github/workflows/ci.yml`). Putting it beside the integration
would need its own CI job, and a test that does not run is not a test.
"""

import importlib.util
from pathlib import Path

import pytest

_RANKING = (Path(__file__).resolve().parents[2]
            / "custom_components" / "echomuse_spotify" / "search.py")


def _load():
    # By path rather than sys.path: the file is called `search.py`, which is
    # far too generic a name to put on the import path of a whole suite.
    spec = importlib.util.spec_from_file_location("echomuse_spotify_ranking", _RANKING)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


assert _RANKING.exists(), f"the ranking module is missing at {_RANKING}"
s = _load()


def track(name, artists=(), uri=None, **extra):
    return {
        "name": name,
        "uri": uri or "spotify:track:" + "a" * 22,
        "artists": [{"name": a} for a in artists],
        **extra,
    }


def body(**sections):
    return {k + "s": {"items": list(v)} for k, v in sections.items()}


def uris(cands):
    return [c.uri for c in cands]


def names(cands):
    return [c.name for c in cands]


# ── normalise ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize("a,b", [
    ("Beyoncé", "Beyonce"),
    ("MOTOMAMI", "motomami"),
    ("Don't Stop Me Now", "dont stop me now"),
    ("Sigur Rós – Hoppípolla", "sigur ros hoppipolla"),
    ("  spaced   out  ", "spaced out"),
    ("AC/DC", "ac dc"),
])
def test_a_spoken_name_matches_a_written_one(a, b):
    # A spoken request has no accents, no punctuation and no case. The
    # catalogue has all three.
    assert s.normalise(a) == s.normalise(b)


def test_normalise_survives_rubbish():
    for v in (None, 123, [], {}):
        assert s.normalise(v) == ""


# ── name_score ──────────────────────────────────────────────────────────────


def test_the_score_ladder_is_ordered():
    q = "bohemian rhapsody"
    assert s.name_score("Bohemian Rhapsody", q) > s.name_score("Bohemian Rhapsody - Live", q)
    assert s.name_score("Bohemian Rhapsody - Live", q) > s.name_score("The Bohemian Rhapsody Story", q)
    assert s.name_score("The Bohemian Rhapsody Story", q) > s.name_score("Rhapsody In Bohemian", q)
    assert s.name_score("Something Else", q) == 0


def test_words_in_any_order_still_count():
    # Speech reorders things: "thriller by michael jackson" against a name
    # carrying both words in the other order.
    assert s.name_score("Michael Jackson Thriller", "thriller michael jackson") > 0


def test_an_empty_query_matches_nothing():
    assert s.name_score("Anything", "") == 0
    assert s.name_score("", "anything") == 0


# ── variants ────────────────────────────────────────────────────────────────


def test_a_variant_is_penalised():
    assert s.variant_penalty("Creep - Live at the BBC", "creep") > 0
    assert s.variant_penalty("Creep (Karaoke Version)", "creep") > 0
    assert s.variant_penalty("Creep", "creep") == 0


def test_asking_for_a_variant_cancels_the_penalty():
    # THE REASON variant_penalty takes the query. Someone asking for the live
    # version must get it, and a blanket penalty would bury it.
    assert s.variant_penalty("Creep - Live at the BBC", "creep live") == 0
    assert s.variant_penalty("Creep (Karaoke Version)", "creep karaoke") == 0


def test_a_variant_still_appears_when_it_is_all_there_is():
    # Demoted, never dropped: a penalty that removed things would answer
    # "nothing found" for a song that only exists as a live recording.
    got = s.rank(s.candidates(body(track=[track("Creep - Live", ["Radiohead"])]), "creep"))
    assert names(got) == ["Creep - Live"]


def test_the_asked_for_version_outranks_the_variant():
    b = body(track=[
        track("Creep - Live at the BBC", ["Radiohead"], uri="spotify:track:" + "l" * 22),
        track("Creep", ["Radiohead"], uri="spotify:track:" + "s" * 22),
    ])
    got = s.rank(s.candidates(b, "creep"))
    assert names(got)[0] == "Creep", f"got {names(got)}"


# ── candidates ──────────────────────────────────────────────────────────────


def test_the_credited_artist_is_a_second_chance_to_match():
    # "thriller michael jackson" names both the track and the artist, and
    # neither alone is an exact match for the other's field.
    b = body(track=[track("Thriller", ["Michael Jackson"])])
    assert s.candidates(b, "thriller michael jackson")[0].score > 0


def test_malformed_json_costs_only_the_malformed_part():
    # Somebody else's JSON over a network. A missing section must not cost
    # the results that did arrive.
    b = {
        "tracks": {"items": [track("Good", ["A"]), None, {"name": "No URI"},
                             {"uri": "spotify:track:" + "b" * 22}]},
        "albums": None,
        "artists": {"items": "not a list"},
    }
    got = s.candidates(b, "good")
    assert names(got) == ["Good"]


def test_nothing_at_all_is_not_an_error():
    assert s.candidates({}, "anything") == []
    assert s.candidates({"tracks": {}}, "anything") == []
    assert s.rank([]) == []
    assert s.play_target([]) is None


def test_a_playlist_falls_back_to_its_owner():
    b = {"playlists": {"items": [
        {"name": "Deep Focus", "uri": "spotify:playlist:" + "p" * 22,
         "owner": {"display_name": "Spotify"}},
    ]}}
    got = s.candidates(b, "deep focus")
    assert got[0].artists == ["Spotify"]


def test_shows_and_episodes_are_never_candidates():
    # Valid Connect contexts, but not music. A podcast surfacing for "play X"
    # is a wrong answer rather than a near miss.
    b = {"shows": {"items": [{"name": "Serial", "uri": "spotify:show:" + "s" * 22}]},
         "episodes": {"items": [{"name": "Serial", "uri": "spotify:episode:" + "e" * 22}]}}
    assert s.candidates(b, "serial") == []


# ── rank ────────────────────────────────────────────────────────────────────


def test_a_zero_score_is_dropped_not_offered():
    # Spotify matches liberally. Offering an item that answers none of the
    # words means a voice request plays something unrelated instead of
    # reporting that it found nothing.
    b = body(track=[track("Totally Unrelated", ["Nobody"])])
    assert s.rank(s.candidates(b, "bohemian rhapsody")) == []


def test_the_kind_preference_breaks_a_tie():
    b = {
        "tracks": {"items": [track("Radiohead", [], uri="spotify:track:" + "t" * 22)]},
        "artists": {"items": [{"name": "Radiohead", "uri": "spotify:artist:" + "r" * 22}]},
    }
    cands = s.candidates(b, "radiohead")
    assert s.rank(cands, prefer=("artist", "track"))[0].kind == "artist"
    assert s.rank(cands, prefer=("track", "artist"))[0].kind == "track"


def test_ranking_is_deterministic_and_stable():
    # The property the module exists for: the same words give the same
    # answer. Two items that cannot be separated keep Spotify's own order.
    b = body(track=[
        track("Tie", ["X"], uri="spotify:track:" + "1" * 22),
        track("Tie", ["X"], uri="spotify:track:" + "2" * 22),
        track("Tie", ["X"], uri="spotify:track:" + "3" * 22),
    ])
    first = uris(s.rank(s.candidates(b, "tie")))
    for _ in range(5):
        assert uris(s.rank(s.candidates(b, "tie"))) == first
    assert first == ["spotify:track:" + c * 22 for c in "123"]


def test_popularity_is_not_consulted():
    # Including it would make the same words resolve differently week to
    # week, and a request that worked yesterday failing today is worse than
    # one that never worked.
    low = track("Song", ["A"], uri="spotify:track:" + "1" * 22, popularity=1)
    high = track("Song", ["A"], uri="spotify:track:" + "2" * 22, popularity=99)
    assert uris(s.rank(s.candidates(body(track=[low, high]), "song"))) == \
        uris(s.rank(s.candidates(body(track=[high, low]), "song")))[::-1]


def test_the_limit_is_respected():
    b = body(track=[track(f"Song {i}", ["A"], uri=f"spotify:track:{i:022d}")
                    for i in range(25)])
    assert len(s.rank(s.candidates(b, "song"), limit=10)) == 10
    assert len(s.rank(s.candidates(b, "song"), limit=3)) == 3
    assert s.rank(s.candidates(b, "song"), limit=0) == []


def test_play_target_is_the_top_of_the_same_ranking():
    # Separate function, same decision — the voice path must not
    # re-implement the choice the list already made.
    b = body(track=[
        track("Creep - Live", ["Radiohead"], uri="spotify:track:" + "l" * 22),
        track("Creep", ["Radiohead"], uri="spotify:track:" + "s" * 22),
    ])
    ranked = s.rank(s.candidates(b, "creep"))
    assert s.play_target(ranked).uri == ranked[0].uri


# ── search_params ───────────────────────────────────────────────────────────


def test_the_limit_is_clamped_to_what_development_mode_allows():
    # Asking for more than 10 is refused by Spotify with an error rather
    # than answered with fewer, so a 400 for asking politely.
    assert s.search_params("x", limit=50)["limit"] == str(s.MAX_LIMIT)
    assert s.search_params("x", limit=0)["limit"] == "1"
    assert s.search_params("x", limit=5)["limit"] == "5"


def test_search_params_refuses_an_empty_query():
    for q in ("", "   ", None):
        with pytest.raises(ValueError):
            s.search_params(q)


def test_only_searchable_kinds_are_requested():
    assert s.search_params("x", kinds=("track", "show"))["type"] == "track"
    with pytest.raises(ValueError):
        s.search_params("x", kinds=("show", "episode"))


def test_the_market_is_optional_and_passed_through():
    assert "market" not in s.search_params("x")
    assert s.search_params("x", market="GB")["market"] == "GB"


def test_a_candidate_exposes_only_what_a_client_needs():
    c = s.candidates(body(track=[track("Song", ["A"])]), "song")[0]
    assert set(c.as_json()) == {"kind", "uri", "name", "artists"}

