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
the hook in a process of its own, on the whole file, before it stores it. A
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
holds a data connection or a query table, or a formula anywhere in a
sheet calls a web service (``WEBSERVICE``, ``RTD``), a picture from a URL
(``IMAGE``) or DDE (``cmd|'...'!A0``); when its XML writes an OLE object
or a control (``w:object``, ``o:OLEObject``, ``w:control``), or a picture
names a hyperlink's relationship; or when an XML part declares a DTD or
an entity, which the Open Packaging Conventions forbid, or is in an
encoding but UTF-8 and UTF-16. A part is XML by its name or its content
type, never by its first bytes, and is read whole; a DTD is refused in
its bytes, before lxml parses them, so nothing outside the file is ever
opened. An embedded workbook, document or
presentation (a chart's data) is checked the same way, one level deep,
from a temporary file.

``save_upload`` runs the checks in a process of their own
(``run_checks``): the system holds it to an address space and CPU time,
and it is killed past its wall clock. It gets none of the application's
settings or environment; it is not a sandbox, and runs as the same user,
with the same files and network. pypdf cannot be bounded from inside
the process that runs it: its constructor decodes a chain of tables
before any check starts, and one of its calls can run a minute (ADR
0167). The checks keep bounds of their own as a second layer: a
deadline (``max_seconds``) looked at per object or part; a PDF's object
streams decode to ``max_decoded`` bytes in all, each stream to
``PDF_STREAM_LIMIT`` (``PDF_LIMITS``); the check reads at most
``PDF_NODES`` values, and refuses a PDF pypdf read only in part; a ZIP's
sizes are read from its directory before a byte is inflated. The checks
do not make a file harmless; they refuse what is known to be used
against readers and converters. Keep the virus scan (``UPLOAD_SCANNER``)
too.
"""

import asyncio
import codecs
import contextlib
import io
import json
import logging
import math
import os
import pickle
import re
import shutil
import signal
import struct
import sys
import tempfile
import threading
import time
import traceback
import unicodedata
import zipfile
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field
from posixpath import dirname, join, normpath
from typing import IO, TYPE_CHECKING, Any, Final, Literal, Self
from urllib.parse import unquote, urlsplit

from domainkit.exceptions import DomainkitError, ImproperlyConfigured, ServiceBusy
from domainkit.i18n.lazy import LazyString, gettext_lazy, ngettext_lazy, pgettext_lazy

if TYPE_CHECKING:
    from pypdf import PdfReader
    from pypdf.generic import IndirectObject

__all__ = ["FileRefused", "NotOfType", "OfficeCheck", "PdfCheck", "run_checks"]

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
PDF_SKIPPED: Final = (
    "Previous trailer cannot be read",
    "XRef object at",
    "Circular xref chain",
    "Invalid/Truncated xref table",
    "Invalid parent xref",
    "Ignoring wrong pointing object",
    "Invalid stream (index",
    "Invalid object number",
    "CAUTION: startxref found",
    "Clamping XRef count",
    "Negative XRef count",
)
"""What pypdf logs when it reads a file only in part, or elsewhere than its tables
say, even in strict mode: the start of its messages."""
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
FORMULA_CALLS: Final = re.compile(r"\b(?:WEBSERVICE|RTD|IMAGE)\s*\(", re.IGNORECASE)
"""Excel functions that reach outside the workbook when the sheet calculates."""
FORMULA_QUOTED: Final = re.compile(r""""(?:[^"]|"")*"|'(?:[^']|'')*'""")
"""A formula's text literals and quoted sheet names (``'Q1|Q2'!B1``), each from
its first quote to its last (a quote inside is doubled), left out before it is
read for DDE."""
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
OBJECTS: Final = frozenset(
    {
        *(
            f"{namespace}{name}"
            for namespace in WORDML
            for name in ("object", "control")
        ),
        "{urn:schemas-microsoft-com:office:office}OLEObject",
    }
)
"""Elements of an OLE object or an ActiveX control, refused wherever they are."""
RELATIONSHIP_NAMESPACES: Final = (
    "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}",
    "{http://purl.oclc.org/ooxml/officeDocument/relationships}",
)
"""Where an attribute that names a relationship (``r:id``) lives."""
HYPERLINK_ELEMENTS: Final = frozenset(
    {"hyperlink", "hlinkClick", "hlinkHover", "hlinkMouseOver"}
)
"""Elements that may name a hyperlink to outside the package."""
FORMULA_NAMESPACES: Final = (
    *SHEETML,
    "{http://schemas.microsoft.com/office/excel/2006/main}",
    "{http://schemas.openxmlformats.org/drawingml/2006/chart}",
)
"""Where a formula's element lives: SpreadsheetML, Excel 2010's, a chart's."""
FORMULA_ELEMENTS: Final = frozenset(
    {
        "f",
        "definedName",
        "formula",
        "formula1",
        "formula2",
        "calculatedColumnFormula",
        "totalsRowFormula",
    }
)
"""Elements that hold a formula."""
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
XML_DECLARED: Final = re.compile(r"""encoding\s*=\s*["']([^"']*)["']""")
"""The encoding an XML declaration names."""
MARKS: Final = {
    codecs.BOM_UTF8: "utf-8",
    codecs.BOM_UTF16_LE: "utf-16-le",
    codecs.BOM_UTF16_BE: "utf-16-be",
}
"""The byte order marks an XML part may start with, and their encodings."""
XML_EXTENSIONS: Final = frozenset({"xml", "rels", "vml"})
"""Parts XML by their name."""
XML_TYPES: Final = frozenset({"application/xml", "text/xml"})
"""Parts XML by their content type, besides those ending in ``+xml``."""
XML_ENCODINGS: Final = frozenset({"utf8", "utf16"})
"""What an XML part of a package may be in (ECMA-376 Part 2)."""
DECLARATIONS: Final = ("<!DOCTYPE", "<!ENTITY")
"""What declares a DTD or an entity: refused before lxml reads the part."""


class FileRefused(DomainkitError):
    """The file is of its type, but holds what is not accepted.

    ``message`` is safe to show: text, in English, which the HTTP layer
    translates, or a lazy string (``gettext_lazy``, ``ngettext_lazy``,
    ``pgettext_lazy``), made text in the caller's language. ``code`` is
    the problem's: ``unsafe_file`` by default, or ``unreadable_file``.
    ``save_upload`` answers it 422.
    """

    message: str
    """Kept as given, and declared text, as ``DomainError.message`` is: a
    lazy one compares and formats as its text, and ``str()`` makes it text."""

    def __init__(self, message: str | LazyString, *, code: str = "unsafe_file") -> None:
        """Say why, and with which code."""
        super().__init__(message)
        self.message = message  # type: ignore[assignment]  # Kept lazy: see above.
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
        with pypdf.apply_configuration(**PDF_LIMITS), _Skipped() as skipped:
            try:
                reader = pypdf.PdfReader(
                    io.BytesIO(data),
                    strict=True,
                    xref_stream_maximum_entries=self.max_objects,
                )
                skipped.check()
                if reader.is_encrypted:
                    raise FileRefused(ENCRYPTED)
                _Pdf(self, reader, data, clock).check()
                skipped.check()
            except FileRefused:
                raise
            except Exception as exc:  # pypdf raises anything on a malformed file.
                short = isinstance(exc, MemoryError) or "allocate" in str(exc)
                raise FileRefused(
                    TOO_MUCH if short else DAMAGED, code=UNREADABLE
                ) from exc


class _Heard:
    """pypdf's warnings let through while a PDF is checked, however logging is set.

    A check reads what pypdf skipped from its warnings, which a logger set
    above ``WARNING``, a disabled one, or ``logging.disable`` would drop.
    The first check to start saves how pypdf's loggers and
    ``logging.disable`` are set and lets ``WARNING`` through; the last to
    end puts them back. Meanwhile, in this process, ``logging.disable``
    lets every logger's warnings through, as it does in a check's process
    of its own, where nothing else runs.
    """

    def __init__(self) -> None:
        """Nothing saved yet."""
        self.lock = threading.Lock()
        self.checks = 0
        self.disabled = logging.NOTSET
        self.saved: list[tuple[logging.Logger, int, bool, bool]] = []

    def __enter__(self) -> None:
        """Let pypdf's warnings through, if no other check does already."""
        with self.lock:
            self.checks += 1
            if self.checks > 1:
                return
            top = logging.getLogger("pypdf")
            loggers = [
                top,
                *(
                    each
                    for name, each in list(logging.Logger.manager.loggerDict.items())
                    if name.startswith("pypdf.") and isinstance(each, logging.Logger)
                ),
            ]
            self.saved = [
                (each, each.level, each.disabled, each.propagate) for each in loggers
            ]
            self.disabled = logging.Logger.manager.disable
            for each in loggers:  # Below "pypdf", each passes its records up.
                level = each.getEffectiveLevel() if each is top else each.level
                if level > logging.WARNING:
                    each.setLevel(logging.WARNING)
                each.disabled = False
                each.propagate = True if each is not top else each.propagate
            if self.disabled >= logging.WARNING:
                logging.disable(logging.INFO)

    def __exit__(self, *exc: object) -> None:
        """Put logging back as it was, once the last check has ended."""
        with self.lock:
            self.checks -= 1
            if self.checks:
                return
            for each, level, disabled, propagate in self.saved:
                each.setLevel(level)
                each.disabled = disabled
                each.propagate = propagate
            if self.disabled >= logging.WARNING:
                logging.disable(self.disabled)


_heard = _Heard()


class _Skipped(logging.Handler):
    """What pypdf says it skipped or repaired, in this thread, while a check runs.

    Even in strict mode pypdf passes over an earlier table it cannot read
    (out of memory, a loop), or finds an object elsewhere than its table
    says, and only logs it: what it skipped, the check never read. Its
    warnings are heard however logging is set (``_Heard``).
    """

    def __init__(self) -> None:
        """Note nothing yet; only this thread's records."""
        super().__init__(logging.WARNING)
        self.thread = threading.get_ident()
        self.said: list[str] = []
        self.logger = logging.getLogger("pypdf")

    def emit(self, record: logging.LogRecord) -> None:
        """Keep what this thread's pypdf said."""
        if record.thread == self.thread:
            self.said.append(record.getMessage())

    def __enter__(self) -> Self:
        """Listen to pypdf until the block ends."""
        _heard.__enter__()
        self.logger.addHandler(self)
        return self

    def __exit__(self, *exc: object) -> None:
        """Stop listening."""
        self.logger.removeHandler(self)
        _heard.__exit__()

    def check(self) -> None:
        """Refuse a file pypdf read only in part.

        Raises:
            FileRefused: pypdf skipped or repaired something
                (``unreadable_file``): out of memory, or damaged.
        """
        for said in self.said:
            if said.startswith(PDF_SKIPPED):
                short = "allocate" in said
                raise FileRefused(TOO_MUCH if short else DAMAGED, code=UNREADABLE)


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
            self.clock.check()  # Each may read to the end of the file.
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
    hyperlinks: dict[str, set[str]] = field(default_factory=dict)
    """The ids of each part's hyperlinks to outside the package, by part."""

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
                for rels in (True, False):  # Relationships first: parts name them.
                    for name, info in self.parts.items():
                        if name.endswith(".rels") is rels:
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
            for kind, target, external, _ in self._relationships(archive, "_rels/.rels")
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
            source = join(dirname(dirname(name)), name.rpartition("/")[2][:-5])
            for kind, target, external, rid in self._relationships(archive, name):
                self._check_relationship(archive, kind, target, external, nested=nested)
                if external:  # A hyperlink, as the check of it found.
                    self.hyperlinks.setdefault(source, set()).add(rid)
            return
        data = self._xml(archive, name)
        if data is None:
            return
        fields = _Fields()
        hyperlinks = self.hyperlinks.get(name, set())
        for element in self._elements(data):
            fields.read(element)
            _check_formula(element)
            _check_element(element, hyperlinks)

    def _xml(self, archive: zipfile.ZipFile, name: str) -> bytes | None:
        """The part ``name``, if its name or content type says XML; refused if bad.

        Whether a part is XML is never read from its bytes, which may start
        with anything. It is read whole, but never past its stated size;
        another part (an image) is opened all the same, and not read.

        Raises:
            FileRefused: It declares a DTD or an entity, or is XML in an
                encoding a package may not use, or holds more than its
                stated size.
        """
        info = self.parts[name]
        with archive.open(info) as part:
            if not _is_xml(name, self.types.get(name, "")):
                return None
            # In chunks, and never past the stated size: read whole at
            # once, zipfile copies what it has so far, four times the part.
            buffer = io.BytesIO()
            shutil.copyfileobj(part, buffer, CHUNK)
        data = buffer.getvalue()
        encoding = _xml_encoding(data)
        if any(declaration.encode(encoding) in data for declaration in DECLARATIONS):
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
    ) -> Iterator[tuple[str, str, bool, str]]:
        r"""Each relationship of the ``.rels`` part ``name``: type, target, mode, id.

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
            rid = str(element.get("Id", ""))
            mode = element.get("TargetMode")
            if (
                (mode is not None and mode != "Internal")
                or urlsplit(target).scheme
                or target.startswith(("\\\\", "//"))
            ):
                yield kind, target, True, rid
                continue
            within = (
                target.lstrip("/") if target.startswith("/") else join(source, target)
            )
            yield kind, unquote(normpath(within)).lower(), False, rid

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


def _is_xml(name: str, content_type: str) -> bool:
    """Whether a part is XML, by its name or its content type: never its bytes."""
    return (
        _extension(name) in XML_EXTENSIONS
        or content_type.endswith("+xml")
        or content_type in XML_TYPES
    )


def _xml_encoding(data: bytes) -> str:
    """The encoding of the XML part ``data``, read from its start.

    White space before the first markup is passed over, however long; the
    declaration is read whole, wherever it ends.

    Raises:
        FileRefused: It is in an encoding but UTF-8 or UTF-16, which a
            package may not use (UTF-32, EBCDIC, another a declaration
            names), or something else than white space comes first.
    """
    if data.startswith((codecs.BOM_UTF32_LE, codecs.BOM_UTF32_BE, b"\x4c\x6f\xa7\x94")):
        raise FileRefused(DAMAGED, code=UNREADABLE)
    mark = next((each for each in MARKS if data.startswith(each)), None)
    if mark is not None:
        encoding, after = MARKS[mark], len(mark)
    else:
        first, after = data.find(b"<"), 0
        encoding = (
            "utf-16-be"
            if first % 2 == 1 and data[first - 1] == 0
            else "utf-16-le"
            if data[first + 1 : first + 2] == b"\x00"
            else "utf-8"
        )
    # Offsets, not slices: a slice of a large part is a copy of it.
    start = data.find("<".encode(encoding), after)
    if start < 0 or data[after:start].decode(encoding, "replace").strip(" \t\r\n"):
        raise FileRefused(DAMAGED, code=UNREADABLE)
    if data.startswith("<?xml".encode(encoding), start):
        end = data.find("?>".encode(encoding), start)
        declaration = data[start : max(end, start)].decode(encoding, "replace")
        named = XML_DECLARED.search(declaration)
        if end < 0 or (
            named is not None and named[1].lower().replace("-", "") not in XML_ENCODINGS
        ):
            raise FileRefused(DAMAGED, code=UNREADABLE)
    return encoding


def _extension(name: str) -> str:
    """The extension of the part ``name``, without its dot; empty if none."""
    last = name.rpartition("/")[2]
    return last.rpartition(".")[2] if "." in last else ""


def _local(element: Any, namespaces: tuple[str, ...]) -> str | None:  # noqa: ANN401 - an lxml element.
    """``element``'s name in one of ``namespaces``; None in another."""
    tag = str(element.tag)
    return next((tag[len(each) :] for each in namespaces if tag.startswith(each)), None)


def _check_formula(element: Any) -> None:  # noqa: ANN401 - an lxml element.
    """Refuse a formula that calls outside the workbook, wherever a sheet keeps one.

    A cell's, a defined name's, a conditional format's, a validation's, a
    table column's, Excel 2010's (``xm:f``), or a chart's.

    Raises:
        FileRefused: It calls a web service, a real-time data server, a
            picture from a URL (``IMAGE``), or DDE (``app|topic!item``).
    """
    if _local(element, FORMULA_NAMESPACES) not in FORMULA_ELEMENTS:
        return
    formula = element.text or ""
    if FORMULA_CALLS.search(formula):
        raise FileRefused(EXTERNAL)
    if "|" in FORMULA_QUOTED.sub("", formula):
        raise FileRefused(ACTIONS)


def _check_element(element: Any, hyperlinks: set[str]) -> None:  # noqa: ANN401 - an lxml element.
    """Refuse an embedded object, a control, a link, or a hyperlink used as one.

    ``hyperlinks`` are the ids of the part's relationships to outside the
    package, which only a hyperlink may name: a picture or an object that
    names one is fetched from there.

    Raises:
        FileRefused: It is one of them.
    """
    tag = str(element.tag)
    if tag in OBJECTS:
        raise FileRefused(EMBEDDED)
    if any(element.get(link) is not None for link in LINKS):
        raise FileRefused(EXTERNAL)  # A picture or an object linked, not held.
    if hyperlinks and tag.rpartition("}")[2] not in HYPERLINK_ELEMENTS:
        for name, value in element.attrib.items():
            if str(name).startswith(RELATIONSHIP_NAMESPACES) and value in hyperlinks:
                raise FileRefused(EXTERNAL)


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


# A process of its own ----------------------------------------------------------------


BOOTSTRAP: Final = (
    "import json,os,sys\n"
    "config=json.loads(sys.stdin.buffer.readline())\n"
    "sys.path[:0]=config['path']\n"
    "answer=os.fdopen(os.dup(1),'wb')\n"
    "os.dup2(2,1)\n"
    "from domainkit.inspection import _serve\n"
    "_serve(config,sys.stdin.buffer,answer)\n"
)
"""What a check's process runs (``python -I -X faulthandler -c``): the parent's
path, then the check. What the check prints goes to standard error, never
into its answer, and so does the traceback of a crash."""
ENVIRONMENT: Final = frozenset(
    {"PATH", "LANG", "LANGUAGE", "TMPDIR", "TEMP", "TMP", "SYSTEMROOT"}
)
"""What a check's process gets of the parent's environment, with each
``LC_*``: never the application's settings or secrets. ``-I`` ignores the
``PYTHON*`` ones; Windows needs ``SYSTEMROOT`` to start Python."""
MAX_ANSWER: Final = 64 * 1024
"""The most bytes of a check's answer the parent reads."""
MAX_SAID: Final = 4 * 1024
"""The most bytes of a check's standard error the parent keeps, its last, to log."""
OOM_SCORE: Final = "/proc/self/oom_score_adj"
"""Where Linux reads whom its out-of-memory killer ends first (1000: this one)."""
CODE: Final = re.compile(r"[a-z][a-z0-9_]{0,63}")
"""A problem code a check may answer with."""
MAX_MESSAGE: Final = 500
"""The most characters the parent keeps of a check's message, or of a text it names."""
MAX_NAME: Final = 100
"""The most characters the parent keeps of the name of what a check raised."""
HIDDEN: Final = frozenset({"Cc", "Cf"})
"""Unicode's control and format characters (a right-to-left override): dropped
from a text a check's process sends, but for white space, which is a space."""
LOOKUPS: Final[dict[str, tuple[Callable[..., LazyString], tuple[type, ...]]]] = {
    "gettext": (gettext_lazy, (str,)),
    "ngettext": (ngettext_lazy, (str, str, int)),
    "pgettext": (pgettext_lazy, (str, str)),
}
"""The lookups a check's lazy refusal may name (``LazyString.source``), what
makes each again in the parent, and the types of its arguments."""
MAX_COUNT: Final = 2**64 - 1
"""The largest count a refusal's ``ngettext`` may name: C's ``unsigned long``."""
BUSY: Final = "Uploads cannot be checked now; try again later."
TOO_LONG: Final = "The file took too long to check."
TOO_MUCH: Final = "The file needed too much memory to check."

logger = logging.getLogger(__name__)

_running = 0
"""The checks' processes running now, in this process, on any event loop."""
_running_lock = threading.Lock()


async def run_checks(
    checks: Sequence[Callable[[IO[bytes]], None] | None],
    file: IO[bytes],
    *,
    processes: int,
    memory: int,
    seconds: float,
) -> int:
    """Run ``checks`` on ``file`` in a process of their own; the first that took it.

    The process is handed ``file`` from its start to its end, read in a
    thread, and the checks are tried in order on all of it, as
    ``save_upload`` tries the types of a file: one that raises
    ``NotOfType`` passes it to the next, and None takes it unchecked.
    The process gets ``memory`` bytes of address space and ``seconds`` of
    CPU from the system (``RLIMIT_AS``, ``RLIMIT_CPU``, where there are
    such limits; not on Windows), and is killed past ``seconds`` of wall
    clock. It gets none of this process's environment but ``PATH``, the
    locale and the temporary directory, and a session of its own. At most
    ``processes`` run at once in this process; a slot is freed only once
    its process has exited, a request cancelled meanwhile included. Each
    check must pickle, and be importable by the process: a function at
    the top level of a module that is not ``__main__``, or an object of a
    class that is.

    Raises:
        FileRefused: A check refused the file (a lazy message is made
            again here, from the lookup its process named), or its process
            was stopped: past its clock or CPU (``unreadable_file``, too
            long), out of memory as it said (too much), or by another
            signal, logged (the file could not be read).
        NotOfType: No check took it.
        ServiceBusy: ``processes`` checks run already (503).
        ImproperlyConfigured: A check raised it in its process: its
            library is not installed, or it read the settings, which the
            process has not. Logged with where it was raised.
        RuntimeError: A check failed otherwise, as a bug does, or its
            process ended without an answer; logged with what it wrote to
            standard error.
    """
    global _running  # The count every event loop of the process shares.
    with _running_lock:
        if _running >= processes:
            raise ServiceBusy(BUSY)
        _running += 1
    try:
        return await _in_a_process(list(checks), file, memory, seconds)
    finally:
        with _running_lock:
            _running -= 1


async def _in_a_process(
    checks: list[Callable[[IO[bytes]], None] | None],
    file: IO[bytes],
    memory: int,
    seconds: float,
) -> int:
    """Start the process, hand it the checks and the file, read its answer.

    The process has exited when this returns or raises, cancelled or not.
    """
    path = [os.fspath(each) for each in sys.path if isinstance(each, str | os.PathLike)]
    config = {"path": path, "memory": memory, "seconds": seconds}
    request = pickle.dumps(checks)
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-I",
        "-X",
        "faulthandler",
        "-c",
        BOOTSTRAP,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=_environment(),
        start_new_session=True,  # Not the parent's group: no signal meant for it.
    )
    said = bytearray()
    errors = asyncio.ensure_future(_tail(process, said))
    clock = asyncio.timeout(seconds)
    answer = b""
    try:
        async with clock:
            await _hand(process, json.dumps(config).encode() + b"\n", request, file)
            answer = await _answer(process)
            await process.wait()
    except TimeoutError:
        pass  # The clock's: ``expired`` says so below.
    finally:
        try:
            await _ended(process)
            await asyncio.wait({errors}, timeout=1.0)  # Its end closes as it exits.
        finally:
            errors.cancel()
    if clock.expired():
        logger.warning(
            "A check of an upload ran past %s seconds; it was killed.", seconds
        )
        raise FileRefused(TOO_LONG, code=UNREADABLE)
    returncode = process.returncode
    assert returncode is not None  # noqa: S101 - it has exited.
    return _outcome(answer, returncode, bytes(said), len(checks))


def _environment() -> dict[str, str]:
    """What a check's process gets of this environment: no settings, no secrets."""
    return {
        name: value
        for name, value in os.environ.items()
        if name in ENVIRONMENT or name.startswith("LC_")
    }


async def _hand(
    process: asyncio.subprocess.Process, config: bytes, request: bytes, file: IO[bytes]
) -> None:
    """Write the configuration, the request, and the file to its end to the process.

    The file is read in a thread: it may be on disk. A process that
    stopped reading (it refused at once, or a limit ended it) is not
    written to further.
    """
    writer = process.stdin
    assert writer is not None  # noqa: S101 - made with a pipe.
    try:
        writer.write(config + struct.pack(">Q", len(request)) + request)
        await asyncio.to_thread(file.seek, 0)
        while chunk := await asyncio.to_thread(file.read, CHUNK):
            writer.write(chunk)
            await writer.drain()
        writer.close()
    except (BrokenPipeError, ConnectionResetError):
        return


async def _answer(process: asyncio.subprocess.Process) -> bytes:
    """The process's answer, at most ``MAX_ANSWER`` bytes."""
    reader = process.stdout
    assert reader is not None  # noqa: S101 - made with a pipe.
    answer = b""
    while len(answer) <= MAX_ANSWER and (chunk := await reader.read(CHUNK)):
        answer += chunk
    return answer


async def _tail(process: asyncio.subprocess.Process, said: bytearray) -> None:
    """Keep the last ``MAX_SAID`` bytes the process writes to standard error."""
    reader = process.stderr
    assert reader is not None  # noqa: S101 - made with a pipe.
    while chunk := await reader.read(CHUNK):
        said += chunk
        del said[:-MAX_SAID]


async def _ended(process: asyncio.subprocess.Process) -> None:
    """Kill the process if it still runs, and wait until it has exited.

    A cancellation while it is killed waits for it all the same, then is
    raised again.
    """
    if process.returncode is None:
        with contextlib.suppress(ProcessLookupError):
            process.kill()
    waiting = asyncio.ensure_future(process.wait())
    cancelled = False
    while not waiting.done():
        try:
            await asyncio.shield(waiting)
        except asyncio.CancelledError:
            cancelled = True
    if cancelled:
        raise asyncio.CancelledError


def _outcome(answer: bytes, returncode: int, said: bytes, count: int) -> int:
    """Which check took the file, from the process's answer, or how it ended.

    An answer is a JSON object, read as data (``_verdict``). A process
    that gave none ended by a signal or on its own. The system's CPU
    limit ends it with ``SIGXCPU``: too long. Another signal says no more
    than that it crashed: a failed allocation under ``RLIMIT_AS`` may
    end a library that way, and so may a bug, and the out-of-memory
    killer's ``SIGKILL`` is no one else's to tell apart. The file could
    not be read, then, and the signal is logged. A process that exited
    without an answer failed: its import, or a bug.

    Raises:
        FileRefused: A check refused it, or a signal ended the process.
        NotOfType: No check took it.
        ImproperlyConfigured: A check raised it in its process; logged.
        RuntimeError: A check failed, or the process gave no answer.
    """
    tail = said.decode(errors="replace")
    try:
        found = json.loads(answer)
    except (ValueError, RecursionError):
        found = None
    if isinstance(found, dict):
        return _verdict(found, count, tail)
    if returncode < 0:
        try:
            name = signal.Signals(-returncode).name
        except ValueError:
            name = f"signal {-returncode}"
        logger.warning("A check of an upload was ended by %s: %s", name, tail)
        raise FileRefused(TOO_LONG if name == "SIGXCPU" else DAMAGED, code=UNREADABLE)
    logger.error(
        "A check of an upload ended without an answer (exit status %s): %s",
        returncode,
        tail,
    )
    raise RuntimeError(
        f"A check of an upload ended without an answer (exit status {returncode})."
    )


def _verdict(found: dict[str, Any], count: int, tail: str) -> int:
    """Which check took the file, from the process's answer; else raise.

    The answer is JSON of a few known shapes, read as data: the process
    read an untrusted file, and could say anything. Each text in it is
    UTF-8 on one line (``_shown``), or the answer is of no known shape.

    Raises:
        FileRefused: A check refused it, with text or a lookup's lazy
            string (``_lazy``), or the answer is of no known shape.
        NotOfType: No check took it.
        ImproperlyConfigured: A check raised it in its process: its
            library is not installed, or it read the settings, which the
            process has not. Worded as the process's, and logged with
            ``tail``, the end of what the process wrote to standard error.
        RuntimeError: A check failed otherwise; logged with ``tail``.
    """
    took = found.get("took")
    if isinstance(took, int) and 0 <= took < count:
        return took
    refused = found.get("refused")
    message = _shown(refused) if isinstance(refused, str) else _lazy(refused)
    if message is not None:
        code = found.get("code")
        valid = isinstance(code, str) and CODE.fullmatch(code)
        raise FileRefused(message, code=code if valid else UNSAFE)  # type: ignore[arg-type]
    if found.get("none") is True:
        raise NotOfType
    missing = found.get("improperly")
    if isinstance(missing, str) and (said := _shown(missing)) is not None:
        logger.error("A check of an upload raised ImproperlyConfigured: %s", tail)
        raise ImproperlyConfigured(f"A check of an upload, in its process: {said}")
    failed = found.get("failed")
    if isinstance(failed, str) and (name := _shown(failed, MAX_NAME)) is not None:
        logger.error("A check of an upload failed in its process: %s", tail)
        raise RuntimeError(f"A check of an upload failed in its process: {name}.")
    raise FileRefused(DAMAGED, code=UNREADABLE)


def _shown(text: str, limit: int = MAX_MESSAGE) -> str | None:
    """``text`` from a check's process, cut to ``limit``, on one line; None if not text.

    JSON lets a lone surrogate through (an escaped ``D800``), which no body
    or log encodes: a text that does not encode as UTF-8 is of no known
    shape. Only what is kept of it counts. White space becomes a space,
    and a control or format character (``HIDDEN``) is dropped, so that a
    refusal is one line in a log, shown as it reads.
    """
    kept = text[:limit]
    try:
        kept.encode()
    except UnicodeEncodeError:
        return None
    return "".join(
        " " if character.isspace() else character
        for character in kept
        if character.isspace() or unicodedata.category(character) not in HIDDEN
    )


def _lazy(named: object) -> LazyString | None:
    """The lazy string a process's refusal names, or None if it names none.

    ``{"lookup": "ngettext", "arguments": ["A page.", "Pages.", 3]}``, and
    nothing else: a lookup of ``LOOKUPS``, with as many arguments as it
    takes, each of its exact type (a count is not a boolean). A text is
    read as a plain message is (``_shown``); a count is from 0 to
    ``MAX_COUNT``. The process read an untrusted file: it may name texts
    to look up, never code to run.
    """
    if not isinstance(named, dict) or named.keys() != {"lookup", "arguments"}:
        return None
    lookup, arguments = named["lookup"], named["arguments"]
    known = LOOKUPS.get(lookup) if isinstance(lookup, str) else None
    if known is None or not isinstance(arguments, list):
        return None
    make, types = known
    if [type(value) for value in arguments] != list(types) or any(
        isinstance(value, int) and not 0 <= value <= MAX_COUNT for value in arguments
    ):
        return None
    shown = [_shown(value) if isinstance(value, str) else value for value in arguments]
    return None if None in shown else make(*shown)


def _serve(config: dict[str, Any], source: IO[bytes], sink: IO[bytes]) -> None:
    """The check's process: limit itself, read the request and the file, answer.

    The request comes from the parent, which is trusted, pickled; the
    file is the rest of ``source``. The answer goes back as JSON, always:
    whatever a check raises, ``SystemExit`` too, is told to the parent,
    and its traceback written to standard error, which the parent logs.
    """
    _limit(int(config["memory"]), float(config["seconds"]))
    try:
        (length,) = struct.unpack(">Q", source.read(8))
        checks = pickle.loads(source.read(length))  # noqa: S301 - the parent's.
        answer = json.dumps(_checked(checks, source.read()), default=str)
    except MemoryError:
        answer = json.dumps({"refused": TOO_MUCH, "code": UNREADABLE})
    except BaseException as exc:  # noqa: BLE001 - told to the parent, which raises it.
        traceback.print_exc()
        answer = json.dumps({"failed": type(exc).__name__})
    sink.write(answer.encode() + b"\n")
    sink.flush()


def _checked(
    checks: list[Callable[[IO[bytes]], None] | None], data: bytes
) -> dict[str, Any]:
    """The verdict of ``checks`` on ``data``, as the parent reads it."""
    try:
        for index, check in enumerate(checks):
            if check is None:
                return {"took": index}
            try:
                check(io.BytesIO(data))
            except NotOfType:
                continue
            return {"took": index}
    except FileRefused as refused:
        return {"refused": _named(refused.message), "code": refused.code}
    except ImproperlyConfigured as missing:
        traceback.print_exc()  # Where it was raised, which the parent logs.
        return {"improperly": str(missing)}
    return {"none": True}


def _named(message: object) -> object:
    """A refusal's message as the parent reads it: text, or a lazy string's lookup.

    The process has no settings to make a lazy string text: it names the
    lookup and its arguments (``LazyString.source``), and the parent makes
    the string again (``_lazy``). Any other message is made text here.
    """
    if isinstance(message, LazyString) and (source := message.source()) is not None:
        lookup, arguments = source
        return {"lookup": lookup, "arguments": arguments}
    return str(message)


def _limit(memory: int, seconds: float) -> None:
    """Hold this process to ``memory`` bytes and ``seconds`` of CPU, where it can be.

    On Linux it also asks to be the first the out-of-memory killer ends.
    Windows has no such limits: the parent's clock alone ends a check
    there. A limit the system will not lower to (macOS keeps its own
    address space) is left as it is.
    """
    with contextlib.suppress(OSError), open(OOM_SCORE, "w") as score:  # noqa: PTH123
        score.write("1000")
    try:
        import resource
    except ImportError:
        return
    cpu = math.ceil(seconds)
    for kind, soft, hard in (
        (resource.RLIMIT_AS, memory, memory),
        (resource.RLIMIT_CPU, cpu, cpu + 1),
    ):
        with contextlib.suppress(OSError, ValueError):
            resource.setrlimit(kind, (soft, hard))
