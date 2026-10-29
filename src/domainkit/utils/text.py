"""Name conversions for error codes and generated code."""

import re
from typing import Final

_WORD_BOUNDARY: Final = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")


def snake_case(name: str) -> str:
    """Convert ``ProductNotFound`` to ``product_not_found`` (acronyms stay whole)."""
    return _WORD_BOUNDARY.sub("_", name).replace("-", "_").lower()


def camel_case(name: str) -> str:
    """Convert ``order_line`` to ``OrderLine``."""
    return "".join(part[:1].upper() + part[1:] for part in name.split("_") if part)


def pluralize(word: str) -> str:
    """Return a naive English plural; generated code accepts an explicit one instead.

    Routes and the admin's paths take their default plural from it, so it
    never changes; the generators use ``plural_of``.
    """
    if word.endswith(("s", "x", "z", "ch", "sh")):
        return f"{word}es"
    if word.endswith("y") and word[-2:-1] not in {"a", "e", "i", "o", "u"}:
        return f"{word[:-1]}ies"
    return f"{word}s"


_SAME_IN_BOTH: Final = frozenset(
    {"news", "series", "species", "means", "analytics", "logistics", "economics"}
)
"""Nouns whose plural is their singular: ``pluralize`` names their many."""
_SINGULAR_IN_S: Final = frozenset(
    {"alias", "atlas", "bias", "bonus", "bus", "campus", "canvas", "census"}
    | {"gas", "lens", "surplus", "status", "virus"}
)
"""Singular nouns that end in a single ``s``, not ``ss``, ``us`` or ``is``."""
_ONLY_S: Final = frozenset(
    {"aches", "caches", "cookies", "headaches", "lies", "movies", "niches", "pies"}
    | {"ties", "zombies"}
)
"""Plurals ending in ``ches`` or ``ies`` that only add an ``s``."""


def singular_of(word: str) -> str:
    """The singular of a plural noun, for generated code: ``notes``, ``note``.

    Read in English from its end: ``categories``, ``category``; ``boxes``,
    ``box``; ``statuses``, ``status``. A word that does not read as a plural
    (``status``, ``class``, ``analysis``, ``news``) is returned as it is.
    The singular is always one whose ``pluralize`` is ``word``.
    """
    last = word.rpartition("_")[2]
    if (
        not last.endswith("s")
        or last.endswith(("ss", "us", "is"))
        or last in _SAME_IN_BOTH | _SINGULAR_IN_S
    ):
        return word
    if last.endswith("ies") and last not in _ONLY_S:
        candidate = f"{word[:-3]}y"
    elif last[:-2] in _SINGULAR_IN_S or (
        last.endswith(("sses", "xes", "ches", "shes")) and last not in _ONLY_S
    ):
        candidate = word[:-2]
    else:
        candidate = word[:-1]
    if candidate[-1:] in {"", "_"} or pluralize(candidate) != word:
        return word
    return candidate


def plural_of(word: str) -> str:
    """The plural of a noun, for generated code: ``order``, ``orders``.

    ``pluralize``'s, except for a word that already reads as a plural
    (``notes``), which is its own.
    """
    return word if singular_of(word) != word else pluralize(word)
