"""Lists as CSV and NDJSON, chosen by ``Accept`` (DRF's renderers).

A resource with ``exports=("csv", "ndjson")`` answers its listing in the
media type the request prefers: JSON pages as always, or every match,
streamed, as ``text/csv`` or ``application/x-ndjson``. A request that
accepts none of them is ``406``.

CSV cells that a spreadsheet would run as a formula (text starting with
``=``, ``+``, ``-``, ``@``, a tab, or a carriage return) start with an
apostrophe instead, as OWASP advises; numbers stay numbers.
"""

import csv
import io
import json
import re
from collections.abc import Iterable, Mapping, Sequence
from typing import Any, Final

from fastapi import status

from domainkit.http.errors import APIError

JSON: Final = "application/json"
CSV: Final = "text/csv"
NDJSON: Final = "application/x-ndjson"
FORMATS: Final = {"csv": CSV, "ndjson": NDJSON}
"""The export formats a resource may offer, by name."""
MAX_RANGES: Final = 32
"""The most media ranges of an ``Accept`` header that are read."""
FORMULA: Final = ("=", "+", "-", "@", "\t", "\r")
NUMBER: Final = re.compile(r"-?\d+(\.\d+)?([eE][-+]?\d+)?")


class NotAcceptable(APIError):
    """``406``: the request accepts none of the media types offered."""

    def __init__(self, offered: Sequence[str]) -> None:
        """Name what is offered, in a header the client can read."""
        super().__init__(
            status.HTTP_406_NOT_ACCEPTABLE,
            "This resource cannot answer in a media type the request accepts.",
            code="not_acceptable",
            headers={"Accept": ", ".join(offered)},
        )


class ExportTooLarge(APIError):
    """``422``: an export would hold more rows than the resource allows."""

    def __init__(self) -> None:
        """Say how to get a smaller one."""
        super().__init__(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "This export would be too large; narrow it with filters.",
            code="export_too_large",
        )


def negotiate(accept: str | None, offered: Sequence[str]) -> str:
    """The media type of ``offered`` that ``accept`` prefers.

    Without an ``Accept`` header, the first. Of equal preference, the one
    offered first. The most specific range decides each type's quality
    (``text/csv`` before ``text/*`` before ``*/*``), and ``q=0`` refuses.

    Raises:
        NotAcceptable: ``accept`` takes none of them.
    """
    if not accept or not accept.strip():
        return offered[0]
    ranges = _ranges(accept)
    best, quality = None, 0.0
    for media in offered:
        found = _quality(media, ranges)
        if found > quality:
            best, quality = media, found
    if best is None:
        raise NotAcceptable(offered)
    return best


def _ranges(accept: str) -> list[tuple[str, str, float]]:
    """``(type, subtype, q)`` of each media range, lower case."""
    ranges = []
    for part in accept.split(",")[:MAX_RANGES]:
        media, *parameters = part.split(";")
        kind, _, subtype = media.strip().lower().partition("/")
        quality = 1.0
        for parameter in parameters:
            name, _, value = parameter.strip().partition("=")
            if name.strip().lower() == "q":
                try:
                    quality = min(max(float(value), 0.0), 1.0)
                except ValueError:
                    quality = 0.0
        ranges.append((kind, subtype, quality))
    return ranges


def _quality(media: str, ranges: Iterable[tuple[str, str, float]]) -> float:
    """The quality of ``media``: that of the most specific range it falls in."""
    kind, _, subtype = media.partition("/")
    best, specificity = 0.0, -1
    for each_kind, each_subtype, quality in ranges:
        if (each_kind, each_subtype) == (kind, subtype):
            rank = 2
        elif (each_kind, each_subtype) == (kind, "*"):
            rank = 1
        elif (each_kind, each_subtype) == ("*", "*"):
            rank = 0
        else:
            continue
        if rank > specificity:
            best, specificity = quality, rank
    return best


def cell(value: object) -> str:
    """One CSV cell: JSON's text for values, safe from spreadsheet formulas."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, Mapping | list):
        return guarded(json.dumps(value, ensure_ascii=False))
    return guarded(str(value))


def guarded(text: str) -> str:
    """``text``, with an apostrophe first if a spreadsheet would run it."""
    if text.startswith(FORMULA) and not NUMBER.fullmatch(text):
        return f"'{text}"
    return text


def csv_rows(rows: Iterable[Sequence[object]]) -> str:
    """``rows`` as CSV text (RFC 4180 line endings), each value a safe cell."""
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerows([cell(value) for value in row] for row in rows)
    return buffer.getvalue()


def ndjson_lines(documents: Iterable[Mapping[str, Any]]) -> str:
    """One JSON document per line."""
    return "".join(
        json.dumps(document, ensure_ascii=False, separators=(",", ":")) + "\n"
        for document in documents
    )
