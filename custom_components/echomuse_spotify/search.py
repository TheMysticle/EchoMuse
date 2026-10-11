"""
Choosing what to play from a Spotify search, as pure logic.

The Web API returns what MATCHED; this decides what was MEANT. It is pure so
the ranking is tested from the edges without a Spotify account, an access
token or aiohttp — the HTTP lives in `em_api.py`, as it does for
`em_spotify_web`.

Three rules shape all of it:

* **Popularity is not a signal.** Spotify's `popularity` would make the
  ranking depend on what everyone else is streaming this week, so the same
  words would stop resolving to the same song — and a request that worked
  yesterday failing today is worse than one that never worked. Everything
  here is a property of the NAMES and the query.
* **Deterministic, so the same words always give the same answer.** Every
  comparison is a total order with explicit tie-breaks, and the final sort is
  stable, so two candidates that genuinely cannot be separated keep Spotify's
  own order rather than depending on dict iteration.
* **Variants are demoted, never dropped.** "Live", "remix", "karaoke",
  "sped up" are usually not what somebody asking for a song by name wants,
  but they are exactly what somebody who SAYS one of those words wants. So
  the penalty is cancelled by the query asking for it, and a variant still
  appears if it is all there is.

Nothing here sends catalogue data anywhere. It takes Spotify's JSON and
returns a ranked list.
"""

from __future__ import annotations

import re
import unicodedata

# What the Web API will be asked for, and what a caller may request. Shows
# and episodes are deliberately absent: they are valid Connect contexts but
# not music, and a podcast surfacing for "play X" is a wrong answer rather
# than a near miss.
KINDS = ("track", "artist", "album", "playlist")

# Spotify's development mode caps `limit` at 10, so asking for more fails the
# request outright rather than returning fewer.
MAX_LIMIT = 10

# Words that mark a recording as a variant of the thing somebody named.
# Matched against the name only, and only outside the query — see the module
# docstring. Ordered longest-first so "sped up" is not read as "up".
_VARIANTS = (
    "karaoke", "instrumental", "originally performed", "tribute",
    "made famous by", "in the style of", "cover version",
    "live at", "live from", "live in", "live version",
    "remix", "remastered", "re-recorded", "rerecorded", "radio edit",
    "sped up", "slowed", "nightcore", "8d audio", "acoustic version",
    "demo", "commentary", "a cappella", "acapella",
)

# An apostrophe is DELETED, not spaced: "Don't" must become "dont", because
# that is what a spoken request sounds like. Everything else becomes a space,
# so "AC/DC" becomes two words rather than one. Replacing all punctuation
# with a space gave "don t stop me now", which matched nothing.
_DROP = re.compile(r"['‘’ʼ`´]+")
_PUNCT = re.compile(r"[^\w\s]+", re.UNICODE)
_SPACE = re.compile(r"\s+")

# Marker words too generic to mean "I want the variant". Without these,
# "live version" would be cancelled by a query merely containing "version".
_MARKER_STOP = frozenset(
    "at from in the by of on a an and version audio edit performed "
    "originally famous made style".split()
)


def normalise(text: str) -> str:
    """
    A name reduced to what a person said: case-folded, accent-stripped,
    punctuation removed, whitespace collapsed.

    NFKD then dropping combining marks, so "Beyoncé" and "Beyonce" are the
    same words — a spoken request has no accents and a catalogue is full of
    them. Case-folding rather than lowering, because it is the comparison
    Unicode defines for this (and the same one `em_labels.label_key` uses).
    """
    if not isinstance(text, str):
        return ""
    decomposed = unicodedata.normalize("NFKD", text)
    stripped = "".join(c for c in decomposed if not unicodedata.combining(c))
    spaced = _PUNCT.sub(" ", _DROP.sub("", stripped))
    return _SPACE.sub(" ", spaced).strip().casefold()


def variant_penalty(name: str, query: str) -> int:
    """
    How many variant markers a name carries that the query did not ask for.

    Asking for "bohemian rhapsody live" must not demote the live version, so
    a marker present in the query cancels itself. That is the whole reason
    this takes the query at all.
    """
    n, q = normalise(name), normalise(query)
    qwords = set(q.split())
    hits = 0
    for v in _VARIANTS:
        if v not in n:
            continue
        # Cancelled by the query naming the marker, and the match is on its
        # SIGNIFICANT WORDS rather than the whole phrase: the marker is
        # "live at" but somebody asks for "creep live", so a literal
        # substring test never cancels it.
        asked = [w for w in v.split() if w not in _MARKER_STOP]
        if asked and qwords.intersection(asked):
            continue
        hits += 1
    return hits


def name_score(name: str, query: str) -> int:
    """
    How well a name answers a query, highest first.

    A ladder rather than a ratio: exact, then starts-with, then contains,
    then all the query's words present in any order, then nothing. Edit
    distance was considered and rejected — it makes "Hello" and "Hallo"
    near-identical while a one-word query against a long title scores
    terribly, which is backwards for this job.
    """
    n, q = normalise(name), normalise(query)
    if not n or not q:
        return 0
    if n == q:
        return 100
    if n.startswith(q):
        return 80
    if q in n:
        return 60
    words = q.split()
    if words and all(w in n.split() for w in words):
        return 40
    return 0


def artist_names(item: dict) -> list[str]:
    """Every artist credited on a track or album, in order."""
    out = []
    for a in item.get("artists") or []:
        if isinstance(a, dict) and isinstance(a.get("name"), str):
            out.append(a["name"])
    return out


def _owner(item: dict) -> str:
    o = item.get("owner")
    return o.get("display_name", "") if isinstance(o, dict) else ""


class Candidate:
    """One thing that could be played, with the numbers it was ranked on."""

    __slots__ = ("kind", "uri", "name", "artists", "score", "penalty", "order")

    def __init__(self, kind, uri, name, artists, score, penalty, order):
        self.kind, self.uri, self.name = kind, uri, name
        self.artists, self.score = artists, score
        self.penalty, self.order = penalty, order

    def as_json(self) -> dict:
        return {
            "kind": self.kind,
            "uri": self.uri,
            "name": self.name,
            "artists": list(self.artists),
        }

    def __repr__(self) -> str:  # pragma: no cover - diagnostics only
        return f"<{self.kind} {self.name!r} by {self.artists} s={self.score} p={self.penalty}>"


def candidates(body: dict, query: str, kinds=KINDS) -> list[Candidate]:
    """
    Every playable item in a Spotify search response, scored.

    Absent, null and malformed sections are skipped rather than raising: this
    is somebody else's JSON over a network, and a missing `albums` key must
    not cost the results that did arrive. An item with no URI is unplayable
    and is dropped.
    """
    out: list[Candidate] = []
    for kind in kinds:
        section = body.get(kind + "s") if isinstance(body, dict) else None
        items = section.get("items") if isinstance(section, dict) else None
        if not isinstance(items, list):
            continue
        for order, item in enumerate(items):
            if not isinstance(item, dict):
                continue
            uri, name = item.get("uri"), item.get("name")
            if not isinstance(uri, str) or not isinstance(name, str) or not name:
                continue
            arts = artist_names(item)
            if kind == "playlist" and not arts:
                owner = _owner(item)
                arts = [owner] if owner else []
            # An artist's own name is the thing being matched for `artist`;
            # for everything else the credited artists are a second chance,
            # since "thriller michael jackson" names both.
            best = name_score(name, query)
            for a in arts:
                best = max(best, name_score(f"{name} {a}", query),
                           name_score(f"{a} {name}", query))
            out.append(Candidate(kind, uri, name, arts, best,
                                 variant_penalty(name, query), order))
    return out


def rank(cands: list[Candidate], prefer=KINDS, limit=MAX_LIMIT) -> list[Candidate]:
    """
    The candidates in the order they should be offered.

    Sorted on, in order: variant penalty (asked-for things before variants of
    them), name score, the caller's kind preference, then Spotify's own
    position. The sort is STABLE and every key is explicit, so the result
    cannot depend on dict ordering — the same words give the same answer
    every time, which is the property the whole module exists for.

    A zero score is dropped. Spotify matches liberally and an item that
    answers none of the words is noise, not a long shot; offering it means a
    voice request plays something unrelated rather than saying it found
    nothing.
    """
    rankof = {k: i for i, k in enumerate(prefer)}
    scored = [c for c in cands if c.score > 0]
    scored.sort(key=lambda c: (c.penalty, -c.score,
                               rankof.get(c.kind, len(rankof)), c.order))
    return scored[:max(0, limit)]


def search_params(query: str, kinds=KINDS, limit=MAX_LIMIT, market=None) -> dict:
    """
    Query parameters for `GET /v1/search`.

    `limit` is clamped to MAX_LIMIT because development mode refuses more and
    returns an error rather than fewer results — a 400 for asking politely.
    """
    q = (query or "").strip()
    if not q:
        raise ValueError("a search needs something to search for")
    wanted = [k for k in kinds if k in KINDS]
    if not wanted:
        raise ValueError("no searchable kind requested")
    params = {
        "q": q,
        "type": ",".join(wanted),
        "limit": str(max(1, min(int(limit), MAX_LIMIT))),
    }
    if market:
        params["market"] = market
    return params


def play_target(cands: list[Candidate]) -> Candidate | None:
    """
    The one thing to play, or None.

    Just the top of the ranking — separate from `rank` because the two
    answer different questions and the voice path must not re-implement the
    choice the list already made.
    """
    return cands[0] if cands else None
