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
if need be, and the end of its id. The act binds the whole id; what is
shown of it, as the name, is a check for the person, and never a reason
to refuse the act.

    from domainkit.contrib.auth.actions import Shown
    from domainkit.contrib.auth.screens import laid_out, named

    details = {"Member": named(member.name, member.id), "Adds": keys}
    Shown("Grant permissions", laid_out(details))

Text a person typed for the act, which a stolen session could write to
the phone, is marked ``Typed`` (since 2.8, ADR 0180): ``laid_out`` runs the
``on_typed_text`` checks on it, and with ``AUTH.AUTHORIZATIONS.TYPED_LINKS``
set to ``"refused"``, refuses one that holds a link, an email address, or a
number to call (``holds_link``; ``LinkNotShown``, ``422 link_not_shown``).
"""

import functools
import itertools
import json
import re
import unicodedata
from collections.abc import Callable, Iterable, Iterator, Mapping
from pathlib import Path
from typing import Final

from domainkit.apps.hooks import HookPoint
from domainkit.ddd import BusinessRuleViolation
from domainkit.i18n import ngettext, pgettext

from .repositories.authorizations import DETAIL_MAX_LENGTH, MAX_DETAILS
from .settings import auth_settings

__all__ = [
    "LINES",
    "LINE_LENGTH",
    "LinkNotShown",
    "TooMuchToShow",
    "Typed",
    "check_typed",
    "fitted",
    "holds_link",
    "laid_out",
    "named",
    "on_typed_text",
]

LINE_LENGTH: Final = DETAIL_MAX_LENGTH
"""The most characters (code points) a line, its label, or a title holds: 64."""
LINES: Final = MAX_DETAILS
"""The most labelled lines a screen holds: 6."""
SHOWN_ID: Final = 12
"""How many of an id's last characters ``named`` shows: a UUID's last group."""
SEPARATOR: Final = ", "
"""What joins a list's items on a line."""
MORE_LABEL: Final = "…"
"""The label of the line that says how many values are not shown."""
CUT: Final = "…"
"""What ends a text cut to fit."""
AMBIGUOUS: Final = (",", '"')
"""What a text item may not hold as is: it is quoted, so that items stay apart."""
TOP_LEVEL_DOMAINS: Final = Path(__file__).with_name("tlds-alpha-by-domain.txt")
"""IANA's list of top-level domains, as published (its version on the first line)."""
SPECIAL_USE: Final = frozenset(
    {"alt", "example", "internal", "invalid", "local", "localhost", "onion", "test"}
)
"""Top-level names kept out of the root for a use of their own: a name in one
is a link all the same (``evil.example``, ``x.onion``, ``wiki.internal``)."""
CONFUSABLE_LETTERS: Final = Path(__file__).with_name("confusable-letters.txt")
"""Letters of other scripts that read as Latin ones (Unicode's confusables, UTS #39)."""
CALLABLE_DIGITS: Final = 7
"""How many digits make a number someone could call: a local number has seven."""
LOOKALIKE_DIGITS: Final = {"0": ("0", "o"), "1": ("1", "l", "i")}
"""Digits that read as letters in a name: ``evil.c0m``."""
READ_CHOICES: Final = 8
"""How many characters of a label are read each way they look (``_latin``)."""
READINGS: Final = 256
"""How many ways to read a label are tried at most (``_latin``)."""
IGNORED: Final = re.compile(
    "[\u00ad\u034f\u061c\u115f\u1160\u17b4\u17b5\u180b-\u180f\u200b-\u200f"
    "\u202a-\u202e\u2060-\u206f\u3164\ufe00-\ufe0f\ufeff\uffa0\ufff0-\ufff8"
    "\U0001bca0-\U0001bca3\U0001d173-\U0001d17a\U000e0000-\U000e0fff]"
)
"""Characters a screen draws as nothing (Unicode's default ignorable ones)."""
DOTS: Final = str.maketrans(
    dict.fromkeys(
        "\u00b7\u0701\u0702\u1427\u16eb\u2027\u2219\u22c5\u2e31\u2e33\u3002\u30fb"
        "\ua4f8\ua60e\uabec\U00010a50\U0001d16d\U0001ecae",
        ".",
    )
)
"""Characters read as a full stop, once in NFKC: UTS #46's label separators,
the dots UTS #39 says look like one, and the middle dots."""
ZERO_DOT: Final = re.compile("(?<=[^\\W\\d_])[\u0660\u06f0](?=[^\\W\\d_])")
"""An Arabic-Indic zero, which looks like a dot, between two letters."""
DEFANGED: Final = (
    (re.compile(r" {0,3}[\[{] {0,3}(?:\.|dot) {0,3}[\]}] {0,3}"), "."),
    (re.compile(r"(?<=\w)\((?:\.|dot)\)(?=\w)"), "."),
    (re.compile(r" {0,3}[\[{] {0,3}at {0,3}[\]}] {0,3}|(?<=\w)\(at\)(?=\w)"), "@"),
    (re.compile(r"[\[({] {0,3}: {0,3}[\])}]"), ":"),
)
"""A name written so that it is not a link and still reads as one, and what
each part stands for: ``example[.]com``, ``ann[at]example.com``, ``hxxp[:]//``."""
SCHEME: Final = re.compile(
    r"(?<![\w+.\-])[^\W\d_][\w+.\-]*://"
    r"|(?<![^\W_])(?:tel|sms|callto):\s*\+?\(?\d"
    r"|(?<![^\W_])mailto:\s*\w"
)
"""A link's scheme: ``https://``, ``hxxp://``, ``tel:+1…``, ``mailto:…``."""
ADDRESS: Final = re.compile(r"@[^\W_]")
"""An address's at sign, before its domain or a handle: ``ann@…``, ``@support``."""
IPV4: Final = re.compile(r"(?<![\w.])((?:[0-9]{1,3}\.){3}[0-9]{1,3})(?![\w]|\.[0-9])")
"""Four numbers joined by dots: an address a browser goes to."""
DATE: Final = re.compile(
    r"(?<!\d)(?:(\d{4})([-./])(\d{1,2})\2(\d{1,2})|(\d{1,2})([-./])(\d{1,2})\6(\d{4}))"
    r"(?!\d)"
)
"""A date, year first or last: ``2026-11-17``, ``17.11.2026``."""
AMOUNT: Final = re.compile(r"(?<![\d.,])\d+[.,]\d{2}(?![\d.,])")
"""An amount with its cents: ``1250000.00``."""
DIGITS: Final = re.compile(
    "(?:\\+\\s?)?\\(?\\d(?:[\\s.\\-_\u2010-\u2015\u2022\u2212\u2800()/]{0,3}\\d)*"
)
"""Digits written as one number: in groups apart by spaces (a blank braille
cell too), dots, dashes, bullets, underscores, brackets, or slashes, after a
plus sign if any (``+1 (555) 010-0100``)."""

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


class LinkNotShown(BusinessRuleViolation):
    """Text typed for the act holds a link, an email address, or a number to call (422).

    ``AUTH.AUTHORIZATIONS.TYPED_LINKS`` is ``"refused"`` (since 2.8): the
    phone shows no such text that a person typed (``Typed``, ``holds_link``).
    """

    code = "link_not_shown"

    def __init__(self) -> None:
        """Say what to do: write the text without one."""
        super().__init__(
            "Text typed for this act holds a link, an email address, or a number "
            "to call, which a confirmation screen does not show: write it without one."
        )


class Typed(str):
    """Text a person typed for the act, as a screen shows it (since 2.8, ADR 0180).

    A ``str`` like any other, marked: ``laid_out`` runs the ``on_typed_text``
    checks on it, as a value or a list's item, and refuses it where it
    holds a link (``holds_link``) with ``AUTH.AUTHORIZATIONS.TYPED_LINKS``
    set to ``"refused"`` (``LinkNotShown``). Mark what the person asking
    wrote: a reason, a name given, a note. Not what the act is on
    (``named``): a name stored is checked where it was typed, and never
    locks what it names.
    """

    __slots__ = ()


type TypedCheck = Callable[[str], None]
"""A check of text a person typed for an act: raise to refuse the act."""

on_typed_text: Final[HookPoint[TypedCheck]] = HookPoint(
    "typed text", arguments=1, awaited=False
)
"""Register a check of text a person typed for an act (since 2.8).

``laid_out`` calls it with each ``Typed`` value and item, as given, when
the act is asked for, before an authorization is stored, whatever
``AUTH.AUTHORIZATIONS.TYPED_LINKS`` says; raise to refuse the act. The
framework's acts mark an impersonation's reason, a group's new name, the
text fields a change sets on a user (but its identifiers), a rejection's
or a cancellation's reason, and an impersonation's reason held for
approval.
"""


def holds_link(text: str) -> bool:
    """Whether ``text`` holds a link, an email address, or a number to call.

    Read as a person reads it: in NFKC (full-width forms), without the
    characters a screen draws as nothing (zero-width spaces), with the
    dots that look like one (UTS #46's label separators, ideographic and
    full-width; a one dot leader; a middle dot) and names written not to
    be links (``example[.]com``, ``ann[at]example.com``) read as the link
    they spell. It holds one where it holds a scheme (``https://``,
    ``tel:``, ``mailto:``), an at sign before a letter or a digit, four
    numbers joined by dots, a name ending in a top-level domain IANA
    lists (``evil.example.com``, ``www.anything``, the same with Cyrillic
    letters that read as Latin ones), or seven digits or more written as
    one number (``+1 555 010 0100``), but a date, an amount with its
    cents, or a code whose letters touch its digits (``INC0012345``).
    ``v1.2``, ``Q3 report``, and ``readers`` hold none.
    """
    plain = _as_read(text)
    return bool(
        SCHEME.search(plain)
        or ADDRESS.search(plain)
        or any(_is_ipv4(found[1]) for found in IPV4.finditer(plain))
        or any(_is_domain(labels) for labels in _names(plain))
        or _calls(plain)
    )


def check_typed(text: str) -> None:
    """Run the checks a screen runs on ``text``, which a person typed (since 2.8).

    For text typed where no screen shows it yet, which one will (a
    group's name, given without ``AUTH.ADMIN_ACTION``, is shown when a
    grant of it is approved): refused where it is typed, as ``laid_out``
    refuses it as ``Typed``.

    Raises:
        LinkNotShown: ``TYPED_LINKS`` is ``"refused"`` and it holds a link.
        Exception: What an ``on_typed_text`` check raises.
    """
    if auth_settings().AUTHORIZATIONS.TYPED_LINKS == "refused" and holds_link(text):
        raise LinkNotShown
    for hook in on_typed_text:
        hook.call(str(text))


def _check_typed(values: Iterable[object]) -> None:
    """``check_typed`` on each ``Typed`` value of ``values``, or item of one."""
    for value in values:
        for each in value if isinstance(value, list | tuple) else (value,):
            if isinstance(each, Typed):
                check_typed(each)


def _as_read(text: str) -> str:
    """``text`` as a person reads it, for ``holds_link``: see there."""
    plain = IGNORED.sub("", unicodedata.normalize("NFKC", text)).casefold()
    for pattern, meant in DEFANGED:
        plain = pattern.sub(meant, plain)
    return ZERO_DOT.sub(".", plain.translate(DOTS))


def _is_ipv4(text: str) -> bool:
    """Whether ``text``'s four numbers are each a byte: an address."""
    return all(int(each) <= 255 for each in text.split("."))


def _names(text: str) -> Iterator[list[str]]:
    """Each run of two labels or more joined by dots: ``evil.example`` as two."""
    labels: list[str] = []
    label: list[str] = []
    for char in f"{text} ":
        if char == ".":
            labels.append("".join(label))
            label = []
        elif char.isalnum() or char in "-_" or unicodedata.category(char)[0] == "M":
            label.append(char)
        else:
            labels.append("".join(label))
            label = []
            if len(labels) > 1:
                yield labels
            labels = []


def _is_domain(labels: list[str]) -> bool:
    """Whether ``labels`` name a host: ``www`` first, or a top-level domain last."""
    named = [each for each in (label.strip("-_") for label in labels) if each]
    return len(named) > 1 and ("www" in _latin(named[0]) or _is_top_level(named[-1]))


def _is_top_level(label: str) -> bool:
    """Whether ``label``, or the Latin it reads as, is a top-level domain."""
    domains = _top_level_domains()
    if any(each in domains for each in _latin(label)):
        return True
    if label.isascii():
        return False
    try:
        return "xn--" + label.encode("punycode").decode("ascii") in domains
    except UnicodeError:  # pragma: no cover - a casefolded label always encodes.
        return False


def _latin(label: str) -> Iterator[str]:
    """The Latin texts ``label`` reads as: a letter of another script as its Latin.

    Its combining marks dropped. A digit reads as itself, and as the
    letter it looks like (``0`` as ``o``) in a label that holds a letter.
    None where a character reads as no Latin letter. Each choice past the
    eighth character, and past ``READINGS`` texts, is left out.
    """
    letters = _confusable_letters()
    digits = {} if label.isdigit() else LOOKALIKE_DIGITS
    bare = "".join(
        each
        for each in unicodedata.normalize("NFD", label)
        if not unicodedata.category(each).startswith("M")
    )
    choices: list[tuple[str, ...]] = []
    for char in bare:
        read = digits.get(char) or ((char,) if char.isascii() else letters.get(char))
        if read is None:
            return
        choices.append(read if len(choices) < READ_CHOICES else read[:1])
    for each in itertools.islice(itertools.product(*choices), READINGS):
        yield "".join(each)


def _calls(text: str) -> bool:
    """Whether ``text`` holds seven digits or more written as one number.

    Not a date, an amount with its cents, or a code: digits a letter
    touches, unless after a plus sign (``INC0012345``, but ``call+1555…``).
    """
    text = AMOUNT.sub(" ", DATE.sub(_date_or_digits, text))
    for found in DIGITS.finditer(text):
        number = found[0]
        if sum(each.isdigit() for each in number) < CALLABLE_DIGITS:
            continue
        before = text[found.start() - 1] if found.start() else " "
        after = text[found.end()] if found.end() < len(text) else " "
        if number.startswith("+") or not (before.isalpha() or after.isalpha()):
            return True
    return False


def _date_or_digits(found: re.Match[str]) -> str:
    """A space for a date that could be one; the digits, as found, otherwise."""
    year, first, second = (
        (found[1], found[3], found[4]) if found[1] else (found[8], found[5], found[7])
    )
    days = sorted((int(first), int(second)))
    if 1900 <= int(year) <= 2099 and 1 <= days[0] <= 12 and days[1] <= 31:
        return " "
    return found[0]


@functools.cache
def _top_level_domains() -> frozenset[str]:
    """IANA's top-level domains, in lower case, and the special-use ones."""
    lines = TOP_LEVEL_DOMAINS.read_text(encoding="ascii").splitlines()
    listed = {line.strip().lower() for line in lines if not line.startswith("#")}
    return frozenset(listed - {""}) | SPECIAL_USE


@functools.cache
def _confusable_letters() -> dict[str, tuple[str, ...]]:
    """Each letter of another script, and the Latin ones it reads as."""
    letters: dict[str, tuple[str, ...]] = {}
    for line in CONFUSABLE_LETTERS.read_text(encoding="utf-8").splitlines():
        if line and not line.startswith("#"):
            code, *latin = line.split()
            letters[chr(int(code, 16))] = tuple(latin)
    return letters


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
    new line as a backslash and ``n``, a backslash as two). So, laid out
    whole, two different texts never show alike, nor two different lists
    of texts. A text and the JSON value it spells do: ``1`` and ``"1"``,
    ``true`` and ``"true"``, a dict and its JSON as a string. Counted, a
    list's last line ends with words ("and 4 more") an item could spell.

    ``whole``: what would be cut or left out is refused. Otherwise a text
    longer than a line is cut and ends with ``…``; a list given fewer
    lines than it takes says how many more on its last one; and with more
    values than lines, or a label twice, the last line says how many
    values are not shown (``more(count)``, "3 more values" by default).

    A ``Typed`` value or item, text a person typed, is checked first,
    whole or not (since 2.8): the ``on_typed_text`` checks run on it, and
    with ``AUTH.AUTHORIZATIONS.TYPED_LINKS`` set to ``"refused"`` one
    that holds a link is refused (``holds_link``).

    Raises:
        TooMuchToShow: With ``whole``, a label, a text, or an item is longer
            than a line, or empty for a label; a label comes twice; or the
            whole takes more than six lines.
        LinkNotShown: A ``Typed`` text holds a link, and ``TYPED_LINKS``
            is ``"refused"``.
        TypeError: A value or an item is not JSON (bytes, a set, a
            ``Decimal``): say what it is as text.
    """
    pairs = list(details.items() if isinstance(details, Mapping) else details)
    _check_typed(value for _, value in pairs)
    entries = [(_printable(str(label)), _shown(value)) for label, value in pairs]
    lines = _whole(entries)
    if lines is not None:
        return lines
    if whole:
        raise TooMuchToShow
    return _counted(entries, more or _more_values)


def named(name: str | None, subject_id: object) -> str:
    """What an act is on, on one line: its name, cut to fit if need be, and its id.

    "ann@example.com (…123456789abc)": the id's last 12 characters (a
    UUID's last group, random in UUIDs of version 4 and 7 alike), and the
    name in the 48 characters left, cut and ended with ``…`` where it does
    not fit. An empty name (a user erased) shows as "(erased)", which no
    username, address, or number can be. The act binds the whole id:
    whatever the name, this is never too much to show.
    """
    shown = name or pgettext("subject", "(erased)")
    line = pgettext("subject", "{name} ({id})")
    ident = _printable(str(subject_id))
    if len(ident) > SHOWN_ID:
        ident = CUT + ident[-SHOWN_ID:]
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
