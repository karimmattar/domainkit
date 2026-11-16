"""Uploads checked whole before they are kept: PDFs, Office documents, hooks (U1).

The first bytes say what a file claims to be; a Word document with macros
or a PDF that runs a script starts like any other. A type's ``inspect``
hook reads the whole file before ``save_upload`` stores it, and a file it
refuses is never stored (ADR 0167).
"""

import codecs
import io
import logging
import struct
import sys
import time
import zipfile
import zlib
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import IO, Final

import pytest
from pypdf import PdfWriter
from starlette.datastructures import UploadFile

from domainkit import inspection as inspection_module
from domainkit.exceptions import ImproperlyConfigured
from domainkit.http import uploads
from domainkit.http.uploads import (
    CHECKED_PDF,
    DOCX,
    IMAGES,
    OFFICE,
    PDF,
    PPTX,
    XLSX,
    ZIP,
    FileType,
    StoredUpload,
    UploadRefused,
    receive_upload,
    save_upload,
)
from domainkit.inspection import (
    ACTIONS,
    COMPLEX,
    DAMAGED,
    DTD,
    EMBEDDED,
    ENCRYPTED,
    EXTERNAL,
    FORMS,
    MACROS,
    PAGES,
    SCRIPTS,
    FileRefused,
    NotOfType,
    OfficeCheck,
    PdfCheck,
)
from domainkit.storage import MemoryStorage
from domainkit_tests.support import hooks
from domainkit_tests.support.documents import (
    MS,
    OOXML,
    R,
    document,
    office,
    one_page,
    pdf,
    relationships,
    zipped,
)

pytestmark = pytest.mark.anyio

PNG_BYTES: Final = b"\x89PNG\r\n\x1a\n" + b"\0" * 100
MACRO_WORD: Final = "application/vnd.ms-word.document.macroEnabled.main+xml"
FIELD: Final = (
    '<w:r><w:fldChar w:fldCharType="begin"/></w:r>'
    "<w:r><w:instrText>{}</w:instrText></w:r>"
    "<w:r><w:instrText>{}</w:instrText></w:r>"
    '<w:r><w:fldChar w:fldCharType="separate"/></w:r>'
    "<w:r><w:t>shown</w:t></w:r>"
    '<w:r><w:fldChar w:fldCharType="end"/></w:r>'
)
"""A Word field whose instruction is split over two runs, as Word writes them."""
NESTED_DDE: Final = (
    '<w:r><w:fldChar w:fldCharType="begin"/></w:r>'
    '<w:r><w:fldChar w:fldCharType="begin"/></w:r>'
    "<w:r><w:instrText>QUOTE 68 68 69 65 85 84 79</w:instrText></w:r>"
    '<w:r><w:fldChar w:fldCharType="separate"/></w:r>'
    "<w:r><w:t>DDEAUTO</w:t></w:r>"
    '<w:r><w:fldChar w:fldCharType="end"/></w:r>'
    '<w:r><w:instrText> "c:\\cmd.exe" "/c calc"</w:instrText></w:r>'
    '<w:r><w:fldChar w:fldCharType="end"/></w:r>'
)
"""DDEAUTO spelled by a nested QUOTE field: the instruction starts with a field."""
DRAWING: Final = (
    '<w:p><w:r><w:drawing xmlns:r="http://schemas.openxmlformats.org/'
    'officeDocument/2006/relationships" r:embed="{}"/></w:r></w:p>'
)
"""A picture held in the package, by its relationship's id."""
UTF16_DECLARED: Final = '<?xml version="1.0" encoding="UTF-16"?><x/>'
SHEETML: Final = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
CHART: Final = "http://schemas.openxmlformats.org/drawingml/2006/chart"
X14: Final = "http://schemas.microsoft.com/office/spreadsheetml/2009/9/main"
XM: Final = "http://schemas.microsoft.com/office/excel/2006/main"
LATE_UTF7: Final = (
    b'<?xml version="1.0"' + b" " * 300 + b'encoding="UTF-7"?>'
    b'+ADwAIQ-DOCTYPE a +AFsAPAAh-ENTITY e "x"+AD4AXQA+-<a>&e;</a>'
)
"""A UTF-7 part that declares its encoding past 256 bytes, and hides a DTD."""
OLE_OBJECT: Final = (
    '<w:p><w:r><w:object><o:OLEObject xmlns:o="urn:schemas-microsoft-com:office:office"'
    ' Type="Embed" ProgID="Package"/></w:object></w:r></w:p>'
)
CONNECTIONS: Final = "xl/connections.xml"
SHEET: Final = "xl/macrosheets/sheet1.xml"
LINKED: Final = "xl/externalLinks/externalLink1.xml"


def sheet(cells: str) -> bytes:
    """A workbook whose one worksheet holds ``cells`` in its first row."""
    return office(
        "xlsx",
        main_relationships=[(f"{R}worksheet", "worksheets/sheet1.xml")],
        parts={
            "xl/worksheets/sheet1.xml": f'<worksheet xmlns="{SHEETML}"><sheetData>'
            f'<row r="1">{cells}</row></sheetData></worksheet>'
        },
        types={"xl/worksheets/sheet1.xml": f"{OOXML}spreadsheetml.worksheet+xml"},
    )


def worksheet(content: str) -> bytes:
    """A workbook whose one worksheet holds ``content`` after its cells."""
    return office(
        "xlsx",
        main_relationships=[(f"{R}worksheet", "worksheets/sheet1.xml")],
        parts={
            "xl/worksheets/sheet1.xml": f'<worksheet xmlns="{SHEETML}"><sheetData/>'
            f"{content}</worksheet>"
        },
        types={"xl/worksheets/sheet1.xml": f"{OOXML}spreadsheetml.worksheet+xml"},
    )


def bad_utf8_name(data: bytes) -> bytes:
    """``data`` with ``word/zzzz.xml`` named in bytes not UTF-8, flagged UTF-8."""
    changed = bytearray(data)
    start = changed.find(b"PK\x01\x02")
    while start != -1:
        size = struct.unpack_from("<H", changed, start + 28)[0]
        if bytes(changed[start + 46 : start + 46 + size]) == b"word/zzzz.xml":
            flags = struct.unpack_from("<H", changed, start + 8)[0] | 0x800
            struct.pack_into("<H", changed, start + 8, flags)
            changed[start + 46 + 5] = 0xFF
        start = changed.find(b"PK\x01\x02", start + 1)
    return bytes(changed)


def renamed_in_directory(data: bytes) -> bytes:
    """``data`` with ``word/vbaProject.bin`` named otherwise in its directory only."""
    changed = bytearray(data)
    start = changed.find(b"PK\x01\x02")
    while start != -1:
        size = struct.unpack_from("<H", changed, start + 28)[0]
        if bytes(changed[start + 46 : start + 46 + size]) == b"word/vbaProject.bin":
            changed[start + 46 : start + 46 + size] = b"word/vbaProjecX.bin"
        start = changed.find(b"PK\x01\x02", start + 1)
    return bytes(changed)


def jump() -> Callable[[], float]:
    """A clock that reads 0 once, then 100 seconds on: a check's time is up."""
    readings = [0.0]

    def monotonic() -> float:
        return readings.pop() if readings else 100.0

    return monotonic


def verdict(check: PdfCheck | OfficeCheck, data: bytes) -> str:
    """What ``check`` says of ``data``: ``ok``, the refusal, or ``not of type``."""
    try:
        check(io.BytesIO(data))
    except FileRefused as refused:
        return str(refused.message) if refused.code == "unsafe_file" else refused.code
    except NotOfType:
        return "not of type"
    return "ok"


async def refusal(data: bytes, *types: FileType) -> tuple[int, str, str]:
    """``save_upload``'s refusal of ``data``: status, code, detail."""
    with pytest.raises(UploadRefused) as refused:
        await save_upload(data, types=types, max_size=10**7, storage=MemoryStorage())
    return refused.value.status_code, refused.value.code, refused.value.detail


# PDF ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("data", "expected"),
    [
        (one_page(), "ok"),
        (
            one_page("", "/Annots [4 0 R]", "<< /A << /S /URI /URI <9f9f> >> >>"),
            EXTERNAL,
        ),
        (one_page("/OpenAction [3 0 R /Fit]"), "ok"),
        (
            one_page(
                "", "/Annots [4 0 R]", "<< /A << /S /URI /URI (https://a.example) >> >>"
            ),
            "ok",
        ),
        (
            one_page(
                "",
                "/Annots [4 0 R]",
                "<< /A << /S /URI /URI 5 0 R >> >>",
                "(mailto:a@b.c)",
            ),
            "ok",
        ),
        (
            one_page("", "/Annots [4 0 R]", "<< /A << /S /GoTo /D [3 0 R /Fit] >> >>"),
            "ok",
        ),
        (
            one_page(
                "/Names << /JavaScript << /Names [(a) 4 0 R] >> >>",
                "",
                "<< /S /JavaScript /JS (app.alert(1)) >>",
            ),
            SCRIPTS,
        ),
        (
            one_page(
                "", "/Annots [4 0 R]", "<< /A << /S /J#61vaScript /J#53 (x) >> >>"
            ),
            SCRIPTS,
        ),
        (one_page("", "", "<< /S /JavaScript /JS (never referenced) >>"), SCRIPTS),
        (one_page("/OpenAction 4 0 R", "", "<< /S /GoTo /D [3 0 R /Fit] >>"), ACTIONS),
        (one_page("", "/AA << /O 4 0 R >>", "<< /S /Named /N /NextPage >>"), ACTIONS),
        (
            one_page("", "/Annots [4 0 R]", "<< /A << /S /Launch /F (cmd.exe) >> >>"),
            ACTIONS,
        ),
        (
            one_page(
                "",
                "/Annots [4 0 R]",
                "<< /A << /S /SubmitForm /F (https://a.example) >> >>",
            ),
            ACTIONS,
        ),
        (
            one_page(
                "",
                "/Annots [4 0 R]",
                "<< /A << /S /GoToR /F (other.pdf) /D [0 /Fit] >> >>",
            ),
            ACTIONS,
        ),
        (
            one_page(
                "", "/Annots [4 0 R]", "<< /A << /S /URI /URI (file:///c:/x.exe) >> >>"
            ),
            EXTERNAL,
        ),
        (
            one_page(
                "", "/Annots [4 0 R]", "<< /A << /S /URI /URI <5c5c686f73745c73> >> >>"
            ),
            EXTERNAL,
        ),
        (
            one_page(
                "/Names << /EmbeddedFiles << /Names [(a.txt) 4 0 R] >> >>",
                "",
                "<< /Type /Filespec /F (a.txt) >>",
            ),
            EMBEDDED,
        ),
        (
            one_page(
                "", "/Annots [4 0 R]", "<< /Subtype /FileAttachment /FS (a.txt) >>"
            ),
            EMBEDDED,
        ),
        (
            one_page(
                "",
                "",
                "<< /Type /Filespec /F (a.txt) /EF << /F 5 0 R >> >>",
                "<< /Length 0 >>\nstream\n\nendstream",
            ),
            EMBEDDED,
        ),
        (
            one_page(
                "/AcroForm << /Fields [] /XFA 4 0 R >>",
                "",
                "<< /Length 0 >>\nstream\n\nendstream",
            ),
            FORMS,
        ),
        (b"%PDF-1.7\nnot a PDF at all", "unreadable_file"),
        (
            one_page(
                "",
                "/Annots [4 0 R]",
                "<< /Subtype /3D /3DD << /OnInstantiate 5 0 R >> >>",
                "(x)",
            ),
            SCRIPTS,
        ),
        (
            one_page(
                "", "/Annots [4 0 R]", "<< /A << /S /Thread /F (other.pdf) /D 0 >> >>"
            ),
            ACTIONS,
        ),
        (
            one_page("", "/Annots [4 0 R]", "<< /A << /S /Thread /D 0 >> >>"),
            "ok",
        ),
        (
            one_page(
                "", "", "<< /Type /Filespec /FS /URL /F (https://a.example/m.mp4) >>"
            ),
            EXTERNAL,
        ),
        (
            one_page(
                "",
                "/Annots [4 0 R]",
                "<< /A << /S /URI /URI 5 0 R >> >>",
                "<< /Length 4 >>\nstream\nhttp\nendstream",
            ),
            EXTERNAL,
        ),
        (
            one_page("/URI << /Base (https://a.example/) >>"),
            "ok",
        ),
        (
            one_page()
            + b"9 0 obj\n<< /S /JavaScript /JS (written, never listed) >>\nendobj\n",
            SCRIPTS,
        ),
        (
            one_page() + b"9 0 obj\n) not an object (\nendobj\n",
            "ok",
        ),
        (
            one_page()
            + b"9 0 obj\n<< /Length 99 0 R >>\nstream\nx\nendstream\nendobj\n",
            "unreadable_file",
        ),
    ],
)
def test_a_pdf_is_refused_for_what_runs_sends_or_hides(
    data: bytes, expected: str
) -> None:
    """Scripts, actions, files, XFA; every object read, names decoded."""
    assert verdict(PdfCheck(), data) == expected


def test_a_pdf_with_a_password_or_too_many_pages_is_refused() -> None:
    """Encrypted, owner password alone included; pages over ``max_pages``."""
    writer = PdfWriter()
    writer.add_blank_page(100, 100)
    writer.encrypt(user_password="", owner_password="owner")
    encrypted = io.BytesIO()
    writer.write(encrypted)
    two = pdf(
        "<< /Type /Catalog /Pages 2 0 R >>",
        "<< /Type /Pages /Kids [3 0 R 4 0 R] /Count 2 >>",
        "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 9 9] >>",
        "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 9 9] >>",
    )

    assert verdict(PdfCheck(), encrypted.getvalue()) == ENCRYPTED
    assert verdict(PdfCheck(max_pages=1), two) == PAGES
    assert verdict(PdfCheck(max_pages=2), two) == "ok"
    with pytest.raises(ValueError, match="max_pages is more than 0"):
        PdfCheck(max_pages=0)


def test_a_pdf_check_reads_a_bounded_number_of_objects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A file of more objects and entries than the budget is not read to its end."""
    monkeypatch.setattr(inspection_module, "PDF_NODES", 10)
    many = one_page("", "/Annots [4 0 R]", "<< /A << /S /GoTo /D [3 0 R /Fit] >> >>")

    assert verdict(PdfCheck(), many) == COMPLEX


async def test_a_checked_pdf_is_stored_and_a_refused_one_never() -> None:
    """``CHECKED_PDF`` stores a plain PDF; one with a script is 422, nothing kept.

    ``PDF`` still reads the first bytes only, as it did.
    """
    storage = MemoryStorage()
    scripted = one_page("", "/Annots [4 0 R]", "<< /A << /S /JavaScript /JS (x) >> >>")

    stored = await save_upload(
        one_page(), types=[CHECKED_PDF], max_size=10**6, storage=storage
    )
    with pytest.raises(UploadRefused) as refused:
        await save_upload(
            scripted, types=[CHECKED_PDF], max_size=10**6, storage=storage
        )
    head_only = await save_upload(
        scripted, types=[PDF], max_size=10**6, storage=storage
    )

    assert stored.content_type == "application/pdf"
    assert stored.name.endswith(".pdf")
    assert (refused.value.status_code, refused.value.code) == (422, "unsafe_file")
    assert refused.value.detail == SCRIPTS
    assert sorted(storage.files) == sorted([stored.name, head_only.name])


# Office ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("data", "expected"),
    [
        (office(), "ok"),
        (
            office(
                main_relationships=[
                    (f"{R}hyperlink", "https://a.example/x", "External")
                ]
            ),
            "ok",
        ),
        (
            office(main_relationships=[(f"{R}hyperlink", "mailto:a@b.c", "External")]),
            "ok",
        ),
        (
            office(main=document(FIELD.format(' HYPERLINK "https://a', '.example"'))),
            "ok",
        ),
        (office(main=document('<w:fldSimple w:dirty="1" w:instr=" PAGE "/>')), "ok"),
        (office(main=document('<w:r><w:fldChar w:fldCharType="end"/></w:r>')), "ok"),
        (office(parts={"customXml/": b""}), "ok"),
        (office(main_type=MACRO_WORD), MACROS),
        (
            office(
                main_type="application/vnd.ms-word.template.macroEnabledTemplate.main+xml"
            ),
            MACROS,
        ),
        (office(parts={"word/vbaProject.bin": b"\xd0\xcf\x11\xe0"}), MACROS),
        (
            office(
                parts={"word/macros.bin": b"x"},
                types={"word/macros.bin": "application/vnd.ms-office.vbaProject"},
            ),
            MACROS,
        ),
        (office(main_relationships=[(f"{MS}vbaProject", "macros.bin")]), MACROS),
        (office(main_relationships=[(f"{R}oleObject", "embeddings/a.bin")]), EMBEDDED),
        (office(main_relationships=[(f"{R}control", "activeX/a.xml")]), EMBEDDED),
        (
            office(
                parts={"word/activeX/a.xml": "<x/>"},
                types={"word/activeX/a.xml": "application/vnd.ms-office.activeX+xml"},
            ),
            EMBEDDED,
        ),
        (
            office(
                main_relationships=[(f"{R}package", "embeddings/a.zip")],
                parts={"word/embeddings/a.zip": b"PK"},
            ),
            EMBEDDED,
        ),
        (
            office(
                main_relationships=[
                    (f"{R}attachedTemplate", "https://a.example/t.dotm", "External")
                ]
            ),
            EXTERNAL,
        ),
        (
            office(
                main_relationships=[
                    (f"{R}image", "https://a.example/a.png", "External")
                ]
            ),
            EXTERNAL,
        ),
        (
            office(
                main_relationships=[
                    (f"{R}oleObject", "mhtml:https://a.example/!x", "External")
                ]
            ),
            EMBEDDED,
        ),
        (
            office(
                main_relationships=[(f"{R}hyperlink", "file:///c:/x.exe", "External")]
            ),
            EXTERNAL,
        ),
        (
            office(
                main_relationships=[(f"{R}hyperlink", "\\\\host\\share", "External")]
            ),
            EXTERNAL,
        ),
        (
            office(main=document('<w:fldSimple w:instr=" DDEAUTO cmd /c calc"/>')),
            ACTIONS,
        ),
        (
            office(
                main=document(
                    FIELD.format(" INCLUDE", 'PICTURE "https://a.example/a.png"')
                )
            ),
            EXTERNAL,
        ),
        (office(main=document(FIELD.format(" INCLUDETEXT ", '"c:/a.docx"'))), EXTERNAL),
        (
            office(
                main='<!DOCTYPE d [<!ENTITY a "aaaaaaaaaa">]>'
                + document("<w:p>&a;</w:p>")
            ),
            DTD,
        ),
        (
            office(
                parts={
                    "word/_rels/settings.xml.rels": '<!DOCTYPE r SYSTEM "https://a.example/r.dtd">'
                    + relationships()
                }
            ),
            DTD,
        ),
        (office(main="<w:document"), "unreadable_file"),
        (office(main_type=f"{OOXML}wordprocessingml.template.main+xml"), "not of type"),
        (office("xlsx"), "not of type"),
        (office(leave_out=["[Content_Types].xml"]), "not of type"),
        (office(leave_out=["_rels/.rels"]), "not of type"),
        (
            office(
                parts={
                    "_rels/.rels": relationships(
                        (f"{R}officeDocument", "word/document.xml"),
                        (f"{R}officeDocument", "word/other.xml"),
                    )
                }
            ),
            "not of type",
        ),
        (office(compression=zipfile.ZIP_BZIP2), "unreadable_file"),
        (b"PK\x03\x04 and nothing of a zip", "unreadable_file"),
        (
            office(main=" " * 300 + document(FIELD.format(" INCLUDEPICTURE ", '"x"'))),
            EXTERNAL,
        ),
        (office(main="\n" * 300 + document(FIELD.format(" DDEAUTO ", "cmd"))), ACTIONS),
        (office(main="\n" * 300 + '<!DOCTYPE d [<!ENTITY a "a">]>' + document()), DTD),
        (office(main="\x00 junk " + document()), "unreadable_file"),
        (office(main="no markup at all"), "unreadable_file"),
        (office(parts={"customXml/item1.xml": LATE_UTF7}), "unreadable_file"),
        (
            office(
                parts={"customXml/item1.xml": '<?xml version="1.0" encoding="UTF-8"'}
            ),
            "unreadable_file",
        ),
        (
            office(parts={"customXml/item1.xml": b"\x89PNG not XML at all"}),
            "unreadable_file",
        ),
        (
            office(
                parts={"word/media/a.png": b"   <!DOCTYPE x>"},
                defaults={"png": "image/png"},
            ),
            "ok",
        ),
        (office(main=document(OLE_OBJECT)), EMBEDDED),
        (
            office(
                main=document(
                    '<w:r><w:object><w:control r:id="rId1"/></w:object></w:r>'
                )
            ),
            EMBEDDED,
        ),
        (
            office(
                main=document(
                    f'<w:p><w:r><w:control xmlns:r="{R[:-1]}" r:id="rId1"/></w:r></w:p>'
                )
            ),
            EMBEDDED,
        ),
        (
            office(
                main=document(DRAWING.format("rId1")),
                main_relationships=[
                    (f"{R}hyperlink", "https://a.example/a.png", "External")
                ],
            ),
            EXTERNAL,
        ),
        (
            office(
                main=document(
                    f'<w:p><w:hyperlink xmlns:r="{R[:-1]}" r:id="rId1"/></w:p>'
                    + DRAWING.format("rId2")
                ),
                main_relationships=[
                    (f"{R}hyperlink", "https://a.example/", "External"),
                    (f"{R}image", "media/a.png"),
                ],
            ),
            "ok",
        ),
        (
            office(
                parts={
                    "word/be.xml": '<!DOCTYPE x [<!ENTITY e "e">]><x/>'.encode(
                        "utf-16-be"
                    )
                }
            ),
            DTD,
        ),
        (
            office(
                parts={"word/decl.xml": '<?xml version="1.0" encoding="UTF-8"?><x/>'}
            ),
            "ok",
        ),
        (office(main_relationships=[(f"{R}aFChunk", "chunk.rtf")]), EMBEDDED),
        (
            office(
                main_relationships=[
                    (f"{R}attachedTemplate", "https://a.example/t", "external")
                ]
            ),
            EXTERNAL,
        ),
        (
            office(
                main_relationships=[
                    (f"{R}attachedTemplate", "https://a.example/t", " External")
                ]
            ),
            EXTERNAL,
        ),
        (
            office(
                main_relationships=[
                    (f"{R}attachedTemplate", "https://a.example/t", "1")
                ]
            ),
            EXTERNAL,
        ),
        (
            office(
                main_relationships=[(f"{R}attachedTemplate", "https://a.example/t")]
            ),
            EXTERNAL,
        ),
        (
            office(main_relationships=[(f"{R}attachedTemplate", "\\\\host\\t.dotm")]),
            EXTERNAL,
        ),
        (
            office(main_relationships=[(f"{R}attachedTemplate", "//host/t.dotm")]),
            EXTERNAL,
        ),
        (office(main_relationships=[(f"{R}image", "C:/pictures/a.png")]), EXTERNAL),
        (office(main_relationships=[(f"{R}image", "media/a.png", "Internal")]), "ok"),
        (office(main=document(NESTED_DDE)), ACTIONS),
        (office(main=document(FIELD.format('DDEAUTO"c:\\cmd.exe"', ""))), ACTIONS),
        (office(main=document(FIELD.format(' DATABASE \\d "x.mdb"', ""))), EXTERNAL),
        (
            office(
                main=document(FIELD.format(" INCLUDE ", '"https://a.example/b.docx"'))
            ),
            EXTERNAL,
        ),
        (
            office(main=document(FIELD.format(" IMPORT ", "https://a.example/a.png"))),
            EXTERNAL,
        ),
        (
            office(main=document(FIELD.format(" IF ", '{ MERGEFIELD x } = "y" "z"'))),
            "ok",
        ),
        (
            office(
                main=document(
                    f'<w:p xmlns:r="{R[:-1]}">'
                    '<w:r><w:drawing r:link="rId9"/></w:r></w:p>'
                )
            ),
            EXTERNAL,
        ),
        (
            office(
                parts={
                    "word/drawing.vml": '<!DOCTYPE x [<!ENTITY e SYSTEM "file:///dev/stdin">]><x>&e;</x>'
                }
            ),
            DTD,
        ),
        (office(parts={"word/odd.xml": '<x><!ENTITY e "e"></x>'}), DTD),
        (
            office(
                parts={
                    "word/wide.xml": (
                        '<!DOCTYPE x SYSTEM "file:///dev/stdin"><x/>'
                    ).encode("utf-16")
                }
            ),
            DTD,
        ),
        (
            office(
                parts={
                    "word/wide.xml": codecs.BOM_UTF32_LE + "<x/>".encode("utf-32-le")
                }
            ),
            "unreadable_file",
        ),
        (office(parts={"word/old.xml": b"\x4c\x6f\xa7\x94\x93@"}), "unreadable_file"),
        (
            office(
                parts={"word/odd.xml": '<?xml version="1.0" encoding="x-nothing"?><x/>'}
            ),
            "unreadable_file",
        ),
        (
            office(parts={"word/odd.xml": UTF16_DECLARED.encode("utf-16")}),
            "ok",
        ),
        (office(parts={"word/media/image1.png": b"\x89PNG binary"}), "ok"),
        (
            office(parts={"word/_rels/odd.xml.rels": b"\x89PNG not a list"}),
            "unreadable_file",
        ),
        (bad_utf8_name(office(parts={"word/zzzz.xml": "<x/>"})), "unreadable_file"),
        (
            renamed_in_directory(office(parts={"word/vbaProject.bin": b"x" * 10})),
            "unreadable_file",
        ),
    ],
)
def test_a_word_document_is_refused_for_macros_objects_and_outside_links(
    data: bytes, expected: str
) -> None:
    """The main part's type, parts, relationships, fields, and every XML part."""
    assert verdict(OfficeCheck("docx"), data) == expected


@pytest.mark.parametrize(
    ("kind", "data", "expected"),
    [
        ("xlsx", office("xlsx"), "ok"),
        ("pptx", office("pptx"), "ok"),
        (
            "xlsx",
            office(
                "xlsx", main_type="application/vnd.ms-excel.sheet.macroEnabled.main+xml"
            ),
            MACROS,
        ),
        (
            "xlsx",
            office(
                "xlsx",
                parts={SHEET: "<x/>"},
                types={SHEET: "application/vnd.ms-excel.macrosheet+xml"},
            ),
            MACROS,
        ),
        (
            "xlsx",
            office(
                "xlsx",
                main_relationships=[(f"{MS}xlMacrosheet", "macrosheets/sheet1.xml")],
            ),
            MACROS,
        ),
        (
            "xlsx",
            office(
                "xlsx",
                parts={LINKED: "<externalLink/>"},
                types={LINKED: f"{OOXML}spreadsheetml.externalLink+xml"},
            ),
            EXTERNAL,
        ),
        (
            "xlsx",
            office("xlsx", main_relationships=[(f"{R}externalLinkPath", "other.xlsx")]),
            EXTERNAL,
        ),
        (
            "pptx",
            office(
                "pptx",
                main_type="application/vnd.ms-powerpoint.presentation.macroEnabled.main+xml",
            ),
            MACROS,
        ),
        (
            "pptx",
            office("pptx", main_type=f"{OOXML}presentationml.slideshow.main+xml"),
            "not of type",
        ),
        ("pptx", office("docx"), "not of type"),
        (
            "xlsx",
            office(
                "xlsx",
                parts={CONNECTIONS: "<connections/>"},
                types={CONNECTIONS: f"{OOXML}spreadsheetml.connections+xml"},
            ),
            EXTERNAL,
        ),
        (
            "xlsx",
            office(
                "xlsx", main_relationships=[(f"{R}queryTable", "queryTables/q.xml")]
            ),
            EXTERNAL,
        ),
        (
            "xlsx",
            sheet('<c r="A1"><f>WEBSERVICE("https://a.example/")</f></c>'),
            EXTERNAL,
        ),
        (
            "xlsx",
            sheet('<c r="A1"><f>_xlfn.webservice("https://a.example/")</f></c>'),
            EXTERNAL,
        ),
        ("xlsx", sheet("<c r=\"A1\"><f>cmd|' /C calc'!A0</f></c>"), ACTIONS),
        ("xlsx", sheet('<c r="A1"><f>CONCAT("a|b", B1)</f></c>'), "ok"),
        ("xlsx", sheet("<c r=\"A1\"><f>SUM('Q1|Q2'!B1, 'it''s|x'!C2)</f></c>"), "ok"),
        (
            "xlsx",
            sheet("<c r=\"A1\"><f>'Q1|Q2'!B1&amp;cmd|'/c calc'!A0</f></c>"),
            ACTIONS,
        ),
        ("xlsx", sheet('<c r="A1"><f>\'Q1|Q2!B1</f></c>'), ACTIONS),
        (
            "xlsx",
            office(
                "xlsx",
                parts={
                    "xl/charts/chart1.xml": f'<c:chartSpace xmlns:c="{CHART}">'
                    "<c:f>'Q1|Q2'!$B$1:$B$4</c:f></c:chartSpace>"
                },
            ),
            "ok",
        ),
        (
            "xlsx",
            office(
                "xlsx",
                main=f'<workbook xmlns="{SHEETML}"><definedNames>'
                "<definedName name=\"x\">'Q1|Q2'!$A$1</definedName>"
                "</definedNames></workbook>",
            ),
            "ok",
        ),
        ("xlsx", sheet('<c r="A1"><f>SUM(B1:B9)</f></c>'), "ok"),
        (
            "xlsx",
            sheet('<c r="A1"><f>_xlfn.IMAGE("https://a.example/a.png")</f></c>'),
            EXTERNAL,
        ),
        (
            "xlsx",
            worksheet(
                '<conditionalFormatting><cfRule><formula>WEBSERVICE("x")</formula></cfRule></conditionalFormatting>'
            ),
            EXTERNAL,
        ),
        (
            "xlsx",
            worksheet(
                "<dataValidations><dataValidation><formula1>cmd|'/c calc'!A0"
                "</formula1></dataValidation></dataValidations>"
            ),
            ACTIONS,
        ),
        (
            "xlsx",
            worksheet(
                f'<extLst><x14:dataValidation xmlns:x14="{X14}" xmlns:xm="{XM}">'
                '<x14:formula1><xm:f>RTD("a",,"b")</xm:f></x14:formula1>'
                "</x14:dataValidation></extLst>"
            ),
            EXTERNAL,
        ),
        (
            "xlsx",
            office(
                "xlsx",
                main=f'<workbook xmlns="{SHEETML}"><definedNames>'
                '<definedName name="x">RTD("a",,"b")</definedName>'
                "</definedNames></workbook>",
            ),
            EXTERNAL,
        ),
    ],
)
def test_workbooks_and_presentations_are_checked_alike(
    kind: str, data: bytes, expected: str
) -> None:
    """Macro-enabled main parts, macro sheets, links to other workbooks."""
    assert verdict(OfficeCheck(kind), data) == expected  # type: ignore[arg-type]


def test_an_embedded_workbook_is_checked_one_level_deep() -> None:
    """A chart's workbook passes; one with macros, or one inside it, does not."""
    package = f"{R}package"
    sheet = f"{OOXML}spreadsheetml.sheet"

    def with_workbook(workbook: bytes) -> bytes:
        return office(
            main_relationships=[(package, "embeddings/Book1.xlsx")],
            parts={"word/embeddings/Book1.xlsx": workbook},
            defaults={"xlsx": sheet},
        )

    inner = office("xlsx", main_relationships=[(package, "embeddings/a.xlsx")])
    deeper = office(
        "xlsx",
        main_relationships=[(package, "embeddings/a.xlsx")],
        parts={"xl/embeddings/a.xlsx": office("xlsx")},
        defaults={"xlsx": sheet},
    )
    macros = office(
        "xlsx", main_type="application/vnd.ms-excel.sheet.macroEnabled.main+xml"
    )

    assert verdict(OfficeCheck("docx"), with_workbook(office("xlsx"))) == "ok"
    assert verdict(OfficeCheck("docx"), with_workbook(macros)) == MACROS
    assert verdict(OfficeCheck("docx"), with_workbook(office("docx"))) == EMBEDDED
    assert verdict(OfficeCheck("docx"), with_workbook(deeper)) == EMBEDDED
    assert inner


def test_a_zip_is_bounded_by_its_directory_before_it_is_inflated() -> None:
    """Parts, inflated bytes, ratio, encryption, odd compression, repeated names."""
    bomb = office(parts={"word/media/zeros.bin": bytes(2 * 1024 * 1024)})
    repeated = zipped(("a.xml", b"<a/>"), ("A.xml", b"<a/>"))

    assert verdict(OfficeCheck("docx", max_parts=2), office()) == COMPLEX
    assert verdict(OfficeCheck("docx", max_parts=3), office()) == "ok"
    assert verdict(OfficeCheck("docx", max_size=100), office()) == COMPLEX
    assert verdict(OfficeCheck("docx"), bomb) == COMPLEX
    assert verdict(OfficeCheck("docx", max_ratio=5000), bomb) == COMPLEX
    roomy = OfficeCheck("docx", max_ratio=5000, max_expansion=10_000)
    assert verdict(roomy, bomb) == "ok"
    assert verdict(OfficeCheck("docx", max_expansion=10_000), bomb) == COMPLEX
    assert (
        verdict(OfficeCheck("docx"), zipped(("a.xml", b"<a/>"), flags=1)) == ENCRYPTED
    )
    assert verdict(OfficeCheck("docx"), repeated) == "unreadable_file"
    with pytest.raises(ValueError, match="An Office check takes"):
        OfficeCheck("odt")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="max_parts is more than 0"):
        OfficeCheck("docx", max_parts=0)


def test_a_damaged_part_is_unreadable() -> None:
    """A deflated part whose bytes were changed fails as it is inflated."""
    data = bytearray(office(main=document("<w:p>" + "text " * 2000 + "</w:p>")))
    start = data.index(b"word/document.xml") + len(b"word/document.xml")
    data[start + 20 : start + 60] = bytes(40)

    assert verdict(OfficeCheck("docx"), bytes(data)) == "unreadable_file"


async def test_office_types_are_told_apart_by_their_main_part() -> None:
    """Each starts as a ZIP; the first type whose check takes it stores it."""
    storage = MemoryStorage()

    stored = {
        kind: await save_upload(
            office(kind), types=OFFICE, max_size=10**6, storage=storage
        )
        for kind in ("docx", "xlsx", "pptx")
    }
    no_package = office(leave_out=["[Content_Types].xml"])
    as_zip = await save_upload(
        no_package, types=(DOCX, ZIP), max_size=10**6, storage=storage
    )

    assert {kind: each.content_type for kind, each in stored.items()} == {
        "docx": DOCX.content_type,
        "xlsx": XLSX.content_type,
        "pptx": PPTX.content_type,
    }
    assert stored["xlsx"].name.endswith(".xlsx")
    assert as_zip.content_type == "application/zip"
    assert await refusal(office("xlsx"), DOCX) == (
        415,
        "unsupported_file_type",
        "This type of file is not accepted.",
    )
    assert await refusal(office(main_type=MACRO_WORD), *OFFICE) == (
        422,
        "unsafe_file",
        MACROS,
    )
    assert await refusal(b"PK\x03\x04 broken", DOCX) == (
        422,
        "unreadable_file",
        DAMAGED,
    )
    assert len(storage.files) == 4


def test_a_check_without_its_library_names_the_extra(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``documents``: pypdf for PDFs, lxml for Office documents."""
    monkeypatch.setitem(sys.modules, "pypdf", None)
    monkeypatch.setitem(sys.modules, "lxml.etree", None)

    for check, data in ((PdfCheck(), one_page()), (OfficeCheck("docx"), office())):
        with pytest.raises(ImproperlyConfigured, match=r"domainkit\[documents\]"):
            check(io.BytesIO(data))


# save_upload's sources, and receive_upload --------------------------------------


async def chunks(data: bytes, size: int = 7) -> AsyncIterator[bytes]:
    """``data``, ``size`` bytes at a time, as a request's body streams."""
    for start in range(0, len(data), size):
        yield data[start : start + size]


async def test_uploads_come_as_bytes_or_a_stream_with_a_name() -> None:
    """Not only a form's ``UploadFile``: ``filename`` names the file (U3)."""
    storage = MemoryStorage()

    from_bytes = await save_upload(
        PNG_BYTES, types=IMAGES, max_size=1000, storage=storage, filename="a/b.png"
    )
    from_stream = await save_upload(
        chunks(PNG_BYTES), types=IMAGES, max_size=1000, storage=storage
    )
    renamed = await save_upload(
        UploadFile(io.BytesIO(PNG_BYTES), filename="old.png"),
        types=IMAGES,
        max_size=1000,
        storage=storage,
        filename="new.png",
    )

    assert from_bytes.filename == "b.png"
    assert from_stream.filename == "file"
    assert renamed.filename == "new.png"
    assert storage.files[from_stream.name] == PNG_BYTES
    digest = "a" * 64
    assert from_bytes == StoredUpload(
        from_bytes.name, "image/png", 108, "b.png", digest
    )
    assert from_bytes.sha256 == from_stream.sha256 != digest
    assert len(from_bytes.sha256) == 64


async def test_sizes_are_held_in_every_form(monkeypatch: pytest.MonkeyPatch) -> None:
    """Bytes over the limit before a byte is read; a stream as it comes; empty."""
    storage = MemoryStorage()
    checked = (CHECKED_PDF,)

    for data, types in (
        (PNG_BYTES, IMAGES),
        (chunks(PNG_BYTES), IMAGES),
        (chunks(one_page()), checked),
    ):
        with pytest.raises(UploadRefused) as refused:
            await save_upload(data, types=types, max_size=50, storage=storage)
        assert refused.value.status_code == 413
    with pytest.raises(UploadRefused) as empty:
        await save_upload(chunks(b""), types=checked, max_size=50, storage=storage)
    assert empty.value.status_code == 400
    assert storage.files == {}


async def test_a_received_upload_is_checked_and_stored_only_when_asked(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``receive_upload`` holds the file, past 1 MiB on disk; ``store`` keeps it."""
    monkeypatch.setattr(uploads, "SPOOL_MEMORY", 64)
    storage = MemoryStorage()
    data = one_page() + b"%" * 500

    received = await receive_upload(
        chunks(data, 100), types=[CHECKED_PDF], max_size=10**6
    )
    unstored = received.unstored()
    stored = await received.store(storage, directory="contracts")
    received.close()
    head_only = await receive_upload(data, types=[PDF], max_size=10**6)
    head_only.close()

    assert unstored.name == ""
    assert (unstored.size, unstored.content_type) == (len(data), "application/pdf")
    assert stored.name.startswith("contracts/")
    assert stored.sha256 == unstored.sha256 == received.sha256
    assert storage.files[stored.name] == data
    assert head_only.kind is PDF
    with pytest.raises(ValueError, match="needs the types"):
        await receive_upload(data, types=[], max_size=10)


async def test_a_hook_of_ones_own_reads_the_whole_file() -> None:
    """``FileType(..., inspect=...)``: its refusal is 422, with its code.

    It runs in a process of its own, which loads it by name: a lambda or
    a nested function is refused when the type is made.
    """
    text = FileType("text/plain", ".txt", lambda head: True, hooks.no_secrets)
    storage = MemoryStorage()

    stored = await save_upload(b"hello", types=[text], max_size=100, storage=storage)
    with pytest.raises(UploadRefused) as refused:
        await save_upload(b"a SECRET", types=[text], max_size=100, storage=storage)

    assert (refused.value.status_code, refused.value.code) == (422, "secret")
    assert list(storage.files) == [stored.name]
    with pytest.raises(TypeError, match="runs in a process of its own"):
        FileType("text/plain", ".txt", lambda head: True, lambda file: None)


def test_a_hook_defined_in_main_is_refused_when_the_type_is_made(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """It pickled here, and its process, which runs no script, could not load it.

    Every upload was a 500 (third review).
    """
    main = sys.modules["__main__"]

    def scripted(file: IO[bytes]) -> None:
        """A check a script defined."""

    class Scripted:
        """A check of a class a script defined."""

        def __call__(self, file: IO[bytes]) -> None:
            """Take any file."""

    for each in (scripted, Scripted):
        each.__module__ = "__main__"
        each.__qualname__ = each.__name__
        monkeypatch.setattr(main, each.__name__, each, raising=False)

    for check in (scripted, Scripted()):
        with pytest.raises(TypeError, match="cannot import __main__"):
            FileType("text/plain", ".txt", lambda head: True, check)


# What a check costs, whatever the file ------------------------------------------


def xref_stream_pdf(entries: int, padding: int = 1000) -> bytes:
    """A PDF whose cross-reference stream names object 1's offset for each entry.

    pypdf searched the whole file for each object not where its table
    said: thousands of entries made a check of minutes.
    """
    out = b"%PDF-1.7\n"
    one = len(out)
    out += b"1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj\n"
    two = len(out)
    out += b"2 0 obj\n<< /Type /Pages /Kids [] /Count 0 >>\nendobj\n"
    out += b"%" + b"A" * padding + b"\n"
    three = len(out)
    rows = [struct.pack(">BIB", 0, 0, 255), struct.pack(">BIB", 1, one, 0)]
    rows += [struct.pack(">BIB", 1, two, 0), struct.pack(">BIB", 1, three, 0)]
    rows += [struct.pack(">BIB", 1, one, 0)] * (entries - 4)
    data = zlib.compress(b"".join(rows))
    out += (
        (
            f"3 0 obj\n<< /Type /XRef /Size {entries} /W [1 4 1] /Root 1 0 R "
            f"/Filter /FlateDecode /Length {len(data)} >>\nstream\n"
        ).encode()
        + data
        + b"\nendstream\nendobj\n"
    )
    return out + f"startxref\n{three}\n%%EOF\n".encode()


def listed_pdf(objects: dict[int, str], listed: set[int]) -> bytes:
    """A PDF of ``objects`` whose table lists only those of ``listed`` in use."""
    out = b"%PDF-1.7\n"
    offsets = {}
    for number, body in objects.items():
        offsets[number] = len(out)
        out += f"{number} 0 obj\n{body}\nendobj\n".encode()
    start = len(out)
    size = max(objects) + 1
    out += f"xref\n0 {size}\n0000000000 65535 f \n".encode()
    for number in range(1, size):
        if number in listed:
            out += f"{offsets[number]:010d} 00000 n \n".encode()
        else:
            out += b"0000000000 65535 f \n"
    trailer = f"trailer\n<< /Size {size} /Root 1 0 R >>\nstartxref\n{start}\n%%EOF\n"
    return out + trailer.encode()


def streamed_pdf(*sizes: int) -> bytes:
    """A PDF whose object streams each decode to one of ``sizes`` bytes."""
    out = b"%PDF-1.7\n"
    offsets = {1: len(out)}
    out += b"1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj\n"
    offsets[2] = len(out)
    out += b"2 0 obj\n<< /Type /Pages /Kids [] /Count 0 >>\nendobj\n"
    for index, size in enumerate(sizes):
        header = f"{100 + index} 0 ".encode()
        data = zlib.compress(header + b" " * size + b"null")
        offsets[3 + index] = len(out)
        out += (
            (
                f"{3 + index} 0 obj\n<< /Type /ObjStm /N 1 /First {len(header)} "
                f"/Filter /FlateDecode /Length {len(data)} >>\nstream\n"
            ).encode()
            + data
            + b"\nendstream\nendobj\n"
        )
    table = 3 + len(sizes)
    total = 100 + len(sizes)
    rows = []
    for number in range(total):
        if number in offsets:
            rows.append(struct.pack(">BIH", 1, offsets[number], 0))
        elif number >= 100:
            rows.append(struct.pack(">BIH", 2, 3 + number - 100, 0))
        elif number == table:
            rows.append(b"")  # Filled in below, once its offset is known.
        else:
            rows.append(struct.pack(">BIH", 0, 0, 0 if number else 65535))
    rows[table] = struct.pack(">BIH", 1, len(out), 0)
    data = zlib.compress(b"".join(rows))
    start = len(out)
    out += (
        (
            f"{table} 0 obj\n<< /Type /XRef /Size {total} /W [1 4 2] /Root 1 0 R "
            f"/Filter /FlateDecode /Length {len(data)} >>\nstream\n"
        ).encode()
        + data
        + b"\nendstream\nendobj\n"
    )
    return out + f"startxref\n{start}\n%%EOF\n".encode()


@pytest.fixture
def searches(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """How often pypdf copies the whole file to search it for an object."""
    import pypdf._reader

    copies: list[int] = []
    copy = pypdf._reader.get_stream_data  # type: ignore[attr-defined]

    def counted(stream: IO[bytes]) -> bytes:
        data = copy(stream)
        copies.append(len(data))
        return data

    monkeypatch.setattr(pypdf._reader, "get_stream_data", counted)
    return copies


def test_a_pdf_whose_table_points_elsewhere_is_refused_without_a_search(
    searches: list[int],
) -> None:
    """Each wrong entry was a search of the whole file; the first one refuses now."""
    assert verdict(PdfCheck(), xref_stream_pdf(4000)) == "unreadable_file"
    assert searches == []


@pytest.mark.parametrize(
    ("objects", "listed"),
    [
        (
            {
                1: "<< /Type /Catalog /Pages 2 0 R "
                "/OpenAction << /S /JavaScript /JS (x) >> >>",
                2: "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
                3: "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 10 10] >>",
            },
            {2, 3},
        ),
        (
            {
                1: "<< /Type /Catalog /Pages 2 0 R >>",
                2: "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
                3: "<< /Type /Page /Parent 2 0 R /Annots [4 0 R] >>",
                4: "<< /Type /Annot /Subtype /Link /A << /S /Launch /F (cmd) >> >>",
            },
            {1, 2, 3},
        ),
        (
            {
                1: "<< /Type /Catalog /Pages 2 0 R /Names 4 0 R >>",
                2: "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
                3: "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 10 10] >>",
                4: "<< /JavaScript << /Names [(a) << /S /JavaScript /JS (x) >>] >> >>",
            },
            {1, 2, 3},
        ),
    ],
)
def test_an_object_the_table_does_not_declare_is_never_searched_for(
    objects: dict[int, str], listed: set[int], searches: list[int]
) -> None:
    """A reference to an object listed free, or not at all, refuses the file.

    pypdf found such objects by searching the file, and the check, which
    read only what the table lists, never saw their scripts (A6 review).
    """
    assert verdict(PdfCheck(), listed_pdf(objects, listed)) == "unreadable_file"
    assert searches == []


@pytest.mark.parametrize(
    "silenced",
    ["level", "disabled", "disable", "child", "child level"],
)
def test_what_pypdf_skipped_is_refused_however_logging_is_set(silenced: str) -> None:
    """A /Prev pypdf cannot read: it says so in a warning, which logging may drop.

    With the pypdf logger at ERROR, or logging disabled, it passed (third
    review). Logging is as it was afterwards.
    """
    data = one_page().replace(b"/Root 1 0 R ", b"/Root 1 0 R /Prev 3 ")
    top, child = logging.getLogger("pypdf"), logging.getLogger("pypdf._reader")
    saved = (top.level, child.disabled, child.level, child.propagate)
    try:
        if silenced == "level":
            top.setLevel(logging.ERROR)
        elif silenced == "disabled":
            child.disabled = True
        elif silenced == "disable":
            logging.disable(logging.WARNING)
        elif silenced == "child":
            child.propagate = False
        else:
            child.setLevel(logging.ERROR)
        before = (
            top.level,
            child.disabled,
            child.level,
            child.propagate,
            logging.root.manager.disable,
        )

        assert verdict(PdfCheck(), data) == "unreadable_file"
        assert verdict(PdfCheck(), one_page()) == "ok"
        assert before == (
            top.level,
            child.disabled,
            child.level,
            child.propagate,
            logging.root.manager.disable,
        )
    finally:
        logging.disable(logging.NOTSET)
        top.setLevel(saved[0])
        child.disabled = saved[1]
        child.setLevel(saved[2])
        child.propagate = saved[3]


def test_two_checks_at_once_put_logging_back_once_both_have_ended() -> None:
    """The first to start saves how logging is set; the last to end restores it."""
    top = logging.getLogger("pypdf")
    saved = top.level
    top.setLevel(logging.ERROR)
    try:
        with inspection_module._heard:
            with inspection_module._heard:
                assert top.level == logging.WARNING
            assert top.level == logging.WARNING
        assert top.level == logging.ERROR
    finally:
        top.setLevel(saved)


def test_an_earlier_revision_is_read_too() -> None:
    """An object an update replaced keeps what it held, for a reader that repairs."""
    first = one_page("", "/Annots [4 0 R]", "<< /A << /S /JavaScript /JS (x) >> >>")
    update = "4 0 obj\n<< /A << /S /GoTo /D [3 0 R /Fit] >> >>\nendobj\n"
    start = len(first) + len(update)
    data = (
        first
        + update.encode()
        + (
            f"xref\n4 1\n{len(first):010d} 00000 n \ntrailer\n<< /Size 5 /Root 1 0 R "
            f"/Prev {first.index(b'\nxref') + 1} >>\nstartxref\n{start}\n%%EOF\n"
        ).encode()
    )

    assert verdict(PdfCheck(), data) == SCRIPTS
    replaced = data.replace(b"/JavaScript /JS (x)", b"/Named /N /NextPage")
    assert verdict(PdfCheck(), replaced) == "ok"


def test_object_streams_decode_to_a_bound_each_and_in_all(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Many streams under pypdf's own limit made a check of a gigabyte."""
    three = streamed_pdf(400_000, 400_000, 400_000)

    assert verdict(PdfCheck(max_decoded=1_000_000), three) == COMPLEX
    assert verdict(PdfCheck(max_decoded=2_000_000), three) == "ok"
    monkeypatch.setitem(
        inspection_module.PDF_LIMITS, "zlib_maximum_output_length", 1000
    )
    assert verdict(PdfCheck(), streamed_pdf(2000)) == "unreadable_file"


def test_a_pdf_check_is_bounded_in_objects_and_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``max_objects`` the table declares; ``max_seconds``, looked at per object."""
    assert verdict(PdfCheck(max_objects=2), one_page()) == COMPLEX
    monkeypatch.setattr(time, "monotonic", jump())
    assert verdict(PdfCheck(max_seconds=10), one_page()) == COMPLEX


def test_pypdf_may_name_an_object_by_its_number_alone() -> None:
    """``get_object(1)``, as pypdf asks for a catalog it recovers: generation 0."""
    import pypdf

    data = one_page()
    reader = pypdf.PdfReader(io.BytesIO(data), strict=True)
    check = inspection_module._Pdf(
        PdfCheck(), reader, data, inspection_module._Clock(1e18)
    )

    assert check.resolve(1) == reader.trailer["/Root"]


def test_a_pdf_a_strict_reader_must_repair_is_unreadable() -> None:
    """A table at the wrong place: pypdf rebuilt it from the file; no longer."""
    data = one_page()
    broken = data.replace(b"startxref\n", b"startxref\n1", 1)

    assert verdict(PdfCheck(), broken) == "unreadable_file"


def test_an_office_check_is_bounded_by_the_uploads_own_size_and_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """200 parts just under 1 MiB, each held to no ratio, made a check of 37 s.

    In all, a package inflates to ``max_expansion`` times its size; and the
    clock is looked at part after part.
    """
    part = "<x>" + "<a/>" * 5_000 + "</x>"
    many = office(parts={f"word/p{index}.xml": part for index in range(20)})

    assert verdict(OfficeCheck("docx"), many) == COMPLEX
    assert verdict(OfficeCheck("docx", max_expansion=1000), many) == "ok"
    monkeypatch.setattr(time, "monotonic", jump())
    assert verdict(OfficeCheck("docx", max_expansion=1000), many) == COMPLEX


async def test_storing_a_received_upload_drops_its_temporary_file() -> None:
    """Stored or refused, the temporary file is closed; closing again is nothing."""
    storage = MemoryStorage()
    stored = await receive_upload(PNG_BYTES, types=IMAGES, max_size=1000)
    failing = await receive_upload(PNG_BYTES, types=IMAGES, max_size=1000)

    await stored.store(storage)
    with pytest.raises(ValueError, match="storage directory"):
        await failing.store(storage, directory="../x")
    stored.close()

    assert stored.file.closed
    assert failing.file.closed
    assert time.monotonic()
