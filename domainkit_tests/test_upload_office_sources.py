r"""What an Office document names to load: fields however written, SVG, mail merge (U8).

``OfficeCheck`` read a field's name only when it was written bare: a
backslash or a quote before it (``\INCLUDEPICTURE``, ``"INCLUDETEXT"``),
which Word and LibreOffice read past, hid it. It read no SVG picture, whose
``href`` LibreOffice 24.2 follows to a local file and draws in the PDF it
makes; and no mail merge, whose connection Word opens with no relationship
to refuse (ADR 0175, amended).
"""

import base64
import gzip
import io
import random
import time
import urllib.parse
import zlib
from typing import Final

import pytest

from domainkit.inspection import (
    ACTIONS,
    COMPLEX,
    DTD,
    EXTERNAL,
    FileRefused,
    NotOfType,
    OfficeCheck,
)
from domainkit_tests.support.documents import OOXML, R, W, document, office

BACKSLASH: Final = chr(0x5C)
LEFT: Final = chr(0x201C)
RIGHT: Final = chr(0x201D)
LOW: Final = chr(0x201E)
ZWSP: Final = chr(0x200B)
BOM: Final = chr(0xFEFF)
SHY: Final = chr(0x00AD)
RLM: Final = chr(0x200F)
RLO: Final = chr(0x202E)
NBSP: Final = chr(0x00A0)
URL: Final = '"https://a.example/x.png"'
XLINK: Final = 'xmlns:xlink="http://www.w3.org/1999/xlink"'
SVG_NS: Final = 'xmlns="http://www.w3.org/2000/svg"'
PNG: Final = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8DwHwAFBQIAX8jx0gAAAABJ"
    "RU5ErkJggg=="
)
"""A PNG of one pixel, in base64."""


def verdict(data: bytes, kind: str = "docx", check: OfficeCheck | None = None) -> str:
    """What ``check`` (``OfficeCheck(kind)`` if None) says of ``data``."""
    try:
        (check or OfficeCheck(kind))(io.BytesIO(data))  # type: ignore[arg-type]
    except FileRefused as refused:
        return refused.message if refused.code == "unsafe_file" else refused.code
    except NotOfType:
        return "not of type"
    return "ok"


def escaped(text: str) -> str:
    """``text`` as XML writes it in an element or an attribute."""
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def complex_field(*pieces: str, deleted: tuple[str, ...] = ()) -> str:
    """A field whose instruction is ``pieces``, one run each, as Word writes it.

    ``deleted`` are runs of a tracked deletion, before the others.
    """
    runs = ['<w:r><w:fldChar w:fldCharType="begin"/></w:r>']
    runs += [
        f'<w:r><w:delInstrText xml:space="preserve">{escaped(each)}</w:delInstrText>'
        "</w:r>"
        for each in deleted
    ]
    runs += [
        f'<w:r><w:instrText xml:space="preserve">{escaped(each)}</w:instrText></w:r>'
        for each in pieces
    ]
    runs += [
        '<w:r><w:fldChar w:fldCharType="separate"/></w:r>',
        "<w:r><w:t>shown</w:t></w:r>",
        '<w:r><w:fldChar w:fldCharType="end"/></w:r>',
    ]
    return "<w:p>" + "".join(runs) + "</w:p>"


def simple_field(instruction: str) -> str:
    """A simple field (``w:fldSimple``) of ``instruction``."""
    return (
        f'<w:p><w:fldSimple w:instr="{escaped(instruction)}">'
        "<w:r><w:t>shown</w:t></w:r></w:fldSimple></w:p>"
    )


def word(body: str) -> bytes:
    """A Word document of ``body``."""
    return office(main=document(body))


# Fields ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("instruction", "expected"),
    [
        pytest.param(f"{BACKSLASH}INCLUDEPICTURE {URL}", EXTERNAL, id="backslash"),
        pytest.param(
            f" {BACKSLASH}INCLUDEPICTURE {URL}", EXTERNAL, id="space-backslash"
        ),
        pytest.param(
            f"{BACKSLASH * 2}INCLUDETEXT {URL}", EXTERNAL, id="two-backslashes"
        ),
        pytest.param(f'"INCLUDETEXT" {URL}', EXTERNAL, id="straight-quotes"),
        pytest.param(f"'INCLUDETEXT' {URL}", EXTERNAL, id="single-quotes"),
        pytest.param(
            f'"{BACKSLASH}INCLUDETEXT" {URL}', EXTERNAL, id="quoted-backslash"
        ),
        pytest.param(f"{LEFT}INCLUDETEXT{RIGHT} {URL}", EXTERNAL, id="curly-quotes"),
        pytest.param(f"{LOW}INCLUDEPICTURE{RIGHT} {URL}", EXTERNAL, id="low-quote"),
        pytest.param(f"{ZWSP}INCLUDEPICTURE {URL}", EXTERNAL, id="zero-width-space"),
        pytest.param(f"{BOM}INCLUDEPICTURE {URL}", EXTERNAL, id="byte-order-mark"),
        pytest.param(f"{SHY}INCLUDEPICTURE {URL}", EXTERNAL, id="soft-hyphen"),
        pytest.param(f"{RLM}{RLO}INCLUDEPICTURE {URL}", EXTERNAL, id="directional"),
        pytest.param(f"{NBSP}INCLUDEPICTURE {URL}", EXTERNAL, id="no-break-space"),
        pytest.param(
            f'LI{ZWSP}NK Excel.Sheet "c:/a.xlsx"', EXTERNAL, id="zero-width-inside"
        ),
        pytest.param(f"{BACKSLASH}includePicture {URL}", EXTERNAL, id="mixed-case"),
        pytest.param(f'"ddeauto" "c:{BACKSLASH}cmd.exe"', ACTIONS, id="quoted-dde"),
        pytest.param(f'{BACKSLASH}LINK Excel.Sheet "c:/a.xlsx"', EXTERNAL, id="link"),
        pytest.param(f'{LEFT}DATABASE{RIGHT} {BACKSLASH}d "x.mdb"', EXTERNAL, id="db"),
        pytest.param('"PAGE"', "ok", id="quoted-page"),
        pytest.param(f"{BACKSLASH}SEQ Figure {BACKSLASH}* ARABIC", "ok", id="seq"),
        pytest.param(f"{BACKSLASH}* MERGEFORMAT", "ok", id="a-switch-alone"),
    ],
)
def test_a_fields_name_is_read_past_what_a_reader_passes_over(
    instruction: str, expected: str
) -> None:
    r"""White space, invisible marks, backslashes, quotes: any case, refused alike.

    LibreOffice 24.2 imports ``\INCLUDETEXT``, ``"INCLUDETEXT"`` and
    ``\includeText`` as the field ``INCLUDETEXT`` (tdf#54584); the rest
    is read as a reader more lenient than LibreOffice may read it.
    """
    assert verdict(word(complex_field(instruction))) == expected
    assert verdict(word(simple_field(instruction))) == expected


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        pytest.param(
            complex_field(BACKSLASH, "INCLUDE", f"TEXT {URL}"),
            EXTERNAL,
            id="split-after-the-backslash",
        ),
        pytest.param(
            complex_field(LEFT, "INCLUDEPIC", f"TURE{RIGHT} {URL}"),
            EXTERNAL,
            id="split-in-quotes",
        ),
        pytest.param(
            complex_field(f'"INCLUDETEXT" {URL}', deleted=(BACKSLASH,)),
            EXTERNAL,
            id="deleted-backslash",
        ),
        pytest.param(
            complex_field(f'{BACKSLASH}INCLUDETEXT" {URL}', deleted=('"',)),
            EXTERNAL,
            id="deleted-quote",
        ),
        pytest.param(
            complex_field(f"PICTURE {URL}", deleted=(f"{BACKSLASH}INCLUDE",)),
            EXTERNAL,
            id="name-in-a-deletion",
        ),
    ],
)
def test_a_name_split_or_deleted_is_read_whole(body: str, expected: str) -> None:
    """Runs of the instruction are joined, read with and without their deletions."""
    assert verdict(word(body)) == expected


@pytest.mark.parametrize(
    ("instruction", "expected"),
    [
        pytest.param('"HYPERLINK" "file:///etc/passwd"', EXTERNAL, id="quoted-file"),
        pytest.param(
            f'{BACKSLASH}HYPERLINK "{BACKSLASH * 4}host{BACKSLASH * 2}share"',
            EXTERNAL,
            id="backslash-share",
        ),
        pytest.param(
            f'{LEFT}hyperlink{RIGHT} "file:///c:/x"', EXTERNAL, id="curly-file"
        ),
        pytest.param('"HYPERLINK" "https://a.example/"', "ok", id="quoted-https"),
        pytest.param(f'{LEFT}HYPERLINK{RIGHT} "https://a.example/"', "ok", id="curly"),
        pytest.param(f'{BACKSLASH}HYPERLINK "mailto:a@b.example"', "ok", id="mailto"),
        pytest.param(f'"HYPERLINK" {BACKSLASH}l "anchor"', "ok", id="quoted-anchor"),
    ],
)
def test_a_hyperlink_however_named_links_as_a_hyperlink_may(
    instruction: str, expected: str
) -> None:
    """Its target is read after the quote that closes its name, never as one."""
    assert verdict(word(complex_field(instruction))) == expected
    assert verdict(word(simple_field(instruction))) == expected


TOC: Final = (
    '<w:p><w:r><w:fldChar w:fldCharType="begin"/></w:r>'
    '<w:r><w:instrText xml:space="preserve"> TOC \\o "1-3" \\h \\z \\u </w:instrText>'
    "</w:r>"
    '<w:r><w:fldChar w:fldCharType="separate"/></w:r>'
    '<w:hyperlink w:anchor="_Toc1" w:history="1"><w:r><w:t>Introduction</w:t></w:r>'
    '<w:r><w:fldChar w:fldCharType="begin"/></w:r>'
    '<w:r><w:instrText xml:space="preserve"> PAGEREF _Toc1 \\h </w:instrText></w:r>'
    '<w:r><w:fldChar w:fldCharType="separate"/></w:r><w:r><w:t>1</w:t></w:r>'
    '<w:r><w:fldChar w:fldCharType="end"/></w:r></w:hyperlink></w:p>'
    '<w:p><w:r><w:fldChar w:fldCharType="end"/></w:r></w:p>'
)
"""A table of contents as Word writes one: its entries link to headings and name
their pages by fields of their own, in its result."""


def test_the_fields_documents_hold_still_pass() -> None:
    """A table of contents, page and cross references, a caption, a web link."""
    body = (
        TOC
        + complex_field(" PAGEREF _Ref42 \\h ")
        + complex_field(" REF _Ref42 \\h ")
        + complex_field(' HYPERLINK "https://a.example/docs" \\o "Docs" ')
        + complex_field(' HYPERLINK \\l "_Toc1" ')
        + complex_field(" SEQ Figure \\* ARABIC ")
        + complex_field(' DATE \\@ "d MMMM yyyy" ')
        + complex_field(" MERGEFIELD  Name  \\* MERGEFORMAT ")
        + simple_field(" PAGE ")
        + simple_field(" NUMPAGES ")
    )

    assert verdict(word(body)) == "ok"


# SVG ---------------------------------------------------------------------------------


def svg(inner: str, *, root: str = "svg") -> str:
    """An SVG of ``inner``, its root ``root``, with the SVG and XLink namespaces."""
    return f'<{root} {SVG_NS} {XLINK} width="10" height="10">{inner}</{root}>'


def pictured(
    picture: str | bytes,
    *,
    name: str = "media/image1.svg",
    kind: str = "docx",
    content_type: str | None = None,
    reached: bool = True,
) -> bytes:
    """A ``kind`` document whose main part's image relationship names ``picture``.

    The part is ``name`` in the main part's folder, typed by its extension
    (``svg``: an SVG; ``png``: a PNG) unless ``content_type`` says
    otherwise; with ``reached`` false, no relationship names it.
    """
    folder = {"docx": "word", "xlsx": "xl", "pptx": "ppt"}[kind]
    part = f"{folder}/{name}"
    return office(
        kind,
        main_relationships=[(f"{R}image", name)] if reached else [],
        parts={part: picture},
        defaults={"svg": "image/svg+xml", "svgz": "image/svg+xml", "png": "image/png"},
        types={part: content_type} if content_type else None,
    )


def data_url(content: str | bytes, media: str = "image/svg+xml;base64,") -> str:
    """``content`` as a ``data:`` URL in base64, after ``media``."""
    raw = content.encode() if isinstance(content, str) else content
    return f"data:{media}{base64.b64encode(raw).decode()}"


FETCHING: Final = svg('<image href="file:///etc/hostname" width="10" height="10"/>')
"""An SVG whose picture is a local file: LibreOffice 24.2 drew such a file into
the PDF it made of a document that held the SVG."""


@pytest.mark.parametrize(
    "inner",
    [
        pytest.param('<image href="https://a.example/x.png"/>', id="href"),
        pytest.param('<image xlink:href="file:///etc/passwd"/>', id="xlink-href"),
        pytest.param(
            '<image xlink:href="FILE:///etc/passwd"/>', id="scheme-in-capitals"
        ),
        pytest.param('<image href="x.png"/>', id="relative"),
        pytest.param('<image href=" //host/share/x.png"/>', id="share"),
        pytest.param('<image href="vnd.sun.star.expand:$HOME"/>', id="another-scheme"),
        pytest.param('<use href="other.svg#a"/>', id="use"),
        pytest.param(
            '<filter id="f"><feImage href="file:///x.png"/></filter>', id="fe-image"
        ),
        pytest.param(
            '<image xmlns:l="urn:other" l:href="file:///x.png"/>', id="any-namespace"
        ),
        pytest.param('<a href="https://a.example/"><rect/></a>', id="a-link"),
        pytest.param(
            '<foreignObject><img xmlns="http://www.w3.org/1999/xhtml" '
            'src="https://a.example/x.png"/></foreignObject>',
            id="html-src",
        ),
        pytest.param(
            '<image><set attributeName="href" to="file:///x.png"/></image>',
            id="set-href",
        ),
        pytest.param(
            '<image><animate attributeName="xlink:href" values="#a;file:///x.png"/>'
            "</image>",
            id="animate-href",
        ),
        pytest.param(
            '<rect style="fill:url(https://a.example/p.svg#g)"/>', id="style-url"
        ),
        pytest.param("<rect fill=\"url('file:///x.svg#g')\"/>", id="attribute-url"),
        pytest.param(
            "<style>@import url(https://a.example/s.css);</style>", id="import-url"
        ),
        pytest.param('<style>@import "s.css";</style>', id="import-string"),
        pytest.param(
            '<style>rect{fill:url("https://a.example/x")}</style>', id="style-element"
        ),
        pytest.param("<style><![CDATA[@import url(s.css);]]></style>", id="cdata"),
        pytest.param("<style>&#64;import 's.css';</style>", id="character-reference"),
        pytest.param(
            f"<style>@{BACKSLASH}69mport 's.css';</style>", id="css-escape-import"
        ),
        pytest.param(
            f"<style>rect{{fill:{BACKSLASH}75 rl(https://a.example/x)}}</style>",
            id="css-escape-url",
        ),
        pytest.param("<style>/* a */@import/**/'s.css';</style>", id="comments"),
        pytest.param(
            "<rect style=\"background:-webkit-image-set('x.png' 1x)\"/>",
            id="image-set",
        ),
        pytest.param(
            "<style>a{b:c}<x/>@import 's.css';</style>", id="text-after-a-child"
        ),
        pytest.param(
            f"<style>@imp{BACKSLASH}\nort 's.css';</style>", id="escaped-line-end"
        ),
        pytest.param(f'<image href="{data_url(FETCHING)}"/>', id="svg-in-a-data-url"),
        pytest.param(
            f'<image href="{data_url(FETCHING, "image/png;base64,")}"/>',
            id="svg-in-a-data-url-typed-png",
        ),
        pytest.param(
            f'<image href="{data_url(FETCHING, "image/png; Base64 ,")}"/>',
            id="data-url-spaced",
        ),
        pytest.param(
            f'<image href="{data_url(" " + FETCHING, "image/png;base64ICAg,")}"/>',
            id="base64-not-last",
        ),
        pytest.param(
            f'<image href="{data_url(FETCHING, "image/png,x;base64,")}"/>',
            id="a-comma-before-the-semicolon",
        ),
        pytest.param(
            f'<image href="{data_url("  ", "image/png;base64,")}'
            f'{data_url(FETCHING, "")[5:]}"/>',
            id="padding-inside",
        ),
        pytest.param(
            f'<image href="data:image/png,x;base64,IA=={data_url(FETCHING, "")[5:]}"/>',
            id="padding-inside-for-libreoffice",
        ),
        pytest.param(
            '<image href="data:image/svg+xml,'
            + escaped(urllib.parse.quote(FETCHING))
            + '"/>',
            id="svg-in-a-percent-data-url",
        ),
        pytest.param(
            f'<image href="{data_url(gzip.compress(FETCHING.encode()))}"/>',
            id="gzip-svg-in-a-data-url",
        ),
        pytest.param(
            f'<rect style="fill:url({data_url(FETCHING)})"/>', id="svg-in-css-data"
        ),
    ],
)
def test_an_svg_loads_nothing_from_elsewhere(inner: str) -> None:
    """An ``href`` names a fragment or a ``data:`` URL; CSS imports nothing.

    LibreOffice reads ``xlink:href`` by its prefix whatever its namespace,
    draws a ``feImage`` and an SVG in a ``data:`` URL whatever its media
    type; a browser reads the rest.
    """
    assert verdict(pictured(svg(inner))) == EXTERNAL


@pytest.mark.parametrize(
    "picture",
    [
        pytest.param(FETCHING, id="typed-svg"),
        pytest.param(
            '<?xml version="1.0"?>\n<!-- Generator: x -->\n' + FETCHING,
            id="comment-first",
        ),
        pytest.param(
            svg('<image href="file:///x"/>', root="s:svg").replace(
                SVG_NS, 'xmlns:s="http://www.w3.org/2000/svg"'
            ),
            id="prefixed",
        ),
        pytest.param(
            f'<image {SVG_NS} href="file:///x.png"/><!--<svg-->', id="image-at-the-root"
        ),
        pytest.param(b"\xff\xfe" + FETCHING.encode("utf-16-le"), id="utf-16"),
    ],
)
@pytest.mark.parametrize(
    "name", ["media/image1.svg", "media/image1.png", "media/x.xml"]
)
def test_an_svg_is_read_by_its_name_its_type_or_its_bytes(
    picture: str | bytes, name: str
) -> None:
    """Called ``image1.png`` and typed a PNG, an SVG is still one to LibreOffice.

    It finds ``<svg`` in the bytes, in a comment too, and draws an
    ``image`` at the root; whatever the part is named.
    """
    assert verdict(pictured(picture, name=name)) == EXTERNAL


@pytest.mark.parametrize("kind", ["xlsx", "pptx"])
def test_a_workbooks_and_a_presentations_svg_are_read_alike(kind: str) -> None:
    """``xl/media`` and ``ppt/media`` hold pictures as ``word/media`` does."""
    assert verdict(pictured(FETCHING, kind=kind), kind) == EXTERNAL
    assert verdict(pictured(FETCHING, kind=kind, name="media/x.png"), kind) == EXTERNAL


@pytest.mark.parametrize(
    ("picture", "name", "content_type"),
    [
        pytest.param(gzip.compress(FETCHING.encode()), "media/a.svgz", None, id="svgz"),
        pytest.param(
            gzip.compress(FETCHING.encode()), "media/a.png", None, id="as-png"
        ),
        pytest.param(
            gzip.compress(FETCHING.encode()), "media/a.bin", "image/svg+xml", id="typed"
        ),
        pytest.param(
            gzip.compress(FETCHING.encode())[:-8] + b"\x00" * 8,
            "media/a.png",
            None,
            id="checksum-wrong",
        ),
    ],
)
def test_a_compressed_svg_is_read_inflated(
    picture: bytes, name: str, content_type: str | None
) -> None:
    """LibreOffice inflates a gzip picture and draws the SVG it holds.

    It reads the header and the deflated data, never the checksum.
    """
    assert verdict(pictured(picture, name=name, content_type=content_type)) == EXTERNAL


def gzipped(body: bytes, flags: int, after: bytes = b"") -> bytes:
    """``body`` deflated in a gzip stream whose header has ``flags``, ``after`` it."""
    deflate = zlib.compressobj(9, zlib.DEFLATED, -zlib.MAX_WBITS)
    header = b"\x1f\x8b\x08" + bytes([flags]) + bytes(4) + b"\x00\xff" + after
    return header + deflate.compress(body) + deflate.flush() + bytes(8)


@pytest.mark.parametrize(
    ("flags", "after", "expected"),
    [
        pytest.param(0x04, b"\x03\x00abc", EXTERNAL, id="an-extra-field"),
        pytest.param(0x08, b"a.svg\x00", EXTERNAL, id="a-name"),
        pytest.param(0x10, b"made by hand\x00", EXTERNAL, id="a-comment"),
        pytest.param(0x02, b"\x00\x00", EXTERNAL, id="a-header-checksum"),
        pytest.param(0x1E, b"\x01\x00xn\x00c\x00\x00\x00", EXTERNAL, id="all-of-them"),
        pytest.param(0x08, b"a.svg", "ok", id="a-name-never-ended"),
        pytest.param(0x04, b"\xff\xff", "ok", id="an-extra-field-past-the-end"),
    ],
)
def test_a_gzip_header_is_read_as_its_flags_say(
    flags: int, after: bytes, expected: str
) -> None:
    """An extra field, a name, a comment, a checksum: the SVG follows them.

    A header that never ends holds no SVG: no reader inflates one.
    """
    data = gzipped(FETCHING.encode(), flags, after)
    if expected == "ok":
        data = data[: 10 + len(after)]

    assert verdict(pictured(data, name="media/a.png")) == expected


def test_what_does_not_inflate_is_refused_or_not_gzip() -> None:
    """A gzip header and broken data: refused; no gzip header: a picture.

    An embedded font's first bytes are scrambled: they may start as gzip
    does, and their header then fails (its method, its flags).
    """
    broken = gzip.compress(b"")[:10] + b"\xff" * 40  # A block of no type.

    assert verdict(pictured(broken, name="media/a.png")) == COMPLEX
    assert verdict(pictured(b"\x1f\x8b\x09" + b"\x00" * 40, name="media/a.png")) == "ok"
    assert (
        verdict(pictured(b"\x1f\x8b\x08\xe0" + b"\x00" * 40, name="media/a.png"))
        == "ok"
    )
    cut = gzip.compress(svg("<rect/>").encode())[:-12]
    assert verdict(pictured(cut, name="media/a.png")) == COMPLEX  # An element begun.


def test_an_svg_inflating_past_the_budget_is_refused() -> None:
    """What a gzip picture inflates to counts against the check's bytes."""
    bomb = gzip.compress(svg("<!--" + "x" * 5_000_000 + "-->").encode(), 9)

    assert verdict(pictured(bomb, name="media/a.svgz")) == COMPLEX


def test_an_expensive_svg_is_refused_within_the_deadline() -> None:
    """Each value, match and escape of an SVG counts towards a look at the clock.

    Four elements, each a style of 100,000 ``url(data:,%3C...)``, 7.7 MB
    deflating to 1.5 MB: the clock was read every 1,024 elements, and the
    check ran three seconds past a deadline of 0.3.
    """
    numbers = random.Random(3)  # noqa: S311 - a style's bulk, not a secret.
    rects = "".join(
        '<rect style="'
        + "".join(f"url(data:,%3C{numbers.randrange(10**6)})" for _ in range(100_000))
        + '"/>'
        for _ in range(4)
    )
    data = pictured(svg(rects))
    started = time.monotonic()

    assert verdict(data, check=OfficeCheck("docx", max_seconds=0.2)) == COMPLEX
    assert time.monotonic() - started < 1.5


def test_a_long_data_url_is_unquoted_whole() -> None:
    """In chunks, never cut inside a ``%3C``: the SVG it holds is read as written."""
    held = (" " * 21_800 + FETCHING).encode()
    url = "data:," + "".join(f"%{each:02X}" for each in held)

    assert verdict(pictured(svg(f'<image href="{url}"/>'))) == EXTERNAL


def test_svgs_nested_too_deep_are_refused() -> None:
    """An SVG in a ``data:`` URL of an SVG, and so on: four deep at most."""
    nested = svg("<rect/>")
    for _ in range(4):
        nested = svg(f'<image href="{data_url(nested)}"/>')

    assert verdict(pictured(nested)) == "ok"
    assert verdict(pictured(svg(f'<image href="{data_url(nested)}"/>'))) == COMPLEX


@pytest.mark.parametrize(
    ("picture", "expected"),
    [
        pytest.param(
            '<?xml-stylesheet href="https://a.example/s.css"?>' + svg("<rect/>"),
            EXTERNAL,
            id="xml-stylesheet",
        ),
        pytest.param(
            '<!DOCTYPE svg [<!ENTITY e SYSTEM "file:///etc/passwd">]>'
            + svg("<text>&e;</text>"),
            DTD,
            id="external-entity",
        ),
        pytest.param(
            svg(f'<image href="{data_url("<!DOCTYPE x>" + svg("<rect/>"))}"/>'),
            DTD,
            id="entity-in-a-data-url",
        ),
        pytest.param(
            svg(f'<image href="{data_url("<svg><image")}"/>'),
            COMPLEX,
            id="broken-svg-in-a-data-url",
        ),
        pytest.param(
            '<svg><image xlink:href="file:///x"/></svg>',
            "unreadable_file",
            id="xlink-undeclared",
        ),
    ],
)
def test_an_svg_declares_nothing_that_loads(picture: str, expected: str) -> None:
    """No stylesheet instruction, no DTD or entity, in the part or a ``data:`` URL."""
    assert verdict(pictured(picture)) == expected


ILLUSTRATOR: Final = (
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    "<!-- Generator: Adobe Illustrator 27.0.0, SVG Export Plug-In -->\n"
    + svg(
        "<defs><style>.st0{fill:url(#SVGID_1_);}.st1{fill:none;stroke:#000}</style>"
        '<linearGradient id="SVGID_1_"><stop offset="0"/></linearGradient>'
        '<clipPath id="c"><rect width="9" height="9"/></clipPath></defs>'
        '<rect class="st0" width="10" height="10" clip-path="url(#c)"/>'
        '<circle class="st1" r="3" style="fill:url( &quot;#SVGID_1_&quot; )"/>'
        '<use xlink:href="#c" href="#c"/><a href=""><text>Logo</text></a>'
        f'<image width="1" height="1" href="data:image/png;base64,{PNG}"/>'
        f'<image width="1" height="1" xlink:href="DATA:image/png;base64,\n{PNG}"/>'
        f'<image width="1" height="1" href="{data_url(svg("<rect/>"))}"/>'
        f'<image width="1" height="1" href="{data_url("<p>not an SVG</p>")}"/>'
        '<image width="1" height="1" href="data:image/png"/>'
        '<animate attributeName="x" values="0;1"/><set attributeName="href" to="#c"/>'
        "<text>An image (left), a url of the page.</text>"
    )
)
"""An SVG as a drawing program writes one: a comment before its root, styles,
gradients, clips and uses by fragment, pictures held in ``data:`` URLs."""


@pytest.mark.parametrize("kind", ["docx", "xlsx", "pptx"])
def test_an_svg_that_loads_nothing_passes(kind: str) -> None:
    """Fragments and ``data:`` URLs load nothing; nor does a PNG fallback."""
    assert verdict(pictured(ILLUSTRATOR, kind=kind), kind) == "ok"
    assert (
        verdict(
            pictured(
                gzip.compress(ILLUSTRATOR.encode()), kind=kind, name="media/a.svgz"
            ),
            kind,
        )
        == "ok"
    )


SVG_BLIP: Final = (
    '<w:p><w:r><w:drawing><wp:inline xmlns:wp="http://schemas.openxmlformats.org/'
    'drawingml/2006/wordprocessingDrawing" xmlns:a="http://schemas.openxmlformats.org/'
    'drawingml/2006/main" xmlns:pic="http://schemas.openxmlformats.org/drawingml/2006/'
    'picture" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/'
    'relationships"><wp:extent cx="9" cy="9"/><wp:docPr id="1" name="p"/><a:graphic>'
    '<a:graphicData uri="http://schemas.openxmlformats.org/drawingml/2006/picture">'
    '<pic:pic><pic:nvPicPr><pic:cNvPr id="1" name="p"/><pic:cNvPicPr/></pic:nvPicPr>'
    '<pic:blipFill><a:blip r:embed="rId1"><a:extLst><a:ext uri="{96DAC541-7B7A-43D3-'
    '8B79-37D633B846F1}"><asvg:svgBlip xmlns:asvg="http://schemas.microsoft.com/'
    'office/drawing/2016/SVG/main" r:embed="rId2"/></a:ext></a:extLst></a:blip>'
    "</pic:blipFill><pic:spPr/></pic:pic></a:graphicData></a:graphic></wp:inline>"
    "</w:drawing></w:r></w:p>"
)
"""A picture as Word writes an SVG: a PNG to fall back on, the SVG by
``asvg:svgBlip``, which is no ``svg`` element."""


def test_a_word_picture_of_an_svg_passes() -> None:
    """Its PNG, its SVG, and the document that names them: nothing loads."""
    data = office(
        main=document(SVG_BLIP),
        main_relationships=[
            (f"{R}image", "media/image1.png"),
            (f"{R}image", "media/image2.svg"),
        ],
        parts={
            "word/media/image1.png": base64.b64decode(PNG),
            "word/media/image2.svg": ILLUSTRATOR,
        },
        defaults={"png": "image/png", "svg": "image/svg+xml"},
    )

    assert verdict(data) == "ok"


def test_a_part_with_a_comment_before_its_root_is_read() -> None:
    """2.7 refused such a part as unreadable: a drawing program's SVG, typed one."""
    data = office(main="<!-- written by hand -->\n" + document())

    assert verdict(data) == "ok"
    assert (
        verdict(office(main="<?pi x?>" + document(complex_field(f"INCLUDETEXT {URL}"))))
        == EXTERNAL
    )


def test_an_svg_no_relationship_names_is_not_read() -> None:
    """No reader loads a part nothing names, as 2.7 reads none but XML ones.

    Typed an SVG, it is XML by its type, and read.
    """
    assert verdict(pictured(FETCHING, name="media/x.png", reached=False)) == "ok"
    assert verdict(pictured(FETCHING, reached=False)) == EXTERNAL


# Mail merge --------------------------------------------------------------------------


SETTINGS: Final = f"{OOXML}wordprocessingml.settings+xml"


def merged(mail_merge: str) -> bytes:
    """A Word document whose settings hold ``mail_merge``."""
    return office(
        main=document(),
        main_relationships=[(f"{R}settings", "settings.xml")],
        parts={
            "word/settings.xml": f'<w:settings xmlns:w="{W}" '
            'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/'
            f'relationships"><w:mailMerge>{mail_merge}</w:mailMerge></w:settings>'
        },
        types={"word/settings.xml": SETTINGS},
    )


BEGUN: Final = (
    '<w:mainDocumentType w:val="formLetters"/><w:dataType w:val="textFile"/>'
    '<w:activeRecord w:val="-1"/>'
)
"""A mail merge begun, no recipients chosen: what Word writes then."""


@pytest.mark.parametrize(
    ("mail_merge", "expected"),
    [
        pytest.param(
            BEGUN + '<w:connectString w:val="Provider=Microsoft.ACE.OLEDB.12.0;'
            'Data Source=\\\\host\\share\\list.xlsx"/>',
            EXTERNAL,
            id="connect-string",
        ),
        pytest.param(
            BEGUN + '<w:query w:val="SELECT * FROM C:\\list.txt"/>',  # noqa: S608
            EXTERNAL,
            id="query",
        ),
        pytest.param(BEGUN + '<w:dataSource r:id="rId1"/>', EXTERNAL, id="data-source"),
        pytest.param(
            BEGUN + '<w:headerSource r:id="rId2"/>', EXTERNAL, id="header-source"
        ),
        pytest.param(
            BEGUN + '<w:odso><w:udl w:val="Provider=SQLOLEDB;Data Source=host"/>'
            "</w:odso>",
            EXTERNAL,
            id="odso-udl",
        ),
        pytest.param(
            BEGUN + '<w:odso><w:src r:id="rId3"/></w:odso>', EXTERNAL, id="odso-src"
        ),
        pytest.param(BEGUN, "ok", id="no-source"),
        pytest.param(
            BEGUN + '<w:connectString w:val=""/><w:query w:val=" "/>', "ok", id="empty"
        ),
        pytest.param(BEGUN + "<w:viewMergedData/><w:odso/>", "ok", id="no-connection"),
    ],
)
def test_a_mail_merge_names_no_data_source(mail_merge: str, expected: str) -> None:
    """Word connects to a mail merge's source when the document opens.

    As it would to a relationship's outside target, which is refused: a
    share (an NTLM hash sent), a file, a database server.
    """
    assert verdict(merged(mail_merge)) == expected


def test_a_query_outside_a_mail_merge_is_not_one() -> None:
    """``query`` and ``src`` are a mail merge's only inside ``w:mailMerge``."""
    body = '<w:p><w:query w:val="x"/><w:src w:val="y"/></w:p>'

    assert verdict(word(body)) == "ok"
