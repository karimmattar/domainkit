"""``overlay_pdf`` and ``page_sizes``: overlays land where they were drawn, as shown.

The PDFs are built here with pypdf: pages of a size, a media box away
from the origin, a crop box inside it, a rotation, and a line of text
(``MARK``) where an overlay drew it. Where text lands is read back with
pypdf after its ``transfer_rotation_to_content``, which turns the page's
rotation into its content: an independent account of how the page is
shown. So a rotated page that shifted the overlay would fail here.
"""

import io
from typing import Any, Final, TypedDict

import pytest
from pypdf import PdfReader, PdfWriter
from pypdf.generic import (
    DecodedStreamObject,
    DictionaryObject,
    NameObject,
    NumberObject,
    RectangleObject,
)

from domainkit.documents import DocumentError, overlay_pdf, page_sizes

ROTATIONS: Final = (0, 90, 180, 270, -90, 450)


class Page(TypedDict, total=False):
    """One page to build: its size and boxes, its rotation, its content."""

    width: float
    height: float
    media: tuple[float, float, float, float]
    crop: tuple[float, float, float, float]
    rotate: int
    content: bytes


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
        page[NameObject("/Contents")] = writer._add_object(stream)
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
