r"""What an Office document reaches: imported chunks, part names, instructions (U9).

``OfficeCheck`` refused an imported chunk (``w:altChunk``) by its
relationship's type alone, which LibreOffice never reads: it finds the
chunk's part by its id, whatever the type. It resolved a relationship's
target as no reader does, decoding a ``%`` and keeping a query, and read
a part as XML by its bytes only when its own resolution reached it. And it
read no ``w:instrText`` outside a field's instruction (ADR 0175, amended
2026-11-23).
"""

import gzip
import io
import zipfile
from typing import Final

import pytest

from domainkit.inspection import (
    ACTIONS,
    EMBEDDED,
    EXTERNAL,
    FileRefused,
    NotOfType,
    OfficeCheck,
)
from domainkit_tests.support.documents import (
    R,
    W,
    document,
    entry,
    office,
    relationships,
    zipped,
)

STRICT: Final = "http://purl.oclc.org/ooxml/wordprocessingml/main"
RELS: Final = (
    'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"'
)
BACKSLASH: Final = chr(0x5C)
ARABIC: Final = "".join(map(chr, (0x635, 0x648, 0x631, 0x629)))
"""A picture's name in Arabic letters."""
UNREADABLE: Final = "unreadable_file"
PICTURE: Final = '<w:instrText>INCLUDEPICTURE "https://a.example/x.png"</w:instrText>'
BEGIN: Final = '<w:fldChar w:fldCharType="begin"/>'
SEPARATE: Final = '<w:fldChar w:fldCharType="separate"/>'
END: Final = '<w:fldChar w:fldCharType="end"/>'


def verdict(data: bytes, kind: str = "docx") -> str:
    """What ``OfficeCheck(kind)`` says of ``data``: ``ok``, a message, or a code."""
    try:
        OfficeCheck(kind)(io.BytesIO(data))  # type: ignore[arg-type]
    except FileRefused as refused:
        return refused.message if refused.code == "unsafe_file" else refused.code
    except NotOfType:
        return "not of type"
    return "ok"


def field(*runs: str) -> str:
    """A paragraph of runs, each a field character or instruction text."""
    return "<w:p>" + "".join(f"<w:r>{each}</w:r>" for each in runs) + "</w:p>"


def header(body: str) -> str:
    """A Word header of ``body``."""
    return f'<w:hdr xmlns:w="{W}">{body}</w:hdr>'


FETCHING: Final = header(field(BEGIN, PICTURE, END))
"""A header whose field fetches a picture when it is updated."""
HEADER_REFERENCE: Final = (
    f"<w:p/><w:sectPr><w:headerReference {RELS} "
    'w:type="default" r:id="rId1"/></w:sectPr>'
)


# Imported chunks ---------------------------------------------------------------------


def chunked(namespace: str, kind: str | None, *, where: str = "document") -> bytes:
    """A document holding ``w:altChunk`` of ``namespace``, its relationship ``kind``.

    The chunk is in the main part, or in its header (``where``); ``kind``
    None names no relationship at all.
    """
    chunk = f'<w:altChunk xmlns:w="{namespace}" {RELS} r:id="rId1"/>'
    rels = [] if kind is None else [(kind, "chunk.docx")]
    if where == "document":
        return office(
            main=document(f"<w:p/>{chunk}"),
            main_relationships=rels,
            parts={"word/chunk.docx": office()},
        )
    return office(
        main=document(HEADER_REFERENCE),
        main_relationships=[(f"{R}header", "header1.xml")],
        parts={
            "word/header1.xml": header(chunk),
            "word/_rels/header1.xml.rels": relationships(*rels),
            "word/chunk.docx": office(),
        },
    )


@pytest.mark.parametrize(
    "kind",
    [
        pytest.param(f"{R}image", id="typed-an-image"),
        pytest.param("http://a.example/any", id="typed-anything"),
        pytest.param(None, id="no-relationship"),
        pytest.param(f"{R}aFChunk", id="typed-a-chunk"),
    ],
)
@pytest.mark.parametrize("namespace", [W, STRICT], ids=["transitional", "strict"])
@pytest.mark.parametrize("where", ["document", "header"])
def test_an_imported_chunk_is_refused_whatever_its_relationship(
    kind: str | None, namespace: str, where: str
) -> None:
    """LibreOffice imports the part a chunk names by its id, whatever the type.

    It imports it with the filter it detects (a Word document, a web
    page), and no check opens it: a document to fill or show has no use
    for one.
    """
    assert verdict(chunked(namespace, kind, where=where)) == EMBEDDED


# Part names and targets --------------------------------------------------------------


def headed(target: str, name: str, *, content_type: str = "") -> bytes:
    """A document whose header relationship's target is ``target``; ``name`` its part.

    The part is a header whose field fetches, named ``name`` in the archive.
    """
    return office(
        main=document(HEADER_REFERENCE),
        main_relationships=[(f"{R}header", target)],
        parts={name: FETCHING},
        types={name: content_type} if content_type else None,
    )


@pytest.mark.parametrize(
    ("target", "name"),
    [
        pytest.param("header%31.bin", "word/header%31.bin", id="percent"),
        pytest.param("header%31.bin", "word/header1.bin", id="percent-decoded"),
        pytest.param("header1.bin#x", "word/header1.bin", id="fragment"),
        pytest.param("header1.bin?x", "word/header1.bin", id="query"),
        pytest.param(f"x{BACKSLASH}header1.bin", "word/x/header1.bin", id="backslash"),
        pytest.param(" header1.bin", "word/header1.bin", id="leading-space"),
        pytest.param("x//header1.bin", "word/x//header1.bin", id="empty-segment"),
        pytest.param("x//header1.bin", "word/x/header1.bin", id="empty-collapsed"),
        pytest.param("x/", "word/x", id="trailing-slash"),
        pytest.param("", "word/header1.bin", id="empty"),
        pytest.param("x/h:1.bin", "word/x/h1.bin", id="colon"),
        pytest.param(f"header{chr(0xB9)}.bin", "word/header1.bin", id="not-ascii"),
    ],
)
def test_a_target_outside_what_office_writes_is_refused(target: str, name: str) -> None:
    """Readers resolve such a target each their own way: none is guessed.

    LibreOffice keeps its ``%`` and its empty segments and drops its
    query and fragment; Apache POI decodes a ``%``; .NET's packaging
    takes a backslash for a slash. No Office writer writes one.
    """
    assert verdict(headed(target, name)) == UNREADABLE


@pytest.mark.parametrize(
    "name",
    [
        pytest.param("word/header%31.xml", id="percent"),
        pytest.param("word/x//header1.xml", id="empty-segment"),
        pytest.param("word/./header1.xml", id="dot"),
        pytest.param("word/../header1.xml", id="dot-dot"),
        pytest.param("/word/header1.xml", id="leading-slash"),
        pytest.param(f"word/x{BACKSLASH}header1.xml", id="backslash"),
        pytest.param("word/ header1.xml", id="space"),
        pytest.param("word/header1.xml?x", id="query"),
        pytest.param("word/header1.xml#x", id="fragment"),
        pytest.param("word/h:1.xml", id="colon"),
        pytest.param(f"word/media/{ARABIC}.png", id="not-ascii"),
    ],
)
def test_a_part_named_outside_what_office_writes_is_refused(name: str) -> None:
    """A name is matched as written, so it must be one every reader writes alike."""
    assert verdict(office(main=document(), parts={name: "<x/>"})) == UNREADABLE


@pytest.mark.parametrize(
    ("kind", "source", "target"),
    [
        pytest.param("docx", "word/document.xml", "media/image1.png", id="relative"),
        pytest.param("docx", "word/document.xml", "/word/media/image1.png", id="root"),
        pytest.param(
            "pptx", "ppt/slides/slide1.xml", "../media/image1.png", id="parent"
        ),
        pytest.param(
            "xlsx", "xl/worksheets/sheet1.xml", "./../media/image1.png", id="dots"
        ),
        pytest.param(
            "docx", "word/document.xml", "media/image-1_(a)~b!$&'+,;=@.png", id="marks"
        ),
    ],
)
def test_the_names_and_targets_office_writes_pass(
    kind: str, source: str, target: str
) -> None:
    """Letters, digits, and what a URI's path holds but ``%`` and ``:``.

    ``.`` and ``..`` segments in a target, a target from the package's
    root, and a fragment alone (a link within the part) pass.
    """
    folder, _, base = source.rpartition("/")
    data = office(
        kind,
        parts={
            source: document() if kind == "docx" else "<x/>",
            f"{folder}/_rels/{base}.rels": relationships(
                (f"{R}image", target), (f"{R}hyperlink", "#_Toc1")
            ),
            f"{folder.partition('/')[0]}/media/{target.rpartition('/')[2]}": b"\x89PNG",
        },
        defaults={"png": "image/png"},
    )

    assert verdict(data, kind) == "ok"


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        pytest.param("[trash]/0000.dat", "ok", id="trash"),
        pytest.param("[Trash]/00AF.dat", "ok", id="trash-in-capitals"),
        pytest.param("[trash]/0000.xml", UNREADABLE, id="not-a-trash-item"),
        pytest.param("[trash]/x/0000.dat", UNREADABLE, id="deeper"),
        pytest.param("word/[trash]/0000.dat", UNREADABLE, id="not-at-the-root"),
    ],
)
def test_the_trash_office_leaves_is_no_part(name: str, expected: str) -> None:
    """``[trash]/0000.dat`` holds what Office 2007 discarded: never a part.

    No target reaches it, as no target holds ``[``: it is not read, what
    its bytes start with whatever.
    """
    data = office(main=document(), parts={name: FETCHING[:40] + "<<< cut"})

    assert verdict(data) == expected


@pytest.mark.parametrize(
    "data",
    [
        pytest.param(zipped(("My File.txt", b"x")), id="a-zip"),
        pytest.param(
            office(
                main_relationships=[(f"{R}image", "x y.png")],
                parts={
                    "_rels/.rels": relationships((f"{R}other", "x%31.bin")),
                    "a b.bin": b"x",
                },
            ),
            id="another-package",
        ),
    ],
)
def test_a_zip_of_another_kind_is_not_of_the_type(data: bytes) -> None:
    """Its names are read once it is known to be an Office document."""
    assert verdict(data) == "not of type"


def test_a_package_with_folders_in_its_directory_passes() -> None:
    """A ZIP written with its folders (``word/``) names no part by them."""
    data = io.BytesIO()
    with (
        zipfile.ZipFile(io.BytesIO(office())) as source,
        zipfile.ZipFile(data, "w", zipfile.ZIP_DEFLATED) as copy,
    ):
        for folder in ("_rels/", "word/"):
            copy.writestr(entry(folder), b"")
        for info in source.infolist():
            copy.writestr(info, source.read(info))

    assert verdict(data.getvalue()) == "ok"


SVG: Final = (
    '<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10">'
    '<image href="file:///etc/hostname" width="10" height="10"/></svg>'
)
"""An SVG whose picture is a local file, which LibreOffice draws."""


@pytest.mark.parametrize(
    ("data", "expected"),
    [
        pytest.param(
            office(main=document(), parts={"word/header1.bin": FETCHING}),
            EXTERNAL,
            id="a-header-nothing-names",
        ),
        pytest.param(
            office(
                main=document(HEADER_REFERENCE),
                main_relationships=[(f"{R}header", "../../header1.bin")],
                parts={"header1.bin": FETCHING},
            ),
            EXTERNAL,
            id="a-header-above-the-root",
        ),
        pytest.param(
            office(main=document(), parts={"word/media/image1.bin": SVG}),
            EXTERNAL,
            id="an-svg-named-bin",
        ),
        pytest.param(
            office(
                main=document(),
                parts={"word/media/image1.png": gzip.compress(SVG.encode(), mtime=0)},
                defaults={"png": "image/png"},
            ),
            EXTERNAL,
            id="an-svg-compressed",
        ),
        pytest.param(
            office(main=document(), parts={"word/media/image1.bin": b"<\x01binary"}),
            "ok",
            id="binary-data",
        ),
    ],
)
def test_a_part_that_starts_as_xml_is_read_whatever_names_it(
    data: bytes, expected: str
) -> None:
    """A reader that resolves a target its own way still reads what the part says.

    A target past the package's root (``../../header1.bin``) is the root's
    ``header1.bin`` by RFC 3986, and no part by LibreOffice's resolution,
    which keeps the extra ``..``. So every part is read as XML when its
    bytes start as XML does, whatever its name, type, or relationship.
    """
    assert verdict(data) == expected


# Field instructions ------------------------------------------------------------------


PAGE: Final = '<w:instrText xml:space="preserve"> PAGE </w:instrText>'


@pytest.mark.parametrize(
    "body",
    [
        pytest.param(field(BEGIN, PAGE, SEPARATE, PICTURE, END), id="after-separate"),
        pytest.param(field(PICTURE), id="outside-any-field"),
        pytest.param(field(PAGE), id="harmless-outside-any-field"),
        pytest.param(
            field(PICTURE.replace("instrText", "delInstrText")), id="deleted-outside"
        ),
        pytest.param(
            field(BEGIN, PAGE, SEPARATE, PICTURE.replace("instrText", "delInstrText")),
            id="deleted-after-separate",
        ),
        pytest.param(field(BEGIN, PAGE, END, PICTURE), id="after-a-field-ended"),
        pytest.param(
            field(BEGIN, PAGE, SEPARATE, BEGIN, PAGE, END, PICTURE, END),
            id="after-a-nested-field-in-a-result",
        ),
    ],
)
def test_an_instruction_outside_a_fields_instruction_is_refused(body: str) -> None:
    """No writer puts one there; a reader that takes it for one would fetch.

    After a field's ``separate`` mark, its result runs to its ``end``:
    an instruction there, or outside any field, is refused as a field
    that hides its name is.
    """
    in_header = office(
        main=document(HEADER_REFERENCE),
        main_relationships=[(f"{R}header", "header1.xml")],
        parts={"word/header1.xml": header(body)},
    )

    assert verdict(office(main=document(body))) == ACTIONS
    assert verdict(in_header) == ACTIONS


def toc_entry(anchor: str, title: str, page: int) -> str:
    """An entry of a table of contents, as Word 365 writes it."""
    hidden = "<w:rPr><w:noProof/><w:webHidden/></w:rPr>"
    return (
        '<w:p><w:pPr><w:pStyle w:val="TOC1"/><w:tabs><w:tab w:val="right" '
        'w:leader="dot" w:pos="9350"/></w:tabs><w:rPr><w:noProof/></w:rPr></w:pPr>'
        f'<w:hyperlink w:anchor="{anchor}" w:history="1">'
        '<w:r><w:rPr><w:rStyle w:val="Hyperlink"/><w:noProof/></w:rPr>'
        f"<w:t>{title}</w:t></w:r>"
        f"<w:r>{hidden}<w:tab/></w:r>"
        f"<w:r>{hidden}{BEGIN}</w:r>"
        f'<w:r>{hidden}<w:instrText xml:space="preserve"> PAGEREF {anchor} \\h '
        "</w:instrText></w:r>"
        f"<w:r>{hidden}</w:r>"
        f"<w:r>{hidden}{SEPARATE}</w:r>"
        f"<w:r>{hidden}<w:t>{page}</w:t></w:r>"
        f"<w:r>{hidden}{END}</w:r>"
        "</w:hyperlink></w:p>"
    )


WORD_TOC: Final = (
    '<w:sdt><w:sdtPr><w:id w:val="-1520855211"/><w:docPartObj>'
    '<w:docPartGallery w:val="Table of Contents"/><w:docPartUnique/></w:docPartObj>'
    "</w:sdtPr><w:sdtEndPr><w:rPr><w:b/><w:bCs/><w:noProof/></w:rPr></w:sdtEndPr>"
    '<w:sdtContent><w:p><w:pPr><w:pStyle w:val="TOCHeading"/></w:pPr>'
    "<w:r><w:t>Contents</w:t></w:r></w:p>"
    '<w:p><w:pPr><w:pStyle w:val="TOC1"/></w:pPr>'
    f"<w:r>{BEGIN}</w:r>"
    '<w:r><w:instrText xml:space="preserve"> TOC \\o "1-3" \\h \\z \\u </w:instrText>'
    f"</w:r><w:r>{SEPARATE}</w:r></w:p>"
    + toc_entry("_Toc184001", "Parties", 1)
    + toc_entry("_Toc184002", "Rent", 2)
    + toc_entry("_Toc184003", "Signatures", 3)
    + f"<w:p><w:r><w:rPr><w:b/><w:bCs/><w:noProof/></w:rPr>{END}</w:r></w:p>"
    "</w:sdtContent></w:sdt>"
    '<w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr>'
    '<w:bookmarkStart w:id="0" w:name="_Toc184001"/><w:r><w:t>Parties</w:t></w:r>'
    '<w:bookmarkEnd w:id="0"/></w:p>'
)
"""A table of contents as Word 365 writes one: in its result, each entry links
to its heading and names its page by a field of its own, each begun."""


@pytest.mark.parametrize(
    "body",
    [
        pytest.param(WORD_TOC, id="word-table-of-contents"),
        pytest.param(
            field(
                BEGIN,
                '<w:instrText xml:space="preserve"> IF </w:instrText>',
                BEGIN,
                '<w:instrText xml:space="preserve"> MERGEFIELD Kind </w:instrText>',
                SEPARATE,
                "<w:t>a</w:t>",
                END,
                '<w:instrText xml:space="preserve"> = "a" "Dear" "Hi" </w:instrText>',
                SEPARATE,
                "<w:t>Dear</w:t>",
                END,
            ),
            id="a-field-in-an-instruction",
        ),
        pytest.param(
            field(
                BEGIN,
                PAGE.replace("instrText", "delInstrText"),
                PAGE,
                SEPARATE,
                "<w:t>1</w:t>",
                END,
            ),
            id="an-instruction-changed-with-tracking",
        ),
    ],
)
def test_the_fields_writers_write_still_pass(body: str) -> None:
    """Fields begun in a result, or in an instruction, are read as their own."""
    assert verdict(office(main=document(body))) == "ok"
