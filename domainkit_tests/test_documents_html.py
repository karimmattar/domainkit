"""``clean_html``: rich text kept to what loads and runs nothing when printed."""

from typing import Final

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
        ("color: expression(alert(1))", None),
        ("color: red !important", None),
        ("COLOR: Red", "color:Red"),
        ("width: 50%;; :; font-style", "width:50%"),
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
