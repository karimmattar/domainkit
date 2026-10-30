"""Layer rules for domain packages."""

from typing import Any

import pytest
from pydantic import ValidationError

import domainkit.conf
from domainkit import architecture
from domainkit.architecture import (
    LayerRules,
    Violation,
    check_domain,
    check_domains,
    iter_imports,
    project_rules,
)
from domainkit.conf import override_settings
from domainkit.conf.global_settings import ArchitectureConfig, Settings
from domainkit.exceptions import ImproperlyConfigured
from domainkit_tests.conftest import MakePackage


@pytest.fixture
def layered(make_package: MakePackage) -> str:
    """A domain that breaks every rule once, next to a well-behaved neighbor."""
    make_package(
        "arch.billing",
        {
            "domain/__init__.py": "from .events import dataclass\n",
            "domain/events.py": "from dataclasses import dataclass\n",
            "domain/model.py": """
                from __future__ import annotations
                from typing import TYPE_CHECKING
                from domainkit.ddd import AggregateRoot
                from . import events
                from .events import *
                import sqlalchemy
                from ..settings import LIMIT
                if TYPE_CHECKING:
                    from arch.billing.api import routes
            """,
            "application/services.py": """
                from fastapi import status
                import redis.asyncio
                from domainkit.cache import cached
                from arch.billing.domain.model import Invoice
                from ..infrastructure import repo
                from arch.shipping.application import services
                from arch.shipping.domain.events import Shipped
                from arch.shipping.domain.model import Parcel
                import arch.shipping
            """,
            "infrastructure/repo.py": """
                import sqlalchemy
                from starlette.requests import Request
                from arch.billing.api.routes import router
            """,
            "api/routes.py": """
                from fastapi import APIRouter
                from ..infrastructure import repo
            """,
            "settings.py": "import pydantic\nLIMIT = 1\n",
        },
    )
    make_package(
        "arch.shipping", {"domain/model.py": "", "application/services.py": ""}
    )
    return "arch.billing"


def test_imports_are_resolved(layered: str) -> None:
    """Relative, star, and ``TYPE_CHECKING`` imports are found and resolved."""
    imports = {(module, imported) for module, imported in iter_imports(layered)}

    assert ("arch.billing.domain", "arch.billing.domain.events") in imports
    model = "arch.billing.domain.model"
    assert (model, "arch.billing.domain") in imports
    assert (model, "arch.billing.domain.events") in imports
    assert (model, "arch.billing.settings.LIMIT") in imports
    assert (model, "arch.billing.api.routes") in imports
    assert (model, "__future__") in imports
    assert ("arch.billing.api.routes", "arch.billing.infrastructure.repo") in imports


def test_violations_name_the_rule(layered: str) -> None:
    """Every broken rule is reported once, with the offending import."""
    found = {
        (violation.module.removeprefix("arch.billing."), violation.imported)
        for violation in check_domain(layered, ["arch.billing", "arch.shipping"])
    }

    assert found == {
        ("domain.model", "sqlalchemy"),
        ("domain.model", "arch.billing.settings"),
        ("domain.model", "arch.billing.settings.LIMIT"),
        ("domain.model", "arch.billing.api"),
        ("domain.model", "arch.billing.api.routes"),
        ("application.services", "fastapi"),
        ("application.services", "fastapi.status"),
        ("application.services", "redis.asyncio"),
        ("application.services", "arch.billing.infrastructure"),
        ("application.services", "arch.billing.infrastructure.repo"),
        ("application.services", "arch.shipping.domain.model"),
        ("application.services", "arch.shipping.domain.model.Parcel"),
        ("infrastructure.repo", "starlette.requests"),
        ("infrastructure.repo", "starlette.requests.Request"),
        ("infrastructure.repo", "arch.billing.api.routes"),
        ("infrastructure.repo", "arch.billing.api.routes.router"),
    }


def test_violation_messages() -> None:
    """Violations read as one line each."""
    violation = Violation("pkg.domain.model", "sqlalchemy", "no frameworks")

    assert str(violation) == "pkg.domain.model imports sqlalchemy: no frameworks"


def test_unknown_packages_are_rejected() -> None:
    """Only importable packages can be checked."""
    # A module of our own: the standard library's "string" became a package
    # in Python 3.14.
    with pytest.raises(ValueError, match="not an importable package"):
        check_domain("domainkit_tests.settings")
    with pytest.raises(ValueError, match="not an importable package"):
        check_domain("no_such_package")
    with pytest.raises(ModuleNotFoundError):
        check_domain("no_such_package.child")


def found_in(violations: list[Violation]) -> set[tuple[str, str]]:
    """``(module, imported)`` of each violation, without the package prefix."""
    return {
        (violation.module.removeprefix("arch.billing."), violation.imported)
        for violation in violations
    }


def test_the_default_rules_are_the_tables() -> None:
    """Without ``ARCHITECTURE``, the rules are exactly the module's tables."""
    rules = LayerRules()
    assert rules.layers == architecture.LAYERS
    assert rules.domain_allowed == architecture.DOMAIN_ALLOWED_PREFIXES
    assert dict(rules.forbidden_libraries) == architecture.FORBIDDEN_LIBRARIES
    assert dict(rules.forbidden_layers) == architecture.FORBIDDEN_LAYERS
    assert rules.public_surface == architecture.PUBLIC_SURFACE
    assert ArchitectureConfig() == Settings().ARCHITECTURE
    assert project_rules() == rules
    assert rules.extend() == rules


def test_without_settings_the_default_rules_apply(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A script that loads no settings still checks, by the framework's rules."""

    class Unloaded:
        def __getattr__(self, name: str) -> object:
            raise ImproperlyConfigured("No settings module.")

    monkeypatch.setattr(domainkit.conf, "settings", Unloaded())
    assert project_rules() == LayerRules()


def test_a_project_lets_its_domain_layers_import_a_shared_kernel(layered: str) -> None:
    """``DOMAIN_ALLOWED`` adds packages; the message names them; nothing else moves."""
    before = check_domain(layered, ["arch.billing", "arch.shipping"])
    with override_settings(ARCHITECTURE={"DOMAIN_ALLOWED": ["sqlalchemy"]}):
        after = check_domain(layered, ["arch.billing", "arch.shipping"])

    assert found_in(before) - found_in(after) == {("domain.model", "sqlalchemy")}
    assert found_in(after) < found_in(before)
    [settings] = [v for v in after if v.imported == "arch.billing.settings"]
    assert settings.rule == (
        "the domain layer may import only the standard library, domainkit.ddd, "
        "domainkit.clock, domainkit.money, sqlalchemy, and its own domain layer"
    )
    [default] = [v for v in before if v.imported == "arch.billing.settings"]
    assert default.rule == (
        "the domain layer may import only the standard library, domainkit.ddd, "
        "domainkit.clock, domainkit.money, and its own domain layer"
    )


def test_a_project_forbids_more_libraries(layered: str) -> None:
    """``FORBIDDEN_LIBRARIES`` adds to a layer's list; the defaults still hold."""
    extra = {"FORBIDDEN_LIBRARIES": {"application": ["domainkit.cache"]}}
    with override_settings(ARCHITECTURE=extra):
        found = found_in(check_domains(["arch.billing", "arch.shipping"]))

    assert ("application.services", "domainkit.cache.cached") in found
    assert ("application.services", "fastapi") in found


def test_a_project_adds_a_layer_with_rules_of_its_own(
    make_package: MakePackage,
) -> None:
    """``EXTRA_LAYERS`` and ``FORBIDDEN_LAYERS`` check a ``ports`` layer."""
    make_package(
        "arch.billing",
        {
            "ports/payments.py": """
                import httpx
                from arch.billing.infrastructure import gateway
                from arch.billing.domain import model
            """,
            "infrastructure/gateway.py": "",
            "domain/model.py": "",
        },
    )
    extra: dict[str, Any] = {
        "EXTRA_LAYERS": ["ports"],
        "FORBIDDEN_LAYERS": {"ports": ["infrastructure", "api"]},
        "FORBIDDEN_LIBRARIES": {"ports": ["httpx"]},
    }
    assert check_domain("arch.billing") == []
    with override_settings(ARCHITECTURE=extra):
        violations = check_domain("arch.billing")

    assert {(v.imported, v.rule) for v in violations} == {
        ("httpx", "the ports layer must not use httpx"),
        (
            "arch.billing.infrastructure",
            "the ports layer must not depend on outer layers",
        ),
        (
            "arch.billing.infrastructure.gateway",
            "the ports layer must not depend on outer layers",
        ),
    }


def test_a_project_opens_more_of_a_domain_to_the_others(layered: str) -> None:
    """``PUBLIC_SURFACE`` adds parts other domains may import."""
    installed = ["arch.billing", "arch.shipping"]
    parcel = ("application.services", "arch.shipping.domain.model")
    assert parcel in found_in(check_domain(layered, installed))
    with override_settings(ARCHITECTURE={"PUBLIC_SURFACE": ["domain.model"]}):
        violations = check_domain(layered, installed)
    assert parcel not in found_in(violations)
    assert (
        "application.services",
        "arch.billing.infrastructure",
    ) in found_in(violations)


def test_rules_given_outright_win_over_the_settings(layered: str) -> None:
    """``check_domain(..., rules=...)`` uses those rules, whatever is set."""
    rules = LayerRules().extend(domain_allowed=["sqlalchemy"])
    with override_settings(ARCHITECTURE={"DOMAIN_ALLOWED": ["arch"]}):
        found = found_in(check_domain(layered, rules=rules))
    assert ("domain.model", "sqlalchemy") not in found
    assert ("domain.model", "arch.billing.settings") in found


def test_extending_keeps_every_default_and_adds_each_entry_once() -> None:
    """Nothing is removed, and an entry already present is not repeated."""
    rules = LayerRules().extend(
        layers=["ports", "ports"],
        domain_allowed=["domainkit.ddd", "kernel"],
        forbidden_libraries={"application": ["httpx", "fastapi"]},
        forbidden_layers={"ports": ["api"]},
        public_surface=["application"],
    )
    assert rules.layers == (*architecture.LAYERS, "ports")
    assert rules.domain_allowed == (
        "domainkit.ddd",
        "domainkit.clock",
        "domainkit.money",
        "kernel",
    )
    assert rules.forbidden_libraries["application"] == (
        *architecture.FORBIDDEN_LIBRARIES["application"],
        "httpx",
    )
    assert (
        rules.forbidden_libraries["infrastructure"]
        == (architecture.FORBIDDEN_LIBRARIES["infrastructure"])
    )
    assert dict(rules.forbidden_layers) == {
        **architecture.FORBIDDEN_LAYERS,
        "ports": ("api",),
    }
    assert rules.public_surface == architecture.PUBLIC_SURFACE
    with pytest.raises(TypeError):
        rules.forbidden_layers["api"] = ()  # type: ignore[index]


@pytest.mark.parametrize(
    ("value", "message"),
    [
        ({"EXTRA_LAYERS": ["api"]}, "repeats a built-in layer: \\['api'\\]"),
        ({"EXTRA_LAYERS": ["ports", "ports"]}, "names a layer twice"),
        (
            {"FORBIDDEN_LIBRARIES": {"ports": ["httpx"]}},
            (
                "FORBIDDEN_LIBRARIES names layers that are not in the layer rules: "
                "\\['ports'\\]; add them to EXTRA_LAYERS"
            ),
        ),
        (
            {"FORBIDDEN_LAYERS": {"ports": ["api"]}},
            "FORBIDDEN_LAYERS names layers that are not in the layer rules",
        ),
        (
            {"FORBIDDEN_LIBRARIES": {"domain": ["httpx"]}},
            "cannot name the domain layer: .* add packages to DOMAIN_ALLOWED",
        ),
        (
            {"FORBIDDEN_LAYERS": {"api": ["adapters"]}},
            (
                "FORBIDDEN_LAYERS\\['api'\\] names layers that are not in the layer "
                "rules: \\['adapters'\\]"
            ),
        ),
        (
            {"FORBIDDEN_LAYERS": {"application": ["application"]}},
            "FORBIDDEN_LAYERS\\['application'\\] forbids the layer itself",
        ),
        (
            {"PUBLIC_SURFACE": ["values"]},
            "PUBLIC_SURFACE names parts outside the layers: \\['values'\\]",
        ),
        ({"DOMAIN_ALLOWED": ["my-kernel"]}, "DOMAIN_ALLOWED.0\n  String should match"),
        ({"DOMAIN_ALLOWED": [""]}, "String should match"),
        ({"EXTRA_LAYERS": ["ports.inner"]}, "EXTRA_LAYERS.0\n  String should match"),
        ({"DOMAIN_ALLOWED": [f"k{n}" for n in range(65)]}, "at most 64 items"),
        ({"EXTRA_LAYERS": [f"l{n}" for n in range(9)]}, "at most 8 items"),
        (
            {"FORBIDDEN_LAYERS": {"api": ["domain"] * 13}},
            "FORBIDDEN_LAYERS.api\n  Tuple should have at most 12 items",
        ),
        (
            {"FORBIDDEN_LIBRARIES": {f"l{n}": ["x"] for n in range(13)}},
            "FORBIDDEN_LIBRARIES\n  Dictionary should have at most 12 items",
        ),
        ({"PUBLIC_SURFACE": ["domain"], "LAYERS": ["x"]}, "Extra inputs"),
    ],
)
def test_architecture_settings_are_validated(
    value: dict[str, Any], message: str
) -> None:
    """Every layer named is known; names, sizes, and keys are bounded."""
    with pytest.raises(ValidationError, match=message):
        Settings(ARCHITECTURE=value)


def test_extra_layers_may_be_named_by_the_rules() -> None:
    """A layer added in ``EXTRA_LAYERS`` may key and fill the other tables."""
    config = ArchitectureConfig(
        EXTRA_LAYERS=("ports",),
        FORBIDDEN_LIBRARIES={"ports": ("httpx",)},
        FORBIDDEN_LAYERS={"ports": ("api",), "api": ("ports", "domain")},
        PUBLIC_SURFACE=("ports.events",),
    )
    rules = LayerRules.from_config(config)
    assert rules.layers[-1] == "ports"
    assert rules.forbidden_layers["api"] == ("ports", "domain")
    assert rules.public_surface[-1] == "ports.events"


def test_a_table_replaced_the_old_way_still_applies(
    layered: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Before ``ARCHITECTURE``, projects replaced a table; that keeps working."""
    monkeypatch.setattr(
        architecture, "DOMAIN_ALLOWED_PREFIXES", ("domainkit.ddd", "sqlalchemy")
    )
    found = found_in(check_domain(layered, ["arch.billing", "arch.shipping"]))
    assert ("domain.model", "sqlalchemy") not in found
    assert ("domain.model", "arch.billing.settings") in found


def test_rules_given_to_check_domains_win_over_the_settings(layered: str) -> None:
    """``check_domains(..., rules=...)`` hands those rules to every domain."""
    installed = ["arch.billing", "arch.shipping"]
    rules = LayerRules().extend(domain_allowed=["sqlalchemy"])
    with override_settings(ARCHITECTURE={"DOMAIN_ALLOWED": ["arch"]}):
        given = found_in(check_domains(installed, rules))
        configured = found_in(check_domains(installed))
    assert ("domain.model", "sqlalchemy") not in given
    assert ("domain.model", "arch.billing.settings") in given
    assert ("domain.model", "sqlalchemy") in configured
    assert ("domain.model", "arch.billing.settings") not in configured


def test_rules_read_as_one_line_each(make_package: MakePackage) -> None:
    """Library rules name the top package; cross-domain rules every way in."""
    make_package(
        "arch.billing",
        {
            "application/services.py": "import fastapi.status\n",
            "domain/values/money.py": "import sqlalchemy.types\n",
        },
    )
    make_package(
        "arch.shipping",
        {"api/routes.py": "from arch.billing.infrastructure import repo\n"},
    )
    rules = LayerRules().extend(public_surface=["domain.values"])
    installed = ["arch.shipping", "arch.billing"]

    shipping = {
        (v.imported, v.rule) for v in check_domain("arch.shipping", installed, rules)
    }
    billing = {
        (v.module, v.rule) for v in check_domain("arch.billing", installed, rules)
    }

    assert (
        "arch.billing.infrastructure",
        (
            "use another domain only through arch.billing.application or "
            "arch.billing.domain.events or arch.billing.domain.values"
        ),
    ) in shipping
    assert (
        "arch.billing.application.services",
        "the application layer must not use fastapi",
    ) in billing
    assert any(module == "arch.billing.domain.values.money" for module, _ in billing)


def test_a_rule_for_a_layer_not_in_the_rules_does_nothing(layered: str) -> None:
    """Only modules inside a known layer are held to a layer's rules."""
    rules = LayerRules().extend(forbidden_libraries={"settings": ["pydantic"]})
    found = found_in(check_domain(layered, rules=rules))
    assert not any(module == "settings" for module, _ in found)
    assert ("domain.model", "sqlalchemy") in found
