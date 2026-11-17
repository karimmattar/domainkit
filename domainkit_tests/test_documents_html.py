"""``clean_html``: rich text kept to what loads and runs nothing when printed."""

from typing import Any, Final

import pytest

from domainkit.documents import clean_html

PIXEL: Final = (
    "data:image/png;base64,"
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4nGNg"
    "YGD4DwABBAEAX+XDSwAAAABJRU5ErkJggg=="
)


def test_formatting_and_direction_are_kept() -> None:
    """Headings, lists, tables, emphasis, and Arabic written right to left."""
    html = (
        '<h2 dir="rtl" lang="ar">الشروط</h2>'
        '<p dir="rtl" style="text-align: right; color: #333">'
        "يلتزم <strong>المستأجر</strong>"
        " بالدفع <bdi>Salma</bdi>.</p>"
        '<ol start="3"><li><em>one</em></li></ol>'
        '<table><tr><th scope="col">A</th>'
        '<td colspan="2" rowspan="1">B</td></tr></table>'
    )

    assert clean_html(html) == (
        '<h2 dir="rtl" lang="ar">الشروط</h2>'
        '<p dir="rtl" style="text-align:right;color:#333">'
        "يلتزم <strong>المستأجر</strong>"
        " بالدفع <bdi>Salma</bdi>.</p>"
        '<ol start="3"><li><em>one</em></li></ol>'
        '<table><tbody><tr><th scope="col">A</th><td colspan="2" rowspan="1">B</td>'
        "</tr></tbody></table>"
    )


@pytest.mark.parametrize(
    ("html", "cleaned"),
    [
        ("<p>a<script>fetch('https://x')</script>b</p>", "<p>ab</p>"),
        ("<style>@import url(https://x/a.css)</style><p>a</p>", "<p>a</p>"),
        ('<p onclick="x()" class="seal" id="sig">a</p>', "<p>a</p>"),
        ('<img src="https://tracker.example/p.gif" alt="a">', '<img alt="a">'),
        ('<img src="/files/logo.png">', "<img>"),
        ('<img src="data:image/svg+xml;base64,PHN2Zy8+">', "<img>"),
        (f'<img src="{PIXEL}">', f'<img src="{PIXEL}">'),
        ('<iframe src="https://x"></iframe><object data="x">o</object>', ""),
        ('<svg><image href="https://x"/></svg><math><mi>m</mi></math>', ""),
        ('<link rel="stylesheet" href="https://x"><base href="https://x">', ""),
        ('<form action="https://x"><input name="a"><button>go</button></form>', ""),
        ('<video poster="https://x" src="https://y"></video>a', "a"),
        ('<a href="https://example.com">terms</a>', "terms"),
        ("<p>a<!-- comment --></p>", "<p>a</p>"),
        ("<p>&lt;b&gt; &amp; text</p>", "<p>&lt;b&gt; &amp; text</p>"),
    ],
)
def test_nothing_that_loads_or_runs_is_kept(html: str, cleaned: str) -> None:
    """Scripts, style sheets, frames, forms, handlers, and fetched URLs go."""
    assert clean_html(html) == cleaned


@pytest.mark.parametrize(
    ("style", "kept"),
    [
        ("background-image: url(https://x/a.png)", None),
        ("background: red", None),
        ("color: u\\72l(https://x)", None),
        ("color: red; list-style-image: url(x)", "color:red"),
        (
            "font-family: 'Noto Naskh Arabic', serif",
            "font-family:&quot;Noto Naskh Arabic&quot; , serif",
        ),
        ('font-family: "unbalanced', None),
        ("margin-top: -500px", None),
        ("margin: 0 0-500px", None),
        ("margin: 0 0 0 0-500px", None),
        ("text-indent: 0-50em", None),
        ("vertical-align: 900px", None),
        ("vertical-align: Super", "vertical-align:Super"),
        ("line-height: 0", None),
        ("line-height: 0.9", None),
        ("line-height: 90%", None),
        ("line-height: 2em", None),
        ("line-height: 1.5", "line-height:1.5"),
        ("line-height: 120%", "line-height:120%"),
        ("line-height: Normal", "line-height:Normal"),
        ("text-indent: - 2em", None),
        (
            "color: rgb(10, 20, 30); font-weight: bold",
            "color:rgb(10, 20, 30);font-weight:bold",
        ),
        ("color: hsl(120 50% 50% / 0.5)", "color:hsl(120 50% 50% / 0.5)"),
        ("position: absolute; top: 0", None),
        ("height: 0", None),
        ("height: 1px; margin: 0", "margin:0"),
        ("width: 1px", None),
        ("white-space: nowrap", None),
        ("color: expression(alert(1))", None),
        ("color: red !important", None),
        ("COLOR: Red", "color:Red"),
        ("color: red;; :; font-style", "color:red"),
        ("color: " + "a" * 201, None),
    ],
)
def test_a_style_keeps_plain_values_of_listed_properties(
    style: str, kept: str | None
) -> None:
    """No ``url()``, no CSS escape that spells one, nothing negative or placed.

    nh3 writes ``0-500px`` back as ``0 -500px``: the value it writes is the
    one checked. Text is not lifted, nor lines laid over one another.
    """
    attribute = "" if kept is None else f' style="{kept}"'
    assert clean_html(f'<p style="{style}">a</p>') == f"<p{attribute}>a</p>"


def test_links_are_kept_only_when_asked_and_only_https_or_mailto() -> None:
    """A PDF shows a link and never loads it; a relative one has nothing to go to."""
    html = (
        '<a href="https://example.com/terms">t</a>'
        '<a href="mailto:legal@example.com">m</a>'
        '<a href="http://example.com">h</a><a href="javascript:alert(1)">j</a>'
        '<a href="//example.com">p</a><a href="#clause-2">f</a>'
    )

    assert clean_html(html, links=True) == (
        '<a href="https://example.com/terms" rel="noopener noreferrer">t</a>'
        '<a href="mailto:legal@example.com" rel="noopener noreferrer">m</a>'
        '<a rel="noopener noreferrer">h</a><a rel="noopener noreferrer">j</a>'
        '<a rel="noopener noreferrer">p</a><a rel="noopener noreferrer">f</a>'
    )
    assert clean_html(html) == "tmhjpf"


# A narrower allowlist -----------------------------------------------------------------


def test_an_editor_that_needs_less_keeps_less() -> None:
    """No style, no image: text, structure, direction, and alignment by class.

    Text cannot be made tiny or the page's colour; a class not listed, or
    on a tag it is not listed for, goes.
    """
    html = (
        '<p class="ql-align-right ql-size-huge" dir="rtl" style="font-size: 1px">'
        f'عقد <img src="{PIXEL}" alt="seal"><span class="ql-align-right">x</span></p>'
        '<h2 class="ql-align-center ql-align-center">T</h2><li class="other">i</li>'
    )

    cleaned = clean_html(
        html,
        styles=False,
        images=False,
        classes={"p": {"ql-align-right"}, "*": {"ql-align-center"}},
    )

    assert cleaned == (
        '<p class="ql-align-right" dir="rtl">عقد <span>x</span></p>'
        '<h2 class="ql-align-center">T</h2><li>i</li>'
    )
    assert clean_html('<img src="https://x/a.png">', images=False) == ""


def test_some_style_properties_may_be_kept() -> None:
    """A set of ``STYLE_PROPERTIES`` keeps those, with the same checks of value."""
    html = '<p style="text-align: right; color: white; direction: url(x)">a</p>'

    assert clean_html(html, styles={"Text-Align", "direction"}) == (
        '<p style="text-align:right">a</p>'
    )
    assert clean_html(html, styles=set()) == "<p>a</p>"


@pytest.mark.parametrize(
    ("narrower", "message"),
    [
        ({"styles": {"color", "position", "width"}}, "never more: not position, width"),
        ({"classes": {"script": {"a"}}}, "tags that are not kept: script"),
        ({"classes": {"img": {"a"}}, "images": False}, "not kept: img"),
        ({"classes": {"a": {"a"}}}, "not kept: a"),
        ({"classes": {"p": {"a b", "1x", "ok"}}}, "not '1x', 'a b'"),
    ],
)
def test_never_more_than_the_allowlist(narrower: dict[str, Any], message: str) -> None:
    """A property beyond the list, a tag not kept, or a class that is not a name."""
    with pytest.raises(ValueError, match=message):
        clean_html("<p>a</p>", **narrower)


def test_a_class_is_kept_on_a_link_only_when_links_are() -> None:
    """``a`` is a tag kept once ``links`` is on."""
    html = '<a class="mention" href="https://example.com">t</a>'

    assert clean_html(html, links=True, classes={"a": {"mention"}}) == (
        '<a class="mention" href="https://example.com" rel="noopener noreferrer">t</a>'
    )
