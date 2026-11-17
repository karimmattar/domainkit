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
        "\u2022\ua4f8\ua60e\ua78f\uabec\U00010a50\U0001d16d\U0001ecae",
        ".",
    )
)
"""Characters read as a full stop, once in NFKC: UTS #46's label separators,
the dots UTS #39 says look like one, and the middle dots, a bullet among them."""
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
PRICE: Final = re.compile(
    r"(?:\d{1,3}(?P<sep>[., ])\d{3}(?:(?P=sep)\d{3})*|\d+)(?:(?P<point>[.,])\d{2})?"
)
"""An amount, whole: digits in groups of three after the first, by one mark,
or ungrouped, and two digits of cents if any (``1.234.567,89``, ``1250000.00``)."""
CODES: Final = 3, 8
"""How many letters a currency's code holds: ISO 4217's three, up to eight."""
CODE_JOINERS: Final = "-_"
"""What joins a code's letters to its digits: ``CVE-2024-12345``."""
CODE_RUNS: Final = 2
"""The most runs of digits a code joined by a hyphen holds: ``CVE-2024-12345``."""
DIALING: Final = re.compile(
    r"(?<![^\W\d_])(?:call|phone|mobile|dial|fax|whatsapp|telephone"
    r"|(?:tel|ext|cell|sms)(?![^\W\d_]))"
    "|\u0627\u062a\u0635|\u0647\u0627\u062a\u0641"
    "|\u062c\u0648\u0627\u0644|\u0648\u0627\u062a\u0633"
    "|\u062a\u0644\u064a\u0641\u0648\u0646|\u0645\u0648\u0628\u0627\u064a\u0644"
    "|[\u260e\u260f\U0001f4de\U0001f4f1\U0001f4f2]"
)
"""A word that asks for a call: ``call``, ``tel``, ``ext``, ``WhatsApp``, and
Arabic's call, telephone, mobile and WhatsApp, with what they are joined to;
or a telephone's sign (a receiver, a mobile phone)."""
SHORTEST_CUT: Final = 20
"""The fewest characters of a name a screen shows when it cuts it (``check_typed``)."""
DIGITS: Final = re.compile(
    "(?:\\+\\s?)?\\(?\\d(?:(?:,|[\\s.\\-_\u2010-\u2015\u2022\u2212\u2800()/]{0,3})\\d)*"
)
"""Digits written as one number: in groups apart by a comma, or by spaces (a
blank braille cell too), dots, dashes, bullets, underscores, brackets, or
slashes, after a plus sign if any (``+1 (555) 010-0100``)."""

type Value = object
"""What a label shows: a text, or a JSON value (a number, ``True``, ``None``,
a dict) on one line, or a list or a tuple of them over lines."""
type Details = Mapping[str, Value] | Iterable[tuple[str, Value]]
"""Labelled values, in the order a screen shows them."""
type Line = tuple[str, str, int]
"""A line laid out: its label, its text, and which value it shows (-1 for none)."""


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
    (``named``), which never locks what it names: a group's name is
    checked where it is typed (``check_typed``), and a user's username or
    address is an identifier, set as one.
    """

    __slots__ = ()


type TypedCheck = Callable[[str], None]
"""A check of text a person typed for an act: raise to refuse the act."""

on_typed_text: Final[HookPoint[TypedCheck]] = HookPoint(
    "typed text", arguments=1, awaited=False
)
"""Register a check of text a person typed for an act (since 2.8).

``laid_out`` calls it with each ``Typed`` value and item, as given, when
a screen is built, whatever ``AUTH.AUTHORIZATIONS.TYPED_LINKS`` says, and
``check_typed`` with text typed where a screen shows it later; raise to
refuse the act. The framework's acts mark an impersonation's reason, a
group's new name, the text fields a change sets on a user (but its
identifiers), a rejection's or a cancellation's reason, and an
impersonation's reason held for approval. One text may be checked more
than once (a held impersonation's twin builds two screens, and the act
checks it again; a group's name is checked at its twin and at the
change): a check reads, and has no side effect.
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
    one number (``+1 555 010 0100``), but a date, an amount (with its
    cents, or in groups of three beside a currency: ``1.234.567,89``,
    ``12 500 000 EUR``), or a code (``INC0012345``, ``CVE-2024-12345``):
    when unsure, it holds one, and never an amount or a code with a plus
    sign, a bracket, or beside a word that asks for a call. ``v1.2``,
    ``Q3 report``, and ``readers`` hold none.
    """
    seen = _as_read(text)
    plain = seen.casefold()
    for pattern, meant in DEFANGED:
        plain = pattern.sub(meant, plain)
    return bool(
        SCHEME.search(plain)
        or ADDRESS.search(plain)
        or any(_is_ipv4(found[1]) for found in IPV4.finditer(plain))
        or any(_is_domain(labels) for labels in _names(plain))
        or _calls(seen)
    )


def check_typed(text: str) -> None:
    """Run the checks a screen runs on ``text``, which a person typed (since 2.8).

    For text typed where no screen shows it yet, which one will (a
    group's name, given without ``AUTH.ADMIN_ACTION``, is shown when a
    grant of it is approved): refused where it is typed, as ``laid_out``
    refuses it as ``Typed``. A screen may show it cut, ended with ``…``,
    past its ``SHORTEST_CUT``-th character: each such cut is checked too
    (``evil.corporate`` cut to ``evil.co…``).

    Raises:
        LinkNotShown: ``TYPED_LINKS`` is ``"refused"``, and it or a cut of
            it holds a link.
        Exception: What an ``on_typed_text`` check raises.
    """
    shown = _printable(text)
    cuts = (shown[:end] + CUT for end in range(SHORTEST_CUT, len(shown)))
    if _links_refused() and (holds_link(text) or any(map(holds_link, cuts))):
        raise LinkNotShown
    _check_by_hooks(text)


def _check_typed(values: Iterable[object]) -> None:
    """Each ``Typed`` value of ``values``, or item of one, checked as typed.

    Raises:
        LinkNotShown: ``TYPED_LINKS`` is ``"refused"`` and one holds a link.
        Exception: What an ``on_typed_text`` check raises.
    """
    refused = None
    for value in values:
        for text in _typed_in(value):
            refused = _links_refused() if refused is None else refused
            if refused and holds_link(text):
                raise LinkNotShown
            _check_by_hooks(text)


def _check_shown(values: list[object], lines: list[tuple[str, str, int]]) -> None:
    """Refuse a line that shows typed text and holds a link, as it is shown.

    Cut, escaped, or joined, a line may read as a link its text did not
    (``evil.corporate`` cut to ``evil.co…``).

    Raises:
        LinkNotShown: ``TYPED_LINKS`` is ``"refused"`` and such a line
            holds a link.
    """
    typed = {index for index, value in enumerate(values) if any(_typed_in(value))}
    shown = (text for _, text, index in lines if index in typed)
    if typed and _links_refused() and any(map(holds_link, shown)):
        raise LinkNotShown


def _typed_in(value: object) -> Iterator[Typed]:
    """``value`` if it is ``Typed``, or each ``Typed`` item of a list of them."""
    for each in value if isinstance(value, list | tuple) else (value,):
        if isinstance(each, Typed):
            yield each


def _links_refused() -> bool:
    """Whether typed text that holds a link is refused (``TYPED_LINKS``)."""
    return auth_settings().AUTHORIZATIONS.TYPED_LINKS == "refused"


def _check_by_hooks(text: str) -> None:
    """Run each ``on_typed_text`` check on ``text``, a plain ``str``.

    Raises:
        Exception: What a check raises.
    """
    for hook in on_typed_text:
        hook.call(str(text))


def _as_read(text: str) -> str:
    """``text`` as a person reads it, for ``holds_link``: see there; case kept."""
    plain = IGNORED.sub("", unicodedata.normalize("NFKC", text))
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

    When unsure, it does. Not a date; nor an amount or a code, but where
    the number holds a plus sign or a bracket, or the text a word that
    asks for a call (``DIALING``): see ``_amount`` and ``_code``.
    """
    dialing = _dialing(text)
    text = DATE.sub(_date_or_digits, text)
    for found in DIGITS.finditer(text):
        number = found[0]
        if sum(each.isdigit() for each in number) < CALLABLE_DIGITS:
            continue
        if dialing or any(each in number for each in "+()"):
            return True
        if not (
            _amount(number, text, found.start(), found.end())
            or _code(number, text, found.start(), found.end())
        ):
            return True
    return False


def _dialing(text: str) -> bool:
    """Whether ``text`` asks for a call (``DIALING``), read as a person reads it.

    Folded as a top-level domain is (``_latin``): combining marks dropped,
    and each letter of another script read as the Latin one it looks like,
    each way it may: ``call`` and ``tel`` with a Cyrillic es or te.
    """
    folded = "".join(
        each
        for each in unicodedata.normalize("NFD", text.casefold())
        if not unicodedata.category(each).startswith("M")
    )
    letters = _confusable_letters()
    readings = {
        folded,
        *(
            "".join(letters.get(each, (each,))[way] for each in folded)
            for way in (0, -1)
        ),
    }
    return any(DIALING.search(reading) is not None for reading in readings)


def _amount(number: str, text: str, start: int, end: int) -> bool:
    """Whether ``number`` is an amount, not a number to call.

    With two digits of cents, apart by another mark than its groups':
    ``1250000.00``, ``1.234.567,89``. In groups of three alone, by commas
    (``1,250,000``, as no number to call is written), or beside a
    currency's code or sign (``12 500 000 EUR``); bare, ``612.345.678``
    and ``612 345 678`` are numbers to call in Spain.
    """
    price = PRICE.fullmatch(number)
    if price is None or (price["sep"] is not None and price["sep"] == price["point"]):
        return False
    if price["point"] is not None or price["sep"] == ",":
        return True
    return price["sep"] is not None and _priced(text, start, end)


def _code(number: str, text: str, start: int, end: int) -> bool:
    """Whether ``number`` is part of a code, not a number to call.

    Its digits touch a letter (``INC0012345``, ``GB29NWBK6016…``); or a
    hyphen or an underscore joins them to one, in two runs at most, each
    shorter than a number to call (``CVE-2024-12345``, but not
    ``JIRA-5550100123`` nor ``support-555-010-0123``).
    """
    before = text[start - 1] if start else ""
    after = text[end] if end < len(text) else ""
    if before.isalpha() or after.isalpha():
        return True
    if not (before in CODE_JOINERS and start > 1 and text[start - 2].isalpha()):
        return False
    runs = re.findall(r"\d+", number)
    marks = set(re.sub(r"\d", "", number))
    return (
        len(runs) <= CODE_RUNS
        and all(len(run) < CALLABLE_DIGITS for run in runs)
        and marks <= {before}
    )


def _priced(text: str, start: int, end: int) -> bool:
    """Whether a currency's code or sign is next to ``text[start:end]``, a space apart.

    A code is in capitals, and known (``domainkit.money.currencies``):
    ``EUR 12 500 000``, ``12 500 000 EUR``, ``$1 250 000``.
    """
    before = start - 1 if start and text[start - 1] == " " else start
    after = end + 1 if end < len(text) and text[end] == " " else end
    first = before
    while first and text[first - 1].isalpha():
        first -= 1
    last = after
    while last < len(text) and text[last].isalpha():
        last += 1
    signs = (
        text[before - 1] if before else "",
        text[after] if after < len(text) else "",
    )
    return any(sign and unicodedata.category(sign) == "Sc" for sign in signs) or any(
        _is_currency(word) for word in (text[first:before], text[after:last])
    )


def _is_currency(word: str) -> bool:
    """Whether ``word`` is a currency's code, in capitals: ``EUR``, ``EGP``."""
    if not (CODES[0] <= len(word) <= CODES[1] and word.isupper() and word.isascii()):
        return False
    from domainkit.money import currencies

    return any(each.code == word for each in currencies())


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
    that holds a link is refused (``holds_link``), and so is each line
    that shows one and holds a link as it is shown: cut, escaped, or
    joined to the next item.

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
    values = [value for _, value in pairs]
    _check_typed(values)
    entries = [(_printable(str(label)), _shown(value)) for label, value in pairs]
    lines = _whole(entries)
    if lines is None:
        if whole:
            raise TooMuchToShow
        lines = _counted(entries, more or _more_values)
    _check_shown(values, lines)
    return [(label, text) for label, text, _ in lines]


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


def _whole(entries: list[tuple[str, str | list[str]]]) -> list[Line] | None:
    """Every value whole, each list over as many lines as it takes; None if none fit."""
    lines: list[Line] = []
    for index, (label, value) in enumerate(entries):
        if isinstance(value, str):
            lines.append((label, value, index))
            continue
        if any(len(item) > DETAIL_MAX_LENGTH for item in value):
            return None
        rows = _packed(value)
        lines.extend(
            (_continued(label, number), SEPARATOR.join(row), index)
            for number, row in enumerate(rows, 1)
        )
    labels = [label for label, _, _ in lines]
    if (
        len(lines) > MAX_DETAILS
        or len(set(labels)) != len(labels)
        or not all(labels)
        or any(len(text) > DETAIL_MAX_LENGTH for line in lines for text in line[:2])
    ):
        return None
    return lines


def _counted(
    entries: list[tuple[str, str | list[str]]], more: Callable[[int], str]
) -> list[Line]:
    """What fits, each value cut, each list and the rest counted past the room.

    Every value shown has a line; the lines left go to the lists, in order.
    """
    labels = {MORE_LABEL}
    kept: list[tuple[str, str | list[str], int]] = []
    hidden = 0
    for index, (label, value) in enumerate(entries):
        cut = fitted(label)
        if not cut or cut in labels:
            hidden += 1
            continue
        labels.add(cut)
        kept.append((cut, value, index))
    if len(kept) + (1 if hidden else 0) > MAX_DETAILS:
        hidden += len(kept) - (MAX_DETAILS - 1)
        kept = kept[: MAX_DETAILS - 1]
    spare = MAX_DETAILS - len(kept) - (1 if hidden else 0)
    lines: list[Line] = []
    for label, value, index in kept:
        if isinstance(value, str):
            lines.append((label, fitted(value), index))
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
        lines.extend((name, row, index) for name, row in zip(names, rows, strict=False))
    if hidden:
        lines.append((MORE_LABEL, fitted(more(hidden)), -1))
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
