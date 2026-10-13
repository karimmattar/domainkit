"""Layer rules for domain packages."""

import pytest

from domainkit.architecture import Violation, check_domain, iter_imports
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
    with pytest.raises(ValueError, match="not an importable package"):
        check_domain("string")
    with pytest.raises(ValueError, match="not an importable package"):
        check_domain("no_such_package")
    with pytest.raises(ModuleNotFoundError):
        check_domain("no_such_package.child")
