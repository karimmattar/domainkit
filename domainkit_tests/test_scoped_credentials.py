"""Credentials limited to scopes are bounded on every route (AUTH-01).

Before 0.10 a credential limited to scopes passed any route guarded only by
authentication (``Require()``, ``IsAuthenticated``, ``AuthenticatedUser``):
a "read-only" key wrote wherever no permission was checked. A route reached
with one must now declare a rule that checks scopes. In 0.10 the others
still let it through, with a ``DomainkitDeprecationWarning`` and a security
log line (``SCOPED_CREDENTIALS = "warn"``); ``"refuse"`` answers 403, as
0.11 will by default.

Refusals of known principals go to the audit trail, sampled per actor and
route (AUTH-11): ``test_auth_denials.py`` covers the recorder.
"""

import logging
from collections.abc import Collection, Iterator
from dataclasses import dataclass
from typing import Annotated, Any, ClassVar, Final

import pytest
from fastapi import APIRouter, Depends, FastAPI, WebSocket
from fastapi.testclient import TestClient
from starlette.requests import HTTPConnection
from starlette.testclient import WebSocketDenialResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from domainkit.conf import override_settings
from domainkit.deprecation import DomainkitDeprecationWarning
from domainkit.exceptions import ImproperlyConfigured
from domainkit.http import access
from domainkit.http.access import (
    SCOPES_UNBOUNDED,
    AllowScoped,
    AnonymousPrincipal,
    BasePermission,
    HasPermissions,
    HasScope,
    IsAuthenticated,
    IsAuthenticatedOrReadOnly,
    IsUnscoped,
    Not,
    Require,
    check_scopes_bounded,
    reads_principal,
)
from domainkit.http.errors import install_exception_handlers
from domainkit.http.routing import flatten_routes, nests_routers
from domainkit.log import SECURITY_LOGGER

NOTE: Final = "notes.add_note"
VIEW: Final = "notes.view_note"


@dataclass(frozen=True, slots=True)
class Member:
    """A principal holding every permission: only the scopes narrow it."""

    id: str
    is_authenticated: ClassVar[bool] = True
    is_active: ClassVar[bool] = True

    async def ahas_perms(self, perms: Collection[str], obj: object = None) -> bool:
        """Always."""
        return True


@dataclass(frozen=True, slots=True)
class Key:
    """Credentials: ``scopes`` None for a login, a set for a scoped key."""

    scopes: frozenset[str] | None


class HeaderAuthentication:
    """``X-User`` names the principal; ``X-Scopes`` limits its credentials."""

    def __init__(self, app: ASGIApp) -> None:
        """Wrap ``app``."""
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Put the principal and its credentials in the scope."""
        if scope["type"] not in {"http", "websocket"}:
            await self.app(scope, receive, send)
            return
        connection = HTTPConnection(scope)
        name = connection.headers.get("x-user")
        scopes = connection.headers.get("x-scopes")
        scope["user"] = AnonymousPrincipal() if name is None else Member(name)
        scope["auth"] = Key(None if scopes is None else frozenset(scopes.split()))
        await self.app(scope, receive, send)


class ChecksItsOwnScopes(BasePermission):
    """A project's rule that reads the scopes itself."""

    bounds_scopes: ClassVar[bool] = True

    async def has_permission(self, request: HTTPConnection, user: Any) -> bool:  # noqa: ANN401 - a principal.
        """Scoped credentials need ``notes.add_note``."""
        scopes = access.scopes_of(request)
        return user.is_authenticated and (scopes is None or NOTE in scopes)


@reads_principal
async def provider_user(request: HTTPConnection) -> Member:
    """A provider's dependency that reads the principal, as ``AuthenticatedUser``."""
    user = access.current_user(request)
    assert isinstance(user, Member)
    await check_scopes_bounded(request)
    return user


def build(*, flatten: bool) -> FastAPI:
    """Routes guarded every way; included through a router, nested or flattened."""
    router = APIRouter()

    @router.post("/notes", dependencies=[Depends(Require())])
    async def write() -> dict[str, bool]:
        return {"ok": True}

    @router.post("/plain", dependencies=[Depends(Require(IsAuthenticated))])
    async def plain() -> dict[str, bool]:
        return {"ok": True}

    @router.post("/either", dependencies=[Depends(Require(IsAuthenticatedOrReadOnly))])
    async def either() -> dict[str, bool]:
        return {"ok": True}

    rules: dict[str, BasePermission] = {
        "/scope": HasScope(NOTE),
        "/permission": HasPermissions(NOTE),
        "/allowed": AllowScoped(),
        "/own": ChecksItsOwnScopes(),
        "/both": IsAuthenticated() & HasScope(NOTE),
        "/any": IsAuthenticated() | HasScope(NOTE),
        "/any-scoped": HasScope(VIEW) | HasPermissions(NOTE),
        "/not": IsAuthenticated() & Not(HasScope(VIEW)),
        "/unscoped": IsUnscoped(),
    }
    for path, rule in rules.items():
        router.add_api_route(
            path,
            _ok,
            methods=["POST"],
            dependencies=[Depends(Require(rule))],
            operation_id=path.strip("/"),
        )

    @router.post("/provider")
    async def provider(user: Annotated[Member, Depends(provider_user)]) -> str:
        return user.id

    @router.post(
        "/provider-scoped", dependencies=[Depends(Require(HasPermissions(NOTE)))]
    )
    async def provider_scoped(user: Annotated[Member, Depends(provider_user)]) -> str:
        return user.id

    guarded = APIRouter(dependencies=[Depends(Require())])

    @guarded.post("/inner", dependencies=[Depends(Require(HasPermissions(NOTE)))])
    async def inner() -> dict[str, bool]:
        return {"ok": True}

    @guarded.post("/inner-open")
    async def inner_open() -> dict[str, bool]:
        return {"ok": True}

    router.include_router(guarded, prefix="/guarded")
    app = FastAPI()
    app.include_router(router, prefix="/api")
    if flatten:
        flatten_routes(app.router)
    app.add_middleware(HeaderAuthentication)
    install_exception_handlers(app)
    return app


async def _ok() -> dict[str, bool]:
    """Allowed."""
    return {"ok": True}


LOGIN: Final = {"X-User": "alice"}
KEY: Final = {"X-User": "alice", "X-Scopes": NOTE}
NARROW: Final = {"X-User": "alice", "X-Scopes": VIEW}
CHECKED: Final = (
    "/scope",
    "/permission",
    "/allowed",
    "/own",
    "/both",
    "/any-scoped",
    "/provider-scoped",
    "/guarded/inner",
)
UNCHECKED: Final = (
    "/notes",
    "/plain",
    "/either",
    "/any",
    "/not",
    "/provider",
    "/guarded/inner-open",
)


@pytest.fixture(params=["nested", "flattened"])
def client(request: pytest.FixtureRequest) -> Iterator[TestClient]:
    """The application, its routers kept nested by FastAPI or flattened."""
    access.scoped_log.flush()  # Each test sees its routes' first lines.
    flatten = request.param == "flattened"
    if flatten and not nests_routers():  # pragma: no cover - FastAPI before 0.137.
        pytest.skip("this FastAPI copies included routes")
    with TestClient(build(flatten=flatten)) as client:
        yield client


def test_a_scoped_key_writes_where_no_scope_is_checked_with_a_warning(
    client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    """0.10's default keeps the old answer, and says so where projects look.

    The PoC of AUTH-01: a key limited to ``notes.add_note`` passes routes
    guarded by authentication alone. Tests that make deprecations errors
    fail on it; deployments find the security log line.
    """
    caplog.set_level(logging.WARNING, logger=SECURITY_LOGGER)
    for path in UNCHECKED:
        with pytest.warns(DomainkitDeprecationWarning) as warned:
            response = client.post(f"/api{path}", headers=KEY)
        assert response.status_code == 200, path
        (message,) = [str(each.message) for each in warned]
        assert f"POST /api{path}" in message
        assert "removed in 0.11" in message
        assert "HasScope(...), a permission rule, or AllowScoped()" in message
    logged = [r.getMessage() for r in caplog.records if r.name == SECURITY_LOGGER]
    assert len(logged) == len(UNCHECKED)
    assert all("SCOPED_CREDENTIALS" in line for line in logged)
    assert NOTE not in str(logged)  # Routes, never the scopes.


def test_scoped_keys_are_refused_where_no_scope_is_checked(client: TestClient) -> None:
    """``SCOPED_CREDENTIALS = "refuse"``: 0.11's answer, 403, now.

    The same requests with a login, or without the scope limit, still pass.
    """
    with override_settings(SCOPED_CREDENTIALS="refuse"):
        for path in UNCHECKED:
            refused = client.post(f"/api{path}", headers=KEY)
            assert refused.status_code == 403, path
            assert refused.json()["detail"] == SCOPES_UNBOUNDED
            assert refused.json()["code"] == "permission_denied"
            login = client.post(f"/api{path}", headers=LOGIN).status_code
            assert login == (403 if path == "/not" else 200)  # Its own rule.
        assert client.post("/api/notes").status_code == 401


@pytest.mark.parametrize("mode", ["warn", "refuse"])
def test_routes_that_check_scopes_hold_scoped_keys_silently(
    client: TestClient, mode: str
) -> None:
    """A rule that checks scopes, on the route or a router, anywhere in an ``&``.

    ``AnyOf`` checks them only if every branch does; ``Not`` never does.
    The key's own scopes decide there, as before.
    """
    with override_settings(SCOPED_CREDENTIALS=mode):  # Warnings are errors here.
        for path in CHECKED:
            assert client.post(f"/api{path}", headers=KEY).status_code == 200, path
            assert client.post(f"/api{path}", headers=LOGIN).status_code == 200
        assert client.post("/api/scope", headers=NARROW).status_code == 403
        assert client.post("/api/guarded/inner", headers=NARROW).status_code == 403
        unscoped = client.post("/api/unscoped", headers=KEY)
        assert unscoped.json()["detail"] == IsUnscoped.message
        assert client.post("/api/unscoped", headers=LOGIN).status_code == 200


def test_which_rules_check_scopes() -> None:
    """Rules that check scopes, and how combinations carry it."""
    assert IsUnscoped().checks_scopes()
    assert AllowScoped().checks_scopes()
    assert HasScope(NOTE).checks_scopes()
    assert HasPermissions(NOTE).checks_scopes()
    assert not IsAuthenticated().checks_scopes()
    assert not BasePermission().checks_scopes()
    assert (IsAuthenticated() & HasScope(NOTE)).checks_scopes()
    assert not (IsAuthenticated() | HasScope(NOTE)).checks_scopes()
    assert (HasScope(NOTE) | HasPermissions(VIEW)).checks_scopes()
    assert not (~HasScope(NOTE)).checks_scopes()


def test_open_connections_with_scoped_keys_end_where_scopes_are_not_checked() -> None:
    """A WebSocket's rules are checked again while it lasts, scopes included."""
    app = build(flatten=False)
    allows = Require().allows
    checked = Require(HasScope(NOTE)).allows
    seen: list[tuple[bool, bool, bool]] = []

    @app.websocket("/live", dependencies=[Depends(Require())])
    async def live(websocket: WebSocket) -> None:
        now = await allows(websocket), await checked(websocket)
        with override_settings(SCOPED_CREDENTIALS="refuse"):
            seen.append((*now, await allows(websocket)))
        await websocket.accept()
        await websocket.close()

    with TestClient(app) as client:
        with (
            pytest.warns(DomainkitDeprecationWarning),
            client.websocket_connect("/live", headers=KEY),
        ):
            pass
        with override_settings(SCOPED_CREDENTIALS="refuse"):
            with pytest.raises(WebSocketDenialResponse) as refused:
                client.websocket_connect("/live", headers=KEY).__enter__()
            with client.websocket_connect("/live", headers=LOGIN):
                pass

    assert refused.value.status_code == 403
    assert seen == [(True, True, False), (True, True, True)]


def test_scoped_keys_without_a_known_route_are_held_to_the_rule() -> None:
    """A request no route matched declares nothing: scoped keys are refused."""
    scope: dict[str, Any] = {
        "type": "http",
        "method": "POST",
        "path": "/x",
        "headers": [],
        "user": Member("alice"),
        "auth": Key(frozenset({NOTE})),
    }
    connection = HTTPConnection(scope)
    assert not access._route_checks_scopes(connection)
    assert access._route_name(connection) == "POST unmatched"
    with override_settings(SCOPED_CREDENTIALS="refuse"):
        assert access._scopes_refused(connection)
    scope["auth"] = Key(None)
    with override_settings(SCOPED_CREDENTIALS="refuse"):
        assert not access._scopes_refused(connection)


def test_the_setting_is_validated() -> None:
    """Only ``warn`` or ``refuse``."""
    with (
        pytest.raises(ImproperlyConfigured, match="SCOPED_CREDENTIALS"),
        override_settings(SCOPED_CREDENTIALS="sometimes"),
    ):
        pass


def test_credentials_without_a_scopes_attribute_carry_none() -> None:
    """A provider's own credentials, with no ``scopes``, are never held to it."""
    assert not access._scoped({"auth": object()})
    assert not access._scoped({"auth": None})
    assert not access._scoped({})
    assert access._scoped({"auth": Key(frozenset())})
