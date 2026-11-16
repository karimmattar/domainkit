"""Placeholders filled in a Word document (``domainkit[documents]``: lxml).

A placeholder is a name between double braces, ``{{ client_name }}``:
letters (Arabic ones too), digits, and ``_``, starting with a letter or
``_``, with dots between parts (``{{ client.name }}``), and spaces inside
the braces if the writer likes. Word splits a paragraph's text into runs
at will (a spelling mark, an edit, a change of language), so one
placeholder is often spread over several: ``fill_docx`` reads each
paragraph's text across its runs, and puts each value in the run where
its placeholder starts, so the value takes that run's formatting. The
text around it keeps its own runs. A line break in a value becomes a
line break in Word, a tab a tab.

The text of the body, its tables, and its text boxes is filled, and of
the headers, footers, footnotes, and endnotes; a placeholder lives in one
paragraph. The policy is strict, so a contract never goes out with
``{{name}}`` in it: a placeholder without a value, a value no placeholder
takes (unless ``allow_unused``), and braces that do not make a placeholder
(``{{ client name }}``, a lone ``}}``) are all refused at once with a
``PlaceholderError``. ``{{`` and ``}}`` have no other use.

The archive is read within ``ArchiveLimits``, so a zip bomb costs no
more than they allow: its files are stored or deflated, plain relative
names, each inflated no further than the size the archive states. The
XML is read as UTF-8 with lxml, without entities, DTDs, or anything
outside the part: a part that declares a DTD or an entity is refused
before it is parsed (Word writes none), and the parser resolves any
outside reference to nothing. It is written back with the namespace
prefixes it had: Word reads ``mc:Ignorable`` by prefix. Values are text,
escaped as they are written, so a value cannot add markup. Every other
part of the archive is written back unchanged, in its order.
"""

import io
import re
import unicodedata
import zipfile
import zlib
from bisect import bisect_right
from collections.abc import Mapping
from dataclasses import dataclass
from itertools import accumulate
from typing import Any, Final

from domainkit.documents.base import DocumentError, PlaceholderError, missing

W: Final = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
W_P: Final = f"{{{W}}}p"
W_T: Final = f"{{{W}}}t"
W_BR: Final = f"{{{W}}}br"
W_TAB: Final = f"{{{W}}}tab"
XML_SPACE: Final = "{http://www.w3.org/XML/1998/namespace}space"
CONTENT_TYPES: Final = "[Content_Types].xml"
OVERRIDE: Final = (
    "{http://schemas.openxmlformats.org/package/2006/content-types}Override"
)
WORDML: Final = "application/vnd.openxmlformats-officedocument.wordprocessingml"
MAIN: Final = f"{WORDML}.document.main+xml"
"""The content type of a Word document's body (.docx)."""
FILLED: Final = frozenset(
    {
        MAIN,
        f"{WORDML}.header+xml",
        f"{WORDML}.footer+xml",
        f"{WORDML}.footnotes+xml",
        f"{WORDML}.endnotes+xml",
    }
)
"""The parts whose text is filled."""
PLACEHOLDER: Final = re.compile(r"\{\{\s*([^\W\d]\w*(?:\.[^\W\d]\w*)*)\s*\}\}")
BRACES: Final = re.compile(r"\{\{|\}\}")
DECLARATION: Final = re.compile(rb"<!(?:DOCTYPE|ENTITY)", re.IGNORECASE)
"""What declares a DTD or an entity: refused before the XML is parsed."""
METHODS: Final = frozenset({zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED})
"""How a file of the archive may be compressed: as Word writes them."""
NAME_SEGMENT: Final = re.compile(r"[^\x00-\x1f\x7f\\:]+")
"""A segment of a file's name: no control character, backslash, or colon."""
NOT_XML: Final = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\ud800-\udfff￾￿]")
"""Characters XML 1.0 cannot hold."""
BREAKS: Final = re.compile(r"([\n\t])")
SHOWN: Final = 40
"""The most characters of a malformed placeholder an error quotes."""
GRACE: Final = 100 * 1024
"""Bytes a part may inflate to before its ratio counts (Apache POI's grace)."""
MiB: Final = 1024 * 1024


@dataclass(frozen=True, slots=True, kw_only=True)
class ArchiveLimits:
    """How much of a Word document is read: a zip bomb is refused within these.

    The defaults hold any contract or letter; a template full of large
    images may need more ``max_bytes``. The ratio is Apache POI's
    ``ZipSecureFile`` default (an entry inflated past 100 times its size,
    once past 100 KiB).
    """

    max_bytes: int = 32 * MiB
    """The document as given."""
    max_parts: int = 1000
    """Files in the archive."""
    max_part_bytes: int = 64 * MiB
    """One file, inflated."""
    max_total_bytes: int = 256 * MiB
    """Every file, inflated."""
    max_ratio: int = 100
    """Inflated size over stored size, for a file past 100 KiB."""


def fill_docx(
    document: bytes,
    values: Mapping[str, str],
    *,
    allow_unused: bool = False,
    limits: ArchiveLimits | None = None,
) -> bytes:
    """``document`` (a .docx) with each ``{{ name }}`` replaced by ``values[name]``.

    The work is CPU-bound: in async code, run it in a thread
    (``await asyncio.to_thread(fill_docx, document, values)``).

    Raises:
        ImportError: ``domainkit[documents]`` is not installed.
        TypeError: A value is not text (format numbers and dates first).
        DocumentError: The document is not a .docx, is past ``limits``,
            or holds a DTD; a value holds a character XML cannot.
        PlaceholderError: Placeholders and values do not agree (every
            problem listed).
    """
    try:
        from lxml import etree
    except ImportError as exc:
        raise missing("lxml") from exc
    given = _checked(values)
    files = _read(document, limits or ArchiveLimits())
    parts = {info.filename: data for info, data in files}
    found: set[str] = set()
    malformed: list[str] = []
    changed: dict[str, bytes] = {}
    for name in _filled_parts(etree, parts):
        root = _parse(etree, parts[name], name)
        if _fill(etree, root, given, found, malformed):
            changed[name] = etree.tostring(
                root.getroottree(),
                xml_declaration=True,
                encoding="UTF-8",
                standalone=root.getroottree().docinfo.standalone,
            )
    valueless = found - given.keys()
    unused = set() if allow_unused else given.keys() - found
    if valueless or unused or malformed:
        raise PlaceholderError(missing=valueless, unused=unused, malformed=malformed)
    return _written(files, changed)


def _checked(values: Mapping[str, str]) -> dict[str, str]:
    """``values``, each text XML can hold, with Windows line ends made Unix ones.

    Raises:
        TypeError: A value is not text.
        DocumentError: A value holds a character XML cannot.
    """
    checked = {}
    for name, value in values.items():
        if not isinstance(value, str):
            raise TypeError(
                f"The value of {name!r} is a {type(value).__name__}, not text: "
                "format it first."
            )
        if NOT_XML.search(value):
            raise DocumentError(
                f"The value of {name!r} holds a control character a Word "
                "document cannot."
            )
        checked[name] = value.replace("\r\n", "\n").replace("\r", "\n")
    return checked


def _read(
    document: bytes, limits: ArchiveLimits
) -> list[tuple[zipfile.ZipInfo, bytes]]:
    """Every file of the archive, in its order, within ``limits``.

    The sizes are checked as the archive states them, before anything is
    inflated; then each file is inflated no further than its stated size,
    and ``zipfile`` refuses one whose CRC-32 then differs.

    Raises:
        DocumentError: It is not a ZIP archive, is past a limit, is
            damaged, or has a file encrypted, compressed otherwise than
            stored or deflated, named other than by a plain relative path,
            or named as another is.
    """
    if len(document) > limits.max_bytes:
        raise DocumentError(f"The document is larger than {limits.max_bytes:,} bytes.")
    try:
        archive = zipfile.ZipFile(io.BytesIO(document))
    except (zipfile.BadZipFile, ValueError):
        raise DocumentError("The document is not a Word document (.docx).") from None
    with archive:
        infos = archive.infolist()
        _check_entries(infos, limits)
        try:
            return [(info, _inflated(archive, info)) for info in infos]
        except (zipfile.BadZipFile, EOFError, OSError, zlib.error):
            raise DocumentError("The document's archive is damaged.") from None


def _inflated(archive: zipfile.ZipFile, info: zipfile.ZipInfo) -> bytes:
    """One file of the archive, inflated no further than its stated size.

    ``ZipFile.read`` asks the decompressor for up to 2 GiB at once, and
    cuts to the stated size only after: a file that states 10 bytes and
    inflates to gigabytes would be inflated whole. Asked for its stated
    size, the decompressor stops there, and the CRC-32 is checked.
    """
    with archive.open(info) as part:
        return part.read(info.file_size)


def _check_entries(infos: list[zipfile.ZipInfo], limits: ArchiveLimits) -> None:
    """Refuse an archive whose files are too many, too large, or inflate too much.

    Raises:
        DocumentError: One is past ``limits``, encrypted, compressed
            otherwise than stored or deflated, oddly named, or named twice.
    """
    if len(infos) > limits.max_parts:
        raise DocumentError(f"The document holds more than {limits.max_parts:,} files.")
    if not all(_plain(info.filename) for info in infos):
        raise DocumentError(
            "The document holds a file whose name is not a plain relative path."
        )
    names = [unicodedata.normalize("NFKC", info.filename).casefold() for info in infos]
    if len(set(names)) != len(names):
        raise DocumentError("The document holds two files of one name.")
    if any(info.flag_bits & 0x1 for info in infos):
        raise DocumentError("The document holds an encrypted file.")
    if any(info.compress_type not in METHODS for info in infos):
        raise DocumentError(
            "The document holds a file compressed otherwise than stored or deflated."
        )
    if any(info.file_size > limits.max_part_bytes for info in infos) or (
        sum(info.file_size for info in infos) > limits.max_total_bytes
    ):
        raise DocumentError(
            f"The document inflates past {limits.max_part_bytes:,} bytes in a "
            f"file, or {limits.max_total_bytes:,} in all."
        )
    if any(
        info.file_size > GRACE
        and info.file_size > limits.max_ratio * max(info.compress_size, 1)
        for info in infos
    ):
        raise DocumentError(
            f"A file of the document inflates more than {limits.max_ratio} times."
        )


def _plain(name: str) -> bool:
    """Whether ``name`` is a plain relative path: ``word/document.xml``, ``word/``.

    No leading ``/``, no empty, ``.``, or ``..`` segment, and no control
    character, backslash, or colon (a drive, a stream).
    """
    segments = name.removesuffix("/").split("/")
    return all(
        segment not in {".", ".."} and NAME_SEGMENT.fullmatch(segment)
        for segment in segments
    )


def _filled_parts(etree: Any, parts: Mapping[str, bytes]) -> list[str]:  # noqa: ANN401 - lxml has no types.
    """The names of the parts whose text is filled, from ``[Content_Types].xml``.

    Raises:
        DocumentError: The archive is no .docx: it has no content types, or
            its body is a template's or holds macros.
    """
    by_lower = {name.lower(): name for name in parts}
    if CONTENT_TYPES.lower() not in by_lower:
        raise DocumentError("The document is not a Word document (.docx).")
    root = _parse(etree, parts[by_lower[CONTENT_TYPES.lower()]], CONTENT_TYPES)
    typed = {
        (override.get("PartName") or "").lstrip("/").lower(): override.get(
            "ContentType"
        )
        for override in root.iter(OVERRIDE)
    }
    if MAIN not in typed.values():
        raise DocumentError(
            "The document is not a Word document (.docx): save it as one, not "
            "as a template or with macros."
        )
    return [
        by_lower[name]
        for name, kind in typed.items()
        if kind in FILLED and name in by_lower
    ]


def _parse(etree: Any, data: bytes, name: str) -> Any:  # noqa: ANN401 - lxml has no types.
    """The root element of the part ``name``: no entities, DTD, or outside file.

    A DTD or an entity is refused before parsing: lxml without entities or
    a DTD still opens an outside DTD subset, or an outside parameter
    entity, a file that may be a pipe that never ends. The parser also
    resolves any outside reference to nothing, and reads UTF-8 only, as
    Word writes, so the check above sees what it parses.

    Raises:
        DocumentError: It is not well-formed UTF-8 XML, or declares a DTD
            or an entity.
    """
    if DECLARATION.search(data):
        raise DocumentError(
            f"{name} in the document declares a DTD or an entity; Word writes none."
        )
    try:
        root = etree.fromstring(data, _parser(etree))
    except etree.XMLSyntaxError:
        raise DocumentError(f"{name} in the document is not well-formed XML.") from None
    return root


def _parser(etree: Any) -> Any:  # noqa: ANN401 - lxml has no types.
    """An lxml parser that reads nothing beyond the bytes it is given."""

    class Nothing(etree.Resolver):  # type: ignore[misc]
        """Resolves every outside reference to an empty one: nothing is opened."""

        def resolve(self, url: str, public_id: str, context: Any) -> Any:  # noqa: ANN401
            return self.resolve_string("", context)

    parser = etree.XMLParser(
        encoding="utf-8",
        resolve_entities=False,
        no_network=True,
        load_dtd=False,
        dtd_validation=False,
        huge_tree=False,
        collect_ids=False,
    )
    parser.resolvers.add(Nothing())
    return parser


def _fill(
    etree: Any,  # noqa: ANN401 - lxml has no types.
    root: Any,  # noqa: ANN401 - lxml has no types.
    values: Mapping[str, str],
    found: set[str],
    malformed: list[str],
) -> bool:
    """Fill each paragraph under ``root``; whether any text changed.

    Adds the names of the placeholders met to ``found``, and the text of
    braces that make no placeholder to ``malformed``.
    """
    changed = False
    for texts in _paragraphs(etree, root):
        joined = "".join(text.text or "" for text in texts)
        if "{{" not in joined and "}}" not in joined:
            continue
        matches = list(PLACEHOLDER.finditer(joined))
        if stray := _stray(joined, matches):
            malformed.append(stray)
        found.update(match[1] for match in matches)
        known = [match for match in matches if match[1] in values]
        if known:
            _replace(texts, known, values)
            changed = True
    return changed


def _paragraphs(etree: Any, root: Any) -> list[list[Any]]:  # noqa: ANN401 - lxml.
    """Each paragraph's own ``w:t``, in one walk of the tree.

    A text belongs to its nearest paragraph: a text box's paragraphs are
    their own, though they sit inside a run of another. One walk keeps
    the work linear however deep paragraphs nest.
    """
    paragraphs: list[list[Any]] = []
    open_: list[list[Any]] = []
    for event, element in etree.iterwalk(root, events=("start", "end"), tag=(W_P, W_T)):
        if element.tag == W_T:
            if event == "start" and open_:
                open_[-1].append(element)
        elif event == "start":
            open_.append([])
            paragraphs.append(open_[-1])
        else:
            open_.pop()
    return paragraphs


def _stray(joined: str, matches: list[re.Match[str]]) -> str | None:
    """The text around the first brace of ``joined`` no placeholder holds, if any.

    Up to ``SHOWN`` characters, from an opening brace on, or up to a
    closing one.
    """
    starts = [match.start() for match in matches]
    for brace in BRACES.finditer(joined):
        held = bisect_right(starts, brace.start()) - 1
        if held >= 0 and brace.start() < matches[held].end():
            continue
        if brace[0] == "{{":
            return joined[brace.start() : brace.start() + SHOWN]
        return joined[max(0, brace.end() - SHOWN) : brace.end()]
    return None


def _replace(
    texts: list[Any],
    matches: list[re.Match[str]],
    values: Mapping[str, str],
) -> None:
    """Put each match's value where it starts; take its text out of the others.

    ``matches`` are offsets into the joined text of ``texts``, in order;
    they are replaced from the last, so the offsets of the earlier ones hold.
    """
    contents = [text.text or "" for text in texts]
    starts = list(accumulate((len(content) for content in contents), initial=0))
    touched: set[int] = set()
    for match in reversed(matches):
        first = bisect_right(starts, match.start()) - 1
        last = bisect_right(starts, match.end() - 1) - 1
        head = contents[first][: match.start() - starts[first]]
        tail = contents[last][match.end() - starts[last] :]
        value = values[match[1]]
        if first == last:
            contents[first] = head + value + tail
        else:
            contents[first] = head + value
            for between in range(first + 1, last):
                contents[between] = ""
            contents[last] = tail
        touched.update(range(first, last + 1))
    for index in sorted(touched):
        _write(texts[index], contents[index])


def _write(text: Any, content: str) -> None:  # noqa: ANN401 - lxml has no types.
    """Set a ``w:t``'s text, a line break or a tab after it for each in ``content``."""
    pieces = BREAKS.split(content)
    _set(text, pieces[0])
    anchor = text
    for mark, piece in zip(pieces[1::2], pieces[2::2], strict=True):
        element = text.makeelement(W_BR if mark == "\n" else W_TAB, {})
        anchor.addnext(element)
        anchor = element
        if piece:
            following = text.makeelement(W_T, {})
            _set(following, piece)
            anchor.addnext(following)
            anchor = following


def _set(text: Any, content: str) -> None:  # noqa: ANN401 - lxml has no types.
    """Set a ``w:t``'s text, its spaces kept (``xml:space="preserve"``)."""
    text.text = content
    text.set(XML_SPACE, "preserve")


def _written(
    files: list[tuple[zipfile.ZipInfo, bytes]], changed: Mapping[str, bytes]
) -> bytes:
    """The archive again: each file in its order and with its date, changed or not."""
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as archive:
        for info, data in files:
            written = zipfile.ZipInfo(info.filename, date_time=info.date_time)
            written.external_attr = info.external_attr
            written.compress_type = (
                zipfile.ZIP_STORED
                if info.compress_type == zipfile.ZIP_STORED
                else zipfile.ZIP_DEFLATED
            )
            archive.writestr(written, changed.get(info.filename, data))
    return out.getvalue()
