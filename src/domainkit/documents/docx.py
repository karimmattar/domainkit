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
``{{name}}`` in it: a placeholder without a value (unless
``allow_missing``), a value no placeholder takes (unless
``allow_unused``), and braces that do not make a placeholder
(``{{ client name }}``, a lone ``}}``; unless ``allow_malformed``) are
all refused at once with a ``PlaceholderError``. ``{{`` and ``}}`` have
no other use. ``allow_missing`` and ``allow_malformed`` make a draft:
what they let through is left as written, for a preview to show what is
left to fill or fix.

``placeholders`` lists a document's placeholders, and the braces that
make none, as ``fill_docx`` reads them: the same parts, paragraphs, and
grammar, within the same ``ArchiveLimits``.

``afill_docx`` and ``aplaceholders`` are their async forms (New in 2.10):
at most ``DOCUMENTS["DOCX_AT_ONCE"]`` calls of them run at once in the
process, whichever event loop or thread made them, and the others wait
for a place without holding a thread. The bounds below hold one call;
calls at once add up.

The archive is read within ``ArchiveLimits``, so a zip bomb costs no
more than they allow: its files are stored or deflated, plain relative
names, each inflated no further than the size the archive states. Each
is checked a piece at a time, then inflated whole only when it is
parsed or written back, one at a time. The XML parsed is bounded in all
before it is parsed, by its bytes and by the nodes they can make, and
so is what ``fill_docx`` writes in it (New in 2.8): lxml keeps up to
340 bytes of memory a node, some 90 times the bytes of the densest XML.
Each call parses in a thread of its own, which ends with it, and takes
with it the names lxml keeps for the thread. The XML is read as UTF-8
with lxml, without entities, DTDs, or anything outside the part: a part
that declares a DTD or an entity is refused before it is parsed (Word
writes none), and the parser resolves any outside reference to nothing.
It is written back with the namespace prefixes it had: Word reads
``mc:Ignorable`` by prefix. Values are text, escaped as they are
written, so a value cannot add markup. Every other part of the archive
is written back unchanged, in its order.
"""

import asyncio
import io
import os
import re
import struct
import threading
import unicodedata
import zipfile
import zlib
from bisect import bisect_right
from collections.abc import Callable, Iterator, Mapping
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass
from functools import cache, partial
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
FILLED: Final = (
    MAIN,
    f"{WORDML}.header+xml",
    f"{WORDML}.footer+xml",
    f"{WORDML}.footnotes+xml",
    f"{WORDML}.endnotes+xml",
)
"""The parts whose text is filled, in the order they are read."""
PLACEHOLDER: Final = re.compile(r"\{\{\s*([^\W\d]\w*(?:\.[^\W\d]\w*)*)\s*\}\}")
BRACES: Final = re.compile(r"\{\{|\}\}")
NUMBERS: Final = re.compile(r"([0-9]+)")
DECLARATION: Final = re.compile(rb"<!(?:DOCTYPE|ENTITY)", re.IGNORECASE)
"""What declares a DTD or an entity: refused before the XML is parsed."""
END: Final = b"PK\x05\x06"
END_SIZE: Final = 22
"""The ZIP end-of-central-directory record, and its size without a comment."""
ZIP64_LOCATOR: Final = b"PK\x06\x07"
ZIP64_END: Final = b"PK\x06\x06"
DIRECTORY_BYTES: Final = 1024
"""Bytes of the central directory allowed per file: a Word document's names
and extra fields take a tenth of it."""
NOT_UTF_8: Final = (b"\xff\xfe", b"\xfe\xff", b"\x00\x00\xfe\xff", b"<\x00", b"\x00<")
"""How a part in UTF-16 or UTF-32 starts: with its byte order mark, or not."""
METHODS: Final = frozenset({zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED})
"""How a file of the archive may be compressed: as Word writes them."""
NAME_SEGMENT: Final = re.compile(r"[^\x00-\x1f\x7f\\:]+")
"""A segment of a file's name: no control character, backslash, or colon."""
NOT_XML: Final = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\ud800-\udfff\ufffe\uffff]")
"""Characters XML 1.0 cannot hold."""
BREAKS: Final = re.compile(r"([\n\t])")
BREAK_NODES: Final = 3
"""What a line break or tab of a value adds once written: its element, the
``w:t`` of the text after it, and that text's ``xml:space``."""
SHOWN: Final = 40
"""The most characters of a malformed placeholder an error quotes."""
GRACE: Final = 100 * 1024
"""Bytes a part may inflate to before its ratio counts (Apache POI's grace)."""
MiB: Final = 1024 * 1024
CHUNK: Final = MiB
"""Bytes of a file inflated at a time when it is only checked."""
DAMAGED: Final = (zipfile.BadZipFile, EOFError, OSError, zlib.error)
"""What ``zipfile`` raises for a file it cannot inflate as the archive states."""


@dataclass(frozen=True, slots=True, kw_only=True)
class Placeholders:
    """What a Word document holds to fill, as ``fill_docx`` reads it."""

    names: tuple[str, ...]
    """Each placeholder's name, once, in the order written: the body first,
    then the headers, footers, footnotes, and endnotes, each kind by part
    name (``header2`` before ``header10``)."""
    malformed: tuple[str, ...]
    """Text of braces that make no placeholder, in that order, as
    ``PlaceholderError.malformed`` quotes it: ``fill_docx`` refuses it unless
    ``allow_malformed``."""


@dataclass(frozen=True, slots=True, kw_only=True)
class ArchiveLimits:
    """How much of a Word document is read: a zip bomb is refused within these.

    The defaults hold any contract or letter; a template full of large
    images may need more ``max_bytes``. The ratio is Apache POI's
    ``ZipSecureFile`` default (an entry inflated past 100 times its size,
    once past 100 KiB). The XML parsed, the content types and the parts
    filled, and what ``fill_docx`` writes in it, are bounded in all before
    any is parsed or written, by ``max_xml_bytes`` and ``max_nodes``.
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
    max_xml_bytes: int = 12 * MiB
    """The XML parsed, inflated, and the values ``fill_docx`` writes in it,
    in UTF-8, in all (New in 2.8)."""
    max_nodes: int = 1_000_000
    """The nodes the XML parsed may make, the placeholders its text may
    hold, and the elements its values add, in all (New in 2.8). The nodes
    are counted on its bytes before it is parsed: each ``<`` twice (a tag,
    and the text before it), each ``=`` once (an attribute). A paragraph's
    ``{{`` counts once more each, before its placeholders are read; each
    line break or tab of a value written counts three times (a break, the
    text after it, and its ``xml:space``), before it is written."""


def fill_docx(
    document: bytes,
    values: Mapping[str, str],
    *,
    allow_unused: bool = False,
    allow_missing: bool = False,
    allow_malformed: bool = False,
    limits: ArchiveLimits | None = None,
) -> bytes:
    """``document`` (a .docx) with each ``{{ name }}`` replaced by ``values[name]``.

    Strict unless told: ``allow_unused`` lets a value no placeholder takes
    through; ``allow_missing`` leaves a placeholder without a value as it
    is written; ``allow_malformed`` leaves braces that make no placeholder
    as they are written. The last two make a draft, for a preview: never
    send one (``placeholders`` lists what is left).

    The work is CPU-bound: in async code, await ``afill_docx``, which
    holds the calls at once to ``DOCUMENTS["DOCX_AT_ONCE"]``.

    Raises:
        ImportError: ``domainkit[documents]`` is not installed.
        TypeError: A value is not text (format numbers and dates first).
        DocumentError: The document is not a .docx, is past ``limits``,
            or holds a DTD; a value holds a character XML cannot.
        PlaceholderError: Placeholders and values do not agree (every
            problem not allowed listed).
    """
    etree = _etree()
    given = _checked(values)
    bounds = limits or ArchiveLimits()

    def fill() -> bytes:
        with _opened(document, bounds) as archive:
            found: dict[str, None] = {}  # Ordered, as written.
            malformed: list[str] = []
            changed = _filled(etree, archive, _Budget(bounds), given, found, malformed)
            valueless = set() if allow_missing else found.keys() - given.keys()
            unused = set() if allow_unused else given.keys() - found.keys()
            if allow_malformed:
                malformed = []
            if valueless or unused or malformed:
                raise PlaceholderError(
                    missing=valueless, unused=unused, malformed=malformed
                )
            return _written(archive, changed)

    return _alone(fill)


def placeholders(
    document: bytes, *, limits: ArchiveLimits | None = None
) -> Placeholders:
    """The placeholders of ``document`` (a .docx), and the braces that make none.

    Read as ``fill_docx`` reads them, within ``limits``: what it lists is
    what ``fill_docx`` asks a value for, or refuses. CPU-bound, as
    ``fill_docx`` is: in async code, await ``aplaceholders``.

    Raises:
        ImportError: ``domainkit[documents]`` is not installed.
        DocumentError: The document is not a .docx, is past ``limits``,
            or holds a DTD.
    """
    etree = _etree()
    bounds = limits or ArchiveLimits()

    def read() -> Placeholders:
        found: dict[str, None] = {}
        malformed: list[str] = []
        with _opened(document, bounds) as archive:
            _filled(etree, archive, _Budget(bounds), {}, found, malformed)
        return Placeholders(names=tuple(found), malformed=tuple(malformed))

    return _alone(read)


async def afill_docx(
    document: bytes,
    values: Mapping[str, str],
    *,
    allow_unused: bool = False,
    allow_missing: bool = False,
    allow_malformed: bool = False,
    limits: ArchiveLimits | None = None,
) -> bytes:
    """``fill_docx``, once one of the process's places to read a document is free.

    At most ``DOCUMENTS["DOCX_AT_ONCE"]`` calls of ``afill_docx`` and
    ``aplaceholders`` run at once in the process, on every event loop and
    thread together; the others wait their turn, holding no thread. A call
    cancelled while it waits never runs; one cancelled while it runs keeps
    its place until it ends. New in 2.10.

    Raises:
        ImportError: ``domainkit[documents]`` is not installed.
        TypeError: A value is not text.
        DocumentError: As ``fill_docx`` raises it.
        PlaceholderError: As ``fill_docx`` raises it.
    """
    return await _at_once(
        partial(
            fill_docx,
            document,
            values,
            allow_unused=allow_unused,
            allow_missing=allow_missing,
            allow_malformed=allow_malformed,
            limits=limits,
        )
    )


async def aplaceholders(
    document: bytes, *, limits: ArchiveLimits | None = None
) -> Placeholders:
    """``placeholders``, once one of the process's places to read a document is free.

    It shares ``DOCUMENTS["DOCX_AT_ONCE"]`` places with ``afill_docx``, and
    waits as it does. New in 2.10.

    Raises:
        ImportError: ``domainkit[documents]`` is not installed.
        DocumentError: As ``placeholders`` raises it.
    """
    return await _at_once(partial(placeholders, document, limits=limits))


_readers_lock: Final = threading.Lock()
"""Held to make the process's readers: loops on several threads ask at once."""


@cache
def _readers(at_once: int, process: int) -> ThreadPoolExecutor:
    """The ``at_once`` threads of the process ``process`` that wait for readings.

    Each waits for the thread of its own that ``_alone`` reads a document
    in, so lxml's names go with that thread. A process forked from
    ``process`` has none of its threads, and makes its own.
    """
    del process  # A key: the pool of the process that made it.
    return ThreadPoolExecutor(max_workers=at_once, thread_name_prefix="domainkit-docx")


async def _at_once[T](work: Callable[[], T]) -> T:
    """``work()`` on one of the readers ``DOCUMENTS["DOCX_AT_ONCE"]`` sizes.

    Until a reader is free it waits in the readers' queue: a caller
    cancelled meanwhile takes it out, so it never runs.
    """
    from domainkit.conf import settings

    with _readers_lock:  # So two loops never make two pools of one size.
        readers = _readers(settings.DOCUMENTS.DOCX_AT_ONCE, os.getpid())
    return await asyncio.get_running_loop().run_in_executor(readers, work)


def _alone[T](work: Callable[[], T]) -> T:
    """``work()``, in a thread of its own that ends with it.

    lxml keeps every element and attribute name a thread parses in a
    dictionary of that thread's, for as long as the thread lives: a pool's
    thread that read documents of 250,000 names each grew by 700 MiB over
    80 of them. A thread's dictionary goes with it. The thread costs about
    0.4 ms a call.
    """
    done: list[T] = []
    failed: list[BaseException] = []

    def run() -> None:
        try:
            done.append(work())
        except BaseException as error:  # noqa: BLE001 - raised again in the caller.
            failed.append(error)

    thread = threading.Thread(target=run, name="domainkit.documents.docx")
    thread.start()
    thread.join()
    if failed:
        raise failed.pop()
    return done.pop()


def _filled(
    etree: Any,  # noqa: ANN401 - lxml has no types.
    archive: zipfile.ZipFile,
    budget: "_Budget",
    values: Mapping[str, str],
    found: dict[str, None],
    malformed: list[str],
) -> dict[str, bytes]:
    """Each part filled with ``values``, written, by name: those that changed.

    Each part's tree is dropped before the next is parsed. Adds to
    ``found`` and ``malformed`` as ``_fill`` does.

    Raises:
        DocumentError: A part is past ``budget``, or not XML ``_parse``
            reads.
    """
    changed: dict[str, bytes] = {}
    costs = {
        name: (
            len(value.encode()),
            BREAK_NODES * (value.count("\n") + value.count("\t")),
        )
        for name, value in values.items()
    }
    for info in _filled_parts(etree, archive, budget):
        root = _xml(etree, archive, info, budget)
        if _fill(etree, root, values, found, malformed, budget, costs):
            changed[info.filename] = etree.tostring(
                root.getroottree(),
                xml_declaration=True,
                encoding="UTF-8",
                standalone=root.getroottree().docinfo.standalone,
            )
        del root
    return changed


def _etree() -> Any:  # noqa: ANN401 - lxml has no types.
    """The ``etree`` of lxml, imported when first needed.

    Raises:
        ImportError: ``domainkit[documents]`` is not installed.
    """
    try:
        from lxml import etree
    except ImportError as exc:
        raise missing("lxml") from exc
    return etree


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


@contextmanager
def _opened(document: bytes, limits: ArchiveLimits) -> Iterator[zipfile.ZipFile]:
    """The archive, open, its files checked within ``limits``.

    The count of files and the size of the central directory are read
    from the archive's end first: ``zipfile`` reads every entry the
    directory holds before any can be counted. The sizes are checked as
    the archive states them, before anything is inflated; then each file
    is inflated a piece at a time, no further than its stated size, and
    ``zipfile`` refuses one whose CRC-32 then differs. Nothing inflated is
    kept: a file is inflated again when it is read (``_inflated``).

    Raises:
        DocumentError: It is not a ZIP archive, is past a limit, is
            damaged, or has a file encrypted, compressed otherwise than
            stored or deflated, named other than by a plain relative path,
            or named as another is.
    """
    if len(document) > limits.max_bytes:
        raise DocumentError(f"The document is larger than {limits.max_bytes:,} bytes.")
    stated = _directory(document)
    if stated is not None and stated[0] > limits.max_parts:
        raise DocumentError(f"The document holds more than {limits.max_parts:,} files.")
    if stated is not None and stated[1] > limits.max_parts * DIRECTORY_BYTES:
        raise DocumentError(
            f"The document's list of files is larger than {limits.max_parts:,} "
            "files need."
        )
    try:
        archive = zipfile.ZipFile(io.BytesIO(document))
    except (zipfile.BadZipFile, ValueError):
        raise DocumentError("The document is not a Word document (.docx).") from None
    with archive:
        infos = archive.infolist()
        _check_entries(infos, limits)
        for info in infos:
            _checked_whole(archive, info)
        yield archive


def _directory(document: bytes) -> tuple[int, int] | None:
    """The files, and the bytes of the central directory, the archive's end states.

    None where there is no end record: ``zipfile`` then refuses the archive.
    A ZIP64 archive states them in its ZIP64 end record, which the locator
    before the end record points to.
    """
    end = document.rfind(END, max(0, len(document) - END_SIZE - 0xFFFF))
    if end < 0 or end + END_SIZE > len(document):
        return None
    files, size = struct.unpack_from("<HI", document, end + 10)
    locator = end - 20
    if locator >= 0 and document[locator : locator + 4] == ZIP64_LOCATOR:
        (offset,) = struct.unpack_from("<Q", document, locator + 8)
        if document[offset : offset + 4] == ZIP64_END and offset + 56 <= end:
            files, size = struct.unpack_from("<QQ", document, offset + 32)
    return files, size


def _inflated(archive: zipfile.ZipFile, info: zipfile.ZipInfo) -> bytes:
    """One file of the archive, inflated no further than its stated size.

    ``ZipFile.read`` asks the decompressor for up to 2 GiB at once, and
    cuts to the stated size only after: a file that states 10 bytes and
    inflates to gigabytes would be inflated whole. Asked for its stated
    size, the decompressor stops there, and the CRC-32 is checked: once
    more, as ``_opened`` checked every file first.
    """
    with archive.open(info) as part:
        return part.read(info.file_size)


def _checked_whole(archive: zipfile.ZipFile, info: zipfile.ZipInfo) -> None:
    """Inflate one file as ``_inflated`` does, a piece at a time, keeping nothing.

    Raises:
        DocumentError: The file is damaged.
    """
    left = info.file_size
    try:
        with archive.open(info) as part:
            while left > 0 and (piece := part.read(min(CHUNK, left))):
                left -= len(piece)
    except DAMAGED:
        raise DocumentError("The document's archive is damaged.") from None


class _Budget:
    """What is left of ``ArchiveLimits``' bounds on the XML parsed, in one call."""

    def __init__(self, limits: ArchiveLimits) -> None:
        """All of it."""
        self.limits = limits
        self.bytes = limits.max_xml_bytes
        self.nodes = limits.max_nodes

    def take_bytes(self, size: int, *, written: bool = False) -> None:
        """Count ``size`` bytes of XML, as stated or inflated, or ``written``.

        Raises:
            DocumentError: They are more than is left.
        """
        if size > self.bytes:
            filled = ", with its values," if written else ""
            raise DocumentError(
                f"The document's XML{filled} is larger than "
                f"{self.limits.max_xml_bytes:,} bytes in all."
            )
        self.bytes -= size

    def take_nodes(self, count: int) -> None:
        """Count ``count`` nodes, placeholders, or elements written.

        Raises:
            DocumentError: They are more than is left.
        """
        if count > self.nodes:
            raise DocumentError(
                f"The document holds more than {self.limits.max_nodes:,} elements, "
                "texts, attributes, and placeholders in all, with its values."
            )
        self.nodes -= count


def _nodes(data: bytes) -> int:
    """The most nodes lxml makes of ``data``, counted before it is parsed.

    Each ``<`` begins an element, an end tag, a comment, an instruction,
    or a CDATA section, and may end a text; each ``=`` may give an
    attribute or a namespace. With no DTD and no entity, nothing else
    makes a node.
    """
    return 2 * data.count(b"<") + data.count(b"=")


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


def _filled_parts(
    etree: Any,  # noqa: ANN401 - lxml has no types.
    archive: zipfile.ZipFile,
    budget: _Budget,
) -> list[zipfile.ZipInfo]:
    """The parts whose text is filled, from ``[Content_Types].xml``.

    In a stable order, not the content types', which is the writer's: the
    body, the headers, the footers, the footnotes, then the endnotes, each
    kind by name, numbers as numbers (``header2`` before ``header10``).

    Raises:
        DocumentError: The archive is no .docx: it has no content types, or
            its body is a template's or holds macros; or its XML is past
            ``budget``.
    """
    by_lower = {info.filename.lower(): info for info in archive.infolist()}
    if CONTENT_TYPES.lower() not in by_lower:
        raise DocumentError("The document is not a Word document (.docx).")
    root = _xml(etree, archive, by_lower[CONTENT_TYPES.lower()], budget)
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
    filled = [(kind, name) for name, kind in typed.items() if kind in FILLED]
    return [
        by_lower[name]
        for kind, name in sorted(filled, key=_read_before)
        if name in by_lower
    ]


def _xml(
    etree: Any,  # noqa: ANN401 - lxml has no types.
    archive: zipfile.ZipFile,
    info: zipfile.ZipInfo,
    budget: _Budget,
) -> Any:  # noqa: ANN401 - lxml has no types.
    """The root element of the part ``info`` names, within ``budget``.

    Its stated size is counted before it is inflated, and the nodes its
    bytes may make before they are parsed.

    Raises:
        DocumentError: It is past ``budget``, or not XML ``_parse`` reads.
    """
    budget.take_bytes(info.file_size)
    data = _inflated(archive, info)
    budget.take_nodes(_nodes(data))
    return _parse(etree, data, info.filename)


def _read_before(part: tuple[str, str]) -> tuple[int, list[str | int]]:
    """Where a filled part of ``(kind, name)`` is read: by kind, then by name."""
    kind, name = part
    pieces = NUMBERS.split(name)
    return FILLED.index(kind), [
        int(piece) if index % 2 else piece for index, piece in enumerate(pieces)
    ]


def _parse(etree: Any, data: bytes, name: str) -> Any:  # noqa: ANN401 - lxml has no types.
    """The root element of the part ``name``: no entities, DTD, or outside file.

    A DTD or an entity is refused before parsing: lxml without entities or
    a DTD still opens an outside DTD subset, or an outside parameter
    entity, a file that may be a pipe that never ends. The parser also
    resolves any outside reference to nothing, and reads UTF-8 only, as
    Word writes, so the check above sees what it parses.

    Raises:
        DocumentError: It is in UTF-16 or UTF-32, is not well-formed UTF-8
            XML, or declares a DTD or an entity.
    """
    if data.startswith(NOT_UTF_8):
        raise DocumentError(
            f"{name} in the document is in UTF-16 or UTF-32; it is read in UTF-8 "
            "only, as Word writes it."
        )
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
    found: dict[str, None],
    malformed: list[str],
    budget: _Budget,
    costs: Mapping[str, tuple[int, int]],
) -> bool:
    """Fill each paragraph under ``root``; whether any text changed.

    Adds the names of the placeholders met to ``found``, in order, and the
    text of braces that make no placeholder to ``malformed``. Each ``{{``
    of a paragraph is counted in ``budget`` before its placeholders are
    read: a character reference (``&#123;``) makes one where the bytes
    showed none. What each value written adds, its bytes and the nodes of
    its line breaks and tabs (``costs``, by name), is counted before a
    paragraph is written: the template says how many times a value is.

    Raises:
        DocumentError: The placeholders, or what the values add, are past
            ``budget``.
    """
    changed = False
    for texts in _paragraphs(etree, root):
        joined = "".join(text.text or "" for text in texts)
        if "{{" not in joined and "}}" not in joined:
            continue
        budget.take_nodes(joined.count("{{"))
        matches = list(PLACEHOLDER.finditer(joined))
        if stray := _stray(joined, matches):
            malformed.append(stray)
        found.update(dict.fromkeys(match[1] for match in matches))
        known = [match for match in matches if match[1] in values]
        if known:
            budget.take_bytes(sum(costs[match[1]][0] for match in known), written=True)
            budget.take_nodes(sum(costs[match[1]][1] for match in known))
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

    ``matches`` are offsets into the joined text of ``texts``, in order. Each
    text that changes is built once, from the pieces of it left between the
    matches and the values put in it: a text of many placeholders costs no
    more than they do, where rebuilding it for each one cost their square.
    """
    contents = [text.text or "" for text in texts]
    starts = list(accumulate((len(content) for content in contents), initial=0))
    pieces: dict[int, list[str]] = {}
    taken: dict[int, int] = {}  # How far into each changed text its pieces reach.
    for match in matches:
        first = bisect_right(starts, match.start()) - 1
        last = bisect_right(starts, match.end() - 1) - 1
        made = pieces.setdefault(first, [])
        made.append(
            contents[first][taken.get(first, 0) : match.start() - starts[first]]
        )
        made.append(values[match[1]])
        for index in range(first + 1, last + 1):
            pieces.setdefault(index, [])
            taken[index - 1] = len(contents[index - 1])
        taken[last] = match.end() - starts[last]
    for index, made in sorted(pieces.items()):
        _write(texts[index], "".join(made) + contents[index][taken[index] :])


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


def _written(archive: zipfile.ZipFile, changed: Mapping[str, bytes]) -> bytes:
    """The archive again: each file in its order and with its date, changed or not.

    A file not changed is inflated as it is written, one at a time.
    """
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as written_archive:
        for info in archive.infolist():
            written = zipfile.ZipInfo(info.filename, date_time=info.date_time)
            written.external_attr = info.external_attr
            written.compress_type = (
                zipfile.ZIP_STORED
                if info.compress_type == zipfile.ZIP_STORED
                else zipfile.ZIP_DEFLATED
            )
            data = changed.get(info.filename)
            written_archive.writestr(
                written, _inflated(archive, info) if data is None else data
            )
    return out.getvalue()
