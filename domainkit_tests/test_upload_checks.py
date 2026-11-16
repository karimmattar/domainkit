"""Uploads checked whole before they are kept: PDFs, Office documents, hooks (U1).

The first bytes say what a file claims to be; a Word document with macros
or a PDF that runs a script starts like any other. A type's ``inspect``
hook reads the whole file before ``save_upload`` stores it, and a file it
refuses is never stored (ADR 0167).
"""

import io
import sys
import zipfile
from collections.abc import AsyncIterator
from pathlib import Path
from typing import IO, Final

import pytest
from pypdf import PdfWriter
from starlette.datastructures import UploadFile

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
from domainkit.storage import MemoryStorage
from domainkit.storage import inspection as inspection_module
from domainkit.storage.inspection import (
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
SHEET: Final = "xl/macrosheets/sheet1.xml"
LINKED: Final = "xl/externalLinks/externalLink1.xml"


def verdict(check: PdfCheck | OfficeCheck, data: bytes) -> str:
    """What ``check`` says of ``data``: ``ok``, the refusal, or ``not of type``."""
    try:
        check(io.BytesIO(data))
    except FileRefused as refused:
        return refused.message if refused.code == "unsafe_file" else refused.code
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
    with pytest.raises(ValueError, match="max_pages is 1 or more"):
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
    assert verdict(OfficeCheck("docx", max_ratio=5000), bomb) == "ok"
    assert (
        verdict(OfficeCheck("docx"), zipped(("a.xml", b"<a/>"), flags=1)) == ENCRYPTED
    )
    assert verdict(OfficeCheck("docx"), repeated) == "unreadable_file"
    with pytest.raises(ValueError, match="An Office check takes"):
        OfficeCheck("odt")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="max_parts is 1 or more"):
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


async def test_a_hook_of_ones_own_reads_the_whole_file(tmp_path: Path) -> None:
    """``FileType(..., inspect=...)``: its refusal is 422, with its code."""
    seen: list[bytes] = []

    def no_secrets(file: IO[bytes]) -> None:
        content = file.read()
        seen.append(content)
        if b"SECRET" in content:
            raise FileRefused("Files with secrets are not accepted.", code="secret")

    text = FileType("text/plain", ".txt", lambda head: True, no_secrets)
    storage = MemoryStorage()

    stored = await save_upload(b"hello", types=[text], max_size=100, storage=storage)
    with pytest.raises(UploadRefused) as refused:
        await save_upload(b"a SECRET", types=[text], max_size=100, storage=storage)

    assert seen == [b"hello", b"a SECRET"]
    assert (refused.value.status_code, refused.value.code) == (422, "secret")
    assert list(storage.files) == [stored.name]
