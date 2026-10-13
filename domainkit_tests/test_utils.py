"""Helpers: dotted-path imports and name conversions."""

import warnings
from pathlib import Path

import pytest

from domainkit.deprecation import DomainkitDeprecationWarning
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
