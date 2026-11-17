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
most, and refusing to rebuild the table of a damaged file, which would
decode every object stream unchecked. The sections' streams list
``max_decoded / 512`` entries in all (``XREF_ENTRIES`` at least), and
each decodes to ``XREF_ENTRY_BYTES`` an entry at most. pypdf parses the overlay's
drawing into operators, at far more than its bytes, and that is not
counted: the overlay is the renderer's output from the project's HTML,
never a PDF a person sent.
"""

import io
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from functools import cache
from typing import Any, Final

from domainkit.documents.base import DocumentError, missing

MiB: Final = 1024 * 1024

MISFIT: Final = 2.0
"""Points an overlay may differ from its page's shown size, either way (0.7 mm):
a browser rounds a page's size."""
MAX_DECODED: Final = 32 * MiB
"""The bytes a call decodes by default: object streams and the pages' content."""
DECODED: Final = (
    "zlib_maximum_output_length",
    "lzw_maximum_output_length",
    "run_length_maximum_output_length",
    "brotli_maximum_output_length",
    "jbig2_maximum_output_length",
)
"""pypdf's limits on what one stream decodes to, by filter (``pypdf.Configuration``)."""
FILTERS: Final[Mapping[str, str]] = {
    "/FlateDecode": "/FlateDecode",
    "/Fl": "/FlateDecode",
    "/BrotliDecode": "/BrotliDecode",
    "/LZWDecode": "/LZWDecode",
    "/LZW": "/LZWDecode",
    "/RunLengthDecode": "/RunLengthDecode",
    "/RL": "/RunLengthDecode",
    "/ASCIIHexDecode": "/ASCIIHexDecode",
    "/AHx": "/ASCIIHexDecode",
    "/ASCII85Decode": "/ASCII85Decode",
    "/A85": "/ASCII85Decode",
}
"""The filters a page's content and a PDF's objects are decoded with, by each
name pypdf reads; an image's (DCT, JPX, CCITT, JBIG2) or ``/Crypt`` is refused."""
IN_PYTHON: Final = frozenset(
    {"/LZWDecode", "/RunLengthDecode", "/ASCIIHexDecode", "/ASCII85Decode"}
)
"""Filters pypdf decodes in Python: per MiB read, ASCII85's ``z`` costs it 94 MiB
of memory, ASCII85 0.15 s, run-length up to 0.3 s, LZW 0.6 s, and 11 s of
nothing but clear codes."""
IN_PYTHON_SHARE: Final = 32
"""The streams a call decodes in Python store ``max_decoded`` / 32 bytes in all:
at the default, 1 MiB: about 1 s and 100 MiB at worst, LZW's share included."""
LZW_SHARE: Final = 512
"""The streams a call decodes with LZW store ``max_decoded`` / 512 bytes in all,
counted in ``IN_PYTHON_SHARE`` too: at the default, 64 KiB, 0.7 s of clear codes."""
XREF_ENTRIES: Final = 65_536
"""Entries a PDF's cross-reference streams list in all, at least: pypdf takes
up to about 200 bytes and 13 µs for each. One for each 512 bytes of ``max_decoded``
where that is more."""
XREF_ENTRY_BYTES: Final = 8
"""Bytes a cross-reference stream decodes to for each entry it may list: an
entry of a file under 4 GB (``/W [1 4 2]``) and its predictor's byte."""
XREF_SECTIONS: Final = 32
"""Cross-reference sections a PDF may chain by ``/Prev`` and ``/XRefStm``: one
for the file and one for each save added to it (two, in a hybrid file: a form
filled or signed in Acrobat). pypdf follows any number, and runs a stream's
predictor in Python: 32 of 512 KiB took 3.8 s at worst."""
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
    budget = DecodeBudget(pypdf, max_decoded, "The document decodes")
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
    budget = DecodeBudget(pypdf, max_decoded, "The document and its overlays decode")
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


class DecodeBudget:
    """What a call may still decode, and pypdf's limits within it: one per call.

    ``limited`` lowers pypdf's limits for the call; ``spend`` decodes a
    stream, once ``check`` let it, counting what it decodes to against
    ``max_decoded``, and what the streams decoded in Python store against
    ``max_decoded / IN_PYTHON_SHARE``, both in all.
    """

    def __init__(self, pypdf: Any, max_decoded: int, what: str) -> None:  # noqa: ANN401 - the module.
        """Allow ``max_decoded`` bytes; ``what`` names the PDFs and a verb, to refuse.

        Raises:
            ValueError: ``max_decoded`` is not more than 0.
        """
        if not max_decoded > 0:
            raise ValueError(f"max_decoded is more than 0, not {max_decoded!r}.")
        self.pypdf = pypdf
        self.max_decoded = max_decoded
        self.left = max_decoded
        self.what = what
        self.xref_entries = max(XREF_ENTRIES, max_decoded // 512)
        self.in_python = max_decoded // IN_PYTHON_SHARE
        """What the streams decoded in Python may still store."""
        self.lzw = max_decoded // LZW_SHARE
        """What the streams decoded with LZW may still store."""

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

        Its filters are checked first (``check``). It decodes no further
        than what is left (a byte at least): one that would go further
        stops there, and is refused, as one pypdf cannot decode is.

        Raises:
            DocumentError: Its filters are refused, it cannot be decoded,
                or what is decoded passes ``max_decoded``.
        """
        self.check(stream)
        try:
            with self.limited(max(self.left, 1)):
                self.left -= len(stream.get_data())
        except self.pypdf.errors.LimitReachedError:
            self.left = -1
        except Exception as exc:  # noqa: BLE001 - pypdf raises many kinds on a bad stream.
            raise DocumentError(
                f"A stream cannot be decoded ({type(exc).__name__})."
            ) from None
        if self.left < 0:
            raise DocumentError(
                f"{self.what} to more than {self.max_decoded:,} bytes "
                "(max_decoded), or past pypdf's limit on a stream."
            )

    def check(self, stream: Any) -> None:  # noqa: ANN401 - pypdf's types.
        """Refuse ``stream`` before it is decoded: its filters, or what is left.

        Its filters must pass ``filters_refused``, and what the streams
        decoded in Python store must stay within ``max_decoded`` /
        ``IN_PYTHON_SHARE`` in all, and with LZW within ``max_decoded`` /
        ``LZW_SHARE``, this one counted.

        Raises:
            DocumentError: The stream would be decoded otherwise.
        """
        refused = filters_refused(stream)
        if refused is not None:
            raise DocumentError(refused)
        kind, stored = decoded_in_python(stream)
        self.in_python -= stored
        if kind == "/LZWDecode":
            self.lzw -= stored
        if self.lzw < 0:
            raise DocumentError(
                "The streams pypdf decodes with LZW store more than "
                f"{self.max_decoded // LZW_SHARE:,} bytes in all "
                f"(max_decoded / {LZW_SHARE})."
            )
        if self.in_python < 0:
            raise DocumentError(
                "The streams pypdf decodes in Python (LZW, run-length, ASCIIHex, "
                f"ASCII85) store more than {self.max_decoded // IN_PYTHON_SHARE:,} "
                f"bytes in all (max_decoded / {IN_PYTHON_SHARE})."
            )

    def spend_objects(self, reader: Any) -> None:  # noqa: ANN401 - pypdf's types.
        """Decode the object streams of ``reader``, every one, out of what is left.

        Every one: which a call will use is known only as pypdf resolves
        its objects, and cloning the document resolves nearly all. An entry
        whose object stream is no stream decodes nothing.

        Raises:
            DocumentError: An object stream is refused, cannot be decoded,
                or what is decoded passes ``max_decoded``.
        """
        for number in sorted({stream for stream, _ in reader.xref_objStm.values()}):
            stream = reader.get_object(number)
            if hasattr(stream, "get_data"):
                self.spend(stream)

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


def filters_refused(stream: Any) -> str | None:  # noqa: ANN401 - pypdf's types.
    """Why ``stream`` is not decoded, read from its filters before it is, or None.

    pypdf bounds what deflate, LZW, run-length, and Brotli decode to; not
    what ASCII85 or ASCIIHex do, and it runs four filters in Python
    (``IN_PYTHON``). So a stream is decoded only with filters of
    ``FILTERS``; one of ``IN_PYTHON`` at most, first, reading the stream
    as stored, never what another inflated; and no predictor, which is
    for images and cross-reference streams.
    """
    named = _filters(stream)
    for position, name in enumerate(named):
        kind = FILTERS.get(name) if isinstance(name, str) else None
        if kind is None:
            return (
                f"A stream is decoded with {name}: a page's content and a "
                "PDF's objects are decoded with FlateDecode, LZWDecode, "
                "RunLengthDecode, ASCIIHexDecode, ASCII85Decode, or "
                "BrotliDecode only."
            )
        if kind in IN_PYTHON and position > 0:
            return (
                f"A stream is decoded with {kind} after {named[0]}: "
                f"{kind} decodes a stream as it is stored, first, or not at all."
            )
    parameters = stream.get("/DecodeParms")
    parameters = None if parameters is None else parameters.get_object()
    for each in parameters if isinstance(parameters, list) else [parameters]:
        given = None if each is None else each.get_object()
        if isinstance(given, dict) and given.get("/Predictor", 1) != 1:
            return (
                "A stream is decoded with a predictor, which is for images and "
                "cross-reference streams: pypdf runs it in Python."
            )
    return None


def decoded_in_python(stream: Any) -> tuple[str | None, int]:  # noqa: ANN401 - pypdf's types.
    """The filter of ``IN_PYTHON`` decoding ``stream`` first, and the bytes it stores.

    ``(None, 0)`` where pypdf decodes it in C, or not at all. For a stream
    ``filters_refused`` lets through.
    """
    named = _filters(stream)
    kind = FILTERS[named[0]] if named else None
    if kind in IN_PYTHON:
        return kind, len(stream._data)  # As stored: what the first filter reads.
    return None, 0


def _filters(stream: Any) -> list[Any]:  # noqa: ANN401 - pypdf's types.
    """The filters of ``stream``, in the order they decode it, each resolved."""
    filters = stream.get("/Filter")
    filters = None if filters is None else filters.get_object()
    names = filters if isinstance(filters, list) else [filters]
    return [each.get_object() for each in names if each is not None]


@dataclass(slots=True)
class Tally:
    """What a ``bounded_reader`` read as it opened a file, refused or not."""

    sections: int = 0
    """Cross-reference sections it read, or was refused."""
    rebuilt: bool = False
    """Whether pypdf would have rebuilt the cross-reference table."""

    def refusal(self, most: int) -> str | None:
        """Why the file is refused, if it is: more than ``most`` sections, a rebuild."""
        if self.rebuilt:
            return (
                "is damaged: its cross-reference table would be rebuilt, which "
                "decodes every object stream unchecked."
            )
        if self.sections > most:
            return (
                f"chains more than {most} cross-reference sections: one for the "
                "file, and one for each save added to it."
            )
        return None


def bounded_reader(pypdf: Any) -> Any:  # noqa: ANN401 - the module, and its class.
    """A ``PdfReader`` of pypdf that counts the sections it reads, and never rebuilds.

    It takes ``sections``, the cross-reference sections it may read (a
    table, or a stream, followed by ``/Prev``, or by a hybrid file's
    ``/XRefStm``), and ``tally``, a ``Tally`` it counts them in. It counts
    each where pypdf reads it (``_read_xref``, ``_read_pdf15_xref_stream``),
    so no syntax hides one, and refuses the one past ``sections`` with
    ``LimitReachedError`` before reading it. It never rebuilds a damaged
    file's table (``_rebuild_xref_table``), which decodes every object
    stream with whatever filters it has. pypdf swallows some errors as it
    follows a chain, so the tally, not the error, says what was refused.
    The methods are pypdf's own, not its public API: a test checks that
    each runs.
    """
    return _bounded_reader(pypdf.PdfReader, pypdf.errors.LimitReachedError)


@cache
def _bounded_reader(reader: type, limit: type[Exception]) -> type:
    """The class ``bounded_reader`` gives, made once."""

    class BoundedReader(reader):  # type: ignore[misc]
        """A ``PdfReader`` of ``sections`` cross-reference sections at most."""

        def __init__(
            self,
            stream: Any,  # noqa: ANN401 - pypdf's.
            *,
            sections: int,
            tally: Tally,
            **options: Any,  # noqa: ANN401 - pypdf's.
        ) -> None:
            """Read ``sections`` sections at most, each counted in ``tally``."""
            self.most_sections = sections
            self.tally = tally
            super().__init__(stream, **options)

        def _read_xref(self, stream: Any) -> Any:  # noqa: ANN401 - pypdf's.
            """A table's section, counted first."""
            self._count()
            return super()._read_xref(stream)

        def _read_pdf15_xref_stream(self, stream: Any) -> Any:  # noqa: ANN401 - pypdf's.
            """A stream's section, counted first."""
            self._count()
            return super()._read_pdf15_xref_stream(stream)

        def _rebuild_xref_table(self, stream: Any) -> None:  # noqa: ANN401 - pypdf's.
            """Refuse to rebuild the table: it decodes every object stream."""
            self.tally.rebuilt = True
            raise limit("The cross-reference table would be rebuilt.")

        def _count(self) -> None:
            """Count a section; refuse one past ``sections``."""
            self.tally.sections += 1
            if self.tally.sections > self.most_sections:
                raise limit(f"More than {self.most_sections} cross-reference sections.")

    return BoundedReader


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
    tally = Tally()
    try:
        # The cross-reference streams: their entries in all, each one's bytes.
        with budget.limited(budget.xref_entries * XREF_ENTRY_BYTES):
            reader = bounded_reader(pypdf)(
                io.BytesIO(data),
                sections=XREF_SECTIONS,
                tally=tally,
                xref_stream_maximum_entries=budget.xref_entries,
            )
        if refusal := tally.refusal(XREF_SECTIONS):
            raise DocumentError(f"{what} {refusal}")
        encrypted = reader.is_encrypted
        if not encrypted:
            budget.spend_objects(reader)
            len(reader.pages)  # Reads the page tree.
    except DocumentError:
        raise
    except Exception as exc:  # noqa: BLE001 - pypdf raises many kinds on a bad file.
        if refusal := tally.refusal(XREF_SECTIONS):  # What stopped pypdf, if it was.
            raise DocumentError(f"{what} {refusal}") from None
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
