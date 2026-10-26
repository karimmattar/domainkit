"""Helpers: dotted-path imports and name conversions."""

import warnings
from pathlib import Path

import pytest

from domainkit.deprecation import DomainkitDeprecationWarning, moved
from domainkit.http.access import Require
from domainkit.utils.module_loading import import_string
from domainkit.utils.text import camel_case, pluralize, snake_case


def test_import_string() -> None:
    """Both ``module.attr`` and ``module:attr`` forms work."""
    assert import_string("domainkit.utils.text.snake_case") is snake_case
    assert import_string("domainkit.utils.text:camel_case") is camel_case
    with pytest.raises(ImportError, match="not a dotted path"):
        import_string("nodots")
    with pytest.raises(ImportError, match="has no attribute 'missing'"):
        import_string("domainkit.utils.text.missing")


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("ProductNotFound", "product_not_found"),
        ("HTTPError", "http_error"),
        ("OAuth2Token", "o_auth2_token"),
        ("kebab-case", "kebab_case"),
    ],
)
def test_snake_case(name: str, expected: str) -> None:
    """Class names become error codes."""
    assert snake_case(name) == expected


def test_camel_case() -> None:
    """Package names become class names."""
    assert camel_case("order_line") == "OrderLine"
    assert camel_case("_private__name") == "PrivateName"


@pytest.mark.parametrize(
    ("word", "plural"),
    [
        ("product", "products"),
        ("box", "boxes"),
        ("policy", "policies"),
        ("day", "days"),
        ("match", "matches"),
        ("dish", "dishes"),
        ("fez", "fezes"),
        ("bus", "buses"),
        ("key", "keys"),
        ("toy", "toys"),
        ("guy", "guys"),
        ("xiy", "xiys"),
        ("party", "parties"),
    ],
)
def test_pluralize(word: str, plural: str) -> None:
    """Common English endings are handled."""
    assert pluralize(word) == plural


def test_format_python_sorts_imports_and_formats(tmp_path: Path) -> None:
    """Generated files come out import-sorted and ruff-formatted."""
    from domainkit.utils.formatting import format_python

    source = tmp_path / "generated.py"
    source.write_text("import sys\nimport os\nx=[1,2]\n", encoding="utf-8")

    assert format_python([source]) is True
    assert source.read_text(encoding="utf-8") == "import os\nimport sys\n\nx = [1, 2]\n"


def test_format_python_is_quiet_and_leaves_no_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capfd: pytest.CaptureFixture[str]
) -> None:
    """Nothing printed, and no ``.ruff_cache`` left in a project just made."""
    from domainkit.utils.formatting import format_python

    (tmp_path / "pyproject.toml").write_text("[tool.ruff]\n", encoding="utf-8")
    source = tmp_path / "generated.py"
    source.write_text("import sys\nimport os\nx=[1,2]\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    assert format_python([source]) is True

    assert source.read_text(encoding="utf-8") == "import os\nimport sys\n\nx = [1, 2]\n"
    assert capfd.readouterr() == ("", "")
    assert sorted(path.name for path in tmp_path.iterdir()) == [
        "generated.py",
        "pyproject.toml",
    ]


def test_format_python_leaves_code_it_cannot_parse(
    tmp_path: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    """A file ruff cannot parse stays as it is, and the command goes on."""
    from domainkit.utils.formatting import format_python

    broken = tmp_path / "broken.py"
    broken.write_text("def (:\n", encoding="utf-8")

    assert format_python([broken]) is True

    assert broken.read_text(encoding="utf-8") == "def (:\n"
    assert "broken.py" in capfd.readouterr().out


def test_failing_checks_are_logged(caplog: pytest.LogCaptureFixture) -> None:
    """A failing check is named in the log, with its traceback."""
    import asyncio

    from domainkit.utils.readiness import failing_checks

    async def broken() -> None:
        raise RuntimeError("down")

    async def fine() -> None:
        return None

    failing = asyncio.run(failing_checks({"db": broken, "cache": fine}, 1.0))

    assert failing == ["db"]
    (record,) = [r for r in caplog.records if r.name == "domainkit.utils.readiness"]
    assert record.getMessage() == "Readiness check 'db' failed"
    assert "RuntimeError: down" in caplog.text


def test_deprecated_apis_warn_and_keep_working() -> None:
    """The policy's decorator warns with domainkit's category and still runs."""

    @warnings.deprecated(
        "Use new(); removed in 0.4.", category=DomainkitDeprecationWarning
    )
    def old() -> int:
        return 1

    with pytest.warns(DomainkitDeprecationWarning, match="removed in 0.4"):
        assert old() == 1
    assert issubclass(DomainkitDeprecationWarning, DeprecationWarning)


def test_moved_names_answer_from_their_old_module_with_a_warning() -> None:
    """``moved``: the new module's object, a warning naming both places; no more.

    The framework's own moved names went in 0.11; the hook stays public.
    """
    hook = moved("old.place", {"Require": "domainkit.http.access"}, "9.9")

    with pytest.warns(DomainkitDeprecationWarning) as warned:
        found = hook("Require")

    assert found is Require
    (message,) = [str(each.message) for each in warned]
    assert message == (
        "old.place.Require moved to domainkit.http.access.Require; import it "
        "from there. The old name is removed in 9.9."
    )
    assert Path(warned[0].filename).name == Path(__file__).name  # The caller.
    missing = r"^module 'old\.place' has no attribute 'Other'$"
    with pytest.raises(AttributeError, match=missing):
        hook("Other")
