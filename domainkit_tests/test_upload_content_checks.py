"""What a checked document draws and fetches: page content, fields, VML (U5, U6).

``PdfCheck`` read every object of a PDF but decoded no page's content: a
170 KB file of twenty pages, each 8 MiB of spaces deflated, passed, and
laying text over it made 168 MB in the web worker. It now decodes each
page's content in the check's process, counting, within ``max_content``.
``OfficeCheck`` read a field's instruction once the field separated or
ended, and only from ``w:instrText``; and it took a VML picture's source
from ``r:id`` alone (ADR 0175).
"""

import codecs
import gzip
import io
import random
import time
import tracemalloc
import zlib
from collections.abc import Sequence
from typing import Final

import pytest

from domainkit import inspection
from domainkit.inspection import (
    ACTIONS,
    COMPLEX,
    EXTERNAL,
    FileRefused,
    NotOfType,
    OfficeCheck,
    PdfCheck,
)
from domainkit_tests.support.documents import R, W, document, office, pdf, relationships

MIB: Final = 1024 * 1024
VML: Final = 'xmlns:v="urn:schemas-microsoft-com:vml"'
OFFICE: Final = 'xmlns:o="urn:schemas-microsoft-com:office:office"'
RELS: Final = (
    'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"'
)


def verdict(check: PdfCheck | OfficeCheck, data: bytes) -> str:
    """What ``check`` says of ``data``: ``ok``, the refusal, or ``not of type``."""
    try:
        check(io.BytesIO(data))
    except FileRefused as refused:
        return refused.message if refused.code == "unsafe_file" else refused.code
    except NotOfType:
        return "not of type"
    return "ok"


def stream(data: bytes, filters: str = "/FlateDecode") -> str:
    """A stream object's body: ``data`` as written, its ``filters`` named."""
    named = f" /Filter {filters}" if filters else ""
    return (
        f"<< /Length {len(data)}{named} >>\nstream\n{data.decode('latin-1')}\nendstream"
    )


def paged(pages: Sequence[Sequence[int] | int | str], *streams: str) -> bytes:
    """A PDF of a page per item of ``pages``, its contents ``streams`` by index.

    An item is one stream's index, a list of them (an array), or the text
    of the page's ``/Contents`` as written.
    """
    first = 3 + len(pages)

    def contents(item: Sequence[int] | int | str) -> str:
        if isinstance(item, str):
            return item
        if isinstance(item, int):
            return f"{first + item} 0 R"
        return "[" + " ".join(f"{first + each} 0 R" for each in item) + "]"

    kids = " ".join(f"{3 + index} 0 R" for index in range(len(pages)))
    return pdf(
        "<< /Type /Catalog /Pages 2 0 R >>",
        f"<< /Type /Pages /Kids [{kids}] /Count {len(pages)} >>",
        *(
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 100 100] "
            f"/Contents {contents(item)} >>"
            for item in pages
        ),
        *streams,
    )


def spaces(size: int) -> bytes:
    """``size`` spaces, deflated: content that inflates a thousand times."""
    return zlib.compress(b" " * size, 9)


# PDF ---------------------------------------------------------------------------------


def test_twenty_pages_of_deflated_spaces_are_refused() -> None:
    """The report's file: 170 KB, each page's content 8 MiB, 160 MiB in all."""
    page = stream(spaces(8 * MIB))
    bomb = paged(range(20), *([page] * 20))

    assert len(bomb) < 200_000
    assert verdict(PdfCheck(), bomb) == COMPLEX
    assert verdict(PdfCheck(max_content=8 * MIB), paged([0], page)) == "ok"
    assert verdict(PdfCheck(max_content=8 * MIB - 1), paged([0], page)) == COMPLEX


@pytest.mark.parametrize(
    ("pages", "streams", "counted"),
    [
        ([0, 0, 0], [stream(spaces(1000))], 3000),
        ([[0, 1, 0]], [stream(spaces(1000)), stream(spaces(500))], 2500),
        ([0], [stream(b"q Q " * 250, "")], 1000),
        ([0], [stream(b"q Q " * 250, "null")], 1000),
        ([0], [stream(b"20" * 1000 + b">", "/ASCIIHexDecode")], 1000),
        ([0], [stream(zlib.compress(spaces(1000)), "[/FlateDecode /Fl]")], 1000),
        ([0], [stream(spaces(1000), "[/FlateDecode]")], 1000),
        ([0], [stream(spaces(1000) + b"junk after the end")], 1000),
        ([0], [stream(spaces(1000)[:-4])], 1000),
        ([0], [stream(gzip.compress(b" " * 1000, mtime=0))], 1000),
        (["null", "[null 1 0 R]", "/Name"], [], 0),
    ],
)
def test_each_pages_content_counts_each_time_it_is_drawn(
    pages: list[Sequence[int] | int | str], streams: list[str], counted: int
) -> None:
    """A stream drawn twice counts twice; any filter; what is not content is not.

    Deflated twice; with bytes after its end, or cut short of its
    checksum; gzip, which pypdf recovers; hex, which pypdf decodes; none
    at all.
    """
    data = paged(pages, *streams)

    assert verdict(PdfCheck(max_content=counted or 1), data) == "ok"
    if counted:
        assert verdict(PdfCheck(max_content=counted - 1), data) == COMPLEX


def test_other_filters_are_held_to_what_is_left() -> None:
    """Decoded by pypdf, each filter within what remains of ``max_content``."""
    runs = b"\x81 " * 8000 + b"\x80"  # 128 spaces a pair: 1 MiB from 16 KB.
    data = paged([0, 1], stream(spaces(500_000)), stream(runs, "/RunLengthDecode"))

    assert verdict(PdfCheck(max_content=1_524_000), data) == "ok"
    assert verdict(PdfCheck(max_content=1_523_999), data) == COMPLEX
    assert verdict(PdfCheck(max_content=600_000), data) == COMPLEX


def test_page_content_is_counted_never_held() -> None:
    """32 MiB of content is inflated a chunk at a time: nothing near it is kept."""
    data = paged([0], stream(spaces(32 * MIB)))
    assert verdict(PdfCheck(), paged([0], stream(spaces(10)))) == "ok"  # Imports.

    tracemalloc.start()
    try:
        assert verdict(PdfCheck(), data) == "ok"
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert peak < 4 * MIB


def test_content_is_counted_within_the_checks_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The clock is looked at as the content inflates, a chunk at a time.

    Deflated twice, the first inflation gives 4 MiB, which the second
    reads 64 KiB at a time: the time runs out on the way.
    """
    stored = zlib.compress(b" " * 4 * MIB, 0)
    data = paged([0], stream(zlib.compress(stored), "[/Fl /Fl]"))
    looks = 0
    counting = False
    real = time.monotonic
    count_content = inspection._Pdf._count_content

    def counted(check: inspection._Pdf) -> None:
        nonlocal counting
        counting = True
        count_content(check)

    def late() -> float:
        nonlocal looks
        looks += counting
        return real() + (100.0 if looks > 3 else 0.0)

    monkeypatch.setattr(inspection._Pdf, "_count_content", counted)
    monkeypatch.setattr(time, "monotonic", late)

    assert verdict(PdfCheck(), data) == COMPLEX
    assert looks > 3


def test_the_time_is_read_once_pypdf_has_decoded_a_stream(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stream pypdf decoded past the deadline refuses: pypdf looks at no clock."""
    import pypdf.filters

    data = paged([0], stream(b"20" * 10 + b">", "/ASCIIHexDecode"))
    decode = pypdf.filters.decode_stream_data
    late = False
    real = time.monotonic

    def slow(stream: object) -> bytes:
        nonlocal late
        late = True
        return decode(stream)  # type: ignore[arg-type]

    monkeypatch.setattr(pypdf.filters, "decode_stream_data", slow)
    monkeypatch.setattr(time, "monotonic", lambda: real() + (100.0 if late else 0.0))

    assert verdict(PdfCheck(), data) == COMPLEX


def test_a_bound_on_content_is_positive() -> None:
    """``max_content`` as the others."""
    with pytest.raises(ValueError, match="max_content is more than 0"):
        PdfCheck(max_content=0)


def test_a_check_pickles_with_its_content_bound() -> None:
    """It is handed to the check's process."""
    import pickle

    check = PdfCheck(max_content=5)
    assert pickle.loads(pickle.dumps(check)) == check  # noqa: S301 - our own.


# Office ------------------------------------------------------------------------------


def field(*runs: str) -> str:
    """A paragraph of runs, each a field character or instruction text."""
    return "<w:p>" + "".join(f"<w:r>{each}</w:r>" for each in runs) + "</w:p>"


BEGIN: Final = '<w:fldChar w:fldCharType="begin"/>'
SEPARATE: Final = '<w:fldChar w:fldCharType="separate"/>'
END: Final = '<w:fldChar w:fldCharType="end"/>'
PICTURE: Final = '<w:instrText>INCLUDEPICTURE "https://a.example/x.png"</w:instrText>'


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        (field(BEGIN, PICTURE), EXTERNAL),
        (field(BEGIN, PICTURE, SEPARATE), EXTERNAL),
        (field(BEGIN, "<w:instrText>PAGE</w:instrText>"), "ok"),
        (field(BEGIN, PICTURE.replace("instrText", "delInstrText"), END), EXTERNAL),
        (
            field(
                BEGIN,
                "<w:delInstrText>INCLUDE</w:delInstrText>",
                '<w:instrText>PICTURE "https://a.example/x.png"</w:instrText>',
                END,
            ),
            EXTERNAL,
        ),
        (
            field(
                BEGIN,
                "<w:delInstrText>X</w:delInstrText>",
                '<w:instrText>INCLUDEPICTURE "https://a.example/x.png"</w:instrText>',
                END,
            ),
            EXTERNAL,
        ),
        (
            field(BEGIN, "<w:delInstrText>X</w:delInstrText>", BEGIN),
            ACTIONS,
        ),
        (field(BEGIN, "<w:instrText>PAGE</w:instrText>", SEPARATE, BEGIN), "ok"),
        (field(BEGIN, "<w:instrText>PAGE</w:instrText>", SEPARATE, PICTURE), "ok"),
    ],
)
def test_a_field_never_ended_or_deleted_is_read_too(body: str, expected: str) -> None:
    """Its instruction is read at the part's end; a deleted one is an instruction.

    A reader may run a field that never ends, and a tracked deletion of
    its instruction is undone by rejecting the change. Text in a field's
    result is not its instruction.
    """
    assert verdict(OfficeCheck("docx"), office(main=document(body))) == expected


def instruction(text: str) -> str:
    """A field of ``text``, as Word writes one: begun, instructed, shown, ended."""
    return field(
        BEGIN,
        f'<w:instrText xml:space="preserve">{text}</w:instrText>',
        SEPARATE,
        "<w:t>shown</w:t>",
        END,
    )


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        (instruction('HYPERLINK "file://host/share/x"'), EXTERNAL),
        (instruction('HYPERLINK "\\\\\\\\host\\\\share\\\\x"'), EXTERNAL),
        (instruction("HYPERLINK \\\\host\\share"), EXTERNAL),
        (instruction('HYPERLINK "other.docx"'), EXTERNAL),
        (instruction('HYPERLINK \\o "https://a.example" "file:///c:/x"'), EXTERNAL),
        (instruction('HYPERLINK "https://a.example/" \\o "file:///tip"'), "ok"),
        (instruction('HYPERLINK "mailto:a@b.example"'), "ok"),
        (instruction('HYPERLINK \\l "chapter_2"'), "ok"),
        (instruction('HYPERLINK \\* MERGEFORMAT "https://a.example/"'), "ok"),
        (
            "<w:p><w:fldSimple w:instr='HYPERLINK \"file:///c:/x\"'/></w:p>",
            EXTERNAL,
        ),
    ],
)
def test_a_hyperlink_field_links_as_a_hyperlink_may(body: str, expected: str) -> None:
    """To a web page or a mail address, as a hyperlink's relationship may.

    A file or a share is refused, as such a relationship is: a converter
    may fetch it. An anchor in the document is no target.
    """
    assert verdict(OfficeCheck("docx"), office(main=document(body))) == expected


def picture(element: str, *namespaces: str) -> str:
    """A paragraph holding a VML shape of ``element``."""
    declared = " ".join((VML, *namespaces))
    return (
        f'<w:p><w:r><w:pict><v:shape {declared} style="width:10pt;height:10pt">'
        f"{element}</v:shape></w:pict></w:r></w:p>"
    )


HYPERLINK: Final = (f"{R}hyperlink", "https://a.example/x.png", "External")
IMAGE: Final = (f"{R}image", "media/x.png")


@pytest.mark.parametrize(
    ("element", "expected"),
    [
        ('<v:imagedata src="https://a.example/x.png"/>', EXTERNAL),
        ('<v:imagedata src="\\\\host\\share\\x.png"/>', EXTERNAL),
        ('<v:fill src="https://a.example/x.png" type="frame"/>', EXTERNAL),
        ('<v:imagedata src=""/>', "ok"),
        (f'<v:imagedata {OFFICE} o:relid="rId1"/>', EXTERNAL),
        (f'<v:fill {OFFICE} o:relid="rId1" type="frame"/>', EXTERNAL),
        (f'<v:imagedata {OFFICE} o:relid="rId2" o:title=""/>', "ok"),
        (f'<v:imagedata {RELS} r:id="rId2"/>', "ok"),
        (f'<v:imagedata {OFFICE} o:href="https://a.example/x.png"/>', EXTERNAL),
        (f'<v:imagedata {OFFICE} o:althref="file:///c:/x.png"/>', EXTERNAL),
        (f'<v:fill {OFFICE} o:href="https://a.example/x.png" type="frame"/>', EXTERNAL),
        (f'<v:fill {OFFICE} o:althref="https://a.example/x.png"/>', EXTERNAL),
        (f'<v:stroke {OFFICE} o:href="https://a.example/x.png"/>', EXTERNAL),
        (f'<v:shape {OFFICE} o:href="https://a.example/"/>', "ok"),
    ],
)
def test_a_vml_picture_is_held_not_fetched(element: str, expected: str) -> None:
    """``src``, an image's ``o:href``, and ``o:relid`` naming a hyperlink, refused.

    A VML picture names its image by ``r:id`` or ``o:relid``, held in the
    package; a source of its own, or a hyperlink's relationship, is
    fetched. A shape's own ``o:href`` is a link to follow, not a picture.
    """
    data = office(
        main=document(picture(element)),
        main_relationships=[HYPERLINK, IMAGE],
        parts={"word/media/x.png": b"\x89PNG\r\n\x1a\n"},
        defaults={"png": "image/png"},
    )

    assert verdict(OfficeCheck("docx"), data) == expected


def test_a_workbooks_vml_drawing_is_checked_alike() -> None:
    """A comment's or a control's shapes live in a ``.vml`` part, ``o:relid``'s."""
    shape = (
        f'<xml {VML} {OFFICE}><v:shape><v:fill o:relid="rId1" type="frame"/>'
        "</v:shape></xml>"
    )
    rels = (
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/'
        f'relationships"><Relationship Id="rId1" Type="{R}hyperlink" '
        'Target="https://a.example/x.png" TargetMode="External"/></Relationships>'
    )
    data = office(
        "xlsx",
        parts={
            "xl/drawings/vmlDrawing1.vml": shape,
            "xl/drawings/_rels/vmlDrawing1.vml.rels": rels,
        },
    )

    assert verdict(OfficeCheck("xlsx"), data) == EXTERNAL


DRAWINGML: Final = (
    'xmlns:wp="http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing" '
    'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" '
    'xmlns:pic="http://schemas.openxmlformats.org/drawingml/2006/picture" '
    f"{RELS}"
)
HEADER_REFERENCE: Final = (
    f"<w:p/><w:sectPr><w:headerReference {RELS} "
    'w:type="default" r:id="rId1"/></w:sectPr>'
)


def blip(attribute: str) -> str:
    """A paragraph holding a DrawingML picture whose ``a:blip`` has ``attribute``."""
    return (
        f"<w:p><w:r><w:drawing><wp:inline {DRAWINGML}>"
        '<wp:extent cx="914400" cy="914400"/><wp:docPr id="1" name="p"/>'
        '<a:graphic><a:graphicData uri="http://schemas.openxmlformats.org/'
        'drawingml/2006/picture"><pic:pic><pic:nvPicPr><pic:cNvPr id="1" name="p"/>'
        f"<pic:cNvPicPr/></pic:nvPicPr><pic:blipFill><a:blip {attribute}/>"
        "</pic:blipFill><pic:spPr/></pic:pic></a:graphicData></a:graphic>"
        "</wp:inline></w:drawing></w:r></w:p>"
    )


def headed(name: str, header: str | bytes, *rels: tuple[str, ...]) -> bytes:
    """A document whose header is the part ``word/<name>``, a PNG by its type."""
    parts: dict[str, str | bytes] = {f"word/{name}": header}
    if rels:
        parts[f"word/_rels/{name}.rels"] = relationships(*rels)  # type: ignore[arg-type]
    return office(
        main=document(HEADER_REFERENCE),
        main_relationships=[(f"{R}header", name)],
        parts=parts,
        defaults={"png": "image/png"},
    )


def header(body: str) -> str:
    """A Word header of ``body``."""
    return f'<w:hdr xmlns:w="{W}">{body}</w:hdr>'


SHUFFLED: Final = random.Random(7)  # noqa: S311 - white space, not a secret.
WHITE: Final = "".join(SHUFFLED.choice(" \t\r\n") for _ in range(70_000))
"""White space a reader passes over before the markup, more than a chunk of it."""


@pytest.mark.parametrize(
    ("data", "expected"),
    [
        pytest.param(
            headed(
                "header2.png",
                header(blip('r:link="rId1"')),
                (f"{R}hyperlink", "https://a.example/x.png", "External"),
            ),
            EXTERNAL,
            id="picture-linked-to-a-hyperlink",
        ),
        pytest.param(
            headed("header2.png", header(field(BEGIN, PICTURE, END))),
            EXTERNAL,
            id="png",
        ),
        pytest.param(
            headed("header3.dat", header(field(BEGIN, PICTURE, END))),
            EXTERNAL,
            id="no-type",
        ),
        pytest.param(
            headed("header2.png", WHITE + header(field(BEGIN, PICTURE, END))),
            EXTERNAL,
            id="white-space-first",
        ),
        pytest.param(
            headed(
                "header2.png",
                codecs.BOM_UTF16_LE
                + header(field(BEGIN, PICTURE, END)).encode("utf-16-le"),
            ),
            EXTERNAL,
            id="utf-16",
        ),
        pytest.param(
            headed("header2.png", header("<w:p/>").encode("utf-16-be")),
            "unreadable_file",
            id="utf-16-unmarked",
        ),
        pytest.param(headed("header2.png", header("<w:p/>")), "ok", id="harmless"),
        pytest.param(
            headed("header2.png", b"\x89PNG\r\n\x1a\n" + b"<" * 100),
            "ok",
            id="a-png",
        ),
        pytest.param(headed("header2.png", b""), "ok", id="empty"),
        pytest.param(
            headed("header2.png", "<w:hdr"), "unreadable_file", id="not-xml-after-all"
        ),
    ],
)
def test_a_part_a_relationship_names_is_read_as_the_reader_reads_it(
    data: bytes, expected: str
) -> None:
    """A header called ``header2.png``, typed a PNG, is still a header.

    A reader follows the relationship and parses the part as XML whatever
    its content type: so is it checked, when its bytes start as XML does.
    One that does not start so is no XML to any reader.
    """
    assert verdict(OfficeCheck("docx"), data) == expected


def test_a_part_no_reader_takes_as_xml_is_not_read_as_xml() -> None:
    """An image, a font, an embedded package: not parsed, whatever they hold."""
    data = office(
        main=document(),
        main_relationships=[
            (f"{R}image", "media/x.png"),
            (f"{R}font", "fonts/f.odttf"),
        ],
        parts={"word/media/x.png": "<not xml", "word/fonts/f.odttf": "< nor this"},
        defaults={
            "png": "image/png",
            "odttf": "application/vnd.openxmlformats-officedocument.obfuscatedFont",
        },
    )

    assert verdict(OfficeCheck("docx"), data) == "ok"
