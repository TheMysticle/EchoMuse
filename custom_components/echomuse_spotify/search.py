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
from difflib import SequenceMatcher

# What the Web API will be asked for, and what a caller may request. Shows
# and episodes are deliberately absent: they are valid Connect contexts but
# not music, and a podcast surfacing for "play X" is a wrong answer rather
# than a near miss.
#
# The order is also the tie-break between kinds, and it is the order a person
# means them in: "muse" is the band before it is a song called Muse, the way
# Spotify's own search box puts the artist first for an exact name. A track
# beats an album of the same name (the single, not the record), and a playlist
# comes last (see `rank`).
KINDS = ("artist", "track", "album", "playlist")

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

# Short connecting words are often confused by STT. They may distinguish
# literal titles, so only compare reduced names with at least TWO meaningful
# words, and keep Spotify's relevance order when they are equivalent.
_CONNECTING_WORDS = frozenset("a an the and at by for from in of on to with".split())


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

    Exact names and multiword names differing only in connecting words
    share the strongest tier, leaving their order to Spotify. Prefixes,
    substrings and word matches follow. A single small transcription error
    in a multiword name is a fallback; one-word titles are never fuzzed.
    """
    n, q = normalise(name), normalise(query)
    if not n or not q:
        return 0
    if n == q:
        return 100
    # The same letters with the spaces moved. A transcriber splits and joins
    # compound words freely ("every day" / "everyday", "back street boys"), and
    # without this the real track scored 0 and was dropped, leaving a lone
    # playlist that happened to be spelled the way the transcript was.
    cn, cq = n.replace(" ", ""), q.replace(" ", "")
    if cn == cq:
        return 100
    meaningful_name = [w for w in n.split() if w not in _CONNECTING_WORDS]
    meaningful_query = [w for w in q.split() if w not in _CONNECTING_WORDS]
    if len(meaningful_query) >= 2 and meaningful_name == meaningful_query:
        return 100
    if n.startswith(q):
        return 80
    if q in n:
        return 60
    # Contained across a spacing difference. Only for a query of several words
    # and some length: for one short word this would match inside the join of
    # two others.
    if len(q.split()) >= 2 and len(cq) >= 8 and cq in cn:
        return 55
    words = q.split()
    if words and all(w in n.split() for w in words):
        return 40
    if len(meaningful_query) >= 2 and len(meaningful_name) == len(meaningful_query):
        changed = [(a, b) for a, b in zip(meaningful_name, meaningful_query) if a != b]
        if len(changed) == 1:
            a, b = changed[0]
            if min(len(a), len(b)) >= 5 and SequenceMatcher(None, a, b).ratio() >= 0.88:
                return 30
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
    them), playlists last, name score, the caller's kind preference, then
    Spotify's own position.

    **Playlists come after everything else** (Wil, 2026-10-19). Somebody who
    wants a playlist says so, and that request searches playlists alone, so
    one that turns up in an open search is a stranger's collection that happens
    to share the words. It still plays when nothing else matched. Left to its
    score, a playlist named exactly what a transcript said ("i play pokemon go
    every day") outranked every real track and started with somebody else's
    first song. The sort is STABLE and every key is explicit, so the result
    cannot depend on dict ordering — the same words give the same answer
    every time, which is the property the whole module exists for.

    A zero score is dropped. Spotify matches liberally and an item that
    answers none of the words is noise, not a long shot; offering it means a
    voice request plays something unrelated rather than saying it found
    nothing.
    """
    rankof = {k: i for i, k in enumerate(prefer)}
    scored = [c for c in cands if c.score > 0]
    scored.sort(key=lambda c: (c.penalty, c.kind == "playlist", -c.score,
                               rankof.get(c.kind, len(rankof)), c.order))
    return scored[:max(0, limit)]


# What a transcriber writes for a number said aloud: "white two one one five",
# "twenty one fifteen", "twenty twenty four". Spotify's catalogue spells them as
# digits, and a search for the words finds nothing, which Home Assistant then
# reports as "an unexpected error occurred".
_ONES = {w: i for i, w in enumerate(
    "zero one two three four five six seven eight nine".split())}
_ONES["oh"] = 0
_TEENS = {w: 10 + i for i, w in enumerate(
    "ten eleven twelve thirteen fourteen fifteen sixteen seventeen eighteen nineteen".split())}
_TENS = {w: 10 * i for i, w in enumerate(
    "_ _ twenty thirty forty fifty sixty seventy eighty ninety".split()) if w != "_"}
_NUMBER_WORDS = set(_ONES) | set(_TEENS) | set(_TENS)
_COMMA_IN_NUMBER = re.compile(r"(?<=\d),(?=\d)")

# Politeness a transcript carries to the end of a request. Only removed when
# something else is left, so a song called "Please" is still searchable.
_TRAILING_POLITE = ("please", "thanks", "thank you", "for me")


def clean_query(query: str) -> str:
    """The request without trailing politeness ("white 2115 please" -> "white 2115").

    Trailing punctuation goes too. A phrase said twice at the end is a title,
    not manners ("Please Please Please" stays whole).
    """
    text = (query or "").strip()
    while True:
        stripped = re.sub(r"[\s.,!?]+$", "", text)
        low = stripped.lower()
        for tail in _TRAILING_POLITE:
            if low.endswith(" " + tail):
                rest = stripped[: -len(tail)].rstrip(" ,")
                if rest.lower().endswith(tail):
                    return stripped
                text = rest
                break
        else:
            return stripped


def spoken_numbers(query: str) -> str:
    """
    The query with number words written as digits, for a search that found nothing.

    "two one one five" is 2115 and "twenty one fifteen" is 21 and 15 side by
    side, which a person says for 2115, and "twenty twenty four" is 2024. Each
    run of number words becomes the digits of its parts joined, and a comma
    inside a number goes ("2,115"). "hundred" and "thousand" are left alone:
    reading them needs a grammar this does not have, and a wrong number is
    worse than the words. Only used as a retry, because one number word is
    often a title ("One Direction") and must reach the first search unchanged.
    """
    words = _COMMA_IN_NUMBER.sub("", query or "").split()
    out, i = [], 0
    while i < len(words):
        base = re.sub(r"[^\w-]", "", words[i]).lower()
        if base not in _NUMBER_WORDS:
            out.append(words[i]); i += 1
            continue
        digits, start = "", i
        while i < len(words):
            w = re.sub(r"[^\w-]", "", words[i]).lower()
            if w in _TENS:
                nxt = re.sub(r"[^\w-]", "", words[i + 1]).lower() if i + 1 < len(words) else ""
                if nxt in _ONES and 1 <= _ONES[nxt] <= 9:
                    digits += str(_TENS[w] + _ONES[nxt]); i += 2
                else:
                    digits += str(_TENS[w]); i += 1
            elif w in _TEENS:
                digits += str(_TEENS[w]); i += 1
            elif w in _ONES:
                digits += str(_ONES[w]); i += 1
            else:
                break
        follower = re.sub(r"[^\w-]", "", words[i]).lower() if i < len(words) else ""
        if follower in ("hundred", "thousand", "million"):
            out.extend(words[start:i])      # "one hundred" is not "1 hundred"
        else:
            out.append(digits)
    return " ".join(out)


def numeral_retry(query: str):
    """The digit form of a query to search again with, or None when it would be the same."""
    rewritten = spoken_numbers(query)
    return rewritten if rewritten and normalise(rewritten) != normalise(query) else None


# Words that say which kind of thing is wanted, when the sentence carries them
# and the intent did not already turn them into a filter.
_KIND_WORDS = {"album": "album", "playlist": "playlist", "artist": "artist"}


def split_kind(query: str):
    """
    ("album", "abbey road") for "the album abbey road" or "abbey road album".

    A kind word at either end, with something left over, names the kind: when
    somebody says album they mean an album search and when they say playlist
    they mean a playlist. A query that is only the word ("playlist") is a title,
    and a title that happens to contain one ("The Album Leaf") is the caller's
    to protect: this only reads the sentence, it does not know the catalogue.
    Returns (None, query) when there is no kind word.
    """
    words = normalise(query).split()
    if words and words[0] == "the":
        words = words[1:]
    if len(words) >= 2 and words[0] in _KIND_WORDS:
        return _KIND_WORDS[words[0]], " ".join(words[1:])
    if len(words) >= 2 and words[-1] in _KIND_WORDS:
        return _KIND_WORDS[words[-1]], " ".join(words[:-1])
    return None, query


def choose_with_kind_word(whole: list, named: list) -> list:
    """
    Which of two rankings answers "the album X" / "X playlist".

    `whole` ranks the words as spoken across every kind, `named` ranks what is
    left of them within the kind that was named. An exact title for the whole
    sentence wins, because "The Album Leaf" is a band and not a request; after
    that the named kind does, and if it found nothing the sentence as spoken is
    still searched. Pure so each branch can be tested without Home Assistant.
    """
    if whole and whole[0].score >= 100:
        return whole
    return named or whole


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
