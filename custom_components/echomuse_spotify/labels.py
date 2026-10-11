"""
The Klar label, which is what makes "play X in <area>" reach these players.

WHY THIS EXISTS. Klar builds its candidate pool for a music request from
`is_music_player()` (`src/home/roles.rs`), which accepts a `media_player`
entity on three grounds only: its platform is `music_assistant`, its platform
is `alexa_devices`, or it carries a label named music / musik / media /
medien. Ours is a fourth platform, so every entity here was filtered out
BEFORE area and name matching ran. Klar then had zero candidates and answered
"I did not catch that" — which reads as a parsing failure and is not one:
the intent and the area both resolved fine.

Of those three grounds the label is the only one we can satisfy honestly.
Claiming another integration's platform string would be a lie told to one
assistant that every other consumer of the entity registry would also read.

So this applies the label, and the pure function below decides WHICH. HA
derives a label's id by slugifying its name (`slugify(name)`, with a `_2`
suffix on collision) and compares names by `normalize_name` — casefold plus
space removal. A label created as "Music" therefore has id `music` and
normalized name `music`, which satisfies Klar whether it matches on the id or
on the name. We do not know which it reads, so a label that matches on only
one of the two is reported rather than used silently.

The caller applies it ONCE per config entry and records that it has
(`OPTION_LABELLED`), so removing the label by hand sticks instead of being
re-added on the next restart.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

# The label names Klar accepts, already normalized. German included because
# Klar's own list is — it reads musik/medien alongside music/media.
MUSIC_LABELS = frozenset({"music", "musik", "media", "medien"})

# Created when the user has none of the above. "Music" slugifies to the id
# `music` and normalizes to the name `music`, so it matches either way.
CREATE_LABEL_NAME = "Music"


def normalize_name(name: str) -> str:
    """HA's `normalized_name` for a registry entry: casefold, drop spaces.

    Copied rather than imported so this module stays free of HA and can be
    unit-tested where the test suite runs. It is four tokens of a stable
    helper; a drift test is not worth more than the duplication.
    """
    return name.casefold().replace(" ", "")


@dataclass(frozen=True)
class LabelChoice:
    """Which existing label to use, and what was odd about it.

    `label_id` is None when nothing suitable exists and the caller should
    create one. `warning` is non-None when the choice is usable but only
    half-matches, which is worth a log line because whether it works then
    depends on which field Klar reads.
    """

    label_id: str | None
    warning: str | None = None


def choose_music_label(existing: Mapping[str, str]) -> LabelChoice:
    """Pick the label to tag our players with.

    `existing` maps label_id to the label's display name, as the label
    registry holds them.

    Preference order, and the reason for it: a label matching on BOTH id and
    name works whichever field Klar reads; one matching on a single field is
    a coin toss, so it is used but reported. Ties are broken by sorted id so
    two runs on the same registry agree.
    """
    both: list[str] = []
    id_only: list[str] = []
    name_only: list[str] = []

    for label_id, name in sorted(existing.items()):
        by_id = label_id in MUSIC_LABELS
        by_name = normalize_name(name) in MUSIC_LABELS
        if by_id and by_name:
            both.append(label_id)
        elif by_id:
            id_only.append(label_id)
        elif by_name:
            name_only.append(label_id)

    if both:
        return LabelChoice(both[0])
    if id_only:
        return LabelChoice(
            id_only[0],
            f"label '{id_only[0]}' has an id Klar accepts but the name "
            f"'{existing[id_only[0]]}' is not one of "
            f"{sorted(MUSIC_LABELS)}; if Klar matches on the name this will "
            f"not work — rename the label to Music",
        )
    if name_only:
        return LabelChoice(
            name_only[0],
            f"label '{existing[name_only[0]]}' has a name Klar accepts but "
            f"its id is '{name_only[0]}', not one of {sorted(MUSIC_LABELS)} "
            f"(HA keeps the original id across a rename); if Klar matches on "
            f"the id this will not work — delete the label and make a new "
            f"one called Music",
        )
    return LabelChoice(None)


def labels_after(current: frozenset[str] | set[str], label_id: str) -> set[str]:
    """The entity's labels with ours added, never replacing what is there.

    Labels are the user's namespace. An integration that writes the set it
    wants takes away whatever the user put there, so this is additive and the
    caller skips the write when nothing changes.
    """
    return set(current) | {label_id}


def needs_label(current: frozenset[str] | set[str]) -> bool:
    """Whether this entity still needs tagging.

    Any accepted label counts, not only the one we would have chosen: a user
    who tagged it `medien` by hand has already satisfied Klar, and adding a
    second label would be noise in their UI.
    """
    return not (set(current) & MUSIC_LABELS)
