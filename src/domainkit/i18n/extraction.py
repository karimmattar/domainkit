"""Find the messages code marks for translation, and keep catalogs up to date.

``makemessages`` reads each module with ``ast`` (nothing is imported or
run) and collects the literal strings passed to ``gettext``, ``_``,
``ngettext``, ``pgettext``, ``npgettext``, and their ``_lazy`` forms. Each
catalog then gets the messages it lacks, keeps the translations it has,
and keeps the ones no code uses any more as obsolete (``#~``) entries, so
a translation is never lost to a rename.
"""

import ast
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

from domainkit.i18n.catalogs import Message, parse_po

KEYWORDS: Final[dict[str, tuple[str, ...]]] = {
    "_": ("msgid",),
    "gettext": ("msgid",),
    "gettext_lazy": ("msgid",),
    "ngettext": ("msgid", "plural"),
    "ngettext_lazy": ("msgid", "plural"),
    "pgettext": ("context", "msgid"),
    "pgettext_lazy": ("context", "msgid"),
    "npgettext": ("context", "msgid", "plural"),
}
"""The functions that mark a message, and what their leading arguments are."""

DEFAULT_PLURALS: Final = "nplurals=2; plural=(n != 1);"
"""A new catalog's plural rule; set the language's own in its header."""


@dataclass(slots=True)
class Found:
    """A message found in code, with where it is used."""

    msgid: str
    plural: str | None = None
    context: str | None = None
    references: list[str] = field(default_factory=list)
    """``path:line`` of each use."""

    def key(self) -> tuple[str | None, str]:
        """What tells it from another message: its context and its text."""
        return self.context, self.msgid


def extract(files: Iterable[Path], root: Path) -> list[Found]:
    """The marked messages of ``files``, in the order first found.

    Only literal strings count: a message built at run time (an f-string,
    a variable) cannot be looked up in a catalog anyway. References are
    relative to ``root``.

    Raises:
        SyntaxError: A file is not valid Python.
    """
    found: dict[tuple[str | None, str], Found] = {}
    for path in sorted(files):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        where = path.relative_to(root).as_posix()
        for node in ast.walk(tree):
            message = _marked(node)
            if message is None:
                continue
            kept = found.setdefault(message.key(), message)
            if kept.plural is None:
                kept.plural = message.plural
            kept.references.append(f"{where}:{node.lineno}")  # type: ignore[attr-defined]
    return sorted(found.values(), key=_first_use)


def _first_use(message: Found) -> tuple[str, int]:
    """Where a message is first used: by file, then line."""
    path, _, line = message.references[0].rpartition(":")
    return path, int(line)


def _marked(node: ast.AST) -> Found | None:
    """The message a call marks, if ``node`` is one with literal arguments."""
    if not isinstance(node, ast.Call):
        return None
    function = node.func
    name = (
        function.id
        if isinstance(function, ast.Name)
        else function.attr
        if isinstance(function, ast.Attribute)
        else None
    )
    roles = KEYWORDS.get(name or "")
    if roles is None or len(node.args) < len(roles):
        return None
    values: dict[str, str] = {}
    for role, argument in zip(roles, node.args, strict=False):
        if not (isinstance(argument, ast.Constant) and isinstance(argument.value, str)):
            return None
        values[role] = argument.value
    if not values["msgid"]:
        return None
    return Found(values["msgid"], values.get("plural"), values.get("context"))


def merge(
    existing: Sequence[Message], found: Sequence[Found]
) -> tuple[list[tuple[Message, list[str]]], list[Message]]:
    """The catalog's entries for ``found``, and the ones no code uses any more.

    An entry keeps its translation and its fuzzy flag; a message that
    became plural gets one empty form per the header's ``nplurals``. The
    header (``msgid ""``) is kept as it is, first.

    Returns:
        The current entries with their references, header first; then the
        obsolete ones.
    """
    by_key = {(message.context, message.msgid): message for message in existing}
    header = by_key.pop((None, ""), None)
    forms = _plural_forms(header)
    current: list[tuple[Message, list[str]]] = []
    if header is not None:
        current.append((header, []))
    for message in found:
        kept = by_key.pop(message.key(), None)
        if kept is not None and (kept.plural is None) == (message.plural is None):
            msgstr = kept.msgstr
            fuzzy = kept.fuzzy
        else:
            msgstr = ("",) * (forms if message.plural is not None else 1)
            fuzzy = False
        entry = Message(message.msgid, msgstr, message.plural, message.context, fuzzy)
        current.append((entry, message.references))
    obsolete = [message for message in by_key.values() if any(message.msgstr)]
    return current, obsolete


def _plural_forms(header: Message | None) -> int:
    """How many plural forms the header's ``Plural-Forms`` names (default 2)."""
    text = header.msgstr[0] if header is not None else ""
    for line in text.splitlines():
        name, _, value = line.partition(":")
        if name.strip() == "Plural-Forms" and "nplurals=" in value:
            count = value.split("nplurals=")[1].split(";")[0].strip()
            if count.isdigit() and int(count) > 0:
                return int(count)
    return 2


def new_header(project: str, language: str) -> Message:
    """The header of a new catalog for ``language``."""
    fields = (
        f"Project-Id-Version: {project}\n"
        f"Language: {language}\n"
        "MIME-Version: 1.0\n"
        "Content-Type: text/plain; charset=UTF-8\n"
        "Content-Transfer-Encoding: 8bit\n"
        f"Plural-Forms: {DEFAULT_PLURALS}\n"
    )
    return Message("", (fields,))


def render(
    current: Sequence[tuple[Message, Sequence[str]]], obsolete: Sequence[Message]
) -> str:
    """The text of a ``.po`` catalog holding these entries."""
    blocks = [_entry(message, references) for message, references in current]
    blocks += [
        "\n".join(f"#~ {line}" for line in _entry(message, ()).splitlines())
        for message in obsolete
    ]
    return "\n\n".join(blocks) + "\n"


def _entry(message: Message, references: Sequence[str]) -> str:
    """One entry: its references, flags, context, text, and translations."""
    lines = [f"#: {reference}" for reference in references]
    if message.fuzzy:
        lines.append("#, fuzzy")
    if message.context is not None:
        lines.append(f"msgctxt {_quote(message.context)}")
    lines.append(f"msgid {_quote(message.msgid)}")
    if message.plural is None:
        lines.append(f"msgstr {_quote(message.msgstr[0])}")
    else:
        lines.append(f"msgid_plural {_quote(message.plural)}")
        lines += [
            f"msgstr[{index}] {_quote(text)}"
            for index, text in enumerate(message.msgstr)
        ]
    return "\n".join(lines)


def _quote(text: str) -> str:
    """``text`` as a ``.po`` string: escaped, one line per newline it holds."""
    escaped = (
        text.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\t", "\\t")
        .replace("\r", "\\r")
    )
    parts = escaped.split("\n")
    if len(parts) == 1:
        return f'"{parts[0]}"'
    pieces = [f"{part}\\n" for part in parts[:-1]]
    if parts[-1]:
        pieces.append(parts[-1])
    return '""\n' + "\n".join(f'"{piece}"' for piece in pieces)


def updated(
    path: Path, found: Sequence[Found], *, project: str, language: str
) -> tuple[str, int]:
    """``path``'s catalog with ``found``, and how many entries lack a translation.

    Nothing is written. A missing catalog starts with a header for
    ``language``.

    Raises:
        CatalogError: The existing catalog cannot be read.
    """
    if path.is_file():
        text = path.read_text(encoding="utf-8")
        existing = [*parse_po(text), *_obsolete(text)]
    else:
        existing = [new_header(project, language)]
    current, obsolete = merge(existing, found)
    untranslated = sum(
        1 for message, _ in current if message.msgid and not message.translated
    )
    return render(current, obsolete), untranslated


def _obsolete(text: str) -> list[Message]:
    """The obsolete (``#~``) entries of a catalog, which ``parse_po`` leaves out.

    Kept, so their translations outlive a rename and come back if the code
    uses the message again.
    """
    lines = [line[3:] for line in text.splitlines() if line.startswith("#~ ")]
    return parse_po("\n".join(lines)) if lines else []
