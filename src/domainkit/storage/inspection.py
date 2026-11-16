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

A PDF is refused when it is encrypted, has more than ``max_pages`` pages,
or holds JavaScript, an action run on opening or on an event (``/OpenAction``
with an action, ``/AA``), an action that launches, opens another file or
sends data (``/Launch``, ``/GoToR``, ``/GoToE``, ``/SubmitForm``,
``/ImportData``), a link to anything but ``http``, ``https`` or ``mailto``,
an embedded file, rich media, or an XFA form. Every object the file
declares is read, not only those its pages reach, and names are compared
decoded (``/J#61vaScript`` is ``/JavaScript``).

An Office document is refused when its ZIP holds more than ``max_parts``
parts, inflates past ``max_size`` bytes in all or more than ``max_ratio``
times a part, or is encrypted; when its main part is macro-enabled (a
``.docm`` named ``.docx``) or not of the type asked; when it holds macros
(``vbaProject.bin``, Excel 4.0 macro sheets), OLE objects or ActiveX
controls; when a relationship points outside the package but for a
hyperlink to ``http``, ``https`` or ``mailto`` (a remote template, a
linked image or object: CVE-2022-30190 opened one); when a field pulls
content in or runs a program (``INCLUDEPICTURE``, ``INCLUDETEXT``,
``LINK``, ``DDE``, ``DDEAUTO``); when an Excel workbook links to another
or to DDE; or when any XML part declares a DTD, which the Open Packaging
Conventions forbid. XML is parsed by lxml without entities or network.
An embedded workbook, document or presentation (a chart's data) is
checked the same way, one level deep.

The checks bound what they read: a ZIP's sizes are read from its
directory before a byte is inflated, and pypdf runs with smaller limits
than its own (``PDF_LIMITS``) and a budget of objects. They do not make a
file harmless; they refuse what is known to be used against readers and
converters (ADR 0167). Keep the virus scan (``UPLOAD_SCANNER``) too.
"""

import io
import zipfile
import zlib
from collections.abc import Iterator
from dataclasses import dataclass, field
from posixpath import dirname, join, normpath
from typing import IO, TYPE_CHECKING, Any, Final, Literal
from urllib.parse import unquote, urlsplit

from domainkit.exceptions import DomainkitError, ImproperlyConfigured

if TYPE_CHECKING:
    from pypdf import PdfReader

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

PDF_LIMITS: Final = {
    "zlib_maximum_output_length": 32 * 1024 * 1024,
    "lzw_maximum_output_length": 32 * 1024 * 1024,
    "run_length_maximum_output_length": 32 * 1024 * 1024,
    "array_based_stream_maximum_output_length": 32 * 1024 * 1024,
    "maximum_declared_stream_length": 32 * 1024 * 1024,
    "array_maximum_element_count": 50_000,
    "dictionary_maximum_element_count": 10_000,
}
"""pypdf's limits while a PDF is checked: a stream inflates to 32 MiB at most."""
PDF_NODES: Final = 2_000_000
"""Objects, entries and items a PDF check reads at most."""
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
    "externallinkpath": EXTERNAL,
}
"""Relationships refused by the last segment of their type, in lower case."""
OFFICE_FIELDS: Final = {
    "DDE": ACTIONS,
    "DDEAUTO": ACTIONS,
    "INCLUDEPICTURE": EXTERNAL,
    "INCLUDETEXT": EXTERNAL,
    "LINK": EXTERNAL,
}
"""Word fields that run a program or pull content in when they are updated."""
WORDML: Final = (
    "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}",
    "{http://purl.oclc.org/ooxml/wordprocessingml/main}",
)
"""WordprocessingML's namespaces: transitional and strict."""
RELATIONSHIPS: Final = (
    "{http://schemas.openxmlformats.org/package/2006/relationships}Relationship"
)
CONTENT_TYPES: Final = "{http://schemas.openxmlformats.org/package/2006/content-types}"
RATIO_FLOOR: Final = 1024 * 1024
"""A part inflating to less than this is not held to ``max_ratio``."""
UNREAD: Final = (zipfile.BadZipFile, zlib.error, OSError, EOFError, KeyError)
"""What reading a damaged ZIP raises."""


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


@dataclass(frozen=True, slots=True)
class PdfCheck:
    """Refuse a PDF that runs, sends, or hides something; or is too long.

    See the module for what is refused. ``max_pages`` bounds the pages.
    """

    max_pages: int = 1000

    def __post_init__(self) -> None:
        """Refuse a bound that is not a positive number.

        Raises:
            ValueError: It is not.
        """
        if self.max_pages < 1:
            raise ValueError(f"max_pages is 1 or more, not {self.max_pages!r}.")

    def __call__(self, file: IO[bytes]) -> None:
        """Check the PDF ``file`` holds, read from its start.

        Raises:
            FileRefused: It holds what is not accepted, or cannot be read.
            ImproperlyConfigured: pypdf is not installed.
        """
        pypdf = _documents_extra("pypdf")
        with pypdf.apply_configuration(**PDF_LIMITS):
            try:
                reader = pypdf.PdfReader(file)
                if reader.is_encrypted:
                    raise FileRefused(ENCRYPTED)
                if len(reader.pages) > self.max_pages:
                    raise FileRefused(PAGES)
                _check_pdf(reader)
            except FileRefused:
                raise
            except Exception as exc:  # pypdf raises anything on a malformed file.
                raise FileRefused(DAMAGED, code=UNREADABLE) from exc


@dataclass(frozen=True, slots=True)
class OfficeCheck:
    """Refuse an Office document with macros, objects, or outside links.

    ``kind`` is the document taken: ``docx``, ``xlsx`` or ``pptx``. A ZIP
    holds at most ``max_parts`` parts and inflates to ``max_size`` bytes
    in all, and no part above 1 MiB more than ``max_ratio`` times. See
    the module for what is refused.
    """

    kind: Kind
    max_parts: int = 2000
    max_size: int = 200 * 1024 * 1024
    max_ratio: int = 200

    def __post_init__(self) -> None:
        """Refuse a kind not known, or a bound that is not a positive number.

        Raises:
            ValueError: One of them is.
        """
        if self.kind not in OFFICE_MAIN:
            raise ValueError(f"An Office check takes {sorted(OFFICE_MAIN)}.")
        for name in ("max_parts", "max_size", "max_ratio"):
            if getattr(self, name) < 1:
                raise ValueError(f"{name} is 1 or more, not {getattr(self, name)!r}.")

    def __call__(self, file: IO[bytes]) -> None:
        """Check the Office document ``file`` holds, read from its start.

        Raises:
            FileRefused: It holds what is not accepted, or cannot be read.
            NotOfType: It is no Office document, or not of ``kind``.
            ImproperlyConfigured: lxml is not installed.
        """
        etree = _documents_extra("lxml.etree")
        _Package(self, etree, _Budget(self.max_parts, self.max_size)).check(
            file, self.kind, nested=False
        )


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


# PDF ---------------------------------------------------------------------------------


def _check_pdf(reader: "PdfReader") -> None:
    """Read every object the PDF declares, and its trailer, for what is refused.

    References are not followed: each object is read once, as declared.

    Raises:
        FileRefused: Something refused is found, or there is too much to read.
    """
    from pypdf.generic import IndirectObject

    budget = [PDF_NODES]
    _check_pdf_value(reader.trailer, budget)
    numbered = [
        (number, generation)
        for generation, numbers in reader.xref.items()
        for number in numbers
    ]
    numbered += [(number, 0) for number in reader.xref_objStm]
    for number, generation in numbered:
        found = reader.get_object(IndirectObject(number, generation, reader))
        _check_pdf_value(found, budget)


def _check_pdf_value(value: object, budget: list[int]) -> None:
    """Refuse ``value`` if it, or a value written inside it, is refused.

    Raises:
        FileRefused: It is, or the budget ran out.
    """
    from pypdf.generic import ArrayObject, DictionaryObject, NameObject

    stack = [value]
    while stack:
        current = stack.pop()
        budget[0] -= 1
        if budget[0] < 0:
            raise FileRefused(COMPLEX)
        if isinstance(current, DictionaryObject):
            _check_pdf_dictionary(current)
            stack.extend(dict.values(current))
        elif isinstance(current, ArrayObject):
            stack.extend(list.__iter__(current))
        elif isinstance(current, NameObject) and current in PDF_REFUSED_NAMES:
            raise FileRefused(PDF_REFUSED_NAMES[current])


def _check_pdf_dictionary(entries: dict[Any, Any]) -> None:
    """Refuse a dictionary with a refused key, an action on opening, or a link.

    Raises:
        FileRefused: It has one.
    """
    from pypdf.generic import ArrayObject

    for key in entries:
        if key in PDF_REFUSED_KEYS:
            raise FileRefused(PDF_REFUSED_KEYS[key])
    opening = _resolved(dict.get(entries, "/OpenAction"))
    if opening is not None and not isinstance(opening, ArrayObject):
        # An action. A destination (an array) only shows a page.
        raise FileRefused(ACTIONS)
    target = _resolved(dict.get(entries, "/URI"))
    if isinstance(target, bytes):  # Not text: no link a reader would follow.
        raise FileRefused(EXTERNAL)
    if isinstance(target, str):
        _check_link(target)


def _resolved(value: object) -> object:
    """``value``, or the object it refers to."""
    from pypdf.generic import IndirectObject

    return value.get_object() if isinstance(value, IndirectObject) else value


def _check_link(target: str) -> None:
    """Refuse a link to anything but a web page or a mail address.

    Raises:
        FileRefused: It names a file, a share, or a program's scheme.
    """
    scheme = urlsplit(target.strip()).scheme.lower()
    if scheme not in LINK_SCHEMES:
        raise FileRefused(EXTERNAL)


# Office ------------------------------------------------------------------------------


@dataclass(slots=True)
class _Budget:
    """What a check may still read: parts, and inflated bytes."""

    parts: int
    size: int


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
            archive = zipfile.ZipFile(file)
        except UNREAD as exc:
            raise FileRefused(DAMAGED, code=UNREADABLE) from exc
        with archive:
            try:
                self.parts = self._bounded(archive)
                self.types = self._content_types(archive)
                self._check_main(self._main(archive), kind)
                for name, info in self.parts.items():
                    self._check_part(archive, name, info, nested=nested)
            except UNREAD as exc:
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
        for element in self._elements(archive, self.parts["[content_types].xml"]):
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
        elif content_type.endswith("xml") or name.endswith(".xml"):
            fields = _Fields()
            for element in self._elements(archive, info):
                fields.read(element)

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
            try:
                with archive.open(self.parts[target]) as part:
                    inner.check(io.BytesIO(part.read()), embedded, nested=True)
            except NotOfType:
                raise FileRefused(EMBEDDED) from None

    def _relationships(
        self, archive: zipfile.ZipFile, name: str
    ) -> Iterator[tuple[str, str, bool]]:
        """Each relationship of the ``.rels`` part ``name``: type, target, external.

        A type is in lower case. An internal target is the part's name, in
        lower case, resolved from the part the relationships are of.
        """
        source = dirname(dirname(name))  # word/_rels/document.xml.rels: word
        for element in self._elements(archive, self.parts[name]):
            if element.tag != RELATIONSHIPS:
                continue
            kind = str(element.get("Type", "")).lower()
            target = str(element.get("Target", ""))
            if element.get("TargetMode") == "External":
                yield kind, target, True
            else:
                within = (
                    target.lstrip("/")
                    if target.startswith("/")
                    else join(source, target)
                )
                yield kind, unquote(normpath(within)).lower(), False

    def _elements(
        self, archive: zipfile.ZipFile, info: zipfile.ZipInfo
    ) -> Iterator[Any]:
        """The elements of the XML part ``info``, each once it ends.

        Parsed by lxml without entities, a DTD or the network. An element
        is dropped once read, so a large part is never held whole.

        Raises:
            FileRefused: The part declares a DTD, or is not XML.
        """
        etree = self.etree
        with archive.open(info) as stream:
            events = etree.iterparse(
                stream,
                events=("end",),
                resolve_entities=False,
                no_network=True,
                load_dtd=False,
                huge_tree=False,
            )
            try:
                first = True
                for _, element in events:
                    if first and element.getroottree().docinfo.doctype:
                        raise FileRefused(DTD)
                    first = False
                    yield element
                    element.clear(keep_tail=True)
                    while element.getprevious() is not None:
                        del element.getparent()[0]
            except etree.XMLSyntaxError as exc:
                raise FileRefused(DAMAGED, code=UNREADABLE) from exc


def _extension(name: str) -> str:
    """The extension of the part ``name``, without its dot; empty if none."""
    last = name.rpartition("/")[2]
    return last.rpartition(".")[2] if "." in last else ""


@dataclass(slots=True)
class _Fields:
    """The Word fields of a part, read one element at a time."""

    open: list[list[str]] = field(default_factory=list)
    """The instructions of the fields begun and not yet separated or ended."""

    def read(self, element: Any) -> None:  # noqa: ANN401 - an lxml element.
        """Note what ``element`` adds to a field; refuse a field that is.

        Raises:
            FileRefused: A field runs a program or pulls content in.
        """
        tag = str(element.tag)
        local = next(
            (tag[len(each) :] for each in WORDML if tag.startswith(each)), None
        )
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
                self.open.append([])
            elif mark in {"separate", "end"} and self.open:
                _check_field("".join(self.open.pop()))
                if mark == "separate":
                    self.open.append([])  # The result's own fields, if any, until end.


def _check_field(instruction: str) -> None:
    """Refuse a field instruction that runs a program or pulls content in.

    Raises:
        FileRefused: It does.
    """
    words = instruction.split()
    if words and (message := OFFICE_FIELDS.get(words[0].upper())) is not None:
        raise FileRefused(message)
