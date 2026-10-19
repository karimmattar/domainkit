"""Helpers for HTTP headers that several parts of the framework set."""

from collections.abc import Iterable
from typing import Protocol


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
