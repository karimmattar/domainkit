"""Layer rules for domain packages."""

import io
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Final

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError
from starlette.exceptions import HTTPException

import domainkit.conf
from domainkit import architecture, http
from domainkit.architecture import (
    LayerRules,
    Violation,
    check_domain,
    check_domains,
    iter_imports,
    layer_warnings,
    project_rules,
)
from domainkit.conf import override_settings
from domainkit.conf.global_settings import ArchitectureConfig, Settings
from domainkit.exceptions import DomainkitError, ImproperlyConfigured
from domainkit.http import errors, problems, uploads
from domainkit.http.errors import install_exception_handlers
from domainkit_tests.conftest import MakePackage

ROOT: Final = Path(__file__).parents[1]
DOMAINS: Final = ["domainkit_tests.sample_domains.alpha"]
"""The test settings' domains."""


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
    assert dict(rules.allowed_modules) == architecture.ALLOWED_MODULES
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
        "domainkit.clock, domainkit.money, domainkit.i18n.lazy, sqlalchemy, and "
        "its own domain layer"
    )
    [default] = [v for v in before if v.imported == "arch.billing.settings"]
    assert default.rule == (
        "the domain layer may import only the standard library, domainkit.ddd, "
        "domainkit.clock, domainkit.money, domainkit.i18n.lazy, and its own "
        "domain layer"
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
        "domainkit.i18n.lazy",
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
            "arch.billing.domain.events or arch.billing.controllers or "
            "arch.billing.domain.values"
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


def test_the_simple_layout_holds_each_file_to_its_layer(
    make_package: MakePackage,
) -> None:
    """domain.py, controllers.py, models.py, and routes.py are the four layers."""
    make_package(
        "flat",
        {
            "shop/__init__.py": "import sqlalchemy\n",  # no layer: no rule
            "shop/domain.py": "import sqlalchemy\nfrom domainkit.ddd import Command\n",
            "shop/controllers.py": (
                "from flat.shop.domain import x\nfrom flat.shop.models import y\n"
            ),
            "shop/models.py": "import sqlalchemy\nfrom flat.shop.routes import z\n",
            "shop/routes.py": "import fastapi\nfrom flat.shop.models import y\n",
            "orders/__init__.py": "",
            "orders/controllers.py": (
                "from flat.shop.controllers import a\nfrom flat.shop.models import b\n"
            ),
        },
    )

    found = {
        (v.module, v.imported)
        for v in check_domains(["flat.shop", "flat.orders"], LayerRules())
    }

    assert found == {
        ("flat.shop.domain", "sqlalchemy"),
        ("flat.shop.controllers", "flat.shop.models"),
        ("flat.shop.controllers", "flat.shop.models.y"),
        ("flat.shop.models", "flat.shop.routes"),
        ("flat.shop.models", "flat.shop.routes.z"),
        ("flat.orders.controllers", "flat.shop.models"),
        ("flat.orders.controllers", "flat.shop.models.b"),
    }


@pytest.fixture
def gateway(make_package: MakePackage) -> str:
    """A domain whose adapters call another service, in both layouts."""
    make_package(
        "arch.billing",
        {
            "infrastructure/rates.py": """
                import domainkit.http.client
                from domainkit.http.client import CircuitOpen, HttpClient
                from domainkit.http.errors import APIError
                import domainkit.http.routing
            """,
            "application/services.py": """
                from domainkit.http.client import HttpClient
            """,
            "api/routes.py": "from domainkit.http.client import HttpClient\n",
        },
    )
    make_package(
        "arch.flat",
        {"models.py": "from domainkit.http.client import HttpClient\n"},
    )
    return "arch.billing"


def test_infrastructure_may_call_other_services_through_the_http_client(
    gateway: str,
) -> None:
    """The outgoing client is an adapter's; the rest of ``domainkit.http`` is not.

    Every other ``domainkit.http`` import stays forbidden where it was, and
    the application layer still may not call out (user report D2).
    """
    found = {
        (v.module.removeprefix("arch.billing."), v.imported, v.rule)
        for v in check_domains(["arch.billing", "arch.flat"], LayerRules())
    }
    refused = "the infrastructure layer must not use domainkit"

    assert found == {
        ("infrastructure.rates", "domainkit.http.errors", refused),
        ("infrastructure.rates", "domainkit.http.errors.APIError", refused),
        ("infrastructure.rates", "domainkit.http.routing", refused),
        (
            "application.services",
            "domainkit.http.client",
            "the application layer must not use domainkit",
        ),
        (
            "application.services",
            "domainkit.http.client.HttpClient",
            "the application layer must not use domainkit",
        ),
    }


def test_infrastructure_calls_out_through_domainkit_outbound_with_no_exception(
    make_package: MakePackage,
) -> None:
    """The client's home since 2.9 is no web library: no rule names it there.

    The application layer may not call out by its new path either, as by
    its old one; the domain layer imports neither (ADR 0195, audit
    A28-ARCH-04).
    """
    make_package(
        "arch.rates",
        {
            "domain/model.py": "from domainkit.outbound.guard import UnsafeURL\n",
            "application/services.py": """
                from domainkit.outbound.client import HttpClient
            """,
            "infrastructure/feed.py": """
                import domainkit.outbound
                from domainkit.outbound import guard
                from domainkit.outbound.client import CircuitOpen, HttpClient, httpx2
            """,
        },
    )
    make_package(
        "arch.quotes",
        {"models.py": "from domainkit.outbound.client import HttpClient\n"},
    )
    rules = LayerRules()

    found = {
        (v.module.removeprefix("arch.rates."), v.imported, v.rule)
        for v in check_domains(["arch.rates", "arch.quotes"], rules)
    }

    assert "domainkit.outbound" not in str(rules.allowed_modules)
    assert {(module, imported) for module, imported, _ in found} == {
        ("domain.model", "domainkit.outbound.guard"),
        ("domain.model", "domainkit.outbound.guard.UnsafeURL"),
        ("application.services", "domainkit.outbound.client"),
        ("application.services", "domainkit.outbound.client.HttpClient"),
    }
    assert {rule for module, _, rule in found if module.startswith("app")} == {
        "the application layer must not use domainkit"
    }
    assert layer_warnings(["arch.rates", "arch.quotes"], rules) == []


def test_a_project_rule_on_a_2_8_path_covers_the_2_9_home_too(
    make_package: MakePackage,
) -> None:
    """``domainkit.http`` or ``domainkit.http.client`` in a rule keeps the client out.

    A project's own layer that forbade ``domainkit.http``, or an
    infrastructure layer that forbade the client by name, kept the client
    out in 2.8; its new path is kept out too (ADR 0195, review B2).
    """
    new, old = (
        "from domainkit.outbound.client import X\n",
        "from domainkit.http.client import X\n",
    )
    make_package(
        "arch.proj.billing",
        {
            "infrastructure/feed.py": new,
            "infrastructure/feed28.py": old,
            "ports/gateway.py": new,
            "ports/gateway28.py": old,
            "ports/checks.py": "from domainkit.outbound.guard import checked_url\n",
        },
    )
    rules = LayerRules().extend(
        layers=("ports",),
        forbidden_libraries={
            "ports": ("domainkit.http",),
            "infrastructure": ("domainkit.http.client",),
        },
    )

    found = {
        v.module.removeprefix("arch.proj.billing.")
        for v in check_domains(["arch.proj.billing"], rules)
    }

    assert found == {
        "infrastructure.feed",
        "infrastructure.feed28",
        "ports.gateway",
        "ports.gateway28",
        "ports.checks",
    }
    assert rules.forbidden_libraries["ports"] == (
        "domainkit.http",
        "domainkit.outbound.client",
        "domainkit.outbound.guard",
    )


def test_a_rule_on_the_http_package_leaves_a_layer_the_client_its_exception_lifts() -> (
    None
):
    """In 2.8 the exception for ``domainkit.http.client`` lifted such a rule.

    ``domainkit.http``, where the rules do not hold it yet, forbade the
    guard, which no exception lifted, and left the client: the new paths
    follow. A rule under the old path carries its rest over.
    """
    rules = LayerRules(forbidden_libraries={"infrastructure": ("fastapi",)}).extend(
        forbidden_libraries={
            "infrastructure": ("domainkit.http", "domainkit.http.guard.lookup"),
            "api": ("domainkit",),
        }
    )

    assert rules.forbidden_libraries["infrastructure"] == (
        "fastapi",
        "domainkit.http",
        "domainkit.http.guard.lookup",
        "domainkit.outbound.guard",
        "domainkit.outbound.guard.lookup",
    )
    assert rules.forbidden_libraries["api"] == (
        "domainkit",
        "domainkit.outbound.client",
        "domainkit.outbound.guard",
    )


def test_a_project_that_repeats_a_default_rule_asks_for_no_more(
    make_package: MakePackage,
) -> None:
    """``domainkit.http`` for the infrastructure layer is a default rule already.

    Repeated in a project's ``ARCHITECTURE``, it adds no rule on the new
    paths: the guard stays the infrastructure layer's, as by default
    (review).
    """
    make_package(
        "arch.proj2.billing",
        {
            "infrastructure/callbacks.py": """
                from domainkit.outbound.guard import checked_url
                from domainkit.outbound.client import HttpClient
            """
        },
    )
    repeated = LayerRules().extend(
        forbidden_libraries={"infrastructure": ("domainkit.http",)}
    )

    assert repeated.forbidden_libraries == LayerRules().forbidden_libraries
    assert check_domains(["arch.proj2.billing"], repeated) == []


def test_a_file_gateway_may_check_and_store_uploads(
    make_package: MakePackage,
) -> None:
    """``domainkit.http.uploads`` is an adapter's; the web's errors and types are not.

    A gateway takes the bytes its route hands it (``save_upload`` takes
    bytes or a stream since 2.5), so it needs neither Starlette's
    ``UploadFile`` nor ``domainkit.http.errors``: it raises domain errors,
    which the HTTP layer maps. The application layer may name the stored
    upload its commands carry (user report D2).
    """
    make_package(
        "arch.contracts",
        {
            "infrastructure/files.py": """
                from domainkit.http.uploads import CHECKED_PDF, save_upload
                from domainkit.storage import StoredUpload
                from domainkit.http.errors import APIError
                from starlette.datastructures import UploadFile
            """,
            "application/commands.py": """
                from domainkit.storage import StoredUpload
                from domainkit.http.uploads import save_upload
            """,
        },
    )
    found = {
        (v.module.removeprefix("arch.contracts."), v.imported)
        for v in check_domains(["arch.contracts"], LayerRules())
    }

    assert found == {
        ("infrastructure.files", "domainkit.http.errors"),
        ("infrastructure.files", "domainkit.http.errors.APIError"),
        ("infrastructure.files", "starlette.datastructures"),
        ("infrastructure.files", "starlette.datastructures.UploadFile"),
        ("application.commands", "domainkit.http.uploads"),
        ("application.commands", "domainkit.http.uploads.save_upload"),
    }


def test_a_file_gateway_takes_its_routes_file_as_an_upload(
    make_package: MakePackage,
) -> None:
    """``Upload`` names the file a route hands a gateway, without Starlette.

    The gateway takes the route's ``UploadFile`` as it is, typed by
    ``domainkit.http.uploads.Upload``, which ``save_upload`` and
    ``receive_upload`` take, and may catch ``UploadRefused`` from there:
    it needs no layer of its own for Starlette's types (user report D2).
    The domain layer still imports none of it.
    """
    make_package(
        "arch.forms",
        {
            "infrastructure/files.py": """
                from domainkit.http.uploads import (
                    CHECKED_PDF,
                    Upload,
                    UploadRefused,
                    receive_upload,
                )
            """,
            "domain/model.py": "from domainkit.http.uploads import Upload\n",
        },
    )
    found = {
        (v.module.removeprefix("arch.forms."), v.imported)
        for v in check_domains(["arch.forms"], LayerRules())
    }

    assert found == {
        ("domain.model", "domainkit.http.uploads"),
        ("domain.model", "domainkit.http.uploads.Upload"),
    }
    assert "Upload" in uploads.__all__


def test_a_file_gateway_raises_the_problems_a_route_raises(
    make_package: MakePackage,
) -> None:
    """``domainkit.http.problems`` is an adapter's; the rest of the errors are not.

    A gateway refuses a file with a coded ``APIError`` and its
    ``FieldError``, imported from ``domainkit.http.problems``. The module
    that builds responses (``domainkit.http.errors``) stays out of the
    layer, and the application and domain layers import neither (user
    report D2, 2.7). A project that forbids the module by name forbids it.
    Only what its ``__all__`` lists may be imported from it, not what it
    imports itself (``HTTPException``); ``client`` and ``uploads`` are
    held to nothing more than in 2.7 (``StoredUpload``).
    """
    make_package(
        "arch.deeds",
        {
            "infrastructure/files.py": """
                import domainkit.http.problems
                from domainkit.http.problems import APIError, FieldError
                from domainkit.http.errors import APIError as Answered
                from domainkit.http.problems import HTTPException, BaseModel
                from domainkit.http.problems import DomainkitError
                from domainkit.http.uploads import StoredUpload
            """,
            "application/commands.py": (
                "from domainkit.http.problems import APIError\n"
            ),
            "domain/model.py": "from domainkit.http.problems import FieldError\n",
        },
    )
    found = {
        (v.module.removeprefix("arch.deeds."), v.imported)
        for v in check_domains(["arch.deeds"], LayerRules())
    }
    rules = {
        str(v).partition(": ")[2] for v in check_domain("arch.deeds", (), LayerRules())
    }
    extra = {"FORBIDDEN_LIBRARIES": {"infrastructure": ["domainkit.http.problems"]}}
    with override_settings(ARCHITECTURE=extra):
        forbidden = {v.imported for v in check_domain("arch.deeds")}

    assert found == {
        ("infrastructure.files", "domainkit.http.errors"),
        ("infrastructure.files", "domainkit.http.errors.APIError"),
        ("infrastructure.files", "domainkit.http.problems.HTTPException"),
        ("infrastructure.files", "domainkit.http.problems.BaseModel"),
        ("infrastructure.files", "domainkit.http.problems.DomainkitError"),
        ("application.commands", "domainkit.http.problems"),
        ("application.commands", "domainkit.http.problems.APIError"),
        ("domain.model", "domainkit.http.problems"),
        ("domain.model", "domainkit.http.problems.FieldError"),
    }
    assert "domainkit.http.problems" in architecture.ALLOWED_MODULES["infrastructure"]
    assert (
        "the infrastructure layer may import only the __all__ of "
        "domainkit.http.problems"
    ) in rules
    assert {
        "domainkit.http.problems",
        "domainkit.http.problems.APIError",
        "domainkit.http.problems.FieldError",
    } <= forbidden


@pytest.fixture
def web_gateway(make_package: MakePackage) -> str:
    """A file gateway that takes Starlette's types and responses through uploads."""
    make_package(
        "arch.filing",
        {
            "infrastructure/files.py": """
                from domainkit.http.uploads import (
                    CHECKED_PDF,
                    Upload,
                    receive_upload,
                    save_upload,
                )
                from domainkit.http.uploads import UploadFile, StreamingResponse
                from domainkit.http.uploads import serve, StoredUpload
                from domainkit.http.client import HttpClient, httpx2
                from domainkit.http.problems import APIError
                import domainkit.http.uploads
            """,
            "application/commands.py": (
                "from domainkit.http.uploads import UploadFile\n"
            ),
        },
    )
    make_package(
        "arch.flatfiles", {"models.py": "from domainkit.http import uploads\n"}
    )
    return "arch.filing"


def test_names_outside_client_and_uploads_all_are_warned_of(
    web_gateway: str,
) -> None:
    """2.x lets them through with a warning, which 3.0 makes a refusal (ADR 0188).

    A name of the module's ``__all__`` passes, but ``serve``, which builds a
    route's response; one the module only imports (Starlette's types, the
    client's ``httpx2``, ``StoredUpload``) is warned of. The errors stay as
    they were: the application layer is refused, not warned.
    """
    installed = ["arch.filing", "arch.flatfiles"]
    found = {
        (v.module.removeprefix("arch.filing."), v.imported)
        for v in layer_warnings(installed, LayerRules())
    }
    rules = {str(v).partition(": ")[2] for v in layer_warnings(installed, LayerRules())}
    refused = {
        (v.module.removeprefix("arch.filing."), v.imported)
        for v in check_domains(installed, LayerRules())
    }

    assert found == {
        ("infrastructure.files", "domainkit.http.uploads.UploadFile"),
        ("infrastructure.files", "domainkit.http.uploads.StreamingResponse"),
        ("infrastructure.files", "domainkit.http.uploads.serve"),
        ("infrastructure.files", "domainkit.http.uploads.StoredUpload"),
        ("infrastructure.files", "domainkit.http.client.httpx2"),
    }
    assert rules == {
        (
            "the infrastructure layer may import only the __all__ of "
            "domainkit.http.uploads; 3.0 refuses the rest"
        ),
        (
            "the infrastructure layer may import only the __all__ of "
            "domainkit.http.client; 3.0 refuses the rest"
        ),
        (
            "the infrastructure layer may not answer a request with "
            "domainkit.http.uploads.serve, which is its route's; 3.0 refuses it"
        ),
    }
    assert refused == {
        ("application.commands", "domainkit.http.uploads"),
        ("application.commands", "domainkit.http.uploads.UploadFile"),
        ("arch.flatfiles.models", "domainkit.http"),
    }


def test_a_module_forbidden_by_name_is_refused_not_warned_of(
    web_gateway: str,
) -> None:
    """A project that forbids ``domainkit.http.uploads`` gets errors, as in 2.8."""
    extra = {"FORBIDDEN_LIBRARIES": {"infrastructure": ["domainkit.http.uploads"]}}
    with override_settings(ARCHITECTURE=extra):
        warned = {v.imported for v in layer_warnings(["arch.filing"])}
        refused = {v.imported for v in check_domain("arch.filing")}

    assert warned == {"domainkit.http.client.httpx2"}
    assert "domainkit.http.uploads.UploadFile" in refused


def test_check_lists_the_imports_3_0_refuses(make_package: MakePackage) -> None:
    """``architecture.W001``: ``check`` passes, ``SILENCED_CHECKS`` may silence it."""
    from domainkit.management import call_command

    make_package(
        "arch.served",
        {
            "infrastructure/files.py": (
                "from domainkit.http.uploads import UploadFile, save_upload\n"
            ),
        },
    )

    def checked(**change: Any) -> str:  # noqa: ANN401 - settings.
        output = io.StringIO()
        with override_settings(INSTALLED_DOMAINS=[*DOMAINS, "arch.served"], **change):
            call_command("check", stdout=output)
        return output.getvalue()

    listed = checked().splitlines()
    silenced = checked(SILENCED_CHECKS=["architecture.W001"])

    assert listed[0].startswith("System check identified no issues and 1 warning.")
    assert listed[1:] == [
        (
            "  Warning: architecture.W001: arch.served.infrastructure.files imports "
            "domainkit.http.uploads.UploadFile: the infrastructure layer may import "
            "only the __all__ of domainkit.http.uploads; 3.0 refuses the rest"
        )
    ]
    assert silenced.startswith("System check identified no issues (1 silenced).")


@pytest.mark.reads_own_source
def test_the_problems_module_builds_no_response() -> None:
    """``domainkit.http.problems`` holds the two problems and imports nothing more.

    Allowing it lets the infrastructure layer import what it imports by
    name: no FastAPI, no responses, no settings, no database, and none of
    ``domainkit.http``'s other modules (ADR 0182).
    """
    imported = {
        name
        for module, name in iter_imports("domainkit.http")
        if module == "domainkit.http.problems"
    }

    assert problems.__all__ == ["APIError", "FieldError"]
    assert imported == {
        "collections.abc",
        "collections.abc.Mapping",
        "collections.abc.Sequence",
        "typing",
        "typing.TYPE_CHECKING",
        "pydantic",
        "pydantic.BaseModel",
        "starlette.exceptions",
        "starlette.exceptions.HTTPException",
        "domainkit.exceptions",
        "domainkit.exceptions.DomainkitError",
        "domainkit.i18n.lazy",
        "domainkit.i18n.lazy.LazyString",
    }


GRAPH: Final = """
import json, sys
before = set(sys.modules)
import domainkit.http.problems
print(json.dumps(sorted(set(sys.modules) - before)))
"""
"""Import ``domainkit.http.problems`` alone; print every module it loaded."""


def test_importing_the_problems_loads_no_web_framework() -> None:
    """In a fresh interpreter, ``domainkit.http.problems`` loads its graph only.

    Of domainkit, the package, ``exceptions`` and ``http``'s lazy names; of
    Starlette, its exceptions; no FastAPI, no SQLAlchemy, no settings, no
    request or response, and none of ``domainkit.http``'s other modules
    (ADR 0182).
    """
    done = subprocess.run(  # noqa: S603 - the test interpreter and a fixed script.
        [sys.executable, "-c", GRAPH],
        cwd=ROOT,
        env={**os.environ, "PYTHONPATH": str(ROOT / "src")},
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    assert done.returncode == 0, done.stderr
    loaded = set(json.loads(done.stdout))

    def under(*roots: str) -> set[str]:
        return {name for name in loaded if name.partition(".")[0] in roots}

    assert under("domainkit") == {
        "domainkit",
        "domainkit.exceptions",
        "domainkit.http",
        "domainkit.http.problems",
        "domainkit.utils",
        "domainkit.utils.module_loading",
    }
    assert under("starlette") == {"starlette", "starlette.exceptions"}
    assert under("fastapi", "sqlalchemy", "anyio", "httpx2", "multipart") == set()


def test_the_errors_module_still_answers_the_problems() -> None:
    """``domainkit.http.errors`` and ``domainkit.http`` re-export the same classes.

    Code that imports ``APIError`` or ``FieldError`` from either keeps
    working, and a gateway's problem is answered as a route's (2.8).
    """
    app = FastAPI()
    install_exception_handlers(app)

    @app.post("/deeds")
    async def take() -> None:
        raise problems.APIError(
            413,
            "The deed is too large.",
            code="deed_too_large",
            errors=[
                problems.FieldError(loc=["body", "file"], msg="Too large.", type="size")
            ],
        )

    answer = TestClient(app).post("/deeds")

    assert errors.APIError is problems.APIError is http.APIError
    assert errors.FieldError is problems.FieldError is http.FieldError
    assert errors.DomainkitError is DomainkitError
    assert issubclass(problems.APIError, HTTPException)
    assert answer.status_code == 413
    assert answer.json() == {
        "detail": "The deed is too large.",
        "code": "deed_too_large",
        "errors": [{"loc": ["body", "file"], "msg": "Too large.", "type": "size"}],
    }


def test_a_project_that_forbids_the_client_by_name_forbids_it(gateway: str) -> None:
    """``FORBIDDEN_LIBRARIES`` naming the client wins: settings add rules (D2).

    Neither ``ARCHITECTURE`` nor ``extend`` adds exceptions; ``extend``
    keeps the framework's.
    """
    extra = {"FORBIDDEN_LIBRARIES": {"infrastructure": ["domainkit.http.client"]}}
    with override_settings(ARCHITECTURE=extra):
        found = {v.imported for v in check_domain(gateway)}
    extended = LayerRules().extend(forbidden_libraries={"application": ["httpx"]})

    assert {"domainkit.http.client", "domainkit.http.client.HttpClient"} <= found
    assert "domainkit.http.client.CircuitOpen" in found
    assert extended.allowed_modules == LayerRules().allowed_modules
    with pytest.raises(ValidationError, match="Extra inputs"):
        Settings(ARCHITECTURE={"ALLOWED_MODULES": {"application": ["httpx"]}})


def test_a_domain_layer_marks_its_texts_but_looks_none_up(
    make_package: MakePackage,
) -> None:
    """``domainkit.i18n.lazy`` is allowed; the rest of ``domainkit.i18n`` is not.

    Marks and lazy strings read no settings and no catalog when called, so
    the domain layer stays free of them; ``gettext`` reads both (user
    report I2).
    """
    make_package(
        "arch.texts",
        {
            "domain/model.py": """
                from domainkit.ddd import BusinessRuleViolation, TextChoices
                from domainkit.i18n.lazy import gettext_lazy, gettext_noop

                class Status(TextChoices):
                    PAID = "paid", gettext_lazy("Paid in full")

                def pay() -> None:
                    raise BusinessRuleViolation(gettext_noop("Already paid."))
            """,
            "domain/eager.py": """
                from domainkit.i18n import gettext
                from domainkit.i18n.translation import override
            """,
        },
    )

    found = {
        (v.module.removeprefix("arch.texts."), v.imported)
        for v in check_domain("arch.texts", rules=LayerRules())
    }

    assert found == {
        ("domain.eager", "domainkit.i18n"),
        ("domain.eager", "domainkit.i18n.gettext"),
        ("domain.eager", "domainkit.i18n.translation"),
        ("domain.eager", "domainkit.i18n.translation.override"),
    }
