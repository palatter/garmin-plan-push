"""Garmin's strength exercise catalog, offline.

garminconnect ships every exercise the Garmin Connect workout editor offers:
its display name ("Goblet Squat"), its category (SQUAT) and its exercise
(GOBLET_SQUAT). A plan names exercises the way a person writes them
("push-ups", "goblet squat"), so names are matched ignoring case,
punctuation, spacing and a plural "s". Nothing here touches the network.
"""

from __future__ import annotations

import functools
import re
import unicodedata

_WORD = re.compile(r"[a-z0-9]+")


def _words(text: str) -> list[str]:
    folded = unicodedata.normalize("NFKD", text.lower())
    folded = "".join(ch for ch in folded if not unicodedata.combining(ch))
    folded = folded.replace("'", "").replace("\u2019", "")
    return [_singular(w) for w in _WORD.findall(folded)]


def _singular(word: str) -> str:
    if len(word) > 2 and word.endswith("s") and not word.endswith("ss"):
        return word[:-1]
    return word


def _key(text: str) -> str:
    return "".join(_words(text))


@functools.cache
def _catalog() -> tuple[dict[str, dict[str, str]], dict[str, dict[str, dict[str, str]]]]:
    # About 1,500 rows; loaded on the first strength step, not at import.
    from garminconnect import exercises as garmin

    by_name: dict[str, dict[str, str]] = {}
    by_category: dict[str, dict[str, dict[str, str]]] = {}
    for entry in garmin.EXERCISES:
        by_name.setdefault(_key(entry["name"]), entry)
        by_category.setdefault(_key(entry["category"]), {}).setdefault(_key(entry["name"]), entry)
    # A category also answers to its exercises' own names ("GOBLET_SQUAT"),
    # which is how `gpp pull` writes them back into a plan.
    for entry in garmin.EXERCISES:
        by_category[_key(entry["category"])].setdefault(_key(entry["exercise"]), entry)
    return by_name, by_category


def lookup(name: str, category: str | None = None) -> dict[str, str] | None:
    """The catalog entry for an exercise, or None when Garmin does not list it.

    Given a category Garmin knows, the exercise must be in that category.
    A category Garmin does not know is ignored.
    """
    by_name, by_category = _catalog()
    within = by_category.get(_key(category)) if category else None
    if within is not None:
        return within.get(_key(name))
    return by_name.get(_key(name))


def search(term: str) -> list[dict[str, str]]:
    """Entries whose name has every word of `term`, closest first."""
    wanted = _words(term)
    if not wanted:
        return []
    by_name, _ = _catalog()
    exact = _key(term)
    hits = [entry for key, entry in by_name.items() if all(w in key for w in wanted)]
    return sorted(hits, key=lambda e: (_key(e["name"]) != exact, len(e["name"]), e["name"]))
