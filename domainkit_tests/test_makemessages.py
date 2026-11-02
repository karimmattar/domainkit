"""``makemessages``: catalogs kept up to date with the messages code marks."""

import io
import re
from collections.abc import Iterator
from pathlib import Path

import pytest

from domainkit.apps import registry
from domainkit.conf import override_settings
from domainkit.i18n.catalogs import Message, compile_po, parse_po
from domainkit.i18n.extraction import Found, extract, merge, render
from domainkit.management import call_command
from domainkit.management.base import CommandError
from domainkit_tests.conftest import ALPHA, MakePackage

CODE = """
from domainkit.i18n import translation
from domainkit.i18n.translation import gettext as _, gettext_lazy, ngettext, pgettext

TITLE = gettext_lazy("Orders")
SAME = _("Orders")


def describe(count: int, name: str) -> str:
    built = _(f"Hello {name}")  # Built at run time: not a message.
    return (
        ngettext("{n} order", "{n} orders", count)
        + pgettext("verb", "Order")
        + translation.npgettext("menu", "Line", "Lines", count)
        + _("Say \\"hi\\"\\nthen go")
        + _(name)
        + _("")
        + built
    )
"""


@pytest.fixture
def domain(make_package: MakePackage) -> Iterator[Path]:
    """An installed domain whose code marks messages."""
    root = make_package("shop.sales", {"messages.py": CODE, "broken/__init__.py": ""})
    registry.clear()
    with override_settings(INSTALLED_DOMAINS=[ALPHA, "shop.sales"]):
        yield root
    registry.clear()


def run(*args: str) -> str:
    """Run ``makemessages`` and return what it printed."""
    output = io.StringIO()
    call_command("makemessages", *args, stdout=output)
    return output.getvalue()


def test_literal_messages_are_found_with_where_they_are_used(domain: Path) -> None:
    """Literal strings to the marking functions; context and plurals kept apart."""
    found = extract(domain.rglob("*.py"), domain)

    assert [(each.context, each.msgid, each.plural) for each in found] == [
        (None, "Orders", None),
        (None, "{n} order", "{n} orders"),
        ("verb", "Order", None),
        ("menu", "Line", "Lines"),
        (None, 'Say "hi"\nthen go', None),
    ]
    assert found[0].references == ["messages.py:5", "messages.py:6"]


def test_a_catalog_is_started_then_kept_up_to_date(domain: Path) -> None:
    """New messages empty, translations kept, gone ones kept as obsolete."""
    catalog = domain / "locale" / "ar" / "LC_MESSAGES" / "messages.po"

    assert run("-l", "ar") == f"{catalog}: 5 messages, 5 to translate.\n"
    started = parse_po(catalog.read_text("utf-8"))
    assert started[0].msgid == ""
    assert "Language: ar" in started[0].msgstr[0]
    assert [each.msgstr for each in started if each.plural] == [("", ""), ("", "")]
    compile_po(catalog.read_text("utf-8"))  # A catalog compilemessages takes.

    translated = catalog.read_text("utf-8").replace(
        'msgid "Orders"\nmsgstr ""', 'msgid "Orders"\nmsgstr "الطلبات"'
    )
    catalog.write_text(translated + '\nmsgid "Gone"\nmsgstr "ذهب"\n', "utf-8")
    assert run("--all") == f"{catalog}: 5 messages, 4 to translate.\n"

    text = catalog.read_text("utf-8")
    assert 'msgid "Orders"\nmsgstr "الطلبات"' in text
    assert '#~ msgid "Gone"\n#~ msgstr "ذهب"' in text
    assert run("--check") == "Every catalog is up to date with its code.\n"


def test_check_fails_on_a_catalog_behind_its_code(domain: Path) -> None:
    """Nothing is written; the catalog that is behind is named."""
    catalog = domain / "locale" / "fr" / "LC_MESSAGES" / "messages.po"
    run("-l", "fr")
    (domain / "more.py").write_text('from x import _\nNEW = _("New")\n', "utf-8")
    before = catalog.read_text("utf-8")

    with pytest.raises(CommandError, match=re.escape(f"Behind their code: {catalog}")):
        run("--check", "--all", "sales")
    with pytest.raises(CommandError, match="Behind their code"):
        run("--check", "-l", "de")

    assert catalog.read_text("utf-8") == before
    assert not (domain / "locale" / "de").exists()


def test_what_cannot_be_done_is_refused(domain: Path) -> None:
    """No locale, an unknown domain, a module that does not parse."""
    with pytest.raises(CommandError, match="Name a locale"):
        run()
    with pytest.raises(CommandError, match="No installed domain is labelled"):
        run("-l", "ar", "nowhere")
    (domain / "bad.py").write_text("def (:\n", "utf-8")
    with pytest.raises(CommandError, match=r"bad\.py:1"):
        run("-l", "ar")


def test_a_catalog_that_cannot_be_read_is_named(domain: Path) -> None:
    """Its path and line, as compilemessages names them."""
    catalog = domain / "locale" / "ar" / "LC_MESSAGES" / "messages.po"
    catalog.parent.mkdir(parents=True)
    catalog.write_text('msgstr "orphan"\n', "utf-8")

    with pytest.raises(CommandError, match=r"messages\.po: line 1"):
        run("-l", "ar")


def test_plural_forms_follow_the_header_and_flags_stay() -> None:
    """A new plural gets the header's forms (two without one); fuzzy stays fuzzy."""
    six = Message("", ("Plural-Forms: nplurals=6; plural=n%6;\n",))
    odd = Message("", ("Plural-Forms: nplurals=many;\n",))
    lines = Found("Line", "Lines", references=["m.py:1"])
    rough = Message("Hi", ("أهلًا",), fuzzy=True)

    with_six, _ = merge([six], [lines])
    without, _ = merge([], [lines])
    with_odd, _ = merge([odd], [lines])
    kept, gone = merge([six, rough], [Found("Hi", references=["m.py:2"])])

    assert with_six[1][0].msgstr == ("",) * 6
    assert without[0][0].msgstr == ("", "")
    assert with_odd[1][0].msgstr == ("", "")
    assert kept[1][0].fuzzy
    assert gone == []
    assert '#: m.py:2\n#, fuzzy\nmsgid "Hi"' in render(kept, gone)
