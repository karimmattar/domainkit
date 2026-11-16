"""Documents built for the upload checks: PDFs and Office packages, by hand.

``pdf(...)`` writes a PDF of the objects given, with its cross-reference
table; ``office(...)`` writes the smallest Office package of a kind, with
the parts, content types and relationships a test adds. Nothing is read
from disk, and each test shows the one thing its document holds.
"""

import io
import warnings
import zipfile
from collections.abc import Mapping, Sequence
from typing import Final
from xml.sax.saxutils import quoteattr

R: Final = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/"
"""The transitional relationship types' namespace."""
MS: Final = "http://schemas.microsoft.com/office/2006/relationships/"
"""Microsoft's relationship types' namespace (macros, ActiveX)."""
OOXML: Final = "application/vnd.openxmlformats-officedocument."
MAIN: Final = {
    "docx": ("word/document.xml", f"{OOXML}wordprocessingml.document.main+xml"),
    "xlsx": ("xl/workbook.xml", f"{OOXML}spreadsheetml.sheet.main+xml"),
    "pptx": ("ppt/presentation.xml", f"{OOXML}presentationml.presentation.main+xml"),
}
"""Each kind's main part and its content type."""
W: Final = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
CATALOG: Final = "<< /Type /Catalog /Pages 2 0 R {} >>"
PAGES: Final = "<< /Type /Pages /Kids [3 0 R] /Count 1 >>"
PAGE: Final = "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 100 100] {} >>"

type Relationship = tuple[str, str] | tuple[str, str, str]
"""``(type, target)``, or ``(type, target, "External")``."""


def pdf(*objects: str, trailer: str = "") -> bytes:
    """A PDF of ``objects``, numbered from 1, the first its catalog."""
    out = b"%PDF-1.7\n"
    offsets = []
    for number, body in enumerate(objects, 1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n{body}\nendobj\n".encode("latin-1")
    start = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    out += b"".join(f"{offset:010d} 00000 n \n".encode() for offset in offsets)
    size = len(objects) + 1
    out += (
        f"trailer\n<< /Size {size} /Root 1 0 R {trailer}>>\nstartxref\n{start}\n%%EOF\n"
    ).encode()
    return out


def one_page(catalog: str = "", page: str = "", *more: str) -> bytes:
    """A one-page PDF: ``catalog`` and ``page`` add entries, ``more`` objects 4 on."""
    return pdf(CATALOG.format(catalog), PAGES, PAGE.format(page), *more)


def document(body: str = "<w:p/>") -> str:
    """A Word document's main part with ``body`` in its body."""
    return f'<w:document xmlns:w="{W}"><w:body>{body}</w:body></w:document>'


def relationships(*each: Relationship) -> str:
    """A ``.rels`` part of ``each``, numbered ``rId1`` on."""
    rows = []
    for number, (kind, target, *mode) in enumerate(each, 1):
        external = f' TargetMode="{mode[0]}"' if mode else ""
        rows.append(
            f'<Relationship Id="rId{number}" Type={quoteattr(kind)} '
            f"Target={quoteattr(target)}{external}/>"
        )
    return (
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/'
        f'relationships">{"".join(rows)}</Relationships>'
    )


def office(
    kind: str = "docx",
    *,
    main: str | None = None,
    main_type: str | None = None,
    main_relationships: Sequence[Relationship] = (),
    parts: Mapping[str, str | bytes] | None = None,
    types: Mapping[str, str] | None = None,
    defaults: Mapping[str, str] | None = None,
    leave_out: Sequence[str] = (),
    compression: int = zipfile.ZIP_DEFLATED,
) -> bytes:
    """The smallest ``kind`` package, and what a test adds to it.

    ``main`` replaces the main part's XML and ``main_type`` its content
    type; ``main_relationships`` are the main part's. ``parts`` adds parts
    by name, ``types`` overrides content types by part name, ``defaults``
    by extension. ``leave_out`` names parts not to write.
    """
    name, content_type = MAIN[kind]
    folder, _, base = name.rpartition("/")
    written: dict[str, str | bytes] = {
        "_rels/.rels": relationships((f"{R}officeDocument", name)),
        name: main if main is not None else document() if kind == "docx" else "<x/>",
    }
    if main_relationships:
        written[f"{folder}/_rels/{base}.rels"] = relationships(*main_relationships)
    written.update(parts or {})
    overrides = {name: main_type or content_type, **(types or {})}
    extensions = {
        "rels": "application/vnd.openxmlformats-package.relationships+xml",
        "xml": "application/xml",
        **(defaults or {}),
    }
    written["[Content_Types].xml"] = (
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        + "".join(
            f"<Default Extension={quoteattr(extension)} "
            f"ContentType={quoteattr(value)}/>"
            for extension, value in extensions.items()
        )
        + "".join(
            f"<Override PartName={quoteattr('/' + part)} "
            f"ContentType={quoteattr(value)}/>"
            for part, value in overrides.items()
        )
        + "</Types>"
    )
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", compression) as archive:
        for part, content in written.items():
            if part not in leave_out:
                archive.writestr(part, content)
    return out.getvalue()


def zipped(*entries: tuple[str, bytes], flags: int = 0) -> bytes:
    """A ZIP of ``entries``, deflated; ``flags`` set on each (1: encrypted).

    ``zipfile`` writes its own flags, so ``flags`` are set in the headers
    it wrote. Names may repeat: the warning ``zipfile`` gives is silenced.
    """
    out = io.BytesIO()
    with (
        warnings.catch_warnings(),
        zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as archive,
    ):
        warnings.simplefilter("ignore", UserWarning)
        for name, content in entries:
            archive.writestr(name, content)
    data = bytearray(out.getvalue())
    for signature, at in ((b"PK\x03\x04", 6), (b"PK\x01\x02", 8)):
        start = data.find(signature)
        while start != -1:
            data[start + at] |= flags
            start = data.find(signature, start + 1)
    return bytes(data)
