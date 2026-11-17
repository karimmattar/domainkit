"""What a checked document draws and fetches: page content, fields, VML (U5, U6).

``PdfCheck`` read every object of a PDF but decoded no page's content: a
170 KB file of twenty pages, each 8 MiB of spaces deflated, passed, and
laying text over it made 168 MB in the web worker. It now decodes each
page's content in the check's process, counting, within ``max_content``.
``OfficeCheck`` read a field's instruction once the field separated or
ended, and only from ``w:instrText``; and it took a VML picture's source
from ``r:id`` alone (ADR 0175).
"""

import gzip
import io
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
from domainkit_tests.support.documents import R, document, office, pdf

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
        ([0], [stream(b"20" * 1000 + b">", "/ASCIIHexDecode")], 1000),
        ([0], [stream(zlib.compress(spaces(1000)), "[/FlateDecode /Fl]")], 1000),
        ([0], [stream(spaces(1000), "[/FlateDecode]")], 1000),
        ([0], [stream(spaces(1000) + b"junk after the end")], 1000),
        ([0], [stream(gzip.compress(b" " * 1000, mtime=0))], 1000),
        (["null", "[null 1 0 R]", "/Name"], [], 0),
    ],
)
def test_each_pages_content_counts_each_time_it_is_drawn(
    pages: list[Sequence[int] | int | str], streams: list[str], counted: int
) -> None:
    """A stream drawn twice counts twice; any filter; what is not content is not.

    Deflated twice, or after its end; gzip, which pypdf recovers; hex,
    which pypdf decodes; none at all.
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
