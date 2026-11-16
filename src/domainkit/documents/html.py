"""Rich text kept to what a renderer may print (``domainkit[documents]``: nh3).

``clean_html`` keeps the text of rich text a person wrote (a contract's
terms from an editor) and the tags and styles that format it: headings,
paragraphs, lists, tables, emphasis, direction (``dir``, ``lang``,
``bdi``), and inline images given as data. It keeps nothing that makes
the renderer load or run anything: no script, style sheet, frame, object,
form, or SVG; no event handler, ``class``, or ``id``; no URL to fetch,
in ``src`` or in CSS (``url()``); no link unless ``links`` (and then
https and mailto links only, which a PDF shows and never loads). A style
keeps only the properties of ``STYLE_PROPERTIES``, each with a value of
plain words, numbers, and colours, none negative, so text can neither
fetch nor be moved over other text. Comments are dropped.

It is one of two layers. The renderer is the other: Chromium, started
by Gotenberg with JavaScript off and a deny list, refuses what gets
past (docs/documents.md, "Hardening Gotenberg"). A sanitizer reads
HTML with its own parser, which may not read it as the browser does
(mutation XSS); the template around the clean text is the project's own;
and an allowlist changes with releases. Only the browser that would make
a request can refuse it for sure.
"""

import re
from collections.abc import Mapping
from typing import Final

from domainkit.documents.base import missing

TAGS: Final = frozenset(
    {
        *("p", "br", "hr", "div", "span", "blockquote", "pre", "code"),
        *("h1", "h2", "h3", "h4", "h5", "h6"),
        *("strong", "b", "em", "i", "u", "s", "sub", "sup", "small", "mark"),
        *("ul", "ol", "li", "dl", "dt", "dd"),
        *("table", "caption", "colgroup", "col", "thead", "tbody", "tfoot"),
        *("tr", "th", "td", "figure", "figcaption", "img", "bdi", "bdo"),
    }
)
"""The tags kept; any other is dropped, its text kept."""
DROPPED: Final = frozenset(
    {
        *("script", "style", "template", "noscript", "title", "head"),
        *("iframe", "frame", "object", "embed", "svg", "math"),
        *("form", "textarea", "select", "button"),
    }
)
"""Tags dropped with their content."""
ATTRIBUTES: Final[Mapping[str, frozenset[str]]] = {
    "*": frozenset({"dir", "lang", "style"}),
    "td": frozenset({"colspan", "rowspan"}),
    "th": frozenset({"colspan", "rowspan", "scope"}),
    "col": frozenset({"span"}),
    "colgroup": frozenset({"span"}),
    "ol": frozenset({"start", "type"}),
    "img": frozenset({"src", "alt", "width", "height"}),
}
"""The attributes kept, by tag (``*``: on every tag)."""
STYLE_PROPERTIES: Final = frozenset(
    {
        *("color", "background-color", "font-family", "font-size", "font-style"),
        *("font-weight", "text-align", "text-decoration", "text-indent"),
        *("line-height", "letter-spacing", "direction", "unicode-bidi"),
        *("vertical-align", "white-space", "list-style-type", "width", "height"),
        *("margin", "margin-top", "margin-right", "margin-bottom", "margin-left"),
        *("padding", "padding-top", "padding-right", "padding-bottom"),
        *("padding-left", "border", "border-collapse", "border-color"),
        *("border-style", "border-width", "break-before", "break-after"),
        *("break-inside", "page-break-before", "page-break-after"),
    }
)
"""The CSS properties a ``style`` keeps."""
STYLE_VALUE: Final = re.compile(r"[A-Za-z0-9#%.,\s'\"-]{0,200}")
"""A style's value: words, numbers, units, colours, quoted font names."""
COLOUR_FUNCTION: Final = re.compile(r"(?:rgba?|hsla?)\([0-9.,%\s/]*\)", re.IGNORECASE)
NEGATIVE: Final = re.compile(r"(?<![A-Za-z0-9])-\s*[0-9.]")
IMAGE: Final = re.compile(
    r"data:image/(?:png|jpeg|gif|webp);base64,[A-Za-z0-9+/]+={0,2}"
)
"""An image kept: inline data of a raster type (SVG may link)."""
LINK: Final = re.compile(r"(?:https://|mailto:)\S+", re.IGNORECASE)


def clean_html(html: str, *, links: bool = False) -> str:
    """``html``, a fragment of rich text, kept to the module's allowlist.

    The result goes into a template as HTML (unescaped); plain text goes in
    escaped (``html.escape``). ``links`` keeps ``<a href>`` to https and
    mailto URLs.

    Raises:
        ImportError: ``domainkit[documents]`` is not installed.
    """
    try:
        import nh3
    except ImportError as exc:
        raise missing("nh3") from exc
    attributes = {tag: set(names) for tag, names in ATTRIBUTES.items()}
    tags = set(TAGS)
    if links:
        tags.add("a")
        attributes["a"] = {"href"}
    cleaned: str = nh3.clean(
        html,
        tags=tags,
        clean_content_tags=set(DROPPED),
        attributes=attributes,
        attribute_filter=_attribute,
        url_schemes={"data", "https", "mailto"} if links else {"data"},
        url_relative="deny",
        filter_style_properties=set(STYLE_PROPERTIES),
        strip_comments=True,
        link_rel="noopener noreferrer",
    )
    return cleaned


def _attribute(tag: str, name: str, value: str) -> str | None:
    """``value`` as kept, or None to drop the attribute (nh3's ``attribute_filter``)."""
    if name == "src":
        return value if IMAGE.fullmatch(value.strip()) else None
    if name == "href":
        return value if LINK.fullmatch(value.strip()) else None
    if name == "style":
        return _style(value)
    return value


def _style(style: str) -> str | None:
    """The declarations of ``style`` whose property and value are kept, or None."""
    kept = []
    for declaration in style.split(";"):
        prop, colon, value = declaration.partition(":")
        prop, value = prop.strip().lower(), value.strip()
        if colon and prop in STYLE_PROPERTIES and _plain(value):
            kept.append(f"{prop}: {value}")
    return "; ".join(kept) or None


def _plain(value: str) -> bool:
    """Whether a style's value loads nothing and moves nothing back."""
    bare = COLOUR_FUNCTION.sub("", value)
    return (
        bool(value)
        and STYLE_VALUE.fullmatch(bare) is not None
        and NEGATIVE.search(bare) is None
        and value.count('"') % 2 == 0
        and value.count("'") % 2 == 0
    )
