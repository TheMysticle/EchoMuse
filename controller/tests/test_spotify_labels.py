"""
Picking the label that makes Klar accept our media players.

Klar's `is_music_player()` admits a `media_player` only if its platform is
`music_assistant` or `alexa_devices`, or it carries a label named music /
musik / media / medien. Ours is a fourth platform, so without the label every
entity was filtered out of the candidate pool before area matching ran and
Klar answered "I did not catch that" with nothing wrong upstream of it.

The decision is tested, the registry writes are not: `labels.py` is pure for
that reason, and the glue in `__init__.py` is three HA calls with no branches
worth a fake registry.

Written from the edges, because the awkward cases are all real states a
user's registry can be in: HA derives a label id by slugifying the name ONCE,
at creation, and keeps it across every later rename — so id and name can
disagree, and which one Klar reads decides whether the label works.

Lives here because `controller/tests` is where this repo runs Python tests.
"""

import importlib.util
import sys
from pathlib import Path

_LABELS = (Path(__file__).resolve().parents[2]
           / "custom_components" / "echomuse_spotify" / "labels.py")


def _load():
    # By path, as the ranking test does: `labels.py` is too generic a name to
    # put on a whole suite's import path.
    spec = importlib.util.spec_from_file_location("echomuse_spotify_labels", _LABELS)
    module = importlib.util.module_from_spec(spec)
    # Registered BEFORE exec because the module defines a dataclass, and
    # @dataclass resolves annotations through sys.modules[cls.__module__] —
    # which is None for a module that is not there yet, and fails on the
    # `str | None` field rather than on anything to do with this test. The
    # ranking module has no dataclass, which is why its loader needs no
    # equivalent.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


assert _LABELS.exists(), f"the labels module is missing at {_LABELS}"
m = _load()


# --- normalize_name: HA's own rule, which the choice depends on ------------


def test_normalize_name_matches_home_assistants_rule():
    # casefold and drop spaces, nothing else. From
    # homeassistant/helpers/normalized_name_base_registry.py (2026.2.3).
    assert m.normalize_name("Music") == "music"
    assert m.normalize_name("  My   Music  ") == "mymusic"
    assert m.normalize_name("MUSIK") == "musik"
    assert m.normalize_name("Straße") == "strasse"  # casefold, not lower()


def test_normalize_name_does_not_strip_punctuation():
    # A label called "Music!" is NOT a match, and must not be treated as one
    # just because it reads like one.
    assert m.normalize_name("Music!") == "music!"
    assert m.normalize_name("Music!") not in m.MUSIC_LABELS


# --- nothing suitable: the caller creates one -----------------------------


def test_empty_registry_asks_for_a_label_to_be_created():
    choice = m.choose_music_label({})
    assert choice.label_id is None
    assert choice.warning is None


def test_unrelated_labels_ask_for_a_label_to_be_created():
    choice = m.choose_music_label(
        {"downstairs": "Downstairs", "favourite": "Favourite", "tv": "TV"}
    )
    assert choice.label_id is None
    assert choice.warning is None


def test_the_name_we_create_satisfies_both_fields():
    # The whole reason CREATE_LABEL_NAME is "Music": slugify gives the id
    # `music` and normalize gives the name `music`, so it matches whichever
    # field Klar reads. A name like "My Music" would satisfy neither.
    assert m.normalize_name(m.CREATE_LABEL_NAME) in m.MUSIC_LABELS
    assert m.CREATE_LABEL_NAME.lower() in m.MUSIC_LABELS


# --- a clean match on both fields -----------------------------------------


def test_a_label_matching_both_id_and_name_is_used_without_complaint():
    choice = m.choose_music_label({"music": "Music", "tv": "TV"})
    assert choice.label_id == "music"
    assert choice.warning is None


def test_every_accepted_name_is_matched():
    for label_id in ("music", "musik", "media", "medien"):
        choice = m.choose_music_label({label_id: label_id.capitalize()})
        assert choice.label_id == label_id, label_id
        assert choice.warning is None, label_id


def test_case_and_spacing_in_the_name_do_not_prevent_a_clean_match():
    # The id is what HA stored at creation; the user has since typed the name
    # with different case. Still a both-fields match.
    choice = m.choose_music_label({"music": "  MUSIC "})
    assert choice.label_id == "music"
    assert choice.warning is None


def test_a_both_match_wins_over_a_half_match():
    choice = m.choose_music_label(
        {"music": "Music", "media": "Renamed Away", "zz": "Musik"}
    )
    assert choice.label_id == "music"
    assert choice.warning is None


# --- half matches: usable, but reported -----------------------------------


def test_an_id_match_with_a_renamed_label_is_used_and_warned_about():
    # The user made "Music" (id `music`) and later renamed it to "Jazz". HA
    # keeps the id. Klar matching on the name would now miss it.
    choice = m.choose_music_label({"music": "Jazz"})
    assert choice.label_id == "music"
    assert choice.warning is not None
    assert "Jazz" in choice.warning


def test_a_name_match_with_a_suffixed_id_is_used_and_warned_about():
    # A slug collision gave HA `music_2` while the name reads "Music".
    choice = m.choose_music_label({"music_2": "Music"})
    assert choice.label_id == "music_2"
    assert choice.warning is not None
    assert "music_2" in choice.warning


def test_an_id_half_match_is_preferred_over_a_name_half_match():
    # Arbitrary but fixed, and the test is here so a change to the order is
    # a deliberate one.
    choice = m.choose_music_label({"music": "Jazz", "zz": "Media"})
    assert choice.label_id == "music"
    assert choice.warning is not None


# --- determinism ----------------------------------------------------------


def test_the_choice_does_not_depend_on_registry_order():
    a = m.choose_music_label({"musik": "Musik", "music": "Music"})
    b = m.choose_music_label({"music": "Music", "musik": "Musik"})
    assert a.label_id == b.label_id


# --- needs_label ----------------------------------------------------------


def test_an_untagged_entity_needs_the_label():
    assert m.needs_label(set())
    assert m.needs_label({"downstairs", "favourite"})


def test_any_accepted_label_counts_as_already_tagged():
    # A user who tagged it `medien` by hand has already satisfied Klar;
    # adding a second label would be clutter in their UI for no gain.
    for label_id in m.MUSIC_LABELS:
        assert not m.needs_label({label_id}), label_id
    assert not m.needs_label({"downstairs", "media"})


# --- labels_after: the user's namespace is not ours -----------------------


def test_applying_the_label_keeps_the_users_other_labels():
    assert m.labels_after({"downstairs", "favourite"}, "music") == {
        "downstairs",
        "favourite",
        "music",
    }


def test_applying_the_label_twice_is_not_a_duplicate():
    assert m.labels_after({"music"}, "music") == {"music"}


def test_applying_to_an_empty_set_works_and_does_not_mutate_the_input():
    current = set()
    assert m.labels_after(current, "music") == {"music"}
    assert current == set()


def test_a_frozenset_from_the_registry_is_accepted():
    # RegistryEntry.labels is a set, but it is the registry's own object and
    # must not be mutated in place — hence the copy, asserted here.
    current = frozenset({"tv"})
    out = m.labels_after(current, "music")
    assert out == {"tv", "music"}
    assert current == frozenset({"tv"})
