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
    """Return a naive English plural; generated code accepts an explicit one instead."""
    if word.endswith(("s", "x", "z", "ch", "sh")):
        return f"{word}es"
    if word.endswith("y") and word[-2:-1] not in {"a", "e", "i", "o", "u"}:
        return f"{word[:-1]}ies"
    return f"{word}s"
