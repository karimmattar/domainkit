"""The 1.5 conveniences: the default DomainConfig, --user, and their edges."""

from decimal import Decimal
from pathlib import Path
from typing import Annotated
from uuid import UUID

import pydantic
import pytest

from domainkit.apps import DomainConfig
from domainkit.ddd import AggregateRoot, Range
from domainkit.management.commands import startdomain
from domainkit.management.generators import (
    EMPTY_AUTH,
    OPEN_AUTH,
    SETTINGS_FILE,
    Domain,
    install_user_model,
)
from domainkit.resources import schema_for
from domainkit_tests.conftest import MakePackage


def test_the_default_config_maps_models_py(make_package: MakePackage) -> None:
    """ready() imports models.py and calls start_mappers(); none: nothing."""
    make_package(
        "flatapp.shop",
        {"models.py": "MAPPED = []\n\ndef start_mappers():\n    MAPPED.append(1)\n"},
    )
    make_package("flatapp.bare", {})
    make_package("flatapp.plain", {"models.py": "TABLES = ()\n"})

    DomainConfig.create("flatapp.shop").ready()
    DomainConfig.create("flatapp.bare").ready()
    DomainConfig.create("flatapp.plain").ready()

    from flatapp.shop import models  # type: ignore[import-not-found]

    assert models.MAPPED == [1]


def test_install_user_model_leaves_unknown_settings_alone(tmp_path: Path) -> None:
    """No settings file, or no empty AUTH line: nothing is done."""
    assert install_user_model("domains.accounts", "User", "users", tmp_path) == []
    settings = tmp_path / SETTINGS_FILE
    settings.parent.mkdir(parents=True)
    settings.write_text(
        'INSTALLED_DOMAINS = [\n    "domainkit.contrib.auth",\n]\nAUTH = {"X": 1}\n',
        encoding="utf-8",
    )
    assert install_user_model("domains.accounts", "User", "users", tmp_path) == []
    assert EMPTY_AUTH not in settings.read_text(encoding="utf-8")


def test_install_user_model_fills_the_auth_dict_init_writes(tmp_path: Path) -> None:
    """Since 1.8, init's AUTH holds the identifiers: the model joins them."""
    settings = tmp_path / SETTINGS_FILE
    settings.parent.mkdir(parents=True)
    settings.write_text(
        'INSTALLED_DOMAINS = [\n    "domainkit.contrib.auth",\n]\n'
        f'{OPEN_AUTH}    "VERIFICATION": "code",\n}}\n',
        encoding="utf-8",
    )
    assert install_user_model("domains.accounts", "User", "users", tmp_path)
    assert settings.read_text(encoding="utf-8").endswith(
        "AUTH: dict[str, object] = {\n"
        '    "USER_MODEL": "domains.accounts.models.User",\n'
        '    "USER_TABLE": "domains.accounts.models.users",\n'
        '    "VERIFICATION": "code",\n'
        "}\n"
    )


def test_install_user_model_leaves_a_computed_domain_list_alone(
    tmp_path: Path,
) -> None:
    """INSTALLED_DOMAINS built by code: nothing is done, AUTH included."""
    settings = tmp_path / SETTINGS_FILE
    settings.parent.mkdir(parents=True)
    text = f"INSTALLED_DOMAINS = BASE + []\n{EMPTY_AUTH}\n"
    settings.write_text(text, encoding="utf-8")
    assert install_user_model("domains.accounts", "User", "users", tmp_path) == []
    assert settings.read_text(encoding="utf-8") == text


def test_user_steps_explain_what_was_not_done(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """--manual (or unsafe settings): the AUTH lines to write by hand."""
    monkeypatch.setattr(startdomain, "project_root", lambda: tmp_path)
    context = startdomain.template_context("people", "domains", "User", None)

    output = startdomain._user_steps(context, tmp_path / "p", tmp_path / "t", False)

    assert "Add 'domains.people' to INSTALLED_DOMAINS" in output
    assert '"USER_MODEL": "domains.people.models.User"' in output


def test_generators_find_a_four_file_domain(tmp_path: Path) -> None:
    """A domain with a ``domain.py`` is the four-file layout the generators grow."""
    domain = tmp_path / "src" / "domains" / "shop"
    domain.mkdir(parents=True)
    (domain / "domain.py").write_text("", encoding="utf-8")

    found = Domain.locate("shop", domain, tmp_path / "tests", tmp_path)

    assert found.simple


def test_an_integer_range_may_have_a_lower_bound_only() -> None:
    """Range(min=0) on an int: the upper limit stays the column's."""

    class Stock(AggregateRoot[UUID]):
        count: Annotated[int, Range(min=0)]
        ratio: Annotated[Decimal, Range(max=1)]

    StockIn = schema_for(Stock, write=True, include=["count", "ratio"])

    assert StockIn(count=3, ratio="0.5").model_dump()["count"] == 3
    with pytest.raises(pydantic.ValidationError):
        StockIn(count=-1, ratio="0.5")
