"""Names 0.10 deprecated are gone in 0.11 (``docs/versioning.md``).

An old import fails as Python makes it fail, and type checkers report it:
no module keeps a hook that would answer for a removed name (ADR 0080,
amended in 0.11). The ``type: ignore`` comments below are the type
checker's report: mypy fails on an ignore it does not need.
"""

import importlib
from typing import Final

import pytest

import domainkit.contrib.auth
import domainkit.contrib.auth.access
import domainkit.ddd
import domainkit.ddd.query
from domainkit.contrib.evidence import Evidence
from domainkit.ddd import Selection
from domainkit.ddd.messages import Query as QueryMessage
from domainkit.http import access

AUTH_RULES: Final = (
    "AllowAny",
    "BasePermission",
    "HasObjectPermissions",
    "HasPermissions",
    "HasScope",
    "IsAuthenticated",
    "IsAuthenticatedOrReadOnly",
    "IsUnscoped",
    "ModelPermissions",
    "NotAuthenticated",
    "Public",
    "Require",
    "check_object_permissions",
    "mark_public",
    "open_routes",
)
"""What ``domainkit.contrib.auth`` answered for ``domainkit.http.access`` in 0.10."""
ACCESS_RULES: Final = (
    *AUTH_RULES,
    "AllOf",
    "AnyOf",
    "MODEL_ACTIONS",
    "Not",
    "Rule",
    "SAFE_METHODS",
    "bearer",
    "public",
    "routes_need_users",
    "scopes_of",
    "unguarded_routes",
)
"""What ``domainkit.contrib.auth.access`` answered for it in 0.10."""
KEPT: Final = (
    "AuthenticatedUser",
    "CurrentUser",
    "IsAdminUser",
    "IsSuperuser",
    "acurrent_user",
    "current_user",
)
"""Auth's own: they read auth's users, and stay."""


def test_the_old_import_statements_fail() -> None:
    """As projects wrote them: Python's ``ImportError``, and mypy's error."""
    auth = r"from 'domainkit\.contrib\.auth' "
    with pytest.raises(ImportError, match=f"^cannot import name 'Require' {auth}"):
        from domainkit.contrib.auth import (  # type: ignore[attr-defined]
            Require,  # noqa: F401 - the import is the test.
        )
    with pytest.raises(ImportError, match="cannot import name 'IsAuthenticated'"):
        from domainkit.contrib.auth.access import (  # type: ignore[attr-defined]
            IsAuthenticated,  # noqa: F401 - the import is the test.
        )
    with pytest.raises(ImportError, match="cannot import name 'Query'"):
        from domainkit.ddd.query import (  # type: ignore[attr-defined]
            Query,  # noqa: F401 - the import is the test.
        )


@pytest.mark.parametrize("name", AUTH_RULES)
def test_rule_names_are_gone_from_auth(name: str) -> None:
    """Each is ``domainkit.http.access``'s alone."""
    with pytest.raises(AttributeError, match=f"has no attribute '{name}'$"):
        getattr(domainkit.contrib.auth, name)
    assert name not in dir(domainkit.contrib.auth)
    assert name in access.__all__


@pytest.mark.parametrize("name", ACCESS_RULES)
def test_rule_names_are_gone_from_auth_access(name: str) -> None:
    """``domainkit.contrib.auth.access`` keeps only what reads auth's users."""
    with pytest.raises(AttributeError, match=f"has no attribute '{name}'$"):
        getattr(domainkit.contrib.auth.access, name)
    assert hasattr(access, name)


def test_no_module_answers_for_names_it_lacks() -> None:
    """No ``__getattr__`` hook is left, so type checkers see the modules as they are."""
    for module in (
        domainkit.contrib.auth,
        domainkit.contrib.auth.access,
        domainkit.ddd.query,
    ):
        assert "__getattr__" not in vars(module), module.__name__


def test_auth_keeps_the_rules_about_its_own_users() -> None:
    """``IsAdminUser``, ``CurrentUser``, and the rest still import from auth."""
    for name in KEPT:
        assert name in domainkit.contrib.auth.__all__
        kept = getattr(domainkit.contrib.auth, name)
        assert kept is getattr(domainkit.contrib.auth.access, name)
    assert domainkit.contrib.auth.current_user is access.current_user


def test_the_old_name_of_selection_is_gone() -> None:
    """``ddd.query.Query`` is gone; ``ddd.Query`` is the CQRS message, as before."""
    assert not hasattr(domainkit.ddd.query, "Query")
    assert "Query" not in domainkit.ddd.query.__all__
    assert domainkit.ddd.query.Selection is Selection
    assert domainkit.ddd.Query is QueryMessage
    assert {"Selection", "Query"} <= set(domainkit.ddd.__all__)


def test_the_old_name_of_the_evidence_models_is_gone() -> None:
    """``evidence.model`` is no module; ``evidence`` and its ``models`` are."""
    with pytest.raises(ModuleNotFoundError, match=r"evidence\.model'"):
        importlib.import_module("domainkit.contrib.evidence.model")
    models = importlib.import_module("domainkit.contrib.evidence.models")
    assert models.Evidence is Evidence
    assert Evidence.__module__ == "domainkit.contrib.evidence.models"
