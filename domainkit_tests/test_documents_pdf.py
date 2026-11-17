"""``overlay_pdf`` and ``page_sizes``: overlays land where they were drawn, as shown.

The PDFs are built here with pypdf: pages of a size, a media box away
from the origin, a crop box inside it, a rotation, and a line of text
(``MARK``) where an overlay drew it. Where text lands is read back with
pypdf after its ``transfer_rotation_to_content``, which turns the page's
rotation into its content: an independent account of how the page is
shown. So a rotated page that shifted the overlay would fail here.
"""

import io
import struct
import zlib
from typing import Any, Final, TypedDict

import pypdf
import pytest
from pypdf import PdfReader, PdfWriter
from pypdf.generic import (
    ArrayObject,
    DecodedStreamObject,
    DictionaryObject,
    NameObject,
    NullObject,
    NumberObject,
    RectangleObject,
)

from domainkit.documents import DocumentError, overlay_pdf, page_sizes

ROTATIONS: Final = (0, 90, 180, 270, -90, 450)
KiB: Final = 1024
MiB: Final = 1024 * KiB


class Page(TypedDict, total=False):
    """One page to build: its size and boxes, its rotation, its content."""

    width: float
    height: float
    media: tuple[float, float, float, float]
    crop: tuple[float, float, float, float]
    rotate: int
    content: bytes
    deflated: bool
    """The content deflated, as most PDFs keep it: it decodes when read."""


def pdf(*pages: Page, password: str | None = None) -> bytes:
    """A PDF of ``pages``, its text in Helvetica."""
    writer = PdfWriter()
    font = writer._add_object(
        DictionaryObject(
            {
                NameObject("/Type"): NameObject("/Font"),
                NameObject("/Subtype"): NameObject("/Type1"),
                NameObject("/BaseFont"): NameObject("/Helvetica"),
            }
        )
    )
    for spec in pages:
        page = writer.add_blank_page(spec.get("width", 612), spec.get("height", 792))
        if "media" in spec:
            page.mediabox = RectangleObject(spec["media"])
        if "crop" in spec:
            page.cropbox = RectangleObject(spec["crop"])
        if "rotate" in spec:
            page[NameObject("/Rotate")] = NumberObject(spec["rotate"])
        page[NameObject("/Resources")] = DictionaryObject(
            {NameObject("/Font"): DictionaryObject({NameObject("/F1"): font})}
        )
        stream = DecodedStreamObject()
        stream.set_data(spec.get("content", b""))
        kept = stream.flate_encode(level=9) if spec.get("deflated") else stream
        page[NameObject("/Contents")] = writer._add_object(kept)
    if password is not None:
        writer.encrypt(password, algorithm="AES-128")
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


def text(at: tuple[float, float], words: str = "MARK") -> bytes:
    """A content stream that writes ``words`` with its baseline starting ``at``."""
    return f"BT /F1 12 Tf {at[0]} {at[1]} Td ({words}) Tj ET".encode()


def shown(
    document: bytes, index: int = 0, words: str = "MARK"
) -> list[tuple[float, float]]:
    """Where ``words`` start on page ``index`` as it is shown, from its corner."""
    page = PdfWriter(clone_from=PdfReader(io.BytesIO(document))).pages[index]
    page.transfer_rotation_to_content()
    crop, media = [float(v) for v in page.cropbox], [float(v) for v in page.mediabox]
    left = max(min(crop[0], crop[2]), min(media[0], media[2]))
    bottom = max(min(crop[1], crop[3]), min(media[1], media[3]))
    found = []

    def visit(found_text: str, cm: Any, tm: Any, *_: Any) -> None:  # noqa: ANN401 - pypdf's.
        if words in found_text:
            x = tm[4] * cm[0] + tm[5] * cm[2] + cm[4]
            y = tm[4] * cm[1] + tm[5] * cm[3] + cm[5]
            found.append((round(x - left, 3), round(y - bottom, 3)))

    page.extract_text(visitor_text=visit)
    return found


def inflating(size: int, at: tuple[float, float] = (10, 10)) -> bytes:
    """Content that writes ``MARK`` ``at``, then ``size`` bytes of spaces."""
    return text(at) + b" " * size


def streamed(*sizes: int, broken: bool = False, stray: bool = False) -> bytes:
    """A PDF of one page whose object streams decode to ``sizes`` bytes each.

    ``broken`` adds an object stream of a filter pypdf does not know;
    ``stray`` an entry whose object stream is the catalog, no stream.
    Nothing refers to the objects the streams hold.
    """
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 300 400] >>",
    ]
    held: list[int] = []  # The stream of each object held, in order.
    for size in sizes:
        number = 100 + len(held)
        header = f"{number} 0 ".encode()
        data = zlib.compress(header + b" " * size + b"null")
        objects.append(
            f"<< /Type /ObjStm /N 1 /First {len(header)} /Filter /FlateDecode "
            f"/Length {len(data)} >>\nstream\n".encode()
            + data
            + b"\nendstream"
        )
        held.append(len(objects))
    if broken:
        objects.append(
            b"<< /Type /ObjStm /N 1 /First 6 /Filter /Bogus /Length 10 >>\n"
            b"stream\n100 0 null\nendstream"
        )
        held.append(len(objects))
    if stray:
        held.append(1)
    out = b"%PDF-1.7\n"
    offsets = []
    for number, body in enumerate(objects, 1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n".encode() + body + b"\nendobj\n"
    table = len(objects) + 1
    rows = [struct.pack(">BIH", 0, 0, 65535)]
    rows += [struct.pack(">BIH", 1, offset, 0) for offset in offsets]
    rows.append(struct.pack(">BIH", 1, len(out), 0))
    rows += [struct.pack(">BIH", 0, 0, 0)] * (100 - len(rows))
    rows += [struct.pack(">BIH", 2, stream, 0) for stream in held]
    data = zlib.compress(b"".join(rows))
    start = len(out)
    out += (
        f"{table} 0 obj\n<< /Type /XRef /Size {len(rows)} /W [1 4 2] /Root 1 0 R "
        f"/Filter /FlateDecode /Length {len(data)} >>\nstream\n"
    ).encode()
    out += data + b"\nendstream\nendobj\n"
    return out + f"startxref\n{start}\n%%EOF\n".encode()


def overlay_for(document: bytes, at: tuple[float, float] = (30, 40)) -> bytes:
    """An overlay of each page of ``document``, as shown, with ``MARK`` ``at``."""
    return pdf(
        *(
            Page(width=width, height=height, content=text(at))
            for width, height in page_sizes(document)
        )
    )


# Where the overlay lands --------------------------------------------------------------


@pytest.mark.parametrize("rotation", ROTATIONS)
def test_a_rotated_cropped_page_does_not_shift_the_overlay(rotation: int) -> None:
    """The mark lands where the overlay drew it, on the page as shown.

    The page's own content leaves a transformation behind it (``cm``
    without ``q``); the overlay is drawn in a state of its own all the same.
    """
    document = pdf(
        Page(
            media=(0, 0, 612, 792),
            crop=(50, 60, 400, 500),
            rotate=rotation,
            content=b"2 0 0 2 100 100 cm " + text((10, 10), "BASE"),
        )
    )

    laid = overlay_pdf(document, overlay_for(document))

    assert shown(laid) == [(30.0, 40.0)]
    assert shown(laid, words="BASE") == shown(document, words="BASE")


@pytest.mark.parametrize("rotation", [90, 270])
def test_a_plain_merge_shifts_the_overlay_of_a_rotated_page(rotation: int) -> None:
    """Why the placement is computed: pypdf's own merge ignores the rotation."""
    document = pdf(Page(media=(0, 0, 612, 792), rotate=rotation))
    reader = PdfReader(io.BytesIO(document))
    writer = PdfWriter(clone_from=reader)
    writer.pages[0].merge_page(PdfReader(io.BytesIO(overlay_for(document))).pages[0])
    out = io.BytesIO()
    writer.write(out)

    assert shown(out.getvalue()) != [(30.0, 40.0)]


def test_a_media_box_away_from_the_origin_is_followed() -> None:
    """Boxes may start anywhere, and name their corners in any order."""
    document = pdf(
        Page(media=(612, 892, 100, 100), crop=(700, 800, 150, 150), rotate=90)
    )

    assert page_sizes(document) == [(650.0, 462.0)]
    assert shown(overlay_pdf(document, overlay_for(document, (5, 600)))) == [
        (5.0, 600.0)
    ]


def test_a_crop_box_outside_the_media_box_is_cut_to_it() -> None:
    """What is shown is where the two boxes meet."""
    document = pdf(Page(media=(0, 0, 300, 400), crop=(-50, 100, 350, 500)))

    assert page_sizes(document) == [(300.0, 300.0)]
    assert shown(overlay_pdf(document, overlay_for(document, (1, 2)))) == [(1.0, 2.0)]


def test_a_slightly_smaller_overlay_meets_the_pages_top_edge() -> None:
    """Browsers round a page's size: the overlay's top-left corner is the page's."""
    document = pdf(Page(width=595.28, height=841.89))
    overlay = pdf(Page(width=595.5, height=840.5, content=text((10, 820.5))))

    assert shown(overlay_pdf(document, overlay)) == [(10.0, 821.89)]


def test_a_rotated_overlay_is_read_as_it_is_shown() -> None:
    """An overlay page with a rotation of its own is laid as a viewer shows it."""
    document = pdf(Page(width=300, height=500))
    overlay = pdf(Page(width=500, height=300, rotate=90, content=text((100, 30))))

    # Shown turned, the overlay's (100, 30) is 30 from the left, 100 from the top.
    assert shown(overlay_pdf(document, overlay)) == [(30.0, 400.0)]


def test_overlays_by_page_go_on_their_pages_only() -> None:
    """A mapping of page indexes: the other pages stay as they were."""
    document = pdf(Page(), Page(rotate=90), Page())
    sizes = page_sizes(document)
    second = pdf(Page(width=sizes[1][0], height=sizes[1][1], content=text((7, 8))))

    laid = overlay_pdf(document, {1: second})

    assert [shown(laid, index) for index in range(3)] == [[], [(7.0, 8.0)], []]
    assert len(PdfReader(io.BytesIO(laid)).pages) == 3


def test_page_sizes_are_shown_sizes() -> None:
    """Width and height in points, turned by the rotation."""
    document = pdf(
        Page(width=595, height=842),
        Page(width=595, height=842, rotate=90),
        Page(width=595, height=842, rotate=-270),
        Page(width=595, height=842, crop=(0, 0, 200, 100), rotate=180),
    )

    assert page_sizes(document) == [(595, 842), (842, 595), (842, 595), (200, 100)]


# What is refused ----------------------------------------------------------------------


def test_an_overlay_of_another_size_is_refused() -> None:
    """Made for the page unrotated, an overlay would land beside the text."""
    document = pdf(Page(width=595, height=842, rotate=90))
    portrait = pdf(Page(width=595, height=842))

    with pytest.raises(DocumentError) as refused:
        overlay_pdf(document, portrait)

    assert str(refused.value) == (
        "Page index 0 is shown 842 by 595 points, and its overlay is 595 by 842: "
        "make the overlay the size of the page as shown (page_sizes)."
    )


def test_the_pages_and_the_overlays_must_match() -> None:
    """One overlay page per page, or a mapping of one-page overlays."""
    document = pdf(Page(), Page())
    one = pdf(Page())

    with pytest.raises(DocumentError, match="has 1 pages and the document 2"):
        overlay_pdf(document, one)
    with pytest.raises(DocumentError, match="no page index 2"):
        overlay_pdf(document, {2: one})
    with pytest.raises(DocumentError, match="no page index -1"):
        overlay_pdf(document, {-1: one})
    with pytest.raises(DocumentError, match="page index 0 has 2 pages, not one"):
        overlay_pdf(document, {0: document})


@pytest.mark.parametrize(
    ("broken", "message"),
    [
        ("document", r"The document is not a PDF that can be read \("),
        ("overlay", r"The overlay is not a PDF that can be read \("),
        ("encrypted", "The document is encrypted."),
    ],
)
def test_an_unreadable_or_encrypted_pdf_is_refused(broken: str, message: str) -> None:
    """Nothing is laid over what cannot be read whole."""
    document = {
        "document": b"not a pdf",
        "encrypted": pdf(Page(), password="pw"),
    }.get(broken, pdf(Page()))
    overlay = b"%PDF-1.7\n" if broken == "overlay" else pdf(Page())

    with pytest.raises(DocumentError, match=message):
        overlay_pdf(document, overlay)
    if broken != "overlay":
        with pytest.raises(DocumentError, match=message):
            page_sizes(document)


def test_a_rotation_that_is_no_quarter_turn_is_refused() -> None:
    """``/Rotate`` is a multiple of 90; anything else cannot be shown."""
    document = pdf(Page(rotate=45))

    with pytest.raises(DocumentError, match="rotated 45 degrees: not a multiple of 90"):
        page_sizes(document)


def test_a_page_pypdf_cannot_merge_is_refused() -> None:
    """A broken page is a refusal, never pypdf's own error."""
    document = pdf(Page())
    reader = PdfReader(io.BytesIO(document))
    writer = PdfWriter(clone_from=reader)
    writer.pages[0][NameObject("/Resources")] = NumberObject(7)
    broken = io.BytesIO()
    writer.write(broken)

    with pytest.raises(DocumentError, match=r"could not be laid over \("):
        overlay_pdf(broken.getvalue(), overlay_for(document))


# What is decoded ----------------------------------------------------------------------


def test_a_small_pdf_that_inflates_far_is_refused_by_default() -> None:
    """The report's file: pages of deflated spaces, a few kilobytes each.

    pypdf's own limit is 75 MB a stream; each page is under it, and the
    pages together are far past ``MAX_DECODED`` (32 MiB). Nothing is laid
    over, and what is decoded stops where the budget ends.
    """
    document = pdf(*(Page(content=inflating(8 * MiB), deflated=True) for _ in range(5)))
    assert len(document) < 100 * KiB

    with pytest.raises(DocumentError) as refused:
        overlay_pdf(document, overlay_for(document))

    assert str(refused.value) == (
        "The document and its overlays decode to more than 33,554,432 bytes "
        "(max_decoded), or past pypdf's limit on a stream."
    )


def test_the_pages_laid_over_and_their_overlays_count_in_all() -> None:
    """Each fits ``max_decoded`` alone; together they do not."""
    document = pdf(
        *(Page(content=inflating(400 * KiB), deflated=True) for _ in range(3))
    )
    overlay = overlay_for(document)
    big = pdf(Page(content=inflating(700 * KiB, (30, 40))))

    laid = overlay_pdf(document, {0: pdf(Page()), 1: pdf(Page())}, max_decoded=MiB)
    with pytest.raises(DocumentError, match="decode to more than 1,048,576 bytes"):
        overlay_pdf(document, overlay, max_decoded=MiB)
    with pytest.raises(DocumentError, match="decode to more than 1,048,576 bytes"):
        overlay_pdf(document, {2: big}, max_decoded=MiB)
    assert shown(overlay_pdf(document, {2: big}, max_decoded=2 * MiB), 2) == [
        (10.0, 10.0),
        (30.0, 40.0),
    ]
    assert len(PdfReader(io.BytesIO(laid)).pages) == 3


def test_a_page_laid_over_is_written_deflated() -> None:
    """Its content is not written back as it decoded: 170 KB stays small."""
    document = pdf(Page(content=inflating(4 * MiB), deflated=True), Page())

    laid = overlay_pdf(document, overlay_for(document))

    assert len(laid) < 64 * KiB
    assert shown(laid) == [(10.0, 10.0), (30.0, 40.0)]
    assert shown(laid, 1) == [(30.0, 40.0)]


def test_a_page_of_many_streams_or_none_is_counted_as_it_is() -> None:
    """``/Contents`` may be an array, hold a null, or be left out."""
    reader = PdfReader(io.BytesIO(pdf(Page(content=text((10, 10))), Page())))
    writer = PdfWriter(clone_from=reader)
    first, second = writer.pages
    contents = ArrayObject([first.raw_get("/Contents"), NullObject()])
    first[NameObject("/Contents")] = writer._add_object(contents)
    del second[NameObject("/Contents")]
    out = io.BytesIO()
    writer.write(out)
    document = out.getvalue()

    laid = overlay_pdf(document, overlay_for(document), max_decoded=1000)

    assert [shown(laid, index) for index in range(2)] == [
        [(10.0, 10.0), (30.0, 40.0)],
        [(30.0, 40.0)],
    ]


def test_object_streams_count_in_all_too() -> None:
    """Where a PDF keeps its objects compressed: each decoded, every one."""
    document = streamed(600 * KiB, 600 * KiB)

    assert page_sizes(document, max_decoded=2 * MiB) == [(300.0, 400.0)]
    with pytest.raises(DocumentError) as refused:
        page_sizes(document, max_decoded=MiB)
    assert str(refused.value) == (
        "The document decode to more than 1,048,576 bytes (max_decoded), or "
        "past pypdf's limit on a stream."
    )
    with pytest.raises(DocumentError, match="decode to more than 1,048,576"):
        overlay_pdf(document, overlay_for(streamed()), max_decoded=MiB)


def test_an_object_stream_pypdf_cannot_decode_fails_only_where_it_is_used() -> None:
    """Unused, as these are, it is skipped, as before the budget."""
    document = streamed(100, broken=True, stray=True)

    assert page_sizes(document, max_decoded=1000) == [(300.0, 400.0)]
    assert len(overlay_pdf(document, overlay_for(document), max_decoded=1000)) > 0


def test_pypdfs_limits_are_lowered_for_the_call_only() -> None:
    """The caller's configuration is the same after; a lower limit of its own holds.

    pypdf keeps its configuration in a context variable: what a call sets,
    no other thread sees.
    """
    document = pdf(Page(content=inflating(4000), deflated=True))
    overlay = overlay_for(document)
    before = pypdf.get_configuration()

    overlay_pdf(document, overlay)
    with pytest.raises(DocumentError):
        overlay_pdf(document, overlay, max_decoded=1000)
    assert pypdf.get_configuration() == before
    with (
        pypdf.apply_configuration(zlib_maximum_output_length=1000),
        pytest.raises(DocumentError, match="past pypdf's limit on a stream"),
    ):
        overlay_pdf(document, overlay)
    with pypdf.apply_configuration(zlib_maximum_output_length=0):  # Unbounded.
        with pytest.raises(DocumentError, match="more than 1,000 bytes"):
            overlay_pdf(document, overlay, max_decoded=1000)
        assert pypdf.get_configuration().zlib_maximum_output_length == 0


@pytest.mark.parametrize("bound", [0, -1])
def test_max_decoded_is_more_than_nothing(bound: int) -> None:
    """A budget of nothing would refuse every page."""
    document = pdf(Page())

    with pytest.raises(ValueError, match=f"max_decoded is more than 0, not {bound}"):
        overlay_pdf(document, document, max_decoded=bound)
    with pytest.raises(ValueError, match="max_decoded is more than 0"):
        page_sizes(document, max_decoded=bound)
