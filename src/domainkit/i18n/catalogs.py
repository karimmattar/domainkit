"""Reading ``.po`` catalogs and compiling them to ``.mo``, as GNU ``msgfmt`` does.

Translators edit ``.po`` files; ``gettext`` reads ``.mo`` files. This module
turns one into the other in Python, so no GNU tools are needed, and the
output depends only on the input: the same ``.po`` always gives the same
bytes (no hash table, entries sorted, little-endian), which is how the
shipped ``.mo`` files are checked against their sources.

Fuzzy and untranslated entries are left out, as ``msgfmt`` leaves them
out. A translation may only use the ``{placeholders}`` its message has, so
a typo in a catalog cannot break a response.
"""

import re
import string
import struct
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from email.parser import HeaderParser
from typing import Final

MAGIC: Final = 0x950412DE
"""The first four bytes of a ``.mo`` file."""
HEADER_SIZE: Final = 28
_KEYWORD: Final = re.compile(
    r'(msgctxt|msgid_plural|msgid|msgstr(?:\[(\d+)\])?)\s+(".*)'
)
_STRING: Final = re.compile(r'"((?:[^"\\]|\\.)*)"')
_ESCAPES: Final = {"\\": "\\", '"': '"', "n": "\n", "t": "\t", "r": "\r"}
_NPLURALS: Final = re.compile(r"nplurals\s*=\s*([0-9]+)")


class CatalogError(ValueError):
    """A ``.po`` catalog cannot be compiled; the message names the line."""


@dataclass(frozen=True, slots=True)
class Message:
    """One entry of a catalog."""

    msgid: str
    msgstr: tuple[str, ...]
    """The translation, or one per plural form."""
    plural: str | None = None
    context: str | None = None
    fuzzy: bool = False
    line: int = 0
    """Where the entry starts in its file (1 is the first line)."""

    @property
    def translated(self) -> bool:
        """Whether ``msgfmt`` would keep it: not fuzzy, every form filled."""
        return not self.fuzzy and all(self.msgstr)

    def key(self) -> bytes:
        """The original string the ``.mo`` file looks it up by."""
        key = self.msgid if self.plural is None else f"{self.msgid}\0{self.plural}"
        if self.context is not None:
            key = f"{self.context}\x04{key}"
        return key.encode()


class _Entry:
    """An entry while its lines are read."""

    def __init__(self, line: int, fuzzy: bool) -> None:
        """Start the entry at ``line``."""
        self.line = line
        self.fuzzy = fuzzy
        self.fields: dict[str, list[str]] = {}
        self.forms: dict[int, list[str]] = {}

    def message(self) -> Message:
        """The finished entry, checked."""
        if "msgid" not in self.fields:
            raise CatalogError(f"line {self.line}: an entry without msgid")
        plural = _join(self.fields.get("msgid_plural"))
        if plural is None:
            if self.forms or "msgstr" not in self.fields:
                raise CatalogError(f"line {self.line}: msgid needs one msgstr")
            msgstr: tuple[str, ...] = ("".join(self.fields["msgstr"]),)
        else:
            if "msgstr" in self.fields or sorted(self.forms) != list(
                range(len(self.forms))
            ):
                raise CatalogError(
                    f"line {self.line}: msgid_plural needs msgstr[0], msgstr[1], ..."
                )
            msgstr = tuple("".join(self.forms[index]) for index in sorted(self.forms))
        return Message(
            msgid="".join(self.fields["msgid"]),
            msgstr=msgstr,
            plural=plural,
            context=_join(self.fields.get("msgctxt")),
            fuzzy=self.fuzzy,
            line=self.line,
        )


def _join(parts: list[str] | None) -> str | None:
    """The string of a field, or None if the entry has none."""
    return None if parts is None else "".join(parts)


def parse_po(text: str) -> list[Message]:
    """The entries of a ``.po`` catalog, obsolete ones (``#~``) left out.

    Raises:
        CatalogError: The catalog is malformed, or holds an entry twice.
    """
    messages: list[Message] = []
    entry: _Entry | None = None
    current: list[str] | None = None
    fuzzy = False
    for number, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#~"):
            continue
        if line.startswith("#"):
            fuzzy = fuzzy or (line.startswith("#,") and "fuzzy" in _flags(line))
            continue
        keyword = _KEYWORD.fullmatch(line)
        if keyword is None:
            if current is None or not line.startswith('"'):
                raise CatalogError(f"line {number}: cannot read {line[:40]!r}")
            current.append(_unquote(line, number))
            continue
        name, index, value = keyword.groups()
        if name in {"msgctxt", "msgid"} and (entry is None or "msgid" in entry.fields):
            if entry is not None:
                messages.append(entry.message())
            entry, fuzzy = _Entry(number, fuzzy), False
        if entry is None:
            raise CatalogError(f"line {number}: {name} before msgid")
        if index is not None:
            if int(index) in entry.forms:
                raise CatalogError(f"line {number}: msgstr[{index}] twice")
            current = entry.forms[int(index)] = []
        else:
            if name in entry.fields:
                raise CatalogError(f"line {number}: {name} twice in one entry")
            current = entry.fields[name] = []
        current.append(_unquote(value, number))
    if entry is not None:
        messages.append(entry.message())
    _check_unique(messages)
    return messages


def _flags(comment: str) -> set[str]:
    """The flags of a ``#,`` comment."""
    return {flag.strip() for flag in comment[2:].split(",")}


def _unquote(text: str, number: int) -> str:
    """The value of one quoted string, its escapes resolved."""
    match = _STRING.fullmatch(text.strip())
    if match is None:
        raise CatalogError(f"line {number}: a string must be in double quotes")
    parts = []
    escaped = iter(re.split(r"\\(.)", match[1]))
    for literal in escaped:
        parts.append(literal)
        code = next(escaped, None)
        if code is not None:
            if code not in _ESCAPES:
                raise CatalogError(f"line {number}: unknown escape \\{code}")
            parts.append(_ESCAPES[code])
    return "".join(parts)


def _check_unique(messages: list[Message]) -> None:
    """Refuse a message (with its context) that appears twice."""
    seen: dict[tuple[str | None, str], int] = {}
    for message in messages:
        key = (message.context, message.msgid)
        if key in seen:
            raise CatalogError(
                f"line {message.line}: {message.msgid[:40]!r} is also on line "
                f"{seen[key]}"
            )
        seen[key] = message.line


def header(messages: Iterable[Message]) -> dict[str, str]:
    """The catalog's header fields (``Content-Type``, ``Plural-Forms``, ...).

    Raises:
        CatalogError: There is no header, it is fuzzy, or not UTF-8.
    """
    found = next((m for m in messages if _is_header(m)), None)
    if found is None:
        raise CatalogError('the catalog has no header (msgid "")')
    if found.fuzzy:
        raise CatalogError(
            f"line {found.line}: the header is fuzzy; check it, then drop the flag"
        )
    fields = dict(HeaderParser().parsestr(found.msgstr[0]).items())
    charset = fields.get("Content-Type", "").partition("charset=")[2].strip()
    if charset.lower() != "utf-8":
        raise CatalogError(
            f"line {found.line}: the header must say Content-Type: text/plain; "
            "charset=UTF-8"
        )
    return fields


def check(messages: list[Message]) -> None:
    """Refuse translations a running service could not use.

    Plural entries need as many forms as ``Plural-Forms`` says, and a
    translation may only use placeholders its message has.

    Raises:
        CatalogError: Naming the line of the first problem.
    """
    fields = header(messages)
    plurals = _NPLURALS.search(fields.get("Plural-Forms", ""))
    forms = int(plurals[1]) if plurals else 2
    for message in messages:
        if not message.translated or _is_header(message):
            continue
        if message.plural is not None and len(message.msgstr) != forms:
            raise CatalogError(
                f"line {message.line}: Plural-Forms says {forms} forms, "
                f"the entry has {len(message.msgstr)}"
            )
        allowed = _placeholders(message.msgid) | _placeholders(message.plural or "")
        for translation in message.msgstr:
            unknown = _placeholders(translation) - allowed
            if unknown:
                raise CatalogError(
                    f"line {message.line}: the translation uses "
                    f"{', '.join(sorted(unknown))}, which the message does not have"
                )


def _placeholders(text: str) -> set[str]:
    """The ``{names}`` a ``str.format`` template fills; none if it is no template."""
    try:
        return {
            "{" + name + "}"
            for _, name, _, _ in string.Formatter().parse(text)
            if name is not None
        }
    except ValueError:  # Lone braces: the text is not a template.
        return set()


def compile_mo(messages: list[Message]) -> bytes:
    """The ``.mo`` file of a catalog: its header and translated entries.

    Raises:
        CatalogError: ``check`` refused the catalog.
    """
    check(messages)
    entries = sorted(
        (message.key(), "\0".join(message.msgstr).encode())
        for message in messages
        if message.translated or _is_header(message)
    )
    count = len(entries)
    keys_at = HEADER_SIZE + 16 * count
    values_at = keys_at + sum(len(key) + 1 for key, _ in entries)
    table: list[int] = []
    values: list[int] = []
    for key, value in entries:
        table += [len(key), keys_at]
        values += [len(value), values_at]
        keys_at += len(key) + 1
        values_at += len(value) + 1
    head = struct.pack(
        "<7I", MAGIC, 0, count, HEADER_SIZE, HEADER_SIZE + 8 * count, 0, 0
    )
    offsets = struct.pack(f"<{4 * count}I", *table, *values)
    strings = b"".join(key + b"\0" for key, _ in entries)
    strings += b"".join(value + b"\0" for _, value in entries)
    return head + offsets + strings


def compile_po(text: str) -> bytes:
    """``parse_po`` then ``compile_mo``."""
    return compile_mo(parse_po(text))


def translated(messages: Iterable[Message]) -> Iterator[Message]:
    """The entries ``compile_mo`` keeps, but the header."""
    return (m for m in messages if m.translated and not _is_header(m))


def _is_header(message: Message) -> bool:
    """Whether ``message`` is the catalog's header (``msgid ""``, no context)."""
    return message.msgid == "" and message.context is None
