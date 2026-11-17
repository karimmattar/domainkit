"""What an act shows, laid out on a phone's screen: whole, or refused (ADR 0173).

A screen holds what ``Shown`` holds (ADR 0144): a title, and six labelled
lines of 64 printable characters at most. ``laid_out`` puts labelled
values on those lines: a text on one line, a list over as many as it
takes, the first labelled as given and the next ones numbered
("Adds", "Adds (2)").

With ``whole=True``, what cannot be shown whole is refused
(``TooMuchToShow``, ``422 too_much_to_show``): a text or an item longer
than a line, or more lines than the screen has. Otherwise it is cut and
counted, never left out unsaid: a text cut ends with ``…``, a list's last
line says how many more ("auditors, clerks and 4 more"), and a last line
("…") says how many values are not shown.

What the act is on is named by ``named(name, id)``: its name, cut to fit
if need be, and its id, whole. The act binds the id; the name is a hint,
and never a reason to refuse it.

    from domainkit.contrib.auth.actions import Shown
    from domainkit.contrib.auth.screens import laid_out, named

    details = {"Member": named(member.name, member.id), "Adds": keys}
    Shown("Grant permissions", laid_out(details))
"""

import json
from collections.abc import Callable, Iterable, Mapping
from typing import Final

from domainkit.ddd import BusinessRuleViolation
from domainkit.i18n import ngettext, pgettext

from .repositories.authorizations import DETAIL_MAX_LENGTH, MAX_DETAILS

__all__ = ["LINES", "LINE_LENGTH", "TooMuchToShow", "fitted", "laid_out", "named"]

LINE_LENGTH: Final = DETAIL_MAX_LENGTH
"""The most characters (code points) a line, its label, or a title holds: 64."""
LINES: Final = MAX_DETAILS
"""The most labelled lines a screen holds: 6."""
SEPARATOR: Final = ", "
"""What joins a list's items on a line."""
MORE_LABEL: Final = "…"
"""The label of the line that says how many values are not shown."""
CUT: Final = "…"
"""What ends a text cut to fit."""
AMBIGUOUS: Final = (",", '"')
"""What a text item may not hold as is: it is quoted, so that items stay apart."""

type Value = object
"""What a label shows: a text, or a JSON value (a number, ``True``, ``None``,
a dict) on one line, or a list or a tuple of them over lines."""
type Details = Mapping[str, Value] | Iterable[tuple[str, Value]]
"""Labelled values, in the order a screen shows them."""


class TooMuchToShow(BusinessRuleViolation):
    """The act cannot be shown whole on one screen: make it in smaller steps (422).

    A text or an item is longer than a line, or the act needs more lines
    than a screen has (``laid_out(..., whole=True)``).
    """

    code = "too_much_to_show"

    def __init__(self) -> None:
        """Say what to do: a smaller act fits."""
        super().__init__(
            "This act cannot be shown whole on one screen: make it in smaller steps."
        )


def laid_out(
    details: Details, *, whole: bool = True, more: Callable[[int], str] | None = None
) -> list[tuple[str, str]]:
    """``details`` on a screen's lines: six labelled lines of 64 characters at most.

    A text is one line; another value is compact JSON on one line (a dict
    whole, ``1``, ``true``, ``null``). A list's or a tuple's items are
    joined with ``, `` on a line while they fit, then go on to the next
    line, labelled with its number ("Adds (2)"): a text item as itself,
    quoted if it holds a comma or a double quote, has spaces around it,
    or is empty; any other item as compact JSON. A character that is not
    printable, and a backslash, are shown escaped as Python writes them (a
    new line as a backslash and ``n``, a backslash as two), so two values
    never show alike.

    ``whole``: what would be cut or left out is refused. Otherwise a text
    longer than a line is cut and ends with ``…``; a list given fewer
    lines than it takes says how many more on its last one; and with more
    values than lines, or a label twice, the last line says how many
    values are not shown (``more(count)``, "3 more values" by default).

    Raises:
        TooMuchToShow: With ``whole``, a label, a text, or an item is longer
            than a line, or empty for a label; a label comes twice; or the
            whole takes more than six lines.
        TypeError: A value or an item is not JSON (bytes, a set, a
            ``Decimal``): say what it is as text.
    """
    pairs = details.items() if isinstance(details, Mapping) else details
    entries = [(_printable(str(label)), _shown(value)) for label, value in pairs]
    lines = _whole(entries)
    if lines is not None:
        return lines
    if whole:
        raise TooMuchToShow
    return _counted(entries, more or _more_values)


def named(name: str | None, subject_id: object) -> str:
    """What an act is on, on one line: its name, cut to fit if need be, and its id.

    "ann@example.com (6f1c9c1e-…)": the id whole, the name cut and ended
    with ``…`` where both do not fit. An empty name (a user erased) shows
    as "erased". The act binds the id: whatever the name, this is never
    too much to show.
    """
    shown = name or pgettext("subject", "erased")
    line = pgettext("subject", "{name} ({id})")
    ident = _printable(str(subject_id))
    room = LINE_LENGTH - len(line.format(name="", id=ident))
    return line.format(name=fitted(shown, room), id=ident)


def _shown(value: Value) -> str | list[str]:
    """``value`` as a screen shows it: a printable text, or printable items.

    Raises:
        TypeError: It is not JSON.
    """
    if isinstance(value, str):
        return _printable(value)
    if isinstance(value, list | tuple):
        return [_item(each) for each in value]
    return _printable(_json(value))


def _item(value: object) -> str:
    """An item of a list: a text quoted if it could be read as two, or as none.

    Raises:
        TypeError: It is not JSON.
    """
    if not isinstance(value, str):
        return _printable(_json(value))
    if not value or value != value.strip() or any(each in value for each in AMBIGUOUS):
        return '"' + _printable(value).replace('"', '\\"') + '"'
    return _printable(value)


def _json(value: object) -> str:
    """``value`` as compact JSON, keys sorted.

    Raises:
        TypeError: It is not JSON (bytes, a set, a ``Decimal``).
    """
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _whole(entries: list[tuple[str, str | list[str]]]) -> list[tuple[str, str]] | None:
    """Every value whole, each list over as many lines as it takes; None if none fit."""
    lines: list[tuple[str, str]] = []
    for label, value in entries:
        if isinstance(value, str):
            lines.append((label, value))
            continue
        if any(len(item) > DETAIL_MAX_LENGTH for item in value):
            return None
        rows = _packed(value)
        lines.extend(
            (_continued(label, number), SEPARATOR.join(row))
            for number, row in enumerate(rows, 1)
        )
    labels = [label for label, _ in lines]
    if (
        len(lines) > MAX_DETAILS
        or len(set(labels)) != len(labels)
        or not all(labels)
        or any(len(text) > DETAIL_MAX_LENGTH for line in lines for text in line)
    ):
        return None
    return lines


def _counted(
    entries: list[tuple[str, str | list[str]]], more: Callable[[int], str]
) -> list[tuple[str, str]]:
    """What fits, each value cut, each list and the rest counted past the room.

    Every value shown has a line; the lines left go to the lists, in order.
    """
    labels = {MORE_LABEL}
    kept: list[tuple[str, str | list[str]]] = []
    hidden = 0
    for label, value in entries:
        cut = fitted(label)
        if not cut or cut in labels:
            hidden += 1
            continue
        labels.add(cut)
        kept.append((cut, value))
    if len(kept) + (1 if hidden else 0) > MAX_DETAILS:
        hidden += len(kept) - (MAX_DETAILS - 1)
        kept = kept[: MAX_DETAILS - 1]
    spare = MAX_DETAILS - len(kept) - (1 if hidden else 0)
    lines: list[tuple[str, str]] = []
    for label, value in kept:
        if isinstance(value, str):
            lines.append((label, fitted(value)))
            continue
        names = [label]
        for number in range(2, spare + 2):
            name = fitted(_continued(label, number))
            if name in labels:
                break
            labels.add(name)
            names.append(name)
        rows = _counted_rows(value, len(names))
        spare -= len(rows) - 1
        lines.extend(zip(names, rows, strict=False))
    if hidden:
        lines.append((MORE_LABEL, fitted(more(hidden))))
    return lines


def _counted_rows(items: list[str], room: int) -> list[str]:
    """``items`` on ``room`` lines at most, the last saying how many more if need be."""
    cut = [fitted(each) for each in items]
    rows = _packed(cut)
    if len(rows) <= room:
        return [SEPARATOR.join(row) for row in rows]
    head = rows[: room - 1]
    rest = cut[sum(len(row) for row in head) :]
    return [*(SEPARATOR.join(row) for row in head), _and_more(rest)]


def _and_more(items: list[str]) -> str:
    """The first of ``items`` that fit on a line, and how many more."""
    for shown in range(len(items) - 1, 0, -1):
        text = _more_items(items[:shown], len(items) - shown)
        if len(text) <= DETAIL_MAX_LENGTH:
            return text
    hidden = len(items) - 1
    room = DETAIL_MAX_LENGTH - len(_more_items([""], hidden)) - len(CUT)
    return _more_items([items[0][:room] + CUT], hidden)


def _packed(items: list[str]) -> list[list[str]]:
    """``items``, in order, as many on each line as fit."""
    rows: list[list[str]] = [[]]
    for item in items:
        if rows[-1] and len(SEPARATOR.join([*rows[-1], item])) > DETAIL_MAX_LENGTH:
            rows.append([item])
        else:
            rows[-1].append(item)
    return rows


def _continued(label: str, number: int) -> str:
    """The label of a list's line ``number``: the label itself, then numbered."""
    if number == 1:
        return label
    return pgettext("continued", "{label} ({number})").format(
        label=label, number=number
    )


def _more_items(items: list[str], hidden: int) -> str:
    """``items``, joined, and the count of those ``hidden``."""
    more = ngettext("{items} and {count} more", "{items} and {count} more", hidden)
    return more.format(items=SEPARATOR.join(items), count=hidden)


def _more_values(hidden: int) -> str:
    """The last line's words: how many values are not shown."""
    more = ngettext("{count} more value", "{count} more values", hidden)
    return more.format(count=hidden)


def fitted(text: str, length: int = LINE_LENGTH) -> str:
    """``text`` as a screen shows it: printable, at most ``length`` characters.

    A character that is not printable, and a backslash, are shown escaped;
    a longer text is cut, and ends with ``…``.
    """
    printable = _printable(text)
    if len(printable) <= length:
        return printable
    return printable[: max(length - len(CUT), 0)] + CUT


def _printable(text: str) -> str:
    """``text``, a backslash and each character not printable escaped as Python does.

    A backslash is doubled, so that a backslash and ``n`` shown is a new
    line, never those two characters.
    """
    return "".join(
        each
        if each.isprintable() and each != "\\"
        else each.encode("unicode_escape").decode("ascii")
        for each in text
    )
