"""``overlay_pdf`` and ``page_sizes``: overlays land where they were drawn, as shown.

The PDFs are built here with pypdf: pages of a size, a media box away
from the origin, a crop box inside it, a rotation, and a line of text
(``MARK``) where an overlay drew it. Where text lands is read back with
pypdf after its ``transfer_rotation_to_content``, which turns the page's
rotation into its content: an independent account of how the page is
shown. So a rotated page that shifted the overlay would fail here.
"""

import base64
import io
import struct
import tracemalloc
import zlib
from typing import Any, Final, TypedDict

import pypdf
import pytest
from pypdf import PdfReader, PdfWriter
from pypdf.generic import (
    ArrayObject,
    DecodedStreamObject,
    DictionaryObject,
    EncodedStreamObject,
    NameObject,
    NullObject,
    NumberObject,
    RectangleObject,
)

from domainkit.documents import DocumentError, overlay_pdf, page_sizes
from domainkit.documents import pdf as pdf_module
from domainkit.documents.pdf import Tally, bounded_reader

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
    stored: bytes
    """The content as stored, decoded by ``filters``, in place of ``content``."""
    filters: tuple[str, ...]
    parameters: Any
    """The content's ``/DecodeParms``."""


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
        if "filters" in spec:
            kept = EncodedStreamObject()
            kept._data = spec["stored"]
            kept[NameObject("/Filter")] = ArrayObject(map(NameObject, spec["filters"]))
        if "parameters" in spec:
            kept[NameObject("/DecodeParms")] = spec["parameters"]
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


def streamed(
    *sizes: int,
    extras: tuple[tuple[bytes, bytes], ...] = (),
    stray: bool = False,
    entries: int = 0,
    padding: int = 0,
    nested: bool = False,
    used: bool = False,
    xref_filter: bytes = b"/FlateDecode",
) -> bytes:
    """A PDF of one page whose object streams decode to ``sizes`` bytes each.

    ``extras`` adds object streams of these filters and stored bytes;
    ``stray`` an entry whose object stream is the catalog, no stream.
    Nothing refers to the objects the streams hold, unless ``used``: the
    page then refers to the last. Its cross-reference stream lists
    ``entries`` at least, decodes to ``padding`` more bytes than they
    take, and says it is decoded with ``xref_filter``. ``nested`` says the
    first object stream is held in the last.
    """
    last = 99 + len(sizes) + len(extras) + stray
    resources = f" /Used {last} 0 R" if used else ""
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 300 400]{resources} >>".encode(),
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
    for filters, stored in extras:
        objects.append(
            b"<< /Type /ObjStm /N 1 /First 6 /Filter "
            + filters
            + f" /Length {len(stored)} >>\nstream\n".encode()
            + stored
            + b"\nendstream"
        )
        held.append(len(objects))
    if stray:
        held.append(1)
    out = bytearray(b"%PDF-1.7\n")
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
    rows += [struct.pack(">BIH", 0, 0, 0)] * (entries - len(rows))
    if nested:
        rows[held[0]] = struct.pack(">BIH", 2, held[-1], 0)
    data = zlib.compress(b"".join(rows) + bytes(padding))
    start = len(out)
    out += (
        f"{table} 0 obj\n<< /Type /XRef /Size {len(rows)} /W [1 4 2] /Root 1 0 R "
        f"/Filter {xref_filter.decode()} /Length {len(data)} >>\nstream\n"
    ).encode()
    out += data + b"\nendstream\nendobj\n"
    return bytes(out + f"startxref\n{start}\n%%EOF\n".encode())


def chained(
    sections: int,
    *,
    streams: bool = False,
    hybrid: bool = False,
    link: str = "/Prev {at}",
) -> bytes:
    """A one-page PDF saved ``sections`` times, each save a section linked by ``/Prev``.

    Tables, or ``streams``; ``hybrid`` gives the last table a stream of its
    own by ``/XRefStm``, as a hybrid file does: one section more. ``link``
    writes the link to the section before, as PDF lets it be written.
    """
    out = bytearray(b"%PDF-1.7\n")
    offsets = []
    for number, body in enumerate(
        (
            b"<< /Type /Catalog /Pages 2 0 R >>",
            b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 300 400] >>",
        ),
        1,
    ):
        offsets.append(len(out))
        out += f"{number} 0 obj\n".encode() + body + b"\nendobj\n"

    def stream(number: int, rows: list[tuple[int, int]], links: str) -> int:
        """An object ``number``: a cross-reference stream of ``rows``; its offset."""
        at = len(out)
        data = b"".join(struct.pack(">BIH", 1, offset, 0) for _, offset in rows)
        index = " ".join(f"{each} 1" for each, _ in rows)
        out.extend(
            f"{number} 0 obj\n<< /Type /XRef /Size {number + 1} /Index [{index}] "
            f"/W [1 4 2] /Root 1 0 R{links} /Length {len(data)} >>\nstream\n".encode()
            + data
            + b"\nendstream\nendobj\n"
        )
        return at

    previous = ""
    for save in range(sections):
        number = 4 + save
        if streams:
            first = list(enumerate(offsets, 1)) if save == 0 else []
            at = stream(number, [*first, (number, len(out))], previous)
        else:
            extra = ""
            if hybrid and save == sections - 1:
                extra = f" /XRefStm {stream(number, [(number, len(out))], '')}"
            at = len(out)
            rows = "".join(f"{offset:010d} 00000 n \n" for offset in offsets)
            table = f"0 4\n0000000000 65535 f \n{rows}" if save == 0 else "0 0\n"
            trailer = f"/Size {number + 1} /Root 1 0 R{previous}{extra}"
            out += f"xref\n{table}trailer\n<< {trailer} >>\n".encode()
        previous = " " + link.format(at=at)
    return bytes(out + f"startxref\n{at}\n%%EOF\n".encode())


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
        "The document decodes to more than 1,048,576 bytes (max_decoded), or "
        "past pypdf's limit on a stream."
    )
    with pytest.raises(DocumentError, match="decode to more than 1,048,576"):
        overlay_pdf(document, overlay_for(streamed()), max_decoded=MiB)


def test_an_object_stream_that_cannot_be_decoded_is_refused_though_unused() -> None:
    """Skipped, it could cost what it decodes before it fails, again and again.

    An entry whose object stream is no stream decodes nothing, and is skipped.
    """
    assert page_sizes(streamed(100, stray=True), max_decoded=1000) == [(300.0, 400.0)]
    with pytest.raises(DocumentError) as refused:
        page_sizes(streamed(extras=((b"/ASCIIHexDecode", b"zz>"),)))
    with pytest.raises(DocumentError, match="decoded with /Bogus"):
        overlay_pdf(streamed(extras=((b"/Bogus", b"100 0 null"),)), pdf(Page()))

    assert str(refused.value) == "A stream cannot be decoded (PdfStreamError)."


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


# What is decoded, and how ------------------------------------------------------------


def test_ascii85_after_deflate_is_refused_before_it_is_decoded() -> None:
    """A test PDF of 31 KB: deflated ``z``s, each four bytes of ASCII85, took GBs.

    pypdf bounds what deflate decodes to, and decodes ASCII85 in Python,
    at 94 MiB of memory a MiB of ``z``: the filters are read first.
    """
    stored = zlib.compress(b"z" * MiB + b"~>")
    document = pdf(Page(stored=stored, filters=("/FlateDecode", "/ASCII85Decode")))
    assert len(document) < 8 * KiB

    tracemalloc.start()
    try:
        with pytest.raises(DocumentError) as refused:
            overlay_pdf(document, overlay_for(document))
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()

    assert str(refused.value) == (
        "A stream is decoded with /ASCII85Decode after /FlateDecode: "
        "/ASCII85Decode decodes a stream as it is stored, first, or not at all."
    )
    assert peak < 4 * MiB
    with pytest.raises(DocumentError, match="ASCII85Decode after /FlateDecode"):
        page_sizes(
            streamed(extras=((b"[/FlateDecode /ASCII85Decode]", stored),)),
            max_decoded=MiB,
        )


@pytest.mark.parametrize(
    ("filters", "message"),
    [
        (("/FlateDecode", "/ASCIIHexDecode"), "/ASCIIHexDecode after /FlateDecode"),
        (("/Fl", "/RL"), "/RunLengthDecode after /Fl"),
        (("/FlateDecode", "/LZWDecode"), "/LZWDecode after /FlateDecode"),
        (("/A85", "/AHx"), "/ASCIIHexDecode after /A85"),
        (("/DCTDecode",), "decoded with /DCTDecode: a page's content"),
        (("/JBIG2Decode",), "decoded with /JBIG2Decode"),
        (("/Crypt",), "decoded with /Crypt"),
    ],
)
def test_filters_that_could_pass_the_budget_are_refused_unread(
    filters: tuple[str, ...], message: str
) -> None:
    """A filter run in Python on what another inflated, or an image's: never decoded.

    The stored bytes would not decode: they are never tried.
    """
    document = pdf(Page(stored=b"not decoded", filters=filters))

    with pytest.raises(DocumentError, match=message):
        overlay_pdf(document, overlay_for(document))


def test_streams_decoded_in_python_store_a_bounded_amount_in_all() -> None:
    """``max_decoded / 32`` bytes stored, in all the call decodes: 1 MiB by default.

    pypdf decodes ASCII85 in Python, at 0.15 s and up to 94 MiB of memory a
    MiB stored. As a generator writes it (ASCII85 over deflate), a page is
    laid over; two pages of 600 KiB are refused, though each alone fits.
    """
    raw = Page(stored=b"z" * (600 * KiB) + b"~>", filters=("/ASCII85Decode",))
    armoured = base64.a85encode(zlib.compress(text((10, 10)))) + b"~>"
    document = pdf(Page(stored=armoured, filters=("/ASCII85Decode", "/FlateDecode")))
    two = pdf(raw, raw)

    with pytest.raises(DocumentError) as refused:
        overlay_pdf(two, overlay_for(two))

    assert str(refused.value) == (
        "The streams pypdf decodes in Python (LZW, run-length, ASCIIHex, "
        "ASCII85) store more than 1,048,576 bytes in all (max_decoded / 32)."
    )
    assert len(overlay_pdf(two, {0: overlay_for(pdf(raw))})) > 0
    assert len(overlay_pdf(two, overlay_for(two), max_decoded=64 * MiB)) > 0
    assert shown(overlay_pdf(document, overlay_for(document))) == [
        (10.0, 10.0),
        (30.0, 40.0),
    ]


def test_many_small_streams_decoded_in_python_are_refused_fast(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 32 MB file of 64 object streams of ASCII85, each half a MiB stored.

    Each fits; together they took 5 seconds a call, LZW 17. Refused once
    they pass 1 MiB in all: two are decoded, the rest never. pypdf's
    decoder counts them, not a stopwatch (TEST-01).
    """
    one = base64.a85encode(bytes(range(256)) * 1600) + b"~>"
    document = streamed(extras=((b"/ASCII85Decode", one),) * 64)
    assert len(document) > 30 * MiB
    decoded: list[int] = []
    decode = pypdf.filters.ASCII85Decode.decode

    def counted(data: bytes, *args: object, **kwargs: object) -> bytes:
        decoded.append(len(data))
        return decode(data, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(pypdf.filters.ASCII85Decode, "decode", staticmethod(counted))
    with pytest.raises(DocumentError, match="store more than 1,048,576 bytes in all"):
        page_sizes(document)

    assert decoded == [len(one)] * 2


@pytest.mark.parametrize(
    "parameters",
    [
        DictionaryObject(
            {
                NameObject("/Predictor"): NumberObject(12),
                NameObject("/Columns"): NumberObject(4),
            }
        ),
        ArrayObject(
            [
                NullObject(),
                DictionaryObject({NameObject("/Predictor"): NumberObject(2)}),
            ]
        ),
    ],
)
def test_a_predictor_is_refused_in_a_pages_content(parameters: Any) -> None:  # noqa: ANN401 - pypdf's.
    """A predictor runs in Python, at up to 8 seconds a megabyte."""
    stored = zlib.compress(text((10, 10)))
    document = pdf(
        Page(stored=stored, filters=("/FlateDecode",), parameters=parameters)
    )
    plain = DictionaryObject({NameObject("/Predictor"): NumberObject(1)})
    unpredicted = pdf(Page(stored=stored, filters=("/FlateDecode",), parameters=plain))

    with pytest.raises(DocumentError, match="decoded with a predictor"):
        overlay_pdf(document, overlay_for(document))
    assert shown(overlay_pdf(unpredicted, overlay_for(unpredicted)), 0, "MARK") == [
        (10.0, 10.0),
        (30.0, 40.0),
    ]


def test_cross_reference_streams_list_a_bounded_number_of_entries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """65,536 entries by default, refused before pypdf reads one: each costs 13 µs.

    One for each 512 bytes of ``max_decoded`` where that is more; and each
    stream decodes to 8 bytes an entry at most.
    """
    with pytest.raises(DocumentError) as refused:
        page_sizes(streamed(entries=65_537))
    monkeypatch.setattr(pdf_module, "XREF_ENTRIES", 1000)
    larger = streamed(entries=1500)
    with pytest.raises(DocumentError, match="past a limit it is read within"):
        overlay_pdf(larger, pdf(Page(width=300, height=400)), max_decoded=512_000)
    with pytest.raises(DocumentError, match="Limit reached while decompressing"):
        page_sizes(streamed(padding=16_000), max_decoded=512_000)

    assert str(refused.value) == (
        "The document is past a limit it is read within: Maximum XRef stream "
        "entry limit reached: 65537 > 65536."
    )
    assert page_sizes(larger, max_decoded=1500 * 512) == [(300.0, 400.0)]
    assert page_sizes(streamed(padding=6_000), max_decoded=512_000) == [(300.0, 400.0)]


@pytest.mark.parametrize(
    "link",
    [
        "/Prev {at}",
        "/P#72ev {at}",
        "/Prev%c\n{at}",
        "/Note (stream startxref) /Prev {at}",
    ],
)
def test_a_chain_of_cross_reference_sections_is_bounded(
    monkeypatch: pytest.MonkeyPatch, link: str
) -> None:
    """32 sections, as a file and the saves added to it make, and not one more.

    pypdf follows every ``/Prev`` and ``/XRefStm``: 500 sections of 1 MiB
    took it 0.8 s and 540 MiB, with a predictor 47 s. They are counted
    where pypdf reads them, so a name escaped, a comment, or a string that
    looks like a keyword hides none. Of the 500, the reader reads the 33rd,
    which refuses, and no more: the tally counts them, not a stopwatch
    (TEST-01).
    """
    assert page_sizes(chained(32, link=link)) == [(300.0, 400.0)]
    assert page_sizes(chained(32, streams=True, link=link)) == [(300.0, 400.0)]
    assert page_sizes(chained(31, hybrid=True, link=link)) == [(300.0, 400.0)]
    for document in (
        chained(33, link=link),
        chained(33, streams=True, link=link),
        chained(32, hybrid=True, link=link),
    ):
        with pytest.raises(DocumentError) as refused:
            overlay_pdf(document, pdf(Page(width=300, height=400)))
        assert str(refused.value) == (
            "The document chains more than 32 cross-reference sections: one for "
            "the file, and one for each save added to it."
        )

    long = chained(500, streams=True, link=link)
    tallies: list[Tally] = []

    def kept(**options: Any) -> Tally:  # noqa: ANN401 - Tally's fields.
        tallies.append(Tally(**options))
        return tallies[-1]

    monkeypatch.setattr(pdf_module, "Tally", kept)
    tracemalloc.start()
    try:
        with pytest.raises(DocumentError, match="more than 32 cross-reference"):
            page_sizes(long)
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    assert [tally.read for tally in tallies] == [33]
    assert peak < 4 * MiB


def test_the_sections_are_counted_where_pypdf_reads_them() -> None:
    """Each of pypdf's own readers runs, counted: a table, a stream, a hybrid's.

    They are pypdf's private methods: renamed in an update, the count
    would stay at nothing, and this fails.
    """
    for document, sections in (
        (chained(3), 3),
        (chained(3, streams=True), 3),
        (chained(2, hybrid=True), 3),
    ):
        tally = Tally()
        bounded_reader(pypdf)(document, tally=tally)
        assert (tally.read, tally.rebuilt) == (sections, False)


@pytest.mark.parametrize(
    "filters", [b"[/FlateDecode /ASCII85Decode]", b"[/LZWDecode /FlateDecode]"]
)
def test_a_file_whose_table_would_be_rebuilt_is_refused(filters: bytes) -> None:
    """A damaged file's table is rebuilt by pypdf, which decodes every object stream.

    With whatever filters they have, unchecked: 1000 of ASCII85 took it 65
    seconds, then the file was accepted. None is decoded here: the object
    streams' bytes would not decode at all.
    """
    document = streamed(extras=((filters, b"not decoded"),) * 3)
    damaged = document[: document.rindex(b"startxref")] + b"startxref\n0\n%%EOF\n"
    tally = Tally()

    with pytest.raises(DocumentError) as refused:
        page_sizes(damaged)
    bounded_reader(pypdf)(damaged, tally=tally)

    assert str(refused.value) == (
        "The document is damaged: its cross-reference table would be rebuilt, "
        "which decodes every object stream unchecked."
    )
    assert tally.rebuilt


def clear_codes(size: int) -> bytes:
    """LZW of ``size`` bytes clearing its table at every code: it decodes to nothing."""
    codes = size * 8 // 9 - 1
    bits = "100000000" * codes + "100000001"  # Clear (256), then the end (257).
    bits += "0" * (-len(bits) % 8)
    return int(bits, 2).to_bytes(len(bits) // 8, "big")


def test_lzw_has_a_share_of_its_own() -> None:
    """64 KiB stored in all by default: clear codes cost pypdf 11 s a MiB."""
    document = pdf(Page(stored=clear_codes(70 * KiB), filters=("/LZWDecode",)))
    small = pdf(Page(stored=clear_codes(60 * KiB), filters=("/LZWDecode",)))

    with pytest.raises(DocumentError) as refused:
        overlay_pdf(document, overlay_for(document))

    assert str(refused.value) == (
        "The streams pypdf decodes with LZW store more than 65,536 bytes in all "
        "(max_decoded / 512)."
    )
    assert len(overlay_pdf(small, overlay_for(small))) > 0


def test_an_object_stream_held_in_another_is_refused_before_either_is_decoded() -> None:
    """PDF allows none: pypdf decoded the other unchecked, 31 KB to 2.9 GB.

    No object stream is decoded but by the budget: an object held in one
    it did not decode (here the catalog, no stream at all) is refused.
    """
    bomb = (b"[/FlateDecode /ASCII85Decode]", zlib.compress(b"z" * MiB + b"~>"))
    nested = streamed(10, extras=(bomb,), nested=True)
    stray = streamed(stray=True, used=True)

    tracemalloc.start()
    try:
        with pytest.raises(DocumentError) as refused:
            page_sizes(nested)
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    with pytest.raises(DocumentError) as unchecked:
        overlay_pdf(stray, pdf(Page(width=300, height=400)))

    assert str(refused.value) == (
        "An object stream is held in another object stream, which PDF does not "
        "allow: pypdf would decode the other unchecked."
    )
    assert peak < 4 * MiB
    assert str(unchecked.value) == (
        "The document holds an object in an object stream that was not decoded "
        "within the bounds."
    )
    used = streamed(10, used=True)
    assert len(overlay_pdf(used, pdf(Page(width=300, height=400)))) > 0


def test_a_cross_reference_stream_is_decoded_with_deflate_only() -> None:
    """LZW's output is bounded by pypdf, not the time of its clear codes: 11 s a MiB.

    The filters are read as pypdf reads the stream, before it decodes it.
    """
    with pytest.raises(DocumentError) as refused:
        page_sizes(streamed(xref_filter=b"/LZWDecode"))

    assert str(refused.value) == (
        "The document has a cross-reference stream decoded otherwise than with "
        "FlateDecode, which pypdf could not bound."
    )
    assert page_sizes(streamed(xref_filter=b"[/FlateDecode]")) == [(300.0, 400.0)]


def referring(missing: int) -> bytes:
    """A one-page PDF whose page refers to ``missing`` objects its table lacks."""
    names = " ".join(f"/P{each} {1000 + each} 0 R" for each in range(missing))
    objects = (
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 300 400] "
        f"/Resources << /Properties << {names} >> >> >>".encode(),
    )
    out = bytearray(b"%PDF-1.7\n")
    offsets = []
    for number, body in enumerate(objects, 1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n".encode() + body + b"\nendobj\n"
    start = len(out)
    rows = "".join(f"{offset:010d} 00000 n \n" for offset in offsets)
    out += f"xref\n0 4\n0000000000 65535 f \n{rows}".encode()
    out += f"trailer\n<< /Size 4 /Root 1 0 R >>\nstartxref\n{start}\n%%EOF\n".encode()
    return bytes(out)


def test_searches_for_objects_the_table_lacks_are_bounded() -> None:
    """The whole file is searched by pypdf for each: 20,000 in 5 MB took 406 s.

    32 at most, counted as pypdf copies the file to search it.
    """
    overlay = pdf(Page(width=300, height=400))
    tally = Tally()

    with pytest.raises(DocumentError) as refused:
        overlay_pdf(referring(40), overlay)
    reader = bounded_reader(pypdf)(referring(40), tally=tally)
    for each in range(40):
        try:
            reader.get_object(1000 + each)
        except pypdf.errors.LimitReachedError:
            break

    assert str(refused.value) == (
        "The document names more than 32 objects its cross-reference table "
        "lacks or misplaces: pypdf searches the whole file for each."
    )
    assert tally.searched == 33
    assert len(overlay_pdf(referring(30), overlay)) > 0
