"""Rendered pages laid over a PDF's own (``domainkit[documents]``: pypdf).

An overlay is made for the page as it is shown: its size is the page's
crop box, turned by the page's rotation (``page_sizes`` gives each), and
its origin the shown page's corner. A browser renders it, from HTML with
an ``@page`` rule of that size, so its text is shaped and laid out as
any page's (Arabic joined, right to left): pypdf draws no text, it moves
the overlay's drawing onto the page. ``overlay_pdf`` turns and moves
each overlay into its page's own coordinates, so a rotated page, or a
crop box away from the origin, shifts nothing. The page's own content is
left as it was, in its own graphics state, and the overlay drawn over it,
clipped to the overlay's box.
"""

import io
from collections.abc import Mapping
from typing import Any, Final

from domainkit.documents.base import DocumentError, missing

MISFIT: Final = 2.0
"""Points an overlay may differ from its page's shown size, either way (0.7 mm):
a browser rounds a page's size."""
type Matrix = tuple[float, float, float, float, float, float]
"""A PDF transformation ``[a b c d e f]``: x' = a x + c y + e, y' = b x + d y + f."""


def page_sizes(pdf: bytes) -> list[tuple[float, float]]:
    """Each page's width and height as it is shown, in points (1/72 inch).

    The size of its crop box (within its media box), turned by its rotation:
    a portrait page rotated 90 degrees is shown, and measured, as landscape.

    Raises:
        ImportError: ``domainkit[documents]`` is not installed.
        DocumentError: It is not a PDF pypdf reads, or it is encrypted.
    """
    reader = _reader(_pypdf(), pdf, "The document")
    return [_shown(page)[1:] for page in reader.pages]


def overlay_pdf(document: bytes, overlay: bytes | Mapping[int, bytes]) -> bytes:
    """``document`` with overlays drawn over its pages.

    ``overlay`` is a PDF whose page *i* goes over the document's page *i*,
    with as many pages; or a mapping of page indexes (0 for the first) to
    PDFs of one page each, for the pages that have one. Each overlay page
    is the size of its page as shown (``page_sizes``), within ``MISFIT``
    points, and is laid with its top-left corner on the shown page's.

    The work is CPU-bound: in async code, run it in a thread.

    Raises:
        ImportError: ``domainkit[documents]`` is not installed.
        DocumentError: A PDF cannot be read or is encrypted, the pages do
            not match, or an overlay is not the size of its page.
    """
    pypdf = _pypdf()
    reader = _reader(pypdf, document, "The document")
    count = len(reader.pages)
    if isinstance(overlay, Mapping):
        layers = {}
        for index, data in overlay.items():
            if not 0 <= index < count:
                raise DocumentError(
                    f"The document has {count} pages: there is no page index {index}."
                )
            pages = _reader(pypdf, data, f"The overlay of page index {index}").pages
            if len(pages) != 1:
                raise DocumentError(
                    f"The overlay of page index {index} has {len(pages)} pages, "
                    "not one."
                )
            layers[index] = pages[0]
    else:
        pages = _reader(pypdf, overlay, "The overlay").pages
        if len(pages) != count:
            raise DocumentError(
                f"The overlay has {len(pages)} pages and the document {count}: "
                "give one overlay page for each page, or a mapping of pages."
            )
        layers = dict(enumerate(pages))
    try:
        writer = pypdf.PdfWriter(clone_from=reader)
        for index, layer in sorted(layers.items()):
            page = writer.pages[index]
            page.merge_transformed_page(
                layer, pypdf.Transformation(_placement(page, layer, index))
            )
        out = io.BytesIO()
        writer.write(out)
    except DocumentError:
        raise
    except Exception as exc:  # noqa: BLE001 - pypdf raises many kinds on a bad page.
        raise DocumentError(
            f"The pages could not be laid over ({type(exc).__name__})."
        ) from None
    return out.getvalue()


def _pypdf() -> Any:  # noqa: ANN401 - the module.
    """pypdf, imported when first needed.

    Raises:
        ImportError: ``domainkit[documents]`` is not installed.
    """
    try:
        import pypdf
    except ImportError as exc:
        raise missing("pypdf") from exc
    return pypdf


def _reader(pypdf: Any, data: bytes, what: str) -> Any:  # noqa: ANN401 - pypdf's types.
    """A reader of ``data`` whose pages could be listed.

    Raises:
        DocumentError: pypdf cannot read it, or it is encrypted.
    """
    try:
        reader = pypdf.PdfReader(io.BytesIO(data))
        encrypted = reader.is_encrypted
        if not encrypted:
            len(reader.pages)  # Reads the page tree.
    except Exception as exc:  # noqa: BLE001 - pypdf raises many kinds on a bad file.
        raise DocumentError(
            f"{what} is not a PDF that can be read ({type(exc).__name__})."
        ) from None
    if encrypted:
        raise DocumentError(f"{what} is encrypted.")
    return reader


def _placement(page: Any, layer: Any, index: int) -> Matrix:  # noqa: ANN401 - pypdf's types.
    """The transformation that lays ``layer`` over ``page``, both as shown.

    From the overlay's coordinates to its shown ones, up so that the two
    top edges meet, then from the page's shown coordinates to its own.

    Raises:
        DocumentError: The overlay is not the size of the shown page.
    """
    to_shown, width, height = _shown(page)
    layer_shown, layer_width, layer_height = _shown(layer)
    if abs(layer_width - width) > MISFIT or abs(layer_height - height) > MISFIT:
        raise DocumentError(
            f"Page index {index} is shown {width:g} by {height:g} points, and its "
            f"overlay is {layer_width:g} by {layer_height:g}: make the overlay "
            "the size of the page as shown (page_sizes)."
        )
    up: Matrix = (1.0, 0.0, 0.0, 1.0, 0.0, height - layer_height)
    return _then(_then(layer_shown, up), _inverse(to_shown))


def _shown(page: Any) -> tuple[Matrix, float, float]:  # noqa: ANN401 - pypdf's types.
    """How ``page`` is shown: a map of its coordinates to the shown ones, and its size.

    The shown page is the crop box within the media box, turned clockwise
    by ``/Rotate``; its origin is the shown bottom-left corner.

    Raises:
        DocumentError: Its rotation is not a multiple of 90 degrees.
    """
    left, bottom, right, top = _visible(page)
    width, height = right - left, top - bottom
    rotation = page.rotation % 360
    if rotation == 0:
        return (1.0, 0.0, 0.0, 1.0, -left, -bottom), width, height
    if rotation == 90:
        return (0.0, -1.0, 1.0, 0.0, -bottom, right), height, width
    if rotation == 180:
        return (-1.0, 0.0, 0.0, -1.0, right, top), width, height
    if rotation == 270:
        return (0.0, 1.0, -1.0, 0.0, top, -left), height, width
    raise DocumentError(
        f"A page is rotated {page.rotation} degrees: not a multiple of 90."
    )


def _visible(page: Any) -> tuple[float, float, float, float]:  # noqa: ANN401 - pypdf's types.
    """The part of ``page`` shown: its crop box within its media box, in order."""
    crop = _corners(page.cropbox)
    media = _corners(page.mediabox)
    return (
        max(crop[0], media[0]),
        max(crop[1], media[1]),
        min(crop[2], media[2]),
        min(crop[3], media[3]),
    )


def _corners(box: Any) -> tuple[float, float, float, float]:  # noqa: ANN401 - pypdf's types.
    """A box's left, bottom, right, and top, whichever corners it names."""
    x0, y0, x1, y1 = (float(value) for value in box)
    return min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1)


def _then(first: Matrix, second: Matrix) -> Matrix:
    """The transformation that applies ``first``, then ``second``."""
    a1, b1, c1, d1, e1, f1 = first
    a2, b2, c2, d2, e2, f2 = second
    return (
        a1 * a2 + b1 * c2,
        a1 * b2 + b1 * d2,
        c1 * a2 + d1 * c2,
        c1 * b2 + d1 * d2,
        e1 * a2 + f1 * c2 + e2,
        e1 * b2 + f1 * d2 + f2,
    )


def _inverse(matrix: Matrix) -> Matrix:
    """The transformation that undoes ``matrix`` (a rotation and a move)."""
    a, b, c, d, e, f = matrix
    det = a * d - b * c
    return (
        d / det,
        -b / det,
        -c / det,
        a / det,
        (c * f - d * e) / det,
        (b * e - a * f) / det,
    )
