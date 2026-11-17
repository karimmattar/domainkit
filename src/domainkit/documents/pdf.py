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
never raises them. The cross-reference streams pypdf reads as it opens
a file are held to those limits each, not counted in all.
"""

import io
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from typing import Any, Final

from domainkit.documents.base import DocumentError, missing

MISFIT: Final = 2.0
"""Points an overlay may differ from its page's shown size, either way (0.7 mm):
a browser rounds a page's size."""
MAX_DECODED: Final = 32 * 1024 * 1024
"""The bytes a call decodes by default: object streams and the pages' content."""
DECODED: Final = (
    "zlib_maximum_output_length",
    "lzw_maximum_output_length",
    "run_length_maximum_output_length",
    "brotli_maximum_output_length",
    "jbig2_maximum_output_length",
)
"""pypdf's limits on what one stream decodes to, by filter (``pypdf.Configuration``)."""
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
    budget = _Budget(pypdf, max_decoded, "The document")
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
    budget = _Budget(pypdf, max_decoded, "The document and its overlays")
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
            raise DocumentError(
                f"The pages could not be laid over ({type(exc).__name__})."
            ) from None
        return out.getvalue()


class _Budget:
    """The bytes a call may still decode, and pypdf's limits within it."""

    def __init__(self, pypdf: Any, max_decoded: int, what: str) -> None:  # noqa: ANN401 - the module.
        """Allow ``max_decoded`` bytes; ``what`` names the PDFs in a refusal.

        Raises:
            ValueError: ``max_decoded`` is not more than 0.
        """
        if not max_decoded > 0:
            raise ValueError(f"max_decoded is more than 0, not {max_decoded!r}.")
        self.pypdf = pypdf
        self.max_decoded = max_decoded
        self.left = max_decoded
        self.what = what

    @contextmanager
    def limited(self, most: int | None = None) -> Iterator[None]:
        """Lower pypdf's limits in this context only: no stream decodes past ``most``.

        ``most`` is ``max_decoded`` unless given. A limit pypdf already holds
        lower stays; one it does not hold (0) becomes ``most``.
        """
        most = self.max_decoded if most is None else most
        held = self.pypdf.get_configuration()
        lowered = {}
        for name in DECODED:
            limit = getattr(held, name)
            lowered[name] = most if limit <= 0 else min(limit, most)
        with self.pypdf.apply_configuration(**lowered):
            yield

    def spend(self, stream: Any) -> None:  # noqa: ANN401 - pypdf's types.
        """Decode ``stream``, which pypdf then keeps, out of what is left.

        It decodes no further than what is left (a byte at least): one
        that would go further stops there, and is refused.

        Raises:
            DocumentError: What is decoded passes ``max_decoded``.
        """
        try:
            with self.limited(max(self.left, 1)):
                self.left -= len(stream.get_data())
        except self.pypdf.errors.LimitReachedError:
            self.left = -1
        if self.left < 0:
            raise DocumentError(
                f"{self.what} decode to more than {self.max_decoded:,} bytes "
                "(max_decoded), or past pypdf's limit on a stream."
            )

    def spend_objects(self, reader: Any) -> None:  # noqa: ANN401 - pypdf's types.
        """Decode the object streams of ``reader``, every one, out of what is left.

        An object stream pypdf cannot decode is left to fail where it is
        used: unused, it fails nothing, as before.

        Raises:
            DocumentError: What is decoded passes ``max_decoded``.
        """
        for number in sorted({stream for stream, _ in reader.xref_objStm.values()}):
            try:
                stream = reader.get_object(number)
                if not hasattr(stream, "get_data"):
                    continue
                self.spend(stream)
            except DocumentError:
                raise
            except Exception:  # noqa: BLE001, S112 - pypdf raises many kinds; see above.
                continue

    def spend_contents(self, page: Any) -> None:  # noqa: ANN401 - pypdf's types.
        """Decode the content of ``page``, each of its streams, out of what is left.

        Raises:
            DocumentError: What is decoded passes ``max_decoded``.
        """
        contents = page.get("/Contents")
        contents = None if contents is None else contents.get_object()
        streams = contents if isinstance(contents, list) else [contents]
        for each in streams:
            stream = None if each is None else each.get_object()
            if hasattr(stream, "get_data"):
                self.spend(stream)


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


def _reader(pypdf: Any, data: bytes, what: str, budget: _Budget) -> Any:  # noqa: ANN401 - pypdf's types.
    """A reader of ``data`` whose pages could be listed, its object streams decoded.

    Raises:
        DocumentError: pypdf cannot read it, it is encrypted, or it decodes
            past what is left of ``budget``.
    """
    try:
        reader = pypdf.PdfReader(io.BytesIO(data))
        encrypted = reader.is_encrypted
        if not encrypted:
            budget.spend_objects(reader)
            len(reader.pages)  # Reads the page tree.
    except DocumentError:
        raise
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
