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
clipped to the overlay's box. The page's content is written back deflated.

What a PDF decodes is bounded by ``max_decoded``: its object streams
(where its objects are kept compressed), and the content of the pages
laid over and of their overlays, in all. Each is decoded before it is
used, under pypdf's limits lowered for the call, so a stream stops
decoding where the budget ends: a page of a few kilobytes that would
inflate to hundreds of megabytes is refused, not decoded. pypdf's
limits are set for the call only, in its context (a ``ContextVar``):
other threads, and the code around the call, keep theirs, and the call
never raises them.

pypdf bounds what deflate, LZW, run-length, and Brotli decode to, not
ASCII85 or ASCIIHex, and it decodes all but deflate and Brotli in
Python, at up to a hundred bytes of memory a byte they read, and up to
0.6 seconds a megabyte (``IN_PYTHON``). So a stream's filters are read
before it is decoded (``filters_refused``): only those of ``FILTERS``,
one decoded in Python at most, first, and no predictor (one is for
images and cross-reference streams). Anything else is refused, never
decoded: an ASCII85 stage after deflate, as one test PDF had, would
turn 31 KB into gigabytes. What the streams decoded in Python store is
counted too, ``max_decoded / IN_PYTHON_SHARE`` in all, and of LZW, whose
clear codes cost pypdf 11 seconds a megabyte, ``max_decoded / LZW_SHARE``.

A file is opened by ``bounded_reader``: pypdf's reader, counting the
cross-reference sections where pypdf reads them, ``XREF_SECTIONS`` at
most, and the searches of the whole file for an object its table lacks,
``SEARCHES`` at most; refusing to rebuild the table of a damaged file,
which would decode every object stream unchecked; and refusing to
decode an object stream ``DecodeBudget`` has not. The sections' streams list
``max_decoded / 512`` entries in all (``XREF_ENTRIES`` at least), and
each decodes to ``XREF_ENTRY_BYTES`` an entry at most. pypdf parses the overlay's
drawing into operators, at far more than its bytes, and that is not
counted: the overlay is the renderer's output from the project's HTML,
never a PDF a person sent.

The policy (``FILTERS``, ``filters_refused``, ``DecodeBudget``,
``bounded_reader`` and their bounds) lives in ``domainkit.inspection``,
which ``PdfCheck`` reads uploads with too (ADR 0175, ADR 0176).
"""

import io
from collections.abc import Mapping
from typing import Any, Final

from domainkit.documents.base import DocumentError, missing
from domainkit.inspection import XREF_ENTRIES, XREF_ENTRY_BYTES, DecodeBudget
from domainkit.inspection import Tally as Tally
from domainkit.inspection import bounded_reader as bounded_reader

MiB: Final = 1024 * 1024

MISFIT: Final = 2.0
"""Points an overlay may differ from its page's shown size, either way (0.7 mm):
a browser rounds a page's size."""
MAX_DECODED: Final = 32 * MiB
"""The bytes a call decodes by default: object streams and the pages' content."""
type Matrix = tuple[float, float, float, float, float, float]
"""A PDF transformation ``[a b c d e f]``: x' = a x + c y + e, y' = b x + d y + f."""


def page_sizes(
    pdf: bytes, *, max_decoded: int = MAX_DECODED
) -> list[tuple[float, float]]:
    """Each page's width and height as it is shown, in points (1/72 inch).

    The size of its crop box (within its media box), turned by its rotation:
    a portrait page rotated 90 degrees is shown, and measured, as landscape.
    Its object streams decode to ``max_decoded`` bytes at most, in all.

    Raises:
        ImportError: ``domainkit[documents]`` is not installed.
        ValueError: ``max_decoded`` is not more than 0.
        DocumentError: It is not a PDF pypdf reads, it is encrypted, or it
            decodes past ``max_decoded``.
    """
    pypdf = _pypdf()
    budget = DecodeBudget(
        pypdf,
        max_decoded,
        "The document decodes",
        refuse=DocumentError,
        xref_entries=XREF_ENTRIES,
    )
    with budget.limited():
        reader = _reader(pypdf, pdf, "The document", budget)
        return [_shown(page)[1:] for page in reader.pages]


def overlay_pdf(
    document: bytes,
    overlay: bytes | Mapping[int, bytes],
    *,
    max_decoded: int = MAX_DECODED,
) -> bytes:
    """``document`` with overlays drawn over its pages.

    ``overlay`` is a PDF whose page *i* goes over the document's page *i*,
    with as many pages; or a mapping of page indexes (0 for the first) to
    PDFs of one page each, for the pages that have one. Each overlay page
    is the size of its page as shown (``page_sizes``), within ``MISFIT``
    points, and is laid with its top-left corner on the shown page's.

    ``max_decoded`` bounds the bytes decoded, in all: the object streams
    of the document and of the overlays, and the content of the pages laid
    over and of their overlays. The default holds a contract of hundreds
    of pages; a drawing or a map of many megabytes a page may need more.
    The work is CPU-bound: in async code, run it in a thread.

    Raises:
        ImportError: ``domainkit[documents]`` is not installed.
        ValueError: ``max_decoded`` is not more than 0.
        DocumentError: A PDF cannot be read or is encrypted, the pages do
            not match, an overlay is not the size of its page, or the PDFs
            decode past ``max_decoded``.
    """
    pypdf = _pypdf()
    budget = DecodeBudget(
        pypdf,
        max_decoded,
        "The document and its overlays decode",
        refuse=DocumentError,
        xref_entries=XREF_ENTRIES,
    )
    with budget.limited():
        reader = _reader(pypdf, document, "The document", budget)
        count = len(reader.pages)
        if isinstance(overlay, Mapping):
            layers = {}
            for index, data in overlay.items():
                if not 0 <= index < count:
                    raise DocumentError(
                        f"The document has {count} pages: there is no page index "
                        f"{index}."
                    )
                pages = _reader(
                    pypdf, data, f"The overlay of page index {index}", budget
                ).pages
                if len(pages) != 1:
                    raise DocumentError(
                        f"The overlay of page index {index} has {len(pages)} pages, "
                        "not one."
                    )
                layers[index] = pages[0]
        else:
            pages = _reader(pypdf, overlay, "The overlay", budget).pages
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
                budget.spend_contents(page)
                budget.spend_contents(layer)
                page.merge_transformed_page(
                    layer, pypdf.Transformation(_placement(page, layer, index))
                )
                page.compress_content_streams()
            out = io.BytesIO()
            writer.write(out)
        except DocumentError:
            raise
        except Exception as exc:  # noqa: BLE001 - pypdf raises many kinds on a bad page.
            budget.refuse_tallied()  # What stopped pypdf, if it was.
            raise DocumentError(
                f"The pages could not be laid over ({type(exc).__name__})."
            ) from None
        budget.refuse_tallied()  # What pypdf went on past.
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


def _reader(pypdf: Any, data: bytes, what: str, budget: DecodeBudget) -> Any:  # noqa: ANN401 - pypdf's types.
    """A reader of ``data`` whose pages could be listed, its object streams decoded.

    Raises:
        DocumentError: pypdf cannot read it, it is encrypted, it chains more
            than ``XREF_SECTIONS`` cross-reference sections, its table would
            be rebuilt, or it decodes past what is left of ``budget``.
    """
    tally = Tally(what=what)
    budget.tallies.append(tally)
    try:
        # The cross-reference streams: their entries in all, each one's bytes.
        with budget.limited(budget.xref_entries * XREF_ENTRY_BYTES):
            reader = bounded_reader(pypdf)(
                data, tally=tally, xref_stream_maximum_entries=budget.xref_entries
            )
        budget.refuse_tallied()
        encrypted = reader.is_encrypted
        if not encrypted:
            budget.spend_objects(reader)
            len(reader.pages)  # Reads the page tree.
        budget.refuse_tallied()
    except DocumentError:
        raise
    except Exception as exc:  # noqa: BLE001 - pypdf raises many kinds on a bad file.
        budget.refuse_tallied()  # What stopped pypdf, if it was.
        limit = exc.__cause__  # pypdf wraps what stops it as it opens a file.
        if isinstance(limit, pypdf.errors.LimitReachedError):
            raise DocumentError(
                f"{what} is past a limit it is read within: {limit}"
            ) from None
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
