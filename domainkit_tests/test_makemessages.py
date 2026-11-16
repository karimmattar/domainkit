"""``makemessages``: catalogs kept up to date with the messages code marks."""

import io
import re
from collections.abc import Iterator
from gettext import GNUTranslations
from pathlib import Path

import pytest

from domainkit.apps import registry
from domainkit.conf import override_settings
from domainkit.i18n import gettext, override
from domainkit.i18n.catalogs import Message, compile_po, parse_po
from domainkit.i18n.extraction import Found, added, extract, merge, render
from domainkit.i18n.translation import reset_catalogs
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


DOMAIN_LAYER = """
from domainkit.ddd import BusinessRuleViolation, TextChoices
from domainkit.i18n.lazy import gettext_lazy, gettext_noop


class Status(TextChoices):
    PAID = "paid", gettext_lazy("Paid in full")


def pay() -> None:
    raise BusinessRuleViolation(gettext_noop("Already paid."))
"""
"""A domain layer marks its labels and refusals (user report I2)."""


@pytest.fixture
def domain(make_package: MakePackage) -> Iterator[Path]:
    """An installed domain whose code marks messages, in the project's package.

    The project's settings and root router are ``shop.settings`` and
    ``shop.urls``, which mark nothing until a test writes them.
    """
    root = make_package(
        "shop.sales",
        {"messages.py": CODE, "broken/__init__.py": "", "domain.py": DOMAIN_LAYER},
    )
    for name in ("settings.py", "urls.py"):
        (root.parent / name).write_text("", "utf-8")
    registry.clear()
    with override_settings(
        INSTALLED_DOMAINS=[ALPHA, "shop.sales"],
        SETTINGS_MODULE="shop.settings",
        ROOT_ROUTER="shop.urls.router",
        PROJECT_NAME="Shop",
    ):
        yield root
    registry.clear()


def run(*args: str, stderr: io.StringIO | None = None) -> str:
    """Run ``makemessages`` and return what it printed."""
    output = io.StringIO()
    call_command("makemessages", *args, stdout=output, stderr=stderr)
    return output.getvalue()


def test_literal_messages_are_found_with_where_they_are_used(domain: Path) -> None:
    """Literal strings to the marking functions; context and plurals kept apart."""
    found = extract(domain.rglob("*.py"), domain)

    assert [(each.context, each.msgid, each.plural) for each in found] == [
        (None, "Paid in full", None),
        (None, "Already paid.", None),
        (None, "Orders", None),
        (None, "{n} order", "{n} orders"),
        ("verb", "Order", None),
        ("menu", "Line", "Lines"),
        (None, 'Say "hi"\nthen go', None),
    ]
    assert found[0].references == ["domain.py:7"]
    assert found[1].references == ["domain.py:11"]
    assert found[2].references == ["messages.py:5", "messages.py:6"]


def test_a_catalog_is_started_then_kept_up_to_date(domain: Path) -> None:
    """New messages empty, translations kept, gone ones kept as obsolete."""
    catalog = domain / "locale" / "ar" / "LC_MESSAGES" / "messages.po"

    assert run("-l", "ar") == f"{catalog}: 7 messages, 7 to translate.\n"
    started = parse_po(catalog.read_text("utf-8"))
    assert started[0].msgid == ""
    assert "Language: ar" in started[0].msgstr[0]
    assert [each.msgstr for each in started if each.plural] == [("", ""), ("", "")]
    compile_po(catalog.read_text("utf-8"))  # A catalog compilemessages takes.

    translated = catalog.read_text("utf-8").replace(
        'msgid "Orders"\nmsgstr ""', 'msgid "Orders"\nmsgstr "الطلبات"'
    )
    catalog.write_text(translated + '\nmsgid "Gone"\nmsgstr "ذهب"\n', "utf-8")
    assert run("--all") == f"{catalog}: 7 messages, 6 to translate.\n"

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


# The project's own texts (user report I6) ---------------------------------------------

SETTINGS = """
from domainkit.i18n.lazy import gettext_noop

CIBA = {"PUSH_TITLE": gettext_noop("Approve?")}
"""
URLS = """
from domainkit.i18n import gettext


def refuse() -> str:
    return gettext("No shop has that name.") + gettext("Orders")
"""
HELD = """\
msgid ""
msgstr ""
"Language: ar\\n"
"Content-Type: text/plain; charset=UTF-8\\n"

# Our own word for the framework's message.
msgid "The request is invalid."
msgstr "طلبك غير صالح."
"""
"""A ``LOCALE_PATHS`` catalog a project wrote by hand: it rewords a message."""


def mark_the_project(domain: Path) -> None:
    """Mark texts in the project's settings and root router's module."""
    (domain.parent / "settings.py").write_text(SETTINGS, "utf-8")
    (domain.parent / "urls.py").write_text(URLS, "utf-8")


def test_the_projects_own_texts_go_to_the_first_locale_path(
    domain: Path, tmp_path: Path
) -> None:
    """The settings' package and the root router's, but its domains: one catalog."""
    mark_the_project(domain)
    locale = tmp_path / "locale"
    catalog = locale / "ar" / "LC_MESSAGES" / "messages.po"
    sales = domain / "locale" / "ar" / "LC_MESSAGES" / "messages.po"

    with override_settings(LOCALE_PATHS=[locale, tmp_path / "later"]):
        printed = run("-l", "ar")

    assert printed.splitlines() == [
        f"{sales}: 7 messages, 7 to translate.",
        f"{catalog}: 3 messages, 3 to translate.",
    ]
    text = catalog.read_text("utf-8")
    entries = parse_po(text)
    assert [each.msgid for each in entries] == [
        "",
        "Approve?",
        "No shop has that name.",
        "Orders",
    ]
    assert "Project-Id-Version: Shop" in entries[0].msgstr[0]
    assert '#: shop/settings.py:4\nmsgid "Approve?"' in text
    assert '#: shop/urls.py:6\nmsgid "Orders"' in text
    assert not (tmp_path / "later").exists()
    assert "Approve?" not in sales.read_text("utf-8")


def test_an_obsolete_translation_comes_back(tmp_path: Path) -> None:
    """A ``#~`` entry of a message marked again comes back, as ``updated`` does.

    The other obsolete entries follow the new ones; one whose message
    changed to plural stays obsolete.
    """
    catalog = tmp_path / "messages.po"
    gone = '#~ msgid "Revived"\n#~ msgstr "عاد"\n\n#~ msgid "Gone"\n#~ msgstr "ذهب"\n'
    catalog.write_text(f'{HELD}\n{gone}\n#~ msgid "Line"\n#~ msgstr "سطر"\n', "utf-8")
    found = [
        Found("Revived", references=["config/urls.py:3"]),
        Found("Line", "Lines", references=["config/urls.py:4"]),
    ]

    text, untranslated = added(catalog, found, project="Shop", language="ar")

    assert untranslated == 1
    assert text.startswith(HELD)
    assert text.endswith(
        '#: config/urls.py:3\nmsgid "Revived"\nmsgstr "عاد"\n\n'
        '#: config/urls.py:4\nmsgid "Line"\nmsgid_plural "Lines"\n'
        'msgstr[0] ""\nmsgstr[1] ""\n\n'
        '#~ msgid "Gone"\n#~ msgstr "ذهب"\n\n#~ msgid "Line"\n#~ msgstr "سطر"\n'
    )
    compiled = GNUTranslations(io.BytesIO(compile_po(text)))
    assert compiled.gettext("Revived") == "عاد"
    assert added(catalog, found[:1], project="Shop", language="ar")[0] == (
        f'{HELD}\n#: config/urls.py:3\nmsgid "Revived"\nmsgstr "عاد"\n\n'
        '#~ msgid "Gone"\n#~ msgstr "ذهب"\n\n#~ msgid "Line"\n#~ msgstr "سطر"\n'
    )


def test_a_locale_path_catalog_keeps_what_it_holds_and_is_read(
    domain: Path, tmp_path: Path
) -> None:
    """New texts are added; rewordings of others' texts stay; gettext reads both.

    Nothing in that catalog is made obsolete: it also holds the project's
    words for the framework's and the domains' messages, which no code of
    the project marks.
    """
    mark_the_project(domain)
    locale = tmp_path / "locale"
    catalog = locale / "ar" / "LC_MESSAGES" / "messages.po"
    catalog.parent.mkdir(parents=True)
    held = HELD
    catalog.write_text(held, "utf-8")

    with override_settings(LOCALE_PATHS=[locale]):
        run("-l", "ar", "--all")
        text = catalog.read_text("utf-8")
        assert text.startswith(held)
        translated = text.replace(
            'msgid "No shop has that name."\nmsgstr ""',
            'msgid "No shop has that name."\nmsgstr "لا يوجد متجر بهذا الاسم."',
        )
        catalog.write_text(translated, "utf-8")
        assert run("--all").endswith(f"{catalog}: 3 messages, 2 to translate.\n")
        assert catalog.read_text("utf-8") == translated
        assert run("--check") == "Every catalog is up to date with its code.\n"
        call_command("compilemessages", stdout=io.StringIO())
        reset_catalogs()
        with override("ar"):
            assert gettext("No shop has that name.") == "لا يوجد متجر بهذا الاسم."
            assert gettext("The request is invalid.") == "طلبك غير صالح."
        reset_catalogs()

        (domain.parent / "urls.py").write_text('_("New")\n', "utf-8")
        with pytest.raises(
            CommandError, match=re.escape(f"Behind their code: {catalog}")
        ):
            run("--check")
        assert catalog.read_text("utf-8") == translated


def test_texts_outside_every_domain_need_a_locale_path(domain: Path) -> None:
    """Without ``LOCALE_PATHS``, they are named: ``--check`` fails, a run says so."""
    mark_the_project(domain)
    run("-l", "ar")
    noted = io.StringIO()

    printed = run("-l", "ar", stderr=noted)

    assert printed.startswith(f"{domain / 'locale'}")
    assert noted.getvalue() == (
        "3 messages are marked outside every domain (shop/settings.py, "
        "shop/urls.py), and LOCALE_PATHS names no directory to keep them: "
        "set it, then run makemessages.\n"
    )
    with pytest.raises(CommandError, match=r"^3 messages are marked outside"):
        run("--check")
    assert run("--check", "sales") == "Every catalog is up to date with its code.\n"


def test_texts_a_catalog_translates_need_no_locale_path(domain: Path) -> None:
    """The framework's catalog translates them: nothing is named, ``--check`` passes.

    With their context and plural forms, in any language of ``LANGUAGES``;
    ``{n} order`` no compiled catalog has.
    """
    (domain.parent / "urls.py").write_text(
        'from x import _, ngettext, pgettext\n_("Not Found")\n'
        'pgettext("code", "enroll your phone")\n'
        'ngettext("{count} day", "{count} days", 2)\n'
        'ngettext("{n} order", "{n} orders", 2)\n',
        "utf-8",
    )
    run("-l", "ar")
    noted = io.StringIO()

    run("-l", "ar", stderr=noted)

    assert noted.getvalue() == (
        "1 message is marked outside every domain (shop/urls.py), and "
        "LOCALE_PATHS names no directory to keep them: set it, then run "
        "makemessages.\n"
    )
    (domain.parent / "urls.py").write_text('_("Not Found")\n', "utf-8")
    assert run("--check") == "Every catalog is up to date with its code.\n"


def test_a_root_router_named_with_a_colon_is_read(domain: Path, tmp_path: Path) -> None:
    """``ROOT_ROUTER = "urls_alone:router"``: a module outside any package."""
    (tmp_path / "urls_alone.py").write_text('_("Routed")\n', "utf-8")
    locale = tmp_path / "locale"

    with override_settings(
        SETTINGS_MODULE="", ROOT_ROUTER="urls_alone:router", LOCALE_PATHS=[locale]
    ):
        run("-l", "ar")

    text = (locale / "ar" / "LC_MESSAGES" / "messages.po").read_text("utf-8")
    assert '#: urls_alone.py:1\nmsgid "Routed"' in text


@pytest.mark.parametrize(
    ("settings_module", "router"),
    [
        ("", None),
        ("domainkit_tests.sample_domains.alpha.settings", "shop.sales.urls.router"),
        ("nowhere_at_all", "nowhere.deep.urls.router"),
        ("shop.sales.settings", "domainkit.http.health.router"),
    ],
)
def test_only_the_projects_own_modules_are_read(
    domain: Path,
    make_package: MakePackage,
    tmp_path: Path,
    settings_module: str,
    router: str | None,
) -> None:
    """No module, a domain's, the framework's, or one not there: nothing to read."""
    (domain / "urls.py").write_text('_("In the domain")\n', "utf-8")
    with override_settings(
        SETTINGS_MODULE=settings_module, ROOT_ROUTER=router, LOCALE_PATHS=[tmp_path]
    ):
        run("-l", "ar")
        assert run("--check") == "Every catalog is up to date with its code.\n"

    assert not (tmp_path / "ar").exists()


def test_a_module_outside_any_package_is_read_alone(
    domain: Path, tmp_path: Path
) -> None:
    """``settings.py`` at the top of the path: that file, and nothing beside it."""
    (tmp_path / "settings_alone.py").write_text('_("Alone")\n', "utf-8")
    (tmp_path / "beside.py").write_text('_("Beside")\n', "utf-8")
    locale = tmp_path / "locale"
    with override_settings(
        SETTINGS_MODULE="settings_alone", ROOT_ROUTER=None, LOCALE_PATHS=[locale]
    ):
        run("-l", "ar")

    text = (locale / "ar" / "LC_MESSAGES" / "messages.po").read_text("utf-8")
    assert '#: settings_alone.py:1\nmsgid "Alone"' in text
    assert "Beside" not in text
