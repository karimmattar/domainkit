"""Helpers for HTTP headers that several parts of the framework set or read."""

import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Final, Protocol

ANY_TAG: Final = "*"
MAX_TAGS: Final = 20
"""The most entity tags an ``If-Match`` or ``If-None-Match`` may list."""
_TAG: Final = re.compile(r'\s*(W/)?"([\x21\x23-\x7e]*)"\s*')


class Headers(Protocol):
    """What ``patch_vary`` needs of headers, as Starlette's ``MutableHeaders``."""

    def get(self, key: str, default: str) -> str:
        """The value of ``key``, or ``default``."""
        ...

    def __setitem__(self, key: str, value: str) -> None:
        """Set ``key`` to ``value``."""
        ...


def patch_vary(headers: Headers, names: Iterable[str]) -> None:
    """Add ``names`` to the ``Vary`` header, once each, whatever their case.

    ``Vary: *`` already covers every header, so it is left alone.
    """
    current = [
        value.strip() for value in headers.get("Vary", "").split(",") if value.strip()
    ]
    if "*" in current:
        return
    seen = {value.lower() for value in current}
    for name in names:
        if name.lower() not in seen:
            current.append(name)
            seen.add(name.lower())
    if current:
        headers["Vary"] = ", ".join(current)


@dataclass(frozen=True, slots=True)
class EntityTag:
    """One entity tag: its opaque value, and whether it is weak (``W/``)."""

    value: str
    weak: bool = False

    def __str__(self) -> str:
        """``W/"value"`` or ``"value"``."""
        return f'{"W/" if self.weak else ""}"{self.value}"'


def parse_tags(header: str) -> list[EntityTag] | None:
    """The tags an ``If-Match`` or ``If-None-Match`` value lists; ``*`` is ``[]``.

    None for anything but ``*`` or a list of at most ``MAX_TAGS`` tags.
    """
    if header.strip() == ANY_TAG:
        return []
    tags = []
    for part in header.split(","):
        match = _TAG.fullmatch(part)
        if match is None:
            return None
        tags.append(EntityTag(match[2], weak=match[1] is not None))
    return tags if len(tags) <= MAX_TAGS else None


def none_match(header: str | None, etag: str) -> bool:
    """Whether ``If-None-Match`` names ``etag`` (or is ``*``), compared weakly.

    Weakly, ``W/"x"`` and ``"x"`` match. A malformed value matches
    nothing, so the full answer is sent.
    """
    if not header:
        return False
    tags = parse_tags(header)
    if tags is None:
        return False
    wanted = etag.removeprefix("W/").strip('"')
    return tags == [] or any(tag.value == wanted for tag in tags)
