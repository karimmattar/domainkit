"""Catalogs: reading ``.po``, compiling ``.mo``, the shipped ones, and the command."""

import ast
import gettext
import io
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Final

import pytest
from pydantic_core._pydantic_core import list_all_errors

from domainkit import setup
from domainkit.apps import registry
from domainkit.conf import override_settings
from domainkit.i18n.catalogs import (
    MAGIC,
    CatalogError,
    Message,
    check,
    compile_mo,
    compile_po,
    header,
    parse_po,
    translated,
)
from domainkit.i18n.translation import LOCALE_DIR
from domainkit.management import call_command
from domainkit.management.base import CommandError
from domainkit.money import CurrencyMismatch, InexactAmount, MoneyError, UnknownCurrency
from domainkit_tests.conftest import ALPHA, MakePackage

SRC: Final = Path(__file__).parents[1] / "src" / "domainkit"
HEADER: Final = (
    'msgid ""\nmsgstr ""\n"Content-Type: text/plain; charset=UTF-8\\n"\n'
    '"Plural-Forms: nplurals=2; plural=(n != 1);\\n"\n\n'
)


def translations(text: str) -> gettext.GNUTranslations:
    """What gettext reads from the compiled catalog."""
    return gettext.GNUTranslations(io.BytesIO(compile_po(text)))


# Reading .po files -------------------------------------------------------------------


def test_entries_are_read_with_their_parts() -> None:
    """Strings across lines, escapes, contexts, plurals, flags, comments."""
    messages = parse_po(
        HEADER
        + "# A translator's comment.\n"
        + "#: src/app.py:12\n"
        + 'msgid ""\n"Hello, "\n"world"\nmsgstr "Bonjour, \\"monde\\"\\n\\t\\\\\\r"\n\n'
        + '#, python-format, fuzzy\nmsgid "Draft"\nmsgstr "Brouillon"\n\n'
        + '#~ msgid "Gone"\n#~ msgstr "Parti"\n\n'
        + 'msgctxt "menu"\nmsgid "Open"\nmsgstr "Ouvrir"\n\n'
        + 'msgid "{n} file"\nmsgid_plural "{n} files"\nmsgstr[0] "{n} fichier"\n'
        + 'msgstr[1] "{n} fichiers"\n\n'
        + 'msgid "Untranslated"\nmsgstr ""\n'
    )

    assert [m.msgid for m in messages] == [
        "",
        "Hello, world",
        "Draft",
        "Open",
        "{n} file",
        "Untranslated",
    ]
    hello, draft, menu, files, untranslated = messages[1:]
    assert hello == Message("Hello, world", ('Bonjour, "monde"\n\t\\\r',), line=8)
    assert (draft.fuzzy, draft.translated, draft.line) == (True, False, 14)
    assert (menu.context, menu.msgstr, menu.fuzzy) == ("menu", ("Ouvrir",), False)
    assert (files.plural, files.msgstr) == (
        "{n} files",
        ("{n} fichier", "{n} fichiers"),
    )
    assert untranslated.translated is False
    assert [m.msgid for m in translated(messages)] == [
        "Hello, world",
        "Open",
        "{n} file",
    ]
    assert parse_po("") == parse_po("# Nothing yet.\n\n") == []


def test_flags_and_strings_over_several_lines() -> None:
    """Flags in any order or spacing, never in comments; continued strings."""
    messages = parse_po(
        HEADER
        + '#, fuzzy, python-format\nmsgid "a"\nmsgstr "x"\n\n'
        + '#,fuzzy\nmsgid "b"\nmsgstr "y"\n\n'
        + '# fuzzy, says a translator\nmsgid "c"\nmsgstr "z"\n\n'
        + 'msgctxt ""\n"me"\n"nu"\nmsgid "{n} file"\nmsgid_plural ""\n"{n} "\n'
        + '"files"\nmsgstr[0] ""\n"{n} fi"\n"chier"\nmsgstr[1] "{n} fichiers"\n'
    )

    a, b, c, files = messages[1:]
    assert (a.fuzzy, b.fuzzy, c.fuzzy) == (True, True, False)
    assert (files.context, files.plural, files.msgstr) == (
        "menu",
        "{n} files",
        ("{n} fichier", "{n} fichiers"),
    )


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ('msgstr "x"\n', "line 1: msgstr before msgid"),
        ('msgid_plural "x"\n', "line 1: msgid_plural before msgid"),
        ('"x"\n', "line 1: cannot read"),
        ('msgid "a"\nmsgstr "b"\nnonsense\n', "^line 3: cannot read 'nonsense'$"),
        (
            'msgid "a"\nmsgstr "b"\n' + "n" * 50 + "\n",
            "^line 3: cannot read '" + "n" * 40 + "'$",
        ),
        ('msgid ""\n"a\\q"\nmsgstr ""\n', r"^line 2: unknown escape \\q"),
        ('msgid "a"\nmsgstr "b" "c"\n', "line 2: a string must be in double quotes"),
        ("msgid 'a'\n", "line 1: cannot read"),
        ('msgid "a\\q"\nmsgstr ""\n', "line 1: unknown escape \\\\q"),
        ('msgid "a"\nmsgid_plural "b"\nmsgstr "c"\n', "line 1: msgid_plural needs"),
        ('msgid "a"\nmsgid_plural "b"\nmsgstr[1] "c"\n', "line 1: msgid_plural needs"),
        ('msgid "a"\nmsgstr[0] "c"\n', "line 1: msgid needs one msgstr"),
        ('msgid "a"\nmsgid "b"\nmsgstr ""\n', "line 1: msgid needs one msgstr"),
        ('msgid "a"\n', "line 1: msgid needs one msgstr"),
        ('msgctxt "x"\nmsgstr "y"\n', "line 1: an entry without msgid"),
        ('msgid "a"\nmsgstr "b"\nmsgstr "c"\n', "line 3: msgstr twice"),
        (
            'msgid "a"\nmsgid_plural "b"\nmsgstr[0] "c"\nmsgstr[0] "d"\n',
            r"line 4: msgstr\[0\] twice",
        ),
        (
            (
                'msgid "a"\nmsgstr "b"\n\nmsgctxt "x"\nmsgid "a"\nmsgstr "c"\n\n'
                'msgid "a"\nmsgstr "d"\n'
            ),
            "^line 8: 'a' is also on line 1$",
        ),
        (
            'msgid "'
            + "a" * 50
            + '"\nmsgstr ""\n\nmsgid "'
            + "a" * 50
            + '"\nmsgstr ""\n',
            "^line 4: '" + "a" * 40 + "' is also on line 1$",
        ),
    ],
)
def test_malformed_catalogs_name_the_line(text: str, message: str) -> None:
    """Every refusal says where the problem is."""
    with pytest.raises(CatalogError, match=message):
        parse_po(text)


def test_headers_must_be_present_reviewed_and_utf8() -> None:
    """The header gives the charset and plural rule gettext needs."""
    with pytest.raises(CatalogError, match=r'^the catalog has no header \(msgid ""\)$'):
        header(parse_po('msgid "a"\nmsgstr "b"\n'))
    with pytest.raises(CatalogError, match="line 2: the header is fuzzy"):
        header(parse_po('#, fuzzy\nmsgid ""\nmsgstr "Language: ar\\n"\n'))
    with pytest.raises(CatalogError, match="charset=UTF-8"):
        header(
            parse_po(
                'msgid ""\nmsgstr "Content-Type: text/plain; charset=ISO-8859-1\\n"\n'
            )
        )
    with pytest.raises(
        CatalogError,
        match=(
            r"^line 1: the header must say Content-Type: text/plain; "
            r"charset=UTF-8$"
        ),
    ):
        header(parse_po('msgid ""\nmsgstr "Language: ar\\n"\n'))
    context_only = parse_po('msgctxt "x"\nmsgid ""\nmsgstr "y"\n')
    with pytest.raises(CatalogError, match="no header"):
        header(context_only)
    fields = header(parse_po(HEADER))
    assert fields["Plural-Forms"] == "nplurals=2; plural=(n != 1);"


@pytest.mark.parametrize(
    ("entry", "message"),
    [
        (
            (
                'msgid "{n} file"\nmsgid_plural "{n} files"\nmsgstr[0] "a"\n'
                'msgstr[1] "b"\nmsgstr[2] "c"\n'
            ),
            "line 6: Plural-Forms says 2 forms, the entry has 3",
        ),
        ('msgid "Hi {name}"\nmsgstr "Salut {nom}"\n', "line 6: .*uses {nom}, which"),
        (
            'msgid "Hi"\nmsgstr "{b} {a}"\n',
            "^line 6: the translation uses {a}, {b}, which the message does not have$",
        ),
        (
            (
                'msgid "{n} file"\nmsgid_plural "{n} files"\nmsgstr[0] "{count}"\n'
                'msgstr[1] "{n}"\n'
            ),
            "uses {count}",
        ),
    ],
)
def test_translations_a_service_cannot_use_are_refused(
    entry: str, message: str
) -> None:
    """Too many plural forms, or placeholders the message does not have."""
    with pytest.raises(CatalogError, match=message):
        check(parse_po(HEADER + entry))


def test_what_is_checked_and_what_is_not() -> None:
    """Placeholders of the message or its plural; lone braces; untranslated entries."""
    check(
        parse_po(
            HEADER
            + 'msgid "{n} file"\nmsgid_plural "{n} files {kind}"\n'
            + 'msgstr[0] "{kind}"\nmsgstr[1] "{n}"\n\n'
            + 'msgid "Use {"\nmsgstr "Utilisez {"\n\n'
            + '#, fuzzy\nmsgid "b"\nmsgstr "{oops}"\n\n'
            + 'msgid "x"\nmsgid_plural "y"\nmsgstr[0] ""\nmsgstr[1] ""\nmsgstr[2] ""\n'
        )
    )
    without_rule = 'msgid ""\nmsgstr "Content-Type: text/plain; charset=UTF-8\\n"\n\n'
    check(
        parse_po(
            without_rule + 'msgid "a"\nmsgid_plural "b"\nmsgstr[0] "c"\nmsgstr[1] "d"\n'
        )
    )


# Compiling .mo files -----------------------------------------------------------------


def test_compiled_catalogs_are_what_gettext_reads() -> None:
    """Messages, contexts, and plurals come back; fuzzy and empty ones do not."""
    text = (
        HEADER
        + 'msgid "Hello"\nmsgstr "Bonjour"\n\n'
        + 'msgctxt "door"\nmsgid "Open"\nmsgstr "Ouverte"\n\n'
        + 'msgid "Open"\nmsgstr "Ouvrir"\n\n'
        + 'msgid "{n} file"\nmsgid_plural "{n} files"\nmsgstr[0] "{n} fichier"\n'
        + 'msgstr[1] "{n} fichiers"\n\n'
        + '#, fuzzy\nmsgid "Draft"\nmsgstr "Brouillon"\n\n'
        + 'msgid "Empty"\nmsgstr ""\n\n'
        + 'msgid "Part"\nmsgid_plural "Parts"\nmsgstr[0] "Partie"\nmsgstr[1] ""\n\n'
        + 'msgid "Œuvre"\nmsgstr "عمل"\n'
    )
    catalog = translations(text)

    assert catalog.gettext("Hello") == "Bonjour"
    assert catalog.pgettext("door", "Open") == "Ouverte"
    assert catalog.gettext("Open") == "Ouvrir"
    assert [catalog.ngettext("{n} file", "{n} files", n) for n in (1, 2)] == [
        "{n} fichier",
        "{n} fichiers",
    ]
    assert catalog.gettext("Draft") == "Draft"
    assert catalog.gettext("Empty") == "Empty"
    assert catalog.ngettext("Part", "Parts", 1) == "Part"
    assert catalog.gettext("Œuvre") == "عمل"
    assert catalog.info()["plural-forms"] == "nplurals=2; plural=(n != 1);"


def test_compiling_is_reproducible_and_laid_out_as_msgfmt_does() -> None:
    """Same input, same bytes: little-endian, sorted, no hash table."""
    text = HEADER + 'msgid "b"\nmsgstr "2"\n\nmsgid "a"\nmsgstr "1"\n'
    data = compile_po(text)

    assert data == compile_po(text)
    assert data == compile_mo(list(reversed(parse_po(text))))
    assert int.from_bytes(data[:4], "little") == MAGIC
    counts = [int.from_bytes(data[i : i + 4], "little") for i in range(4, 28, 4)]
    assert counts == [0, 3, 28, 52, 0, 0]  # Revision, entries, tables, no hash.
    keys_at = 28 + 16 * 3
    assert data[keys_at : keys_at + 5] == b"\0a\0b\0"  # The header's key is "".


def test_compiling_refuses_catalogs_check_refuses() -> None:
    """``compile_mo`` checks first."""
    with pytest.raises(CatalogError, match="no header"):
        compile_po('msgid "a"\nmsgstr "b"\n')


# The shipped catalogs ----------------------------------------------------------------


def shipped(language: str) -> Path:
    """The ``.po`` catalog domainkit ships for ``language``."""
    return LOCALE_DIR / language / "LC_MESSAGES" / "messages.po"


@pytest.mark.parametrize("language", ["en", "ar"])
def test_shipped_catalogs_are_compiled_from_their_sources(language: str) -> None:
    """The ``.mo`` files are exactly what their ``.po`` files compile to."""
    po = shipped(language)
    assert po.with_suffix(".mo").read_bytes() == compile_po(po.read_text("utf-8"))


def test_arabic_translates_every_message() -> None:
    """English lists the messages; Arabic translates each, and nothing else."""
    english = parse_po(shipped("en").read_text("utf-8"))
    arabic = parse_po(shipped("ar").read_text("utf-8"))

    def keys(messages: list[Message]) -> list[tuple[str | None, str]]:
        return [(m.context, m.msgid) for m in messages]

    assert keys(english) == keys(arabic)
    assert [m.msgid for m in arabic if not m.translated] == []
    assert list(translated(english)) == []
    assert header(arabic)["Language"] == "ar"
    assert header(arabic)["Plural-Forms"].startswith("nplurals=6;")


def test_validation_messages_match_pydantics_templates() -> None:
    """Every template the catalogs translate is one pydantic still writes."""
    templates = {error["message_template_python"] for error in list_all_errors()}
    messages = parse_po(shipped("ar").read_text("utf-8"))
    ours = {m.msgid for m in messages if m.context == "validation"}

    assert len(ours) > 70
    assert ours - templates == set()


EXTRA_MESSAGES: Final = {
    "You do not have permission to perform this action.",  # BasePermission.message
    "Some of these fields cannot be changed.",  # resources.policies.FieldNotEditable
    "You do not have permission to change some of these fields.",  # The same.
    "This value is already taken.",  # db.errors, when no field is known.
    "Too many requests; slow down.",  # http.ratelimit.TOO_MANY_REQUESTS
    "Too many open connections; try again later.",  # http.live.TOO_MANY_CONNECTIONS
    "Another item of the batch failed, so nothing was applied.",  # resources.batches
    "Credentials limited to scopes cannot do this.",  # auth.access.IsUnscoped
    "You are not one of the approvers of this request.",  # approvals.rules
    "You cannot approve this request now.",  # approvals.rules
    "Too many attempts; try again later.",  # auth.mfa and auth.recovery, by keyword
    "Too many requests for mail; try again later.",  # auth.recovery and auth.signup
    MoneyError.summary,
    UnknownCurrency.summary,
    InexactAmount.summary,
    CurrencyMismatch.summary,
    "Give money as an object of an amount and a currency, and nothing else.",
    "Give the amount as text, such as 10.50, with at most 38 digits before the point.",
    "The amount has more digits than this field holds.",  # resources.schemas
}
"""Framework messages the scan below cannot see: constants and returns."""
SCANNED: Final = (
    "http",
    "db",
    "ddd",
    "storage",
    "contrib/approvals",
    "contrib/auth",
    "contrib/idempotency",
    "contrib/objectpermissions",
    "resources",
)
_RAISERS: Final = re.compile(
    r"error_response|APIError|.*(NotFound|Denied|Failed|Conflict|Violation|Missing|Refused)"
)


def _texts(call: ast.Call) -> set[str]:
    """The string constants passed to ``call`` by position."""
    return {
        arg.value
        for arg in call.args
        if isinstance(arg, ast.Constant) and isinstance(arg.value, str)
    }


def _name(node: ast.expr) -> str:
    """The name a call or a base class is written with."""
    return getattr(node, "id", getattr(node, "attr", ""))


MUTANT: Final = re.compile(r".*__mutmut_[0-9]+")
"""Names of mutmut's mutated copies of functions, whose text it changed on purpose."""


def _nodes(tree: ast.AST) -> Iterator[ast.AST]:
    """Every node below ``tree``, but those of functions mutmut made."""
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and (
            MUTANT.fullmatch(node.name)
        ):
            continue
        yield node
        yield from _nodes(node)


def framework_messages() -> set[str]:
    """The fixed messages the framework answers requests with.

    The text given to ``error_response``, ``APIError``, and domain,
    authentication, and upload errors, and to ``super().__init__`` in
    their subclasses, in the modules that answer requests. Messages with
    values inside (f-strings) cannot be in a catalog.
    """
    found = set(EXTRA_MESSAGES)
    for directory in SCANNED:
        for path in (SRC / directory).rglob("*.py"):
            tree = ast.parse(path.read_text("utf-8"))
            for node in _nodes(tree):
                if isinstance(node, ast.Call) and _RAISERS.fullmatch(_name(node.func)):
                    found |= _texts(node)
                if isinstance(node, ast.ClassDef) and any(
                    _RAISERS.fullmatch(_name(base)) for base in node.bases
                ):
                    found |= {
                        text
                        for call in _nodes(node)
                        if isinstance(call, ast.Call) and _name(call.func) == "__init__"
                        for text in _texts(call)
                    }
    return found


def test_every_framework_message_is_translated() -> None:
    """A new error message needs its Arabic translation (and English entry).

    Add it to ``src/domainkit/i18n/locale/{en,ar}/LC_MESSAGES/messages.po``,
    then run ``domainkit compilemessages src/domainkit/i18n/locale``.
    """
    messages = parse_po(shipped("ar").read_text("utf-8"))
    ours = {m.msgid for m in messages if m.context is None}

    found = framework_messages()
    assert {"No file has that name.", "The file is empty."} <= found
    assert len(found) >= 34
    assert found - ours == set()


# The command -------------------------------------------------------------------------


def write_po(directory: Path, locale: str, entries: str = "") -> Path:
    """Write ``<directory>/<locale>/LC_MESSAGES/messages.po``; return it."""
    po = directory / locale / "LC_MESSAGES" / "messages.po"
    po.parent.mkdir(parents=True, exist_ok=True)
    po.write_text(HEADER + entries, encoding="utf-8")
    return po


def run(*args: str) -> str:
    """Run ``compilemessages`` and return what it printed."""
    output = io.StringIO()
    call_command("compilemessages", *args, stdout=output)
    return output.getvalue()


def test_the_command_compiles_the_projects_catalogs(
    tmp_path: Path, make_package: MakePackage
) -> None:
    """``LOCALE_PATHS`` and every domain's ``locale``; never the framework's."""
    project = tmp_path / "locale"
    ar = write_po(
        project, "ar", 'msgid "Hi"\nmsgstr "أهلًا"\n\nmsgid "Bye"\nmsgstr ""\n'
    )
    fr = write_po(project, "fr", 'msgid "Hi"\nmsgstr "Salut"\n')
    domain = make_package("talk.words", {})
    pt = write_po(domain / "locale", "pt_BR", 'msgid "Hi"\nmsgstr "Oi"\n')
    shipped_mo = shipped("ar").with_suffix(".mo")
    before = shipped_mo.stat().st_mtime_ns
    registry.clear()
    with override_settings(
        INSTALLED_DOMAINS=[ALPHA, "domainkit.contrib.contenttypes", "talk.words"],
        LOCALE_PATHS=[project, tmp_path / "missing"],
    ):
        printed = run()
        assert printed.splitlines() == [
            f"Compiled {ar} (1 translated).",
            f"Compiled {fr} (1 translated).",
            f"Compiled {pt} (1 translated).",
        ]
        assert run("--check") == "3 catalog(s) up to date.\n"
        assert run("--locale", "fr", "--check") == "1 catalog(s) up to date.\n"
    registry.clear()

    assert translations(ar.read_text("utf-8")).gettext("Hi") == "أهلًا"
    assert ar.with_suffix(".mo").read_bytes() == compile_po(ar.read_text("utf-8"))
    assert not list(project.rglob(".*.tmp"))
    assert shipped_mo.stat().st_mtime_ns == before


def test_the_command_takes_directories_and_locales(tmp_path: Path) -> None:
    """Named directories need no settings; ``--locale`` keeps some locales."""
    ar = write_po(tmp_path, "ar", 'msgid "Hi"\nmsgstr "أهلًا"\n')
    fr = write_po(tmp_path, "fr", 'msgid "Hi"\nmsgstr "Salut"\n')

    assert run(str(tmp_path), "-l", "ar") == f"Compiled {ar} (1 translated).\n"
    assert not fr.with_suffix(".mo").exists()
    assert run(str(tmp_path), "-l", "ar", "-l", "fr").splitlines() == [
        f"Compiled {ar} (1 translated).",
        f"Compiled {fr} (1 translated).",
    ]


def test_check_fails_on_missing_or_stale_files(tmp_path: Path) -> None:
    """``--check`` writes nothing, and names each ``.mo`` to rebuild."""
    ar = write_po(tmp_path, "ar", 'msgid "Hi"\nmsgstr "أهلًا"\n')
    fr = write_po(tmp_path, "fr", 'msgid "Hi"\nmsgstr "Salut"\n')
    stale = f"{ar.with_suffix('.mo')}, {fr.with_suffix('.mo')}"
    with pytest.raises(CommandError) as refused:
        run(str(tmp_path), "--check")
    assert str(refused.value) == f"Out of date: {stale}. Run compilemessages."
    assert not ar.with_suffix(".mo").exists()
    run(str(tmp_path))
    ar.write_text(HEADER + 'msgid "Hi"\nmsgstr "مرحبًا"\n', encoding="utf-8")
    with pytest.raises(CommandError, match="Out of date"):
        run(str(tmp_path), "--check")


def test_the_command_reports_bad_catalogs(tmp_path: Path) -> None:
    """A malformed catalog names its file and line; none at all is an error too."""
    with pytest.raises(CommandError, match=r"^No \.po catalogs found\.$"):
        run(str(tmp_path))
    po = write_po(tmp_path, "ar", 'msgid "Hi"\nmsgstr "{name}"\n')
    with pytest.raises(CommandError, match=f"{po}: line 6: the translation uses"):
        run(str(tmp_path))
    registry.clear()
    with override_settings(INSTALLED_DOMAINS=[ALPHA]):
        with pytest.raises(CommandError, match=r"No \.po catalogs found"):
            run()
        assert registry.ready
    registry.clear()
    setup()
