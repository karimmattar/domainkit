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
import io
import random
import time
import tracemalloc
import zlib
from collections.abc import Sequence
from typing import Final

import pytest

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


def stream(data: bytes, filters: str = "/FlateDecode", more: str = "") -> str:
    """A stream object's body: ``data`` as written, its ``filters`` named, ``more``."""
    named = f" /Filter {filters}" if filters else ""
    return (
        f"<< /Length {len(data)}{named}{more} >>\nstream\n"
        f"{data.decode('latin-1')}\nendstream"
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
        (
            [0],
            [stream(zlib.compress(spaces(1000)), "[/FlateDecode /FlateDecode]")],
            1000,
        ),
        ([0], [stream(spaces(1000), "[/FlateDecode]")], 1000),
        ([0], [stream(spaces(1000) + b"junk after the end")], 1000),
        ([0], [stream(spaces(1000)[:-4])], 1000),
        (["null", "[null 1 0 R]", "/Name"], [], 0),
    ],
)
def test_each_pages_content_counts_each_time_it_is_drawn(
    pages: list[Sequence[int] | int | str], streams: list[str], counted: int
) -> None:
    """A stream drawn twice counts twice; what is not content is not.

    Deflated twice; with bytes after its end, or cut short of its
    checksum; not encoded at all.
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


def test_page_content_is_decoded_no_further_than_the_bound() -> None:
    """64 MiB of content, 8 MiB allowed: pypdf stops at 8, and holds no more.

    zlib's output buffer takes up to twice what it returns.
    """
    data = paged([0], stream(spaces(64 * MIB)))
    assert verdict(PdfCheck(), paged([0], stream(spaces(10)))) == "ok"  # Imports.

    tracemalloc.start()
    try:
        assert verdict(PdfCheck(max_content=8 * MIB), data) == COMPLEX
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert peak < 3 * 8 * MIB


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


# The policy overlay_pdf decodes with --------------------------------------------------


def clear_codes(size: int) -> bytes:
    """LZW of ``size`` bytes clearing its table at every code: it decodes to nothing."""
    codes = size * 8 // 9 - 1
    bits = "100000000" * codes + "100000001"  # Clear (256), then the end (257).
    bits += "0" * (-len(bits) % 8)
    return int(bits, 2).to_bytes(len(bits) // 8, "big")


def empty_blocks(size: int) -> bytes:
    """Data of ``size`` bytes of empty stored blocks, zlib: it inflates to nothing."""
    blocks = b"\x00\x00\x00\xff\xff" * (size // 5)
    return b"\x78\x01" + blocks + b"\x01\x00\x00\xff\xff" + (1).to_bytes(4, "big")


def shared(pages: int, references: int) -> bytes:
    """``pages`` pages, each naming one byte of content ``references`` times."""
    named = " ".join(["4 0 R"] * references)
    kids = " ".join(f"{5 + index} 0 R" for index in range(pages))
    return pdf(
        "<< /Type /Catalog /Pages 2 0 R >>",
        f"<< /Type /Pages /Kids [{kids}] /Count {pages} >>",
        f"[{named}]",
        stream(b" ", ""),
        *(
            "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 100 100] /Contents 3 0 R >>"
            for _ in range(pages)
        ),
    )


PREDICTED: Final = " /DecodeParms << /Predictor 12 /Columns 249999 /Colors 16 >>"
"""A predictor whose rows pypdf pads to 4 MB, from a byte."""


@pytest.mark.parametrize(
    "data",
    [
        pytest.param(
            paged([0] * 1000, stream(zlib.compress(b"\x00"), more=PREDICTED)),
            id="predictor-padding",
        ),
        pytest.param(
            paged(
                [0],
                stream(
                    zlib.compress(b"z" * 4 * MIB + b"~>"),
                    "[/FlateDecode /ASCII85Decode]",
                ),
            ),
            id="ascii85-after-deflate",
        ),
        pytest.param(
            paged(
                [0],
                stream(
                    zlib.compress(b"00 " * MIB + b">"), "[/FlateDecode /ASCIIHexDecode]"
                ),
            ),
            id="hex-after-deflate",
        ),
        pytest.param(
            paged(
                [0],
                stream(
                    zlib.compress(b"\xffA" * MIB + b"\x80"),
                    "[/FlateDecode /RunLengthDecode]",
                ),
            ),
            id="runs-after-deflate",
        ),
        pytest.param(
            paged(
                [0],
                stream(zlib.compress(clear_codes(MIB)), "[/FlateDecode /LZWDecode]"),
            ),
            id="lzw-after-deflate",
        ),
        pytest.param(
            paged([0], stream(b"z" * 3 * MIB + b"~>", "/ASCII85Decode")),
            id="ascii85-past-pythons-share",
        ),
        pytest.param(
            paged([0], stream(clear_codes(200 * 1024), "/LZWDecode")),
            id="lzw-past-its-share",
        ),
        pytest.param(
            paged(
                [0],
                stream(
                    zlib.compress(empty_blocks(70 * MIB)), "[/FlateDecode /FlateDecode]"
                ),
            ),
            id="empty-blocks-deflated",
        ),
        pytest.param(
            paged([[0] * 1100], stream(spaces(64 * 1024))), id="one-stream-1100-times"
        ),
        pytest.param(shared(1000, 49_000), id="a-byte-49000-times-a-page"),
        pytest.param(paged([0], stream(b"x", "/DCTDecode")), id="an-images-filter"),
        pytest.param(paged([0], stream(b"x", "/Crypt")), id="crypt"),
        pytest.param(paged([0], stream(b"q Q", "null")), id="a-null-filter"),
    ],
)
def test_page_content_is_decoded_as_overlay_pdf_decodes_it(data: bytes) -> None:
    """One policy: what overlay_pdf would refuse to decode, the check refuses.

    A predictor (pypdf pads each row: 292 KB made 3.8 GB); a filter pypdf
    runs in Python after deflate (60 KB of ASCII85 made 5.7 GB); more
    stored for Python's filters than their share; an image's filter;
    content a page names more often than the check reads values. Each is
    refused in well under the check's time.
    """
    started = time.perf_counter()

    assert verdict(PdfCheck(), data) == COMPLEX
    assert time.perf_counter() - started < 5


def written_in_the_page(contents: str) -> bytes:
    """A one-page PDF whose ``/Contents`` is ``contents``, as written in the page."""
    return pdf(
        "<< /Type /Catalog /Pages 2 0 R >>",
        "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 100 100] /Contents {contents} >>",
    )


@pytest.mark.parametrize("array", [False, True], ids=["alone", "in-an-array"])
def test_a_stream_written_in_the_page_is_counted_too(array: bool) -> None:
    """Content written in the page, not apart, is read by pypdf: so is it counted.

    1 MB of such content inflated to 1 GiB passed in a quarter of a second,
    and pypdf decoded 75 MB of it at once. ``overlay_pdf``, by the same
    policy, refuses it too.
    """
    from domainkit.documents import DocumentError, overlay_pdf
    from domainkit_tests.test_documents_pdf import Page
    from domainkit_tests.test_documents_pdf import pdf as overlay_of

    bomb = stream(spaces(70 * MIB))
    ascii85 = stream(zlib.compress(b"z" * MIB + b"~>"), "[/FlateDecode /ASCII85Decode]")
    small = stream(spaces(1000))

    def written(each: str) -> bytes:
        return written_in_the_page(f"[{each} {each}]" if array else each)

    assert verdict(PdfCheck(), written(bomb)) == COMPLEX
    assert verdict(PdfCheck(), written(ascii85)) == COMPLEX
    for refused in (written(bomb), written(ascii85)):
        with pytest.raises(DocumentError):
            overlay_pdf(refused, overlay_of(Page(width=100, height=100)))
    assert (
        verdict(PdfCheck(max_content=2000 if array else 1000), written(small)) == "ok"
    )
    assert (
        verdict(PdfCheck(max_content=1999 if array else 999), written(small)) == COMPLEX
    )


def test_what_the_policy_allows_passes() -> None:
    """Python's filters first and within their share; 1000 names of one stream."""
    assert (
        verdict(PdfCheck(), paged([0], stream(b"z" * MIB + b"~>", "/ASCII85Decode")))
        == "ok"
    )
    assert (
        verdict(PdfCheck(), paged([0], stream(clear_codes(60 * 1024), "/LZWDecode")))
        == "ok"
    )
    assert verdict(PdfCheck(), paged([[0] * 1000], stream(spaces(64 * 1024)))) == "ok"
    assert verdict(PdfCheck(), shared(10, 1000)) == "ok"


def test_the_readers_bounds_hold_for_the_check_too() -> None:
    """As for overlay_pdf: sections, a rebuilt table, streams in streams, searches.

    32 sections chained by ``/Prev``, however the link is written; an
    object stream inside another, or an object from one not decoded; a
    cross-reference stream decoded but by deflate; a table to rebuild.
    """
    from domainkit_tests.test_documents_pdf import chained, referring, streamed

    bomb = (b"[/FlateDecode /ASCII85Decode]", zlib.compress(b"z" * MIB + b"~>"))
    document = streamed(extras=((b"[/FlateDecode /ASCII85Decode]", b"not decoded"),))
    damaged = document[: document.rindex(b"startxref")] + b"startxref\n0\n%%EOF\n"

    assert verdict(PdfCheck(), chained(32)) == "ok"
    assert verdict(PdfCheck(), streamed(10, used=True)) == "ok"
    for refused in (
        chained(33),
        chained(33, streams=True),
        chained(32, hybrid=True),
        chained(33, link="/P#72ev {at}"),
        chained(500, streams=True),
        streamed(10, extras=(bomb,), nested=True),
        streamed(stray=True, used=True),
        streamed(xref_filter=b"/LZWDecode"),
    ):
        assert verdict(PdfCheck(), refused) == COMPLEX
    assert verdict(PdfCheck(), damaged) == "unreadable_file"
    assert verdict(PdfCheck(), referring(40)) == "unreadable_file"
    assert verdict(PdfCheck(), streamed(entries=200_001)) == COMPLEX  # max_objects.


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


def headed(
    name: str, header: str | bytes, *rels: tuple[str, ...], kind: str = "header"
) -> bytes:
    """A document whose header is the part ``word/<name>``, a PNG by its type.

    ``kind`` types the relationship to it: a reader takes the part as the
    header the main part names (``headerReference``) whatever it says.
    """
    parts: dict[str, str | bytes] = {f"word/{name}": header}
    if rels:
        parts[f"word/_rels/{name}.rels"] = relationships(*rels)  # type: ignore[arg-type]
    return office(
        main=document(HEADER_REFERENCE),
        main_relationships=[(f"{R}{kind}", name)],
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
        *(
            pytest.param(
                headed(
                    "header2.png",
                    header(blip('r:link="rId1"')),
                    (f"{R}hyperlink", "https://a.example/x.png", "External"),
                    kind=kind,
                ),
                EXTERNAL,
                id=f"typed-{kind}",
            )
            for kind in ("image", "font", "hdphoto")
        ),
        pytest.param(
            headed("header2.png", header(field(BEGIN, PICTURE, END)) + "<<< not XML"),
            EXTERNAL,
            id="refused-before-it-breaks",
        ),
        pytest.param(
            headed("header2.png", header("<w:p/>").encode("utf-16-be")),
            COMPLEX,
            id="utf-16-unmarked",
        ),
        pytest.param(headed("header2.png", header("<w:p/>")), "ok", id="harmless"),
        pytest.param(
            headed("header2.png", b"\x89PNG\r\n\x1a\n" + b"<" * 100),
            "ok",
            id="a-png",
        ),
        pytest.param(headed("header2.png", b""), "ok", id="empty"),
        pytest.param(headed("header2.png", "<w:hdr"), "ok", id="not-xml-after-all"),
    ],
)
def test_a_part_a_relationship_names_is_read_as_the_reader_reads_it(
    data: bytes, expected: str
) -> None:
    """A header called ``header2.png``, typed a PNG, is still a header.

    A reader follows the relationship and parses the part as XML whatever
    its content type, and its relationship's (an image's, a font's): so is
    it checked, when its bytes start as XML does. One that does not start
    so is no XML to any reader; one lxml cannot read is passed over only
    if none of its start tags declares a namespace: binary data, not a
    document a reader may read further than lxml (UTF-16 with no mark).
    """
    assert verdict(OfficeCheck("docx"), data) == expected


DEEP: Final = "<w:sdt>" * 300 + blip('r:link="rId1"') + "</w:sdt>" * 300
"""A picture linked to a hyperlink's relationship, 300 elements deep: lxml stops
at 256, and LibreOffice reads on and fetches it."""


@pytest.mark.parametrize("kind", ["header", "image"])
def test_a_part_too_deep_for_lxml_is_refused(kind: str) -> None:
    """Once an element of a namespace opened, a part lxml stops reading is refused.

    A reader may read further than lxml: a header 300 elements deep, named
    ``header2.png`` and typed a header or an image, passed, and a
    LibreOffice conversion fetched its picture 14 times.
    """
    data = headed(
        "header2.png",
        header(DEEP),
        (f"{R}hyperlink", "https://a.example/x.png", "External"),
        kind=kind,
    )

    assert verdict(OfficeCheck("docx"), data) == COMPLEX


@pytest.mark.parametrize(
    "broken",
    [
        pytest.param(header("<w:p>a & b</w:p>").encode(), id="a-stray-ampersand"),
        pytest.param(header("<w:p>&nope;</w:p>").encode(), id="an-entity-not-defined"),
        pytest.param(
            header('<w:p w:a="1" w:a="2"/>').encode(), id="an-attribute-twice"
        ),
        pytest.param(header("<w:p></w:r>").encode(), id="a-tag-that-does-not-match"),
        pytest.param(
            header("<w:p>QQ</w:p>").encode().replace(b"QQ", b"\xff\xfe"),
            id="bytes-not-utf-8",
        ),
        pytest.param(header("<w:p>")[:-12].encode(), id="cut-short"),
    ],
)
def test_a_part_broken_after_an_element_of_a_namespace_is_refused(
    broken: bytes,
) -> None:
    """Whatever stopped lxml, once a document had begun: refused, not passed over."""
    assert verdict(OfficeCheck("docx"), headed("header2.png", broken)) == COMPLEX


def test_a_binary_part_that_starts_as_xml_is_not_refused() -> None:
    """An image or a font whose first byte is ``<``: parsed, not XML, passed over.

    An embedded font's first bytes are scrambled: one in a few hundred
    starts as XML does.
    """
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
    blob = b"<" + bytes(range(256)) * 4
    font_like = office(
        main=document(),
        main_relationships=[(f"{R}font", "fonts/f.odttf")],
        parts={"word/fonts/f.odttf": blob},
    )
    assert verdict(OfficeCheck("docx"), font_like) == "ok"
