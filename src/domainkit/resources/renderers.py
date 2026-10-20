"""Lists as CSV and NDJSON, chosen by ``Accept`` (DRF's renderers).

A resource with ``exports=("csv", "ndjson")`` answers its listing in the
media type the request prefers: JSON pages as always, or every match,
streamed, as ``text/csv`` or ``application/x-ndjson``. A request that
accepts none of them is ``406``.

CSV cells that a spreadsheet would run as a formula start with an
apostrophe instead, as OWASP advises: text starting with ``=``, ``+``,
``-``, ``@``, a tab, or a carriage return, also behind spaces of any kind,
and in their full-width and small forms (U+FF1D, U+FE62). Numbers stay
numbers. NDJSON escapes the separators other than its newline that split
lines (U+0085, U+2028, U+2029), so each document is one line.
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
FORMULA: Final = (
    *("=", "+", "-", "@", "\t", "\r"),
    *("\uff1d", "\uff0b", "\uff0d", "\uff20"),  # Full-width forms.
    *("\ufe66", "\ufe62", "\ufe63", "\ufe6b"),  # Small forms.
    "\u2212",  # The minus sign.
)
"""What starts a formula; spreadsheets skip the spaces before it."""
LEADING: Final = re.compile(r"[\s\ufeff\u200b\u200c\u200d\u2060]*")
"""Spaces, and characters that show nothing, which spreadsheets skip."""
NUMBER: Final = re.compile(r"-?\d+(\.\d+)?([eE][-+]?\d+)?", re.ASCII)
LINE_BREAKS: Final = {"\u0085": "\\u0085", "\u2028": "\\u2028", "\u2029": "\\u2029"}
"""What splits lines but JSON leaves unescaped, as JSON escapes it."""


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
    head = LEADING.sub("", text, count=1)
    if (text.startswith(FORMULA) or head.startswith(FORMULA)) and not (
        NUMBER.fullmatch(head)
    ):
        return f"'{text}"
    return text


def csv_rows(rows: Iterable[Sequence[object]]) -> str:
    """``rows`` as CSV text (RFC 4180 line endings), each value a safe cell."""
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerows([cell(value) for value in row] for row in rows)
    return buffer.getvalue()


def ndjson_lines(documents: Iterable[Mapping[str, Any]]) -> str:
    """One JSON document per line, whatever line breaks its text holds."""
    return "".join(
        _one_line(json.dumps(document, ensure_ascii=False, separators=(",", ":")))
        + "\n"
        for document in documents
    )


def _one_line(text: str) -> str:
    """JSON ``text`` with the line breaks it leaves raw escaped."""
    for raw, escaped in LINE_BREAKS.items():
        text = text.replace(raw, escaped)
    return text
