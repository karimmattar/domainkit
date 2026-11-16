"""What an uploaded document holds, checked before it is kept (``documents`` extra).

A file's first bytes say which type it claims to be; only its whole
content says what it does when it is opened. ``PdfCheck`` and
``OfficeCheck`` open a PDF or an Office document (Word, Excel, PowerPoint)
and refuse what runs, fetches, or hides something when it is opened or
converted::

    from domainkit.http.uploads import CHECKED_PDF, DOCX, save_upload

    stored = await save_upload(file, types=(CHECKED_PDF, DOCX), max_size=10_000_000)

They are the ``inspect`` hooks of the types ``domainkit.http.uploads``
ships (``CHECKED_PDF``, ``DOCX``, ``XLSX``, ``PPTX``); ``save_upload`` runs
the hook in a worker thread, on the whole file, before it stores it. A
hook raises ``FileRefused`` (422 ``unsafe_file``, or ``unreadable_file``)
for a file it will not accept, and ``NotOfType`` for one that is not its
type after all (a spreadsheet where a Word document was asked for).

A PDF is read by pypdf in strict mode, and only as its cross-reference
table says: a file whose table names an object at an offset that holds
another, or that refers to an object the table does not declare, is
refused (``unreadable_file``) before pypdf would search the file for it.
So every reader that does not repair the file sees what the check saw.
Every object the table declares is read, every reference from the
trailer followed, and every ``N G obj`` written in the file (an earlier
revision, an object no table lists) parsed too. A PDF is refused when it
is encrypted, has more than ``max_pages`` pages, or holds JavaScript (in
an action, a name tree, a 3D annotation's ``/OnInstantiate``), an action
run on opening or on an event (``/OpenAction`` with an action, ``/AA``),
an action that launches, opens another file or sends data (``/Launch``,
``/GoToR``, ``/GoToE``, ``/Thread`` with a file, ``/SubmitForm``,
``/ImportData``), a link to anything but ``http``, ``https`` or
``mailto``, media from a URL, an embedded file, rich media, or an XFA
form. Names are compared decoded (``/J#61vaScript`` is ``/JavaScript``).

An Office document is refused when its ZIP holds more than ``max_parts``
parts, inflates past ``max_expansion`` times its own size (or
``max_size``) in all, or a part more than ``max_ratio`` times; when a
part is encrypted, compressed but by deflate, or named differently in its
header; when its main part is macro-enabled (a ``.docm`` named
``.docx``) or not of the type asked; when it holds macros
(``vbaProject.bin``, Excel 4.0 macro sheets), OLE objects, ActiveX
controls, or a chunk imported from another format (``altChunk``); when a
relationship points outside the package but for a hyperlink to ``http``,
``https`` or ``mailto`` (a remote template, a linked image or object:
CVE-2022-30190 opened one), whatever its ``TargetMode`` says; when a Word
field pulls content in or runs a program (``INCLUDEPICTURE``,
``INCLUDETEXT``, ``INCLUDE``, ``IMPORT``, ``LINK``, ``DATABASE``,
``DDE``, ``DDEAUTO``), or its instruction starts with another field,
which hides its name; when an Excel workbook links to another workbook,
holds a data connection or a query table, or a formula calls a web
service (``WEBSERVICE``, ``RTD``) or DDE (``cmd|'...'!A0``); or when an
XML part, whatever its name, declares a DTD or an entity, which the Open
Packaging Conventions forbid, or is in an encoding but UTF-8 and UTF-16.
A DTD is refused in the bytes, before lxml parses them, so nothing
outside the file is ever opened. An embedded workbook, document or
presentation (a chart's data) is checked the same way, one level deep,
from a temporary file.

Each check runs under a deadline (``max_seconds``) checked at every
object or part. A PDF's object streams decode to ``max_decoded`` bytes
in all, each stream to ``PDF_STREAM_LIMIT`` (``PDF_LIMITS``), and the
check reads at most ``PDF_NODES`` values. A ZIP's sizes are read from its
directory before a byte is inflated. These bound the work in the
process, not as a separate process's resource limits would: what pypdf
and lxml do inside one call is theirs (ADR 0167). The checks do not make
a file harmless; they refuse what is known to be used against readers
and converters. Keep the virus scan (``UPLOAD_SCANNER``) too.
"""

import codecs
import io
import re
import shutil
import tempfile
import time
import zipfile
from collections.abc import Iterator
from dataclasses import dataclass, field
from posixpath import dirname, join, normpath
from typing import IO, TYPE_CHECKING, Any, Final, Literal
from urllib.parse import unquote, urlsplit

from domainkit.exceptions import DomainkitError, ImproperlyConfigured

if TYPE_CHECKING:
    from pypdf import PdfReader
    from pypdf.generic import IndirectObject

__all__ = ["FileRefused", "NotOfType", "OfficeCheck", "PdfCheck"]

type Kind = Literal["docx", "xlsx", "pptx"]
"""The Office documents checked: Word, Excel, PowerPoint."""

UNSAFE: Final = "unsafe_file"
"""The code of a file that holds what is not accepted."""
UNREADABLE: Final = "unreadable_file"
"""The code of a file its type's reader cannot read."""
SCRIPTS: Final = "Files with scripts are not accepted."
MACROS: Final = "Files with macros are not accepted."
ACTIONS: Final = (
    "Files with actions that run by themselves, open other files, or send data "
    "are not accepted."
)
EMBEDDED: Final = "Files with embedded files or objects are not accepted."
EXTERNAL: Final = "Files that load content from elsewhere are not accepted."
FORMS: Final = "PDF files with XFA forms are not accepted."
DTD: Final = "Files with a document type declaration are not accepted."
ENCRYPTED: Final = "Files protected by a password are not accepted."
PAGES: Final = "The file has too many pages."
COMPLEX: Final = "The file is too large or too complex to check."
DAMAGED: Final = "The file could not be read."
LINK_SCHEMES: Final = frozenset({"http", "https", "mailto"})
"""What a link may open: web pages and mail, never a file or a program."""
CHUNK: Final = 64 * 1024
"""Bytes copied at a time."""

PDF_STREAM_LIMIT: Final = 16 * 1024 * 1024
"""What one stream a PDF check decodes may inflate to."""
PDF_LIMITS: Final = {
    "zlib_maximum_output_length": PDF_STREAM_LIMIT,
    "lzw_maximum_output_length": PDF_STREAM_LIMIT,
    "run_length_maximum_output_length": PDF_STREAM_LIMIT,
    "brotli_maximum_output_length": PDF_STREAM_LIMIT,
    "jbig2_maximum_output_length": PDF_STREAM_LIMIT,
    "array_based_stream_maximum_output_length": PDF_STREAM_LIMIT,
    "array_maximum_element_count": 50_000,
    "dictionary_maximum_element_count": 10_000,
}
"""pypdf's limits while a PDF is checked: each stream, not all of them."""
PDF_NODES: Final = 1_000_000
"""Objects, entries and items a PDF check reads at most."""
DEADLINE_EVERY: Final = 1024
"""Values read between two looks at the clock."""
PDF_SPACE: Final = rb"[ \t\r\n\f\x00]"
"""What a PDF counts as white space."""
PDF_OBJECT: Final = re.compile(
    rb"(?<![0-9])([0-9]{1,10})"
    + PDF_SPACE
    + rb"+([0-9]{1,5})"
    + PDF_SPACE
    + rb"+obj(?![A-Za-z])"
)
"""An object's header, as written: ``12 0 obj``."""
PDF_HEADER: Final = re.compile(
    PDF_SPACE
    + rb"*([0-9]{1,10})"
    + PDF_SPACE
    + rb"+([0-9]{1,5})"
    + PDF_SPACE
    + rb"+obj"
)
"""The header a table's offset points at, after any white space."""
PDF_BLANKS: Final = b" \t\r\n\f\x00"
"""The bytes a PDF counts as white space."""
PDF_REFUSED_NAMES: Final = {
    "/JavaScript": SCRIPTS,
    "/Launch": ACTIONS,
    "/GoToR": ACTIONS,
    "/GoToE": ACTIONS,
    "/SubmitForm": ACTIONS,
    "/ImportData": ACTIONS,
    "/RichMediaExecute": ACTIONS,
    "/EmbeddedFile": EMBEDDED,
    "/FileAttachment": EMBEDDED,
    "/RichMedia": EMBEDDED,
}
"""Names refused wherever they are a value: action types, file and media types."""
PDF_REFUSED_KEYS: Final = {
    "/JS": SCRIPTS,
    "/JavaScript": SCRIPTS,
    "/OnInstantiate": SCRIPTS,
    "/AA": ACTIONS,
    "/EmbeddedFiles": EMBEDDED,
    "/EF": EMBEDDED,
    "/XFA": FORMS,
}
"""Keys refused in any dictionary: scripts, actions on events, files, XFA."""

OOXML: Final = "application/vnd.openxmlformats-officedocument."
OFFICE_MAIN: Final = {
    "docx": f"{OOXML}wordprocessingml.document.main+xml",
    "xlsx": f"{OOXML}spreadsheetml.sheet.main+xml",
    "pptx": f"{OOXML}presentationml.presentation.main+xml",
}
"""The main part's content type of each document: not a template, nor macro-enabled."""
OFFICE_PACKAGES: Final[dict[str, Kind]] = {
    f"{OOXML}wordprocessingml.document": "docx",
    f"{OOXML}spreadsheetml.sheet": "xlsx",
    f"{OOXML}presentationml.presentation": "pptx",
}
"""Packages a document may embed (a chart's workbook), each checked as its type."""
OFFICE_REFUSED_TYPES: Final = (
    ("macroenabled", MACROS),
    ("vbaproject", MACROS),
    ("vbadata", MACROS),
    ("macrosheet", MACROS),
    ("keymapcustomizations", MACROS),
    ("activex", EMBEDDED),
    ("oleobject", EMBEDDED),
    ("spreadsheetml.externallink", EXTERNAL),
    ("spreadsheetml.connections", EXTERNAL),
    ("spreadsheetml.querytable", EXTERNAL),
)
"""Parts refused by what their content type holds, in lower case."""
OFFICE_REFUSED_RELATIONSHIPS: Final = {
    "vbaproject": MACROS,
    "vbaprojectsignature": MACROS,
    "wordvbadata": MACROS,
    "xlmacrosheet": MACROS,
    "xlintlmacrosheet": MACROS,
    "keymapcustomizations": MACROS,
    "oleobject": EMBEDDED,
    "control": EMBEDDED,
    "activexcontrolbinary": EMBEDDED,
    "afchunk": EMBEDDED,
    "externallinkpath": EXTERNAL,
    "connections": EXTERNAL,
    "querytable": EXTERNAL,
}
"""Relationships refused by the last segment of their type, in lower case."""
OFFICE_FIELDS: Final = {
    "DDE": ACTIONS,
    "DDEAUTO": ACTIONS,
    "INCLUDEPICTURE": EXTERNAL,
    "INCLUDETEXT": EXTERNAL,
    "INCLUDE": EXTERNAL,
    "IMPORT": EXTERNAL,
    "LINK": EXTERNAL,
    "DATABASE": EXTERNAL,
}
"""Word fields that run a program or pull content in when they are updated."""
RESULT: Final = "\x00"
"""Stands for a field's result, whose own fields are not its instruction."""
FIELD_NAME: Final = re.compile(r"^\s*([A-Za-z]+)")
"""A field instruction's name: the letters it starts with, whatever follows."""
FORMULA_CALLS: Final = re.compile(r"\b(?:WEBSERVICE|RTD)\s*\(", re.IGNORECASE)
"""Excel functions that reach outside the workbook when the sheet calculates."""
FORMULA_STRINGS: Final = re.compile(r'"(?:[^"]|"")*"')
"""A formula's text literals, left out before it is read for DDE."""
WORDML: Final = (
    "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}",
    "{http://purl.oclc.org/ooxml/wordprocessingml/main}",
)
"""WordprocessingML's namespaces: transitional and strict."""
SHEETML: Final = (
    "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}",
    "{http://purl.oclc.org/ooxml/spreadsheetml/main}",
)
"""SpreadsheetML's namespaces: transitional and strict."""
LINKS: Final = (
    "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}link",
    "{http://purl.oclc.org/ooxml/officeDocument/relationships}link",
)
"""``r:link``: a picture or an object fetched from where a relationship points."""
RELATIONSHIPS: Final = (
    "{http://schemas.openxmlformats.org/package/2006/relationships}Relationship"
)
CONTENT_TYPES: Final = "{http://schemas.openxmlformats.org/package/2006/content-types}"
RATIO_FLOOR: Final = 1024 * 1024
"""A part inflating to less than this is not held to ``max_ratio``."""
XML_DECLARED: Final = re.compile(rb"""^<\?xml[^>]*?encoding\s*=\s*["']([^"']*)["']""")
"""The encoding an XML declaration names."""
XML_ENCODINGS: Final = frozenset({"utf8", "utf16"})
"""What an XML part of a package may be in (ECMA-376 Part 2)."""
DECLARATIONS: Final = ("<!DOCTYPE", "<!ENTITY")
"""What declares a DTD or an entity: refused before lxml reads the part."""


class FileRefused(DomainkitError):
    """The file is of its type, but holds what is not accepted.

    ``message`` is safe to show, in English (the HTTP layer translates
    it); ``code`` is the problem's: ``unsafe_file`` by default, or
    ``unreadable_file``. ``save_upload`` answers it 422.
    """

    def __init__(self, message: str, *, code: str = "unsafe_file") -> None:
        """Say why, and with which code."""
        super().__init__(message)
        self.message = message
        self.code = code


class NotOfType(DomainkitError):
    """The file is not of the type its first bytes suggested.

    ``save_upload`` tries the next type the route takes, and answers 415
    ``unsupported_file_type`` when none is left.
    """


@dataclass(slots=True)
class _Clock:
    """A check's deadline, and the values it may still read."""

    deadline: float
    values: int = 0
    """What a PDF check may still read; an Office check counts none."""

    def tick(self) -> None:
        """Count one value; look at the clock now and then.

        Raises:
            FileRefused: The budget or the time ran out.
        """
        self.values -= 1
        if self.values < 0:
            raise FileRefused(COMPLEX)
        if self.values % DEADLINE_EVERY == 0:
            self.check()

    def check(self) -> None:
        """Refuse a check past its deadline.

        Raises:
            FileRefused: It is.
        """
        if time.monotonic() > self.deadline:
            raise FileRefused(COMPLEX)


def _positive(owner: object, *names: str) -> None:
    """Refuse a bound of ``owner`` that is not a positive number.

    Raises:
        ValueError: One is not.
    """
    for name in names:
        value = getattr(owner, name)
        if not value > 0:
            raise ValueError(f"{name} is more than 0, not {value!r}.")


@dataclass(frozen=True, slots=True)
class PdfCheck:
    """Refuse a PDF that runs, sends, or hides something; or is too long.

    See the module for what is refused. ``max_pages`` bounds the pages,
    ``max_objects`` the objects its tables declare, ``max_decoded`` the
    bytes its object streams decode to in all, and ``max_seconds`` the
    time the check takes.
    """

    max_pages: int = 1000
    max_objects: int = 200_000
    max_decoded: int = 64 * 1024 * 1024
    max_seconds: float = 10.0

    def __post_init__(self) -> None:
        """Refuse a bound that is not a positive number.

        Raises:
            ValueError: One is not.
        """
        _positive(self, "max_pages", "max_objects", "max_decoded", "max_seconds")

    def __call__(self, file: IO[bytes]) -> None:
        """Check the PDF ``file`` holds, read from its start.

        Raises:
            FileRefused: It holds what is not accepted, or cannot be read.
            ImproperlyConfigured: pypdf is not installed.
        """
        pypdf = _documents_extra("pypdf")
        clock = _Clock(time.monotonic() + self.max_seconds, PDF_NODES)
        data = file.read()
        with pypdf.apply_configuration(**PDF_LIMITS):
            try:
                reader = pypdf.PdfReader(
                    io.BytesIO(data),
                    strict=True,
                    xref_stream_maximum_entries=self.max_objects,
                )
                if reader.is_encrypted:
                    raise FileRefused(ENCRYPTED)
                _Pdf(self, reader, data, clock).check()
            except FileRefused:
                raise
            except Exception as exc:  # pypdf raises anything on a malformed file.
                raise FileRefused(DAMAGED, code=UNREADABLE) from exc


@dataclass(frozen=True, slots=True)
class OfficeCheck:
    """Refuse an Office document with macros, objects, or outside links.

    ``kind`` is the document taken: ``docx``, ``xlsx`` or ``pptx``. A ZIP
    holds at most ``max_parts`` parts and inflates to ``max_expansion``
    times its own size in all, and never past ``max_size`` bytes; no part
    above 1 MiB inflates more than ``max_ratio`` times. ``max_seconds``
    bounds the time the check takes. See the module for what is refused.
    """

    kind: Kind
    max_parts: int = 2000
    max_size: int = 200 * 1024 * 1024
    max_ratio: int = 200
    max_expansion: int = 50
    max_seconds: float = 10.0

    def __post_init__(self) -> None:
        """Refuse a kind not known, or a bound that is not a positive number.

        Raises:
            ValueError: One of them is.
        """
        if self.kind not in OFFICE_MAIN:
            raise ValueError(f"An Office check takes {sorted(OFFICE_MAIN)}.")
        _positive(
            self, "max_parts", "max_size", "max_ratio", "max_expansion", "max_seconds"
        )

    def __call__(self, file: IO[bytes]) -> None:
        """Check the Office document ``file`` holds, read from its start.

        Raises:
            FileRefused: It holds what is not accepted, or cannot be read.
            NotOfType: It is no Office document, or not of ``kind``.
            ImproperlyConfigured: lxml is not installed.
        """
        etree = _documents_extra("lxml.etree")
        size = file.seek(0, io.SEEK_END)
        file.seek(0)
        budget = _Budget(
            self.max_parts,
            min(self.max_size, self.max_expansion * max(size, 1)),
            _Clock(time.monotonic() + self.max_seconds),
        )
        _Package(self, etree, budget).check(file, self.kind, nested=False)


def _documents_extra(module: str) -> Any:  # noqa: ANN401 - pypdf, or lxml's etree.
    """The module ``module`` of the ``documents`` extra.

    Raises:
        ImproperlyConfigured: It is not installed.
    """
    import importlib

    try:
        return importlib.import_module(module)
    except ImportError:
        raise ImproperlyConfigured(
            f"Checking PDF and Office uploads needs {module.partition('.')[0]}: "
            'install the documents extra, pip install "domainkit[documents]".'
        ) from None


def _check_link(target: str) -> None:
    """Refuse a link to anything but a web page or a mail address.

    Raises:
        FileRefused: It names a file, a share, or a program's scheme.
    """
    scheme = urlsplit(target.strip()).scheme.lower()
    if scheme not in LINK_SCHEMES:
        raise FileRefused(EXTERNAL)


# PDF ---------------------------------------------------------------------------------


class _Pdf:
    """One PDF's check: what its tables declare, and every object written in it."""

    def __init__(
        self, check: PdfCheck, reader: "PdfReader", data: bytes, clock: _Clock
    ) -> None:
        """Read the tables; resolve every object, pypdf's own reads included, here.

        Raises:
            FileRefused: The tables declare more than ``max_objects``.
        """
        self.check_of = check
        self.reader = reader
        self.data = data
        self.clock = clock
        self.decoded = 0
        self.declared: dict[tuple[int, int], int | None] = {}
        """Each object the tables declare in use: its offset, or None in a stream."""
        for generation, numbers in reader.xref.items():  # In use: free ones are not.
            for number, offset in numbers.items():
                self.declared[number, generation] = offset
        for number in reader.xref_objStm:
            self.declared[number, 0] = None
        self.streams = {stream for stream, _ in reader.xref_objStm.values()}
        """The object streams the tables name."""
        if len(self.declared) > check.max_objects:
            raise FileRefused(COMPLEX)
        self._resolve = reader.get_object
        reader.get_object = self.resolve  # type: ignore[method-assign, assignment]

    def resolve(self, reference: "int | IndirectObject") -> object:
        """The object ``reference`` names, as the tables say, and nowhere else.

        pypdf would search the whole file for an object its table does not
        list, or that is not where the table says: each search reads the
        file again, and finds what a reader that does not repair the file
        never sees.

        Raises:
            FileRefused: The tables do not declare it, or it is not at its
                offset (``unreadable_file``); the decoded bytes, the budget,
                or the time ran out.
        """
        from pypdf.generic import IndirectObject

        if isinstance(reference, int):
            reference = IndirectObject(reference, 0, self.reader)
        key = (reference.idnum, reference.generation)
        if key not in self.declared:
            raise FileRefused(DAMAGED, code=UNREADABLE)
        offset = self.declared[key]
        if offset is not None:
            found = PDF_HEADER.match(self.data, offset)
            if found is None or (int(found[1]), int(found[2])) != key:
                raise FileRefused(DAMAGED, code=UNREADABLE)
        self.clock.check()
        resolved: Any = self._resolve(reference)
        if key[1] == 0 and key[0] in self.streams and hasattr(resolved, "get_data"):
            self.streams.discard(key[0])  # Decoded once, and kept by pypdf.
            self.decoded += len(resolved.get_data())
            if self.decoded > self.check_of.max_decoded:
                raise FileRefused(COMPLEX)
        return resolved

    def check(self) -> None:
        """Read the pages' count, then every value of every object.

        Raises:
            FileRefused: Something refused is found, or the file is too
                long, too large, or too slow to read.
        """
        from pypdf.generic import IndirectObject

        reader = self.reader
        if len(reader.pages) > self.check_of.max_pages:
            raise FileRefused(PAGES)
        stack: list[object] = [reader.trailer, *self._written()]
        stack += [IndirectObject(number, gen, reader) for number, gen in self.declared]
        seen: set[tuple[int, int]] = set()
        while stack:
            value = stack.pop()
            self.clock.tick()
            if isinstance(value, IndirectObject):
                key = (value.idnum, value.generation)
                if key in seen:
                    continue
                seen.add(key)
                value = self.resolve(value)
            stack.extend(_inside(value))

    def _written(self) -> Iterator[object]:
        """Every object written in the file that the tables do not point at.

        An earlier revision of an object, or one no table lists, which a
        reader that repairs the file may find. What pypdf cannot parse is
        not an object to it, and is passed over (text that reads ``1 0 obj``).
        """
        from pypdf.generic import read_object

        for found in PDF_OBJECT.finditer(self.data):
            self.clock.tick()
            key = (int(found[1]), int(found[2]))
            if self.declared.get(key) == found.start():
                continue
            stream = io.BytesIO(self.data)
            after = self.data[found.end() : found.end() + CHUNK]
            stream.seek(found.end() + len(after) - len(after.lstrip(PDF_BLANKS)))
            try:
                yield read_object(stream, self.reader)
            except FileRefused:
                raise
            except Exception:  # noqa: BLE001, S112 - not an object, to pypdf or to us.
                continue


def _inside(value: object) -> list[object]:
    """What ``value`` holds, once ``value`` itself is checked.

    Raises:
        FileRefused: ``value`` is refused.
    """
    from pypdf.generic import ArrayObject, DictionaryObject, NameObject

    if isinstance(value, DictionaryObject):
        _check_pdf_dictionary(value)
        return list(dict.values(value))
    if isinstance(value, ArrayObject):
        return list(list.__iter__(value))
    if isinstance(value, NameObject) and value in PDF_REFUSED_NAMES:
        raise FileRefused(PDF_REFUSED_NAMES[value])
    return []


def _check_pdf_dictionary(entries: dict[Any, Any]) -> None:
    """Refuse a dictionary with a refused key, an action on opening, or a link.

    Raises:
        FileRefused: It has one.
    """
    from pypdf.generic import ArrayObject, NameObject, StreamObject

    for key in entries:
        if key in PDF_REFUSED_KEYS:
            raise FileRefused(PDF_REFUSED_KEYS[key])
    opening = _resolved(dict.get(entries, "/OpenAction"))
    if opening is not None and not isinstance(opening, ArrayObject):
        # An action. A destination (an array) only shows a page.
        raise FileRefused(ACTIONS)
    kind = dict.get(entries, "/S")
    if kind == NameObject("/Thread") and "/F" in entries:
        raise FileRefused(ACTIONS)  # An article in another file.
    if dict.get(entries, "/FS") == NameObject("/URL"):
        raise FileRefused(EXTERNAL)  # Media, a movie, a sound, from a URL.
    target = _resolved(dict.get(entries, "/URI"))
    if isinstance(target, bytes | StreamObject):  # Not text: no link to follow.
        raise FileRefused(EXTERNAL)
    if isinstance(target, str):
        _check_link(target)


def _resolved(value: object) -> object:
    """``value``, or the object it refers to."""
    from pypdf.generic import IndirectObject

    return value.get_object() if isinstance(value, IndirectObject) else value


# Office ------------------------------------------------------------------------------


@dataclass(slots=True)
class _Budget:
    """What a check may still read: parts, inflated bytes, time."""

    parts: int
    size: int
    clock: _Clock


@dataclass(slots=True)
class _Package:
    """One Office document's check, and the packages it embeds."""

    check_of: OfficeCheck
    etree: Any
    budget: _Budget
    parts: dict[str, zipfile.ZipInfo] = field(default_factory=dict)
    """The package's parts, by their names in lower case."""
    types: dict[str, str] = field(default_factory=dict)
    """Each part's content type, by its name in lower case."""

    def check(self, file: IO[bytes], kind: Kind, *, nested: bool) -> None:
        """Check the package ``file`` holds as a ``kind`` document.

        Raises:
            FileRefused: It holds what is not accepted, or cannot be read.
            NotOfType: It is no Office document, or not of ``kind``.
        """
        try:
            with zipfile.ZipFile(file) as archive:
                self.parts = self._bounded(archive)
                self.types = self._content_types(archive)
                self._check_main(self._main(archive), kind)
                for name, info in self.parts.items():
                    self.budget.clock.check()
                    self._check_part(archive, name, info, nested=nested)
        except (FileRefused, NotOfType):
            raise
        except Exception as exc:  # A damaged ZIP raises anything: names, data.
            raise FileRefused(DAMAGED, code=UNREADABLE) from exc

    def _bounded(self, archive: zipfile.ZipFile) -> dict[str, zipfile.ZipInfo]:
        """The archive's parts by name, once its directory says they fit the bounds.

        Raises:
            FileRefused: Too many parts, too many bytes, a part inflating too
                much, an encrypted or oddly compressed part, or two parts of
                one name.
        """
        check = self.check_of
        parts: dict[str, zipfile.ZipInfo] = {}
        for info in archive.infolist():
            if info.is_dir():
                continue
            self.budget.parts -= 1
            self.budget.size -= info.file_size
            if self.budget.parts < 0 or self.budget.size < 0:
                raise FileRefused(COMPLEX)
            if info.file_size > RATIO_FLOOR and info.file_size > check.max_ratio * max(
                info.compress_size, 1
            ):
                raise FileRefused(COMPLEX)
            if info.flag_bits & 0x1:
                raise FileRefused(ENCRYPTED)
            if info.compress_type not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}:
                raise FileRefused(DAMAGED, code=UNREADABLE)
            key = info.filename.lower()
            if key in parts:
                raise FileRefused(DAMAGED, code=UNREADABLE)
            parts[key] = info
        return parts

    def _content_types(self, archive: zipfile.ZipFile) -> dict[str, str]:
        """Each part's content type, from ``[Content_Types].xml``.

        Raises:
            NotOfType: The package has no ``[Content_Types].xml``.
        """
        if "[content_types].xml" not in self.parts:
            raise NotOfType
        defaults: dict[str, str] = {}
        overrides: dict[str, str] = {}
        for element in self._elements(self._xml(archive, "[content_types].xml") or b""):
            if element.tag == f"{CONTENT_TYPES}Default":
                extension = str(element.get("Extension", "")).lower()
                defaults[extension] = str(element.get("ContentType", ""))
            elif element.tag == f"{CONTENT_TYPES}Override":
                name = str(element.get("PartName", "")).lstrip("/").lower()
                overrides[unquote(name)] = str(element.get("ContentType", ""))
        return {
            name: overrides.get(name) or defaults.get(_extension(name), "")
            for name in self.parts
        }

    def _main(self, archive: zipfile.ZipFile) -> str:
        """The content type of the package's main part, which ``_rels/.rels`` names.

        Raises:
            NotOfType: The package names no main part, or several.
        """
        if "_rels/.rels" not in self.parts:
            raise NotOfType
        found = [
            target
            for kind, target, external in self._relationships(archive, "_rels/.rels")
            if kind.endswith("/officedocument") and not external
        ]
        if len(found) != 1:
            raise NotOfType
        return self.types.get(found[0], "")

    def _check_main(self, main: str, kind: Kind) -> None:
        """Refuse a main part that is macro-enabled, or not ``kind``'s.

        Raises:
            FileRefused: It is macro-enabled.
            NotOfType: It is another document, or a template.
        """
        if main == OFFICE_MAIN[kind]:
            return
        if "macroenabled" in main.lower():
            raise FileRefused(MACROS)
        raise NotOfType

    def _check_part(
        self,
        archive: zipfile.ZipFile,
        name: str,
        info: zipfile.ZipInfo,
        *,
        nested: bool,
    ) -> None:
        """Refuse a part by its name, its type, its relationships, or its XML.

        Every part is opened, so that a header naming it otherwise than the
        directory does is found (``zipfile`` refuses it).

        Raises:
            FileRefused: It is refused.
        """
        content_type = self.types.get(name, "").lower()
        if name.rpartition("/")[2] == "vbaproject.bin":
            raise FileRefused(MACROS)
        for marker, message in OFFICE_REFUSED_TYPES:
            if marker in content_type:
                raise FileRefused(message)
        if name.endswith(".rels"):
            for kind, target, external in self._relationships(archive, name):
                self._check_relationship(archive, kind, target, external, nested=nested)
            return
        data = self._xml(archive, name)
        if data is None:
            return
        fields = _Fields()
        for element in self._elements(data):
            fields.read(element)
            _check_formula(element)
            if any(element.get(link) is not None for link in LINKS):
                raise FileRefused(EXTERNAL)  # A picture or an object linked, not held.

    def _xml(self, archive: zipfile.ZipFile, name: str) -> bytes | None:
        """The part ``name``, if it holds XML; refused if it declares a DTD.

        Read whole, but never past its stated size. None for a part that
        is not XML (an image); every part is opened all the same.

        Raises:
            FileRefused: It declares a DTD or an entity, or is XML in an
                encoding a package may not use.
        """
        info = self.parts[name]
        with archive.open(info) as part:
            head = part.read(256)
            encoding = _xml_encoding(head)
            if encoding is None:
                return None
            data = head + part.read(info.file_size + 1 - len(head))
        text = codecs.decode(data, encoding, "replace")
        if any(declaration in text for declaration in DECLARATIONS):
            raise FileRefused(DTD)
        return data

    def _check_relationship(
        self,
        archive: zipfile.ZipFile,
        kind: str,
        target: str,
        external: bool,
        *,
        nested: bool,
    ) -> None:
        """Refuse a relationship to macros, objects, or outside the package.

        An external one may be a hyperlink to a web page or a mail address;
        an embedded package is checked as the document it is, one level deep.

        Raises:
            FileRefused: It is refused.
        """
        last = kind.rpartition("/")[2]
        if last in OFFICE_REFUSED_RELATIONSHIPS:
            raise FileRefused(OFFICE_REFUSED_RELATIONSHIPS[last])
        if external:
            if last != "hyperlink":
                raise FileRefused(EXTERNAL)
            _check_link(target)
        elif last == "package":
            embedded = OFFICE_PACKAGES.get(self.types.get(target, ""))
            if embedded is None or nested:
                raise FileRefused(EMBEDDED)
            inner = _Package(self.check_of, self.etree, self.budget)
            with tempfile.TemporaryFile() as copy:
                with archive.open(self.parts[target]) as part:
                    shutil.copyfileobj(part, copy, CHUNK)
                copy.seek(0)
                try:
                    inner.check(copy, embedded, nested=True)
                except NotOfType:
                    raise FileRefused(EMBEDDED) from None

    def _relationships(
        self, archive: zipfile.ZipFile, name: str
    ) -> Iterator[tuple[str, str, bool]]:
        r"""Each relationship of the ``.rels`` part ``name``: type, target, external.

        A type is in lower case. A relationship is external unless its
        ``TargetMode`` is absent or ``Internal``, or when its target names
        a scheme or a share (``http:``, ``C:``, ``\\\\host``, ``//host``),
        whatever its mode says. An internal target is the part's name, in
        lower case, resolved from the part the relationships are of.
        """
        source = dirname(dirname(name))  # word/_rels/document.xml.rels: word
        for element in self._elements(self._xml(archive, name) or b""):
            if element.tag != RELATIONSHIPS:
                continue
            kind = str(element.get("Type", "")).lower()
            target = str(element.get("Target", ""))
            mode = element.get("TargetMode")
            if (
                (mode is not None and mode != "Internal")
                or urlsplit(target).scheme
                or target.startswith(("\\\\", "//"))
            ):
                yield kind, target, True
                continue
            within = (
                target.lstrip("/") if target.startswith("/") else join(source, target)
            )
            yield kind, unquote(normpath(within)).lower(), False

    def _elements(self, data: bytes) -> Iterator[Any]:
        """The elements of the XML ``data``, each once it ends.

        Parsed by lxml without entities, a DTD or the network (the bytes
        hold none: ``_xml`` refused them). An element is dropped once read,
        so a large part is not held whole.

        Raises:
            FileRefused: It is not XML, or the time ran out.
        """
        events = self.etree.iterparse(
            io.BytesIO(data),
            events=("end",),
            resolve_entities=False,
            no_network=True,
            load_dtd=False,
            huge_tree=False,
        )
        clock = self.budget.clock
        try:
            for index, (_, element) in enumerate(events):
                if index % DEADLINE_EVERY == 0:
                    clock.check()
                yield element
                element.clear(keep_tail=True)
                while element.getprevious() is not None:
                    del element.getparent()[0]
        except self.etree.XMLSyntaxError as exc:
            raise FileRefused(DAMAGED, code=UNREADABLE) from exc


def _xml_encoding(head: bytes) -> str | None:
    """The encoding of the XML a part starts with; None if it is not XML.

    Raises:
        FileRefused: It is XML in an encoding but UTF-8 or UTF-16, which
            a package may not use (UTF-32, EBCDIC, another a declaration
            names): a DTD would hide in it.
    """
    if head.startswith((codecs.BOM_UTF32_LE, codecs.BOM_UTF32_BE, b"\x4c\x6f\xa7\x94")):
        raise FileRefused(DAMAGED, code=UNREADABLE)
    if head.startswith((codecs.BOM_UTF16_LE, b"<\x00")):
        return "utf-16-le"
    if head.startswith((codecs.BOM_UTF16_BE, b"\x00<")):
        return "utf-16-be"
    stripped = head.removeprefix(codecs.BOM_UTF8).lstrip(b" \t\r\n")
    if not stripped.startswith(b"<"):
        return None
    declared = XML_DECLARED.match(stripped)
    if declared is not None:
        name = declared[1].decode("ascii", "replace").lower().replace("-", "")
        if name not in XML_ENCODINGS:
            raise FileRefused(DAMAGED, code=UNREADABLE)
    return "utf-8"


def _extension(name: str) -> str:
    """The extension of the part ``name``, without its dot; empty if none."""
    last = name.rpartition("/")[2]
    return last.rpartition(".")[2] if "." in last else ""


def _local(element: Any, namespaces: tuple[str, ...]) -> str | None:  # noqa: ANN401 - an lxml element.
    """``element``'s name in one of ``namespaces``; None in another."""
    tag = str(element.tag)
    return next((tag[len(each) :] for each in namespaces if tag.startswith(each)), None)


def _check_formula(element: Any) -> None:  # noqa: ANN401 - an lxml element.
    """Refuse a cell formula or a defined name that calls outside the workbook.

    Raises:
        FileRefused: It calls a web service, a real-time data server, or
            DDE (``app|topic!item``).
    """
    if _local(element, SHEETML) not in {"f", "definedName"}:
        return
    formula = element.text or ""
    if FORMULA_CALLS.search(formula):
        raise FileRefused(EXTERNAL)
    if "|" in FORMULA_STRINGS.sub("", formula):
        raise FileRefused(ACTIONS)


@dataclass(slots=True)
class _Fields:
    """The Word fields of a part, read one element at a time."""

    open: list[list[str]] = field(default_factory=list)
    """The instructions of the fields begun and not yet separated or ended."""

    def read(self, element: Any) -> None:  # noqa: ANN401 - an lxml element.
        """Note what ``element`` adds to a field; refuse a field that is.

        Raises:
            FileRefused: A field runs a program or pulls content in, or its
                instruction starts with another field, which hides its name.
        """
        local = _local(element, WORDML)
        if local == "fldSimple":
            for name, value in element.attrib.items():
                if str(name).endswith("}instr"):
                    _check_field(str(value))
        elif local == "instrText" and self.open:
            self.open[-1].append(element.text or "")
        elif local == "fldChar":
            mark = next(
                (
                    str(value)
                    for name, value in element.attrib.items()
                    if str(name).endswith("}fldCharType")
                ),
                "",
            )
            if mark == "begin":
                if self.open and not "".join(self.open[-1]).strip():
                    raise FileRefused(ACTIONS)  # { {QUOTE 68 68 69 ...} ... }
                self.open.append([])
            elif mark in {"separate", "end"} and self.open:
                _check_field("".join(self.open.pop()))
                if mark == "separate":
                    self.open.append([RESULT])  # The result's own fields, until end.


def _check_field(instruction: str) -> None:
    """Refuse a field instruction that runs a program or pulls content in.

    Raises:
        FileRefused: It does.
    """
    name = FIELD_NAME.match(instruction)
    if name is not None and (message := OFFICE_FIELDS.get(name[1].upper())):
        raise FileRefused(message)
