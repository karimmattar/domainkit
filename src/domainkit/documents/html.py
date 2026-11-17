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
plain words, numbers, and colours, none negative; ``vertical-align``
keeps keywords only, and ``line-height`` is at least 1 (or 100%). No
``width``, ``height``, or ``white-space`` is kept: a box made too small
for its text lets the text run over the next. So a style fetches
nothing, and keeps out what most often lays text over text. nh3 writes
a style back as its CSS parser read it, so the text is cleaned twice,
and the second pass checks what the first wrote. Comments are dropped.

Not stopped: text made tiny or the colour of the page, and large text
(a big font, wide spacing) in a box the template made narrow, which
runs over its neighbour. Show a preview of what is signed.

An editor that needs less keeps less: ``styles=False`` keeps no
``style``, or a set keeps those of ``STYLE_PROPERTIES`` only;
``images=False`` keeps no image. Never more: a property beyond
``STYLE_PROPERTIES`` is refused. ``classes`` keeps the class names it
lists, by tag (``*``: every tag kept), and no other: a class loads and
runs nothing, and means only what the template's style sheet says, so
list the ones whose rules move no text over other text (an editor's
alignment, ``ql-align-right``).

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
from collections.abc import Set as AbstractSet
from functools import partial
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
        *("vertical-align", "list-style-type"),
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
NEGATIVE: Final = re.compile(r"(?<![A-Za-z])-")
"""A minus sign that is not inside a word: ``0-500px`` is read as ``0 -500px``."""
KEYWORDS: Final[Mapping[str, frozenset[str]]] = {
    "vertical-align": frozenset(
        {
            "baseline",
            "sub",
            "super",
            "text-top",
            "text-bottom",
            "middle",
            "top",
            "bottom",
        }
    ),
}
"""Properties that keep keywords only: a length would lift text over other text."""
LINE_HEIGHT: Final = re.compile(r"([0-9]{1,4}(?:\.[0-9]{1,4})?)(%?)")
"""A line height without a unit, or in percent: at least 1, or 100%, or ``normal``."""
IMAGE: Final = re.compile(
    r"data:image/(?:png|jpeg|gif|webp);base64,[A-Za-z0-9+/]+={0,2}"
)
"""An image kept: inline data of a raster type (SVG may link)."""
LINK: Final = re.compile(r"(?:https://|mailto:)\S+", re.IGNORECASE)
CLASS_NAME: Final = re.compile(r"-?[A-Za-z_][A-Za-z0-9_-]{0,99}")
"""A class name kept: a plain CSS identifier."""


def clean_html(
    html: str,
    *,
    links: bool = False,
    styles: bool | AbstractSet[str] = True,
    images: bool = True,
    classes: Mapping[str, AbstractSet[str]] | None = None,
) -> str:
    """``html``, a fragment of rich text, kept to the module's allowlist, or less.

    The result goes into a template as HTML (unescaped); plain text goes in
    escaped (``html.escape``). ``links`` keeps ``<a href>`` to https and
    mailto URLs. ``styles`` keeps the ``STYLE_PROPERTIES`` (True), none
    (False), or those of a set. ``images`` keeps inline images. ``classes``
    keeps, by tag (``*``: every tag kept), the class names it lists.

    Raises:
        ImportError: ``domainkit[documents]`` is not installed.
        TypeError: ``styles``, or a tag's ``classes``, is one string, not a set.
        ValueError: ``styles`` names a property beyond ``STYLE_PROPERTIES``,
            or ``classes`` a tag not kept or a name that is no CSS identifier.
    """
    try:
        import nh3
    except ImportError as exc:
        raise missing("nh3") from exc
    properties = _properties(styles)
    attributes = {tag: set(names) for tag, names in ATTRIBUTES.items()}
    tags = set(TAGS)
    if not properties:
        attributes["*"].discard("style")
    if not images:
        tags.discard("img")
        del attributes["img"]
    if links:
        tags.add("a")
        attributes["a"] = {"href"}
    kept = _classes(classes or {}, tags)
    cleaned = html
    # Twice: nh3 writes a style back as its CSS parser read it (``0-500px``
    # as ``0 -500px``), so the second pass checks what the first one wrote.
    for _ in range(2):
        cleaned = nh3.clean(
            cleaned,
            tags=tags,
            clean_content_tags=set(DROPPED),
            attributes=attributes,
            attribute_filter=partial(_attribute, properties, kept),
            url_schemes=({"data"} if images else set())
            | ({"https", "mailto"} if links else set()),
            url_relative="deny",
            allowed_classes=kept or None,
            filter_style_properties=set(properties) or None,
            strip_comments=True,
            link_rel="noopener noreferrer",
        )
    return cleaned


def _properties(styles: bool | AbstractSet[str]) -> frozenset[str]:
    """The style properties kept.

    Raises:
        TypeError: ``styles`` is one string, whose letters would be read.
        ValueError: ``styles`` names one beyond ``STYLE_PROPERTIES``.
    """
    if isinstance(styles, bool):
        return STYLE_PROPERTIES if styles else frozenset()
    if isinstance(styles, str):
        raise TypeError(
            f"styles is True, False, or a set of properties, not the string "
            f"{styles!r}: write {{{styles!r}}}."
        )
    asked = frozenset(prop.strip().lower() for prop in styles)
    if beyond := asked - STYLE_PROPERTIES:
        raise ValueError(
            "styles keeps some of the properties clean_html keeps, never more: "
            f"not {', '.join(sorted(beyond))}."
        )
    return asked


def _classes(
    classes: Mapping[str, AbstractSet[str]], tags: AbstractSet[str]
) -> dict[str, set[str]]:
    """The class names kept by tag, ``*`` spread over every tag kept.

    Raises:
        TypeError: A tag's names are one string, whose letters would be read.
        ValueError: A tag is not kept, or a name is no CSS identifier.
    """
    for tag, names in classes.items():
        if isinstance(names, str):
            raise TypeError(
                f"classes maps a tag to a set of names, not the string {names!r}: "
                f"write {{{tag!r}: {{{names!r}}}}}."
            )
    if unknown := set(classes) - set(tags) - {"*"}:
        raise ValueError(
            f"classes names tags that are not kept: {', '.join(sorted(unknown))}."
        )
    for names in classes.values():
        if odd := sorted(name for name in names if not CLASS_NAME.fullmatch(name)):
            raise ValueError(
                f"A class is a CSS identifier: not {', '.join(map(repr, odd))}."
            )
    every = set(classes.get("*", ()))
    kept = {tag: every | set(classes.get(tag, ())) for tag in tags}
    return {tag: names for tag, names in kept.items() if names}


def _attribute(
    properties: AbstractSet[str],
    classes: Mapping[str, AbstractSet[str]],
    tag: str,
    name: str,
    value: str,
) -> str | None:
    """``value`` as kept, or None to drop the attribute (nh3's ``attribute_filter``).

    ``properties`` and ``classes`` are those ``clean_html`` keeps.
    """
    if name == "src":
        return value if IMAGE.fullmatch(value.strip()) else None
    if name == "href":
        return value if LINK.fullmatch(value.strip()) else None
    if name == "style":
        return _style(value, properties)
    if name == "class":
        kept = [each for each in value.split() if each in classes.get(tag, ())]
        return " ".join(dict.fromkeys(kept)) or None
    return value


def _style(style: str, properties: AbstractSet[str]) -> str | None:
    """The declarations of ``style`` whose property and value are kept, or None."""
    kept = []
    for declaration in style.split(";"):
        prop, colon, value = declaration.partition(":")
        prop, value = prop.strip().lower(), value.strip()
        if colon and prop in properties and _plain(prop, value):
            kept.append(f"{prop}: {value}")
    return "; ".join(kept) or None


def _plain(prop: str, value: str) -> bool:
    """Whether a style's value loads nothing and moves no text over other text."""
    if prop in KEYWORDS:
        return value.lower() in KEYWORDS[prop]
    if prop == "line-height" and value.lower() != "normal":
        number = LINE_HEIGHT.fullmatch(value)
        return number is not None and float(number[1]) >= (100 if number[2] else 1)
    bare = COLOUR_FUNCTION.sub("", value)
    return (
        bool(value)
        and STYLE_VALUE.fullmatch(bare) is not None
        and NEGATIVE.search(bare) is None
        and value.count('"') % 2 == 0
        and value.count("'") % 2 == 0
    )
