"""Stepping up: ``StepUp`` asks for a second factor proven again, recently."""

import asyncio
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from importlib import import_module
from typing import Any
from uuid import uuid4

import jwt
import pytest
from fastapi import Depends, FastAPI, WebSocket
from fastapi.testclient import TestClient
from sqlalchemy import select
from starlette.requests import HTTPConnection
from starlette.types import ASGIApp, Receive, Scope, Send
from starlette.websockets import WebSocketDisconnect

from domainkit import clock
from domainkit.clock import FrozenClock, freeze_time
from domainkit.contrib.auth import (
    AnonymousUser,
    AuthUnitOfWork,
    Credentials,
    Proof,
    StepUp,
    StepUpRequired,
)
from domainkit.contrib.auth.access import PROOF, proof_of
from domainkit.contrib.auth.apps import unprovable_step_ups
from domainkit.contrib.auth.authentication import (
    RECHECK,
    JWTAuthentication,
    _proof_claims,
    get_authenticators,
)
from domainkit.contrib.auth.middleware import AuthenticationMiddleware
from domainkit.contrib.auth.tables import step_ups
from domainkit.db import connections
from domainkit.ddd import PermissionDenied
from domainkit.exceptions import ImproperlyConfigured
from domainkit.http.access import (
    AllOf,
    AnyOf,
    BasePermission,
    HasPermissions,
    Require,
    check_object_permissions,
)
from domainkit.http.application import application_problems
from domainkit.http.errors import install_exception_handlers
from domainkit.management import call_command
from domainkit.management.base import run_async
from domainkit_tests.conftest import Api, UseAuth
from domainkit_tests.gadgets import InMemoryGadgets, make_gadget
from domainkit_tests.test_auth_http import (
    JWT,
    PASSWORD,
    audited,
    bearer,
    create_user,
    grant,
    login,
)
from domainkit_tests.test_auth_mfa import code_at, secret_of
from domainkit_tests.test_auth_principals import scope as principal_scope
from domainkit_tests.test_resources import (
    LOCATION,
    RenameGadget,
    RenameIn,
    Users,
    as_,
    everyone,
    gadgets,
    rename,
)

START = "2027-01-04T10:00:00Z"
SITE = "https://testserver"
TRUSTED = {"Origin": SITE}
STEP_UP_REQUIRED = (
    'Bearer realm="api", error="insufficient_user_authentication", max_age="300"'
)


@pytest.fixture
def frozen() -> Iterator[FrozenClock]:
    """Codes, step-ups, and their ages on one clock that moves when told."""
    with freeze_time(START) as clock:
        yield clock


def now_code(secret: bytes, clock: FrozenClock) -> str:
    """The authenticator's code now, a step after the last one used."""
    clock.tick(30)
    return code_at(secret, clock.tick(0).timestamp())


def enrol(client: TestClient, headers: dict[str, str], clock: FrozenClock) -> bytes:
    """Turn alice's second factor on; return its secret."""
    started = client.post(
        "/auth/mfa/totp", json={"password": PASSWORD}, headers=headers
    )
    assert started.status_code == 200, started.json()
    secret = secret_of(started.json())
    confirmed = client.post(
        "/auth/mfa/totp/confirm",
        json={"code": now_code(secret, clock)},
        headers=headers,
    )
    assert confirmed.status_code == 200, confirmed.json()
    return secret


def step_up(client: TestClient, headers: dict[str, str], code: str, **body: str) -> Any:  # noqa: ANN401 - a response.
    """Post a code (and more) to ``/auth/step-up``."""
    return client.post("/auth/step-up", json={"code": code, **body}, headers=headers)


def step_up_rows() -> list[dict[str, Any]]:
    """Every stored step-up."""

    async def read() -> list[dict[str, Any]]:
        async with connections.engine().connect() as connection:
            return [
                dict(row._mapping) for row in await connection.execute(select(step_ups))
            ]

    return run_async(read())


# Login tokens ---------------------------------------------------------------------


def test_a_login_token_steps_up_for_max_age(identity: Api, frozen: FrozenClock) -> None:
    """Refused with step_up_required, stepped up with a code, then refused again."""
    alice = create_user("alice")
    client = identity()
    headers = bearer(login(client, "alice")["access_token"])
    unenrolled = step_up(client, headers, "123456")
    secret = enrol(client, headers, frozen)

    refused = client.post("/payees", headers=headers)
    wrong = step_up(client, headers, "000000")
    stepped = step_up(client, headers, now_code(secret, frozen))
    allowed = client.post("/payees", headers=headers)
    frozen.tick(timedelta(minutes=5))
    still = client.post("/payees", headers=headers)
    frozen.tick(1)
    expired = client.post("/payees", headers=headers)

    assert (unenrolled.status_code, unenrolled.json()["code"]) == (
        409,
        "no_second_factor",
    )
    assert unenrolled.json()["detail"] == (
        "Turn on a second factor first: confirming it is you needs one."
    )
    assert (refused.status_code, refused.json()["code"]) == (401, "step_up_required")
    assert refused.json()["detail"] == (
        "Confirm it is you: authenticate again to do this."
    )
    assert refused.headers["WWW-Authenticate"] == STEP_UP_REQUIRED
    assert (wrong.status_code, wrong.json()["code"]) == (401, "invalid_code")
    assert stepped.status_code == 200, stepped.json()
    assert stepped.json() == {"auth_time": "2027-01-04T10:01:00Z", "amr": ["otp"]}
    assert stepped.headers["Cache-Control"] == "no-store"
    assert allowed.json() == {"user": "alice", "scheme": "token"}
    assert still.status_code == 200
    assert (expired.status_code, expired.json()["code"]) == (401, "step_up_required")
    [row] = step_up_rows()
    assert (row["user_id"], row["amr"]) == (alice, "otp")
    assert [row["details"]["reason"] for row in audited("auth.step_up")] == [
        "invalid_code"
    ]


def test_a_step_up_holds_for_its_own_login_only(
    identity: Api, frozen: FrozenClock
) -> None:
    """Another login of the user, and another user, have stepped up nothing."""
    create_user("alice")
    create_user("bob")
    client = identity()
    first = bearer(login(client, "alice")["access_token"])
    second = bearer(login(client, "alice")["access_token"])
    bob = bearer(login(client, "bob")["access_token"])
    secret = enrol(client, first, frozen)

    assert step_up(client, first, now_code(secret, frozen)).status_code == 200

    assert client.post("/payees", headers=first).status_code == 200
    assert client.post("/payees", headers=second).status_code == 401
    assert client.post("/payees", headers=bob).status_code == 401


def test_the_next_step_up_replaces_the_last(identity: Api, frozen: FrozenClock) -> None:
    """One row per login: its newest step-up."""
    create_user("alice")
    client = identity()
    headers = bearer(login(client, "alice")["access_token"])
    secret = enrol(client, headers, frozen)

    step_up(client, headers, now_code(secret, frozen))
    frozen.tick(timedelta(minutes=4))
    later = step_up(client, headers, now_code(secret, frozen))
    frozen.tick(timedelta(minutes=4))

    assert later.json()["auth_time"] == "2027-01-04T10:05:30Z"
    assert len(step_up_rows()) == 1
    assert client.post("/payees", headers=headers).status_code == 200


def test_recovery_codes_do_not_step_up(identity: Api, frozen: FrozenClock) -> None:
    """Nor are they spent trying: they stand in for a lost app at login only."""
    create_user("alice")
    client = identity()
    headers = bearer(login(client, "alice")["access_token"])
    started = client.post(
        "/auth/mfa/totp", json={"password": PASSWORD}, headers=headers
    )
    secret = secret_of(started.json())
    codes = client.post(
        "/auth/mfa/totp/confirm",
        json={"code": now_code(secret, frozen)},
        headers=headers,
    ).json()["recovery_codes"]

    refused = step_up(client, headers, codes[0])

    assert (refused.status_code, refused.json()["code"]) == (401, "invalid_code")
    assert client.get("/auth/mfa", headers=headers).json()["recovery_codes_left"] == 10


def test_a_code_steps_up_once(identity: Api, frozen: FrozenClock) -> None:
    """The step of a code that stepped up is spent, as at login."""
    create_user("alice")
    client = identity()
    headers = bearer(login(client, "alice")["access_token"])
    secret = enrol(client, headers, frozen)
    code = now_code(secret, frozen)

    assert step_up(client, headers, code).status_code == 200
    assert step_up(client, headers, code).json()["code"] == "invalid_code"


def test_wrong_codes_count_against_the_users_code_rate(
    identity: Api, frozen: FrozenClock
) -> None:
    """Step-ups share ``MFA.RATE`` with every other code check of the user."""
    create_user("alice")
    client = identity(MFA={"RATE": "2/hour"})
    headers = bearer(login(client, "alice")["access_token"])
    secret = enrol(client, headers, frozen)

    answers = [step_up(client, headers, "000000").status_code for _ in range(2)]
    held = step_up(client, headers, now_code(secret, frozen))

    assert answers == [401, 401]
    assert (held.status_code, held.json()["code"]) == (429, "throttled")


def test_scoped_credentials_cannot_step_up(identity: Api, frozen: FrozenClock) -> None:
    """A token limited to scopes is no login to confirm: 403."""
    create_user("alice")
    grant("alice", "shop.view_product")
    client = identity()
    headers = bearer(login(client, "alice")["access_token"])
    secret = enrol(client, headers, frozen)
    scoped = client.post(
        "/auth/tokens",
        json={"name": "ci", "scopes": ["shop.view_product"]},
        headers=headers,
    ).json()["token"]

    refused = step_up(client, bearer(scoped), now_code(secret, frozen))

    assert refused.status_code == 403


# The password too -----------------------------------------------------------------


def test_the_password_too_where_the_project_asks(
    identity: Api, frozen: FrozenClock
) -> None:
    """``STEP_UP.METHODS = ("pwd", "otp")``: both, the password checked first."""
    create_user("alice")
    client = identity(STEP_UP={"METHODS": ["pwd", "otp"]})
    headers = bearer(login(client, "alice")["access_token"])
    secret = enrol(client, headers, frozen)
    code = now_code(secret, frozen)

    missing = step_up(client, headers, code)
    wrong = step_up(client, headers, code, password="nope")
    both = step_up(client, headers, code, password=PASSWORD)

    assert (missing.status_code, missing.json()["code"]) == (400, "password_required")
    assert missing.json()["detail"] == "Enter your password as well."
    assert (wrong.status_code, wrong.json()["code"]) == (400, "wrong_password")
    assert wrong.json()["detail"] == "Your password was entered incorrectly."
    assert both.json()["amr"] == ["otp", "pwd"]
    grant("alice", "shop.view_product")
    assert client.post("/payees/strict", headers=headers).status_code == 200
    assert [row["details"]["reason"] for row in audited("auth.step_up")] == [
        "wrong_password"
    ]


def test_wrong_passwords_count_against_the_login_rate(
    identity: Api, frozen: FrozenClock
) -> None:
    """As every password check of a signed-in user does."""
    create_user("alice")
    client = identity(STEP_UP={"METHODS": ["pwd", "otp"]}, LOGIN_RATE="2/hour")
    headers = bearer(login(client, "alice")["access_token"])
    secret = enrol(client, headers, frozen)

    for _ in range(2):
        step_up(client, headers, "000000", password="nope")
    held = step_up(client, headers, now_code(secret, frozen), password=PASSWORD)
    changing = client.post(
        "/auth/password",
        json={"old_password": PASSWORD, "new_password": "Kq7-zv93-Lmw2"},
        headers=headers,
    )

    assert (held.status_code, held.json()["code"]) == (429, "login_throttled")
    assert changing.status_code == 429  # The user's own count of password checks.


def test_a_plain_denial_answers_before_a_step_up(
    identity: Api, frozen: FrozenClock
) -> None:
    """``StepUp(...) & HasPermissions(...)``: lacking the permission is a 403.

    Recorded as a denial, never a step-up that could not help (SEC-15c).
    """
    create_user("alice")
    client = identity()
    headers = bearer(login(client, "alice")["access_token"])
    secret = enrol(client, headers, frozen)

    lacking = client.post("/payees/strict", headers=headers)
    grant("alice", "shop.view_product")
    not_stepped_up = client.post("/payees/strict", headers=headers)
    step_up(client, headers, now_code(secret, frozen))
    allowed = client.post("/payees/strict", headers=headers)

    assert (lacking.status_code, lacking.json()["code"]) == (403, "permission_denied")
    assert [row["details"]["rule"] for row in audited("auth.access")] == ["AllOf"]
    assert (not_stepped_up.status_code, not_stepped_up.json()["code"]) == (
        401,
        "step_up_required",
    )
    assert allowed.json() == {"ok": True}


def test_step_ups_that_no_step_up_proves_are_found(use_auth: UseAuth) -> None:
    """``auth.E106``: methods beyond ``STEP_UP.METHODS``, in ``&``, ``|``, ``~``."""
    use_auth()
    app = FastAPI()
    beyond = StepUp(max_age=timedelta(minutes=5), methods=("pwd", "otp"))

    @app.get("/plain", dependencies=[Depends(Require(StepUp(max_age=timedelta(1))))])
    async def plain() -> None: ...

    @app.get("/both", dependencies=[Depends(Require(HasPermissions("a.b") & beyond))])
    async def both() -> None: ...

    @app.get(
        "/either", dependencies=[Depends(Require(~beyond | HasPermissions("a.b")))]
    )
    async def either() -> None: ...

    @app.get(
        "/twice",
        dependencies=[Depends(Require(beyond, StepUp(max_age=timedelta(minutes=1))))],
    )
    async def twice() -> None: ...

    problems = [
        problem
        for problem in application_problems(app)
        if getattr(problem, "id", None) == "auth.E106"
    ]

    assert unprovable_step_ups(app) == [
        ("GET /both", frozenset({"pwd"})),
        ("GET /either", frozenset({"pwd"})),
        ("GET /twice", frozenset({"pwd"})),
    ]
    assert len(problems) == 3
    assert str(problems[0]).startswith(
        "GET /both asks StepUp for pwd, which no step-up proves "
        "(AUTH.STEP_UP.METHODS is ['otp']): it refuses everyone."
    )


# Browser sessions and JSON Web Tokens -----------------------------------------------


def test_a_browser_session_steps_up(identity: Api, frozen: FrozenClock) -> None:
    """With its CSRF token, as every unsafe request of a session."""
    create_user("alice")
    client = identity()
    secret = enrol(client, bearer(login(client, "alice")["access_token"]), frozen)
    browser = TestClient(client.app, base_url=SITE)
    started = browser.post(
        "/auth/session",
        json={"username": "alice", "password": PASSWORD},
        headers=TRUSTED,
    )
    challenge = started.json()["mfa_token"]
    csrf = browser.post(
        "/auth/session/mfa",
        json={"mfa_token": challenge, "code": now_code(secret, frozen)},
        headers=TRUSTED,
    ).json()["csrf_token"]
    headers = {**TRUSTED, "X-CSRF-Token": csrf}

    after_login = browser.post("/payees", headers=headers)
    stepped = step_up(browser, headers, now_code(secret, frozen))
    allowed = browser.post("/payees", headers=headers)

    assert after_login.status_code == 401
    assert stepped.status_code == 200
    assert allowed.json() == {"user": "alice", "scheme": "cookie"}


def test_a_jwt_login_steps_up_in_a_new_access_token(
    identity: Api, frozen: FrozenClock
) -> None:
    """``auth_time`` and ``amr`` claims; the old token and a refresh lack them."""
    create_user("alice")
    client = identity(**JWT)
    issued = login(client, "alice")
    old = bearer(issued["access_token"])
    secret = enrol(client, old, frozen)

    stepped = step_up(client, old, now_code(secret, frozen)).json()
    new = bearer(stepped["access_token"])
    claims = jwt.decode(stepped["access_token"], options={"verify_signature": False})
    refreshed = client.post(
        "/auth/refresh", json={"refresh_token": issued["refresh_token"]}
    ).json()

    assert set(stepped) == {
        "auth_time",
        "amr",
        "access_token",
        "token_type",
        "expires_in",
    }
    assert (stepped["token_type"], stepped["expires_in"]) == ("Bearer", 300)
    assert claims["auth_time"] == int(
        datetime(2027, 1, 4, 10, 1, tzinfo=UTC).timestamp()
    )
    assert claims["amr"] == ["otp"]
    assert client.post("/payees", headers=new).json()["scheme"] == "jwt"
    assert client.post("/payees", headers=old).status_code == 401
    assert (
        client.post("/payees", headers=bearer(refreshed["access_token"])).status_code
        == 401
    )
    assert step_up_rows() == []


def test_claims_that_do_not_read_as_a_step_up_are_none() -> None:
    """Our key signed them; anything malformed is no step-up."""
    when = int(datetime(2027, 1, 4, tzinfo=UTC).timestamp())

    assert _proof_claims({"auth_time": when, "amr": ["otp"]}) == (
        datetime(2027, 1, 4, tzinfo=UTC),
        frozenset({"otp"}),
    )
    for claims in (
        {},
        {"auth_time": when},
        {"amr": ["otp"]},
        {"auth_time": str(when), "amr": ["otp"]},
        {"auth_time": True, "amr": ["otp"]},
        {"auth_time": when, "amr": "otp"},
        {"auth_time": when, "amr": ["otp", 1]},
        {"auth_time": 10**20, "amr": ["otp"]},
    ):
        assert _proof_claims(claims) == (None, frozenset())


def test_credentials_of_other_kinds_cannot_step_up(
    identity: Api, frozen: FrozenClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A certificate (or a class of a project's own) keeps no step-up: 400."""
    create_user("alice")
    client = identity()
    headers = bearer(login(client, "alice")["access_token"])
    secret = enrol(client, headers, frozen)
    original = Credentials.__init__

    def as_certificate(self: Credentials, *args: Any, **kwargs: Any) -> None:  # noqa: ANN401
        kwargs["scheme"] = "certificate"
        original(self, *args, **kwargs)

    monkeypatch.setattr(Credentials, "__init__", as_certificate)
    refused = step_up(client, headers, now_code(secret, frozen))

    assert (refused.status_code, refused.json()["code"]) == (400, "step_up_unsupported")


# The rule ---------------------------------------------------------------------------


def connection(credentials: object = None, **scope: Any) -> HTTPConnection:  # noqa: ANN401
    """A request whose credentials are ``credentials``."""
    return HTTPConnection({"type": "http", "headers": [], "auth": credentials, **scope})


def test_the_rule_takes_ages_up_to_a_day(use_auth: UseAuth) -> None:
    """And methods with the second factor, each once."""
    use_auth()
    for age in (timedelta(0), timedelta(seconds=-1), timedelta(days=1, seconds=1)):
        with pytest.raises(ImproperlyConfigured) as refused:
            StepUp(max_age=age)
        assert str(refused.value) == (
            "StepUp(max_age=...) must be more than 0 and a day at most."
        )
    wrong: list[Any] = [("pwd",), ("otp", "otp"), ()]
    for methods in wrong:
        with pytest.raises(ImproperlyConfigured, match=r"StepUp\(methods=\.\.\.\)"):
            StepUp(max_age=timedelta(minutes=1), methods=methods)

    assert StepUp(max_age=timedelta(days=1)).required() == {"otp"}
    assert StepUp(max_age=timedelta(1), methods=["pwd", "otp"]).required() == {
        "pwd",
        "otp",
    }


@pytest.mark.anyio
async def test_the_rule_reads_the_proof_once_a_request(
    use_auth: UseAuth, frozen: FrozenClock
) -> None:
    """Anonymous callers and other principals' credentials have none."""
    use_auth()
    rule = StepUp(max_age=timedelta(minutes=5))
    alice = Credentials(
        AnonymousUser(),  # type: ignore[arg-type]
        scheme="jwt",
        auth_time=frozen.tick(0) - timedelta(minutes=5),
        amr=frozenset({"otp", "pwd"}),
    )
    request = connection(alice)

    assert await rule.has_permission(connection(alice), AnonymousUser()) is False
    assert await proof_of(connection(object())) is None
    assert await proof_of(connection()) is None
    assert await proof_of(request) == Proof(alice.auth_time, alice.amr)  # type: ignore[arg-type]
    request.scope["auth"] = None  # Read once: the scope keeps it.
    assert await proof_of(request) == Proof(alice.auth_time, alice.amr)  # type: ignore[arg-type]


class Known:
    """A principal who proved who they are."""

    id = uuid4()
    is_authenticated = True
    is_active = True

    async def ahas_perms(self, perms: object, obj: object = None) -> bool:
        """Nothing."""
        return False


KNOWN: Any = Known()
"""The principal, as any user type a rule is written for."""


@pytest.mark.anyio
async def test_an_open_connection_closes_once_its_step_up_is_too_old(
    use_auth: UseAuth, frozen: FrozenClock
) -> None:
    """Each re-check asks again: past ``max_age``, the rule refuses (SEC-15c)."""
    use_auth()
    rule = StepUp(max_age=timedelta(minutes=5))
    credentials = Credentials(
        KNOWN,
        scheme="jwt",
        auth_time=frozen.tick(0),
        amr=frozenset({"otp"}),
    )
    request = connection(credentials)

    assert await rule.has_permission(request, KNOWN) is True
    assert await rule.has_object_permission(request, KNOWN, object()) is True
    assert request.scope[PROOF] == Proof(credentials.auth_time, credentials.amr)  # type: ignore[arg-type]
    frozen.tick(timedelta(minutes=5, seconds=1))
    assert await rule.has_permission(request, KNOWN) is False
    assert await rule.has_object_permission(request, KNOWN, object()) is False


@pytest.mark.anyio
async def test_the_proof_must_hold_every_method_and_be_recent(
    use_auth: UseAuth, frozen: FrozenClock
) -> None:
    """Exactly ``max_age`` holds; a second more, or a method short, does not."""
    use_auth()
    rule = StepUp(max_age=timedelta(minutes=5), methods=("pwd", "otp"))

    def made(age: timedelta, *methods: str) -> HTTPConnection:
        return connection(
            Credentials(
                KNOWN,
                scheme="jwt",
                auth_time=frozen.tick(0) - age,
                amr=frozenset(methods),
            )
        )

    assert await rule.has_permission(made(timedelta(minutes=5), "otp", "pwd"), KNOWN)
    assert not await rule.has_permission(
        made(timedelta(minutes=5, seconds=1), "otp", "pwd"), KNOWN
    )
    assert not await rule.has_permission(made(timedelta(0), "otp"), KNOWN)
    assert await rule.has_permission(made(timedelta(0), "otp", "pwd", "hwk"), KNOWN)


@pytest.mark.anyio
async def test_refusals_name_the_rule_inside_and_only(use_auth: UseAuth) -> None:
    """``&`` answers its first refusing rule's own; ``|`` and plain rules, 403."""
    use_auth()
    rule = StepUp(max_age=timedelta(minutes=5))
    known = KNOWN
    request = connection(None)

    inside = await AllOf(BasePermission(), rule).refusal(request, known)
    either = await AnyOf(rule, ~BasePermission()).refusal(request, known)
    passing = await AllOf(BasePermission()).refusal(request, known)

    assert isinstance(inside, StepUpRequired)
    assert inside.max_age == timedelta(minutes=5)
    assert inside.methods == {"otp"}
    assert inside.headers == {"WWW-Authenticate": STEP_UP_REQUIRED}
    assert either is None
    assert passing is None
    assert await HasPermissions("a.b").refusal(request, known) is None


class Stepped:
    """Puts a stepped-up principal's credentials in the scope, as a middleware."""

    def __init__(self, app: ASGIApp, *, at: datetime) -> None:
        """Wrap ``app``; the step-up was at ``at``."""
        self.app = app
        self.at = at

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Set ``user`` and ``auth``."""
        if scope["type"] in {"http", "websocket"}:
            user = Known()
            scope["user"] = user
            scope["auth"] = Credentials(
                user,  # type: ignore[arg-type]
                scheme="jwt",
                auth_time=self.at,
                amr=frozenset({"otp"}),
            )
        await self.app(scope, receive, send)


def test_websockets_take_the_rule(use_auth: UseAuth, frozen: FrozenClock) -> None:
    """A handshake without a recent step-up is refused; with one, it opens."""
    use_auth()

    def served(at: datetime) -> TestClient:
        app = FastAPI()
        install_exception_handlers(app)

        @app.websocket(
            "/live",
            dependencies=[Depends(Require(StepUp(max_age=timedelta(minutes=5))))],
        )
        async def live(websocket: WebSocket) -> None:
            await websocket.accept()
            await websocket.send_json({"ok": True})
            await websocket.close()

        app.add_middleware(Stepped, at=at)
        return TestClient(app)

    with served(frozen.tick(0)).websocket_connect("/live") as socket:
        assert socket.receive_json() == {"ok": True}
    with (
        pytest.raises(WebSocketDisconnect),
        served(frozen.tick(0) - timedelta(minutes=6)).websocket_connect("/live"),
    ):
        pass  # pragma: no cover - refused at the handshake.


def test_the_identity_websocket_takes_a_step_up(
    identity: Api, frozen: FrozenClock
) -> None:
    """Through the auth middleware, a login token's step-up opens it."""
    create_user("alice")
    client = identity()
    headers = bearer(login(client, "alice")["access_token"])
    secret = enrol(client, headers, frozen)

    with (
        pytest.raises(WebSocketDisconnect),
        client.websocket_connect("/live/stepped", headers=headers),
    ):
        pass  # pragma: no cover - refused at the handshake.
    step_up(client, headers, now_code(secret, frozen))
    with client.websocket_connect("/live/stepped", headers=headers) as socket:
        assert socket.receive_json() == {"user": "alice"}
        socket.send_text("bye")


# Keeping and clearing ---------------------------------------------------------------


async def hourly_purge() -> None:
    """Run the hourly task that clears tokens, imported within the test.

    Imported at collection, it would stay registered past the test plugin's
    reset of the tasks, and clash with a test that registers it afresh.
    """
    await import_module("domainkit.contrib.auth.tasks").clear_expired_tokens()


def test_old_step_ups_are_cleared_by_the_hour_and_by_command(
    identity: Api, frozen: FrozenClock
) -> None:
    """Older than a day, no rule takes them: they go."""
    create_user("alice")
    client = identity()
    headers = bearer(login(client, "alice")["access_token"])
    secret = enrol(client, headers, frozen)
    step_up(client, headers, now_code(secret, frozen))
    frozen.tick(timedelta(days=1))

    run_async(hourly_purge())
    kept = step_up_rows()
    frozen.tick(1)
    call_command("cleartokens", stdout=None)

    assert len(kept) == 1
    assert step_up_rows() == []


def test_step_ups_go_with_the_users_credentials(
    identity: Api, frozen: FrozenClock
) -> None:
    """Deleting or erasing a user deletes its step-ups (``forget_credentials``)."""
    alice = create_user("alice")
    client = identity()
    headers = bearer(login(client, "alice")["access_token"])
    secret = enrol(client, headers, frozen)
    step_up(client, headers, now_code(secret, frozen))

    async def forget() -> None:
        async with AuthUnitOfWork() as uow:
            await uow.users.forget_credentials(alice)
            await uow.commit()

    run_async(forget())

    assert step_up_rows() == []


class SteppedUpWhen:
    """Credentials of ``scope["user"]`` that stepped up ``X-Stepped-Up`` seconds ago."""

    def __init__(self, app: ASGIApp) -> None:
        """Wrap ``app``."""
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Set ``auth`` from the header, if sent."""
        ago = dict(scope.get("headers", [])).get(b"x-stepped-up")
        if scope["type"] == "http" and ago is not None:
            scope["auth"] = Credentials(
                scope["user"],
                scheme="jwt",
                auth_time=clock.now() - timedelta(seconds=int(ago)),
                amr=frozenset({"otp"}),
            )
        await self.app(scope, receive, send)


def test_resource_actions_take_the_rule(use_auth: UseAuth, frozen: FrozenClock) -> None:
    """A command's ``rule=StepUp(...)``: refused with step_up_required, then served."""
    use_auth()
    store = InMemoryGadgets([make_gadget("a")])
    resource = gadgets(store)
    resource.command_route(
        RenameGadget,
        service=rename,
        body=RenameIn,
        path="/rename",
        rule=StepUp(max_age=timedelta(minutes=5)),
    )
    app = FastAPI()
    install_exception_handlers(app)
    app.include_router(resource)
    app.add_middleware(SteppedUpWhen)
    app.add_middleware(Users, users=everyone())
    client = TestClient(app)
    gadget = next(iter(store.items))

    def renamed(**headers: str) -> Any:  # noqa: ANN401 - a response.
        return client.post(
            f"{LOCATION}{gadget}/rename",
            json={"name": "b"},
            headers=as_("changer", **headers),
        )

    never = renamed()
    stale = renamed(**{"X-Stepped-Up": "301"})
    fresh = renamed(**{"X-Stepped-Up": "60"})

    assert (never.status_code, never.json()["code"]) == (401, "step_up_required")
    assert stale.status_code == 401
    assert fresh.status_code == 200, fresh.json()


@pytest.mark.anyio
async def test_only_tokens_and_sessions_look_their_step_up_up() -> None:
    """Other schemes, and credentials without a token, read nothing."""
    for credentials in (
        Credentials(KNOWN, scheme="certificate", token_id="digest"),
        Credentials(KNOWN, scheme="token"),
        Credentials(KNOWN, scheme="cookie"),
        Credentials(KNOWN, scheme="jwt", session=uuid4()),
    ):
        assert await credentials.proof() is None


class Told(BasePermission):
    """Refuses, naming the request and the principal it refused."""

    async def has_permission(self, request: HTTPConnection, user: Any) -> bool:  # noqa: ANN401
        """Never."""
        return False

    async def refusal(self, request: HTTPConnection, user: Any) -> Exception:  # noqa: ANN401
        """An error that carries both."""
        return LookupError(request, user)


@pytest.mark.anyio
async def test_all_of_passes_on_the_request_and_the_principal() -> None:
    """The inner rule's refusal sees what the outer one was asked about."""
    request = connection(None)

    refusal = await AllOf(Told()).refusal(request, KNOWN)

    assert isinstance(refusal, LookupError)
    assert refusal.args == (request, KNOWN)


def test_a_step_up_is_kept_for_its_own_user(identity: Api) -> None:
    """Looked up by the credential and its user, both."""
    alice = create_user("alice")
    bob = create_user("bob")

    async def check() -> tuple[object, object]:
        async with AuthUnitOfWork() as uow:
            await uow.step_ups.record("d" * 64, alice, clock.now(), ["otp", "pwd"])
            await uow.commit()
        async with AuthUnitOfWork() as uow:
            return (
                await uow.step_ups.last("d" * 64, alice),
                await uow.step_ups.last("d" * 64, bob),
            )

    mine, theirs = run_async(check())

    assert mine is not None
    assert mine[1] == {"otp", "pwd"}  # type: ignore[index]
    assert theirs is None


def test_concurrent_step_ups_of_one_login_keep_the_latest(identity: Api) -> None:
    """Ten at once on one token: none fails, and the latest proof is kept."""
    alice = create_user("alice")
    digest = "e" * 64
    start = clock.now()

    async def record(seconds: int) -> None:
        methods = ["otp", "pwd"] if seconds == 9 else ["otp"]
        async with AuthUnitOfWork() as uow:
            await uow.step_ups.record(
                digest, alice, start + timedelta(seconds=seconds), methods
            )
            await uow.commit()

    async def race() -> tuple[object, object]:
        await asyncio.gather(*(record(seconds) for seconds in range(10)))
        await record(3)  # An older proof, after: it does not replace the newer.
        async with AuthUnitOfWork() as uow:
            return await uow.step_ups.last(digest, alice), None

    kept, _ = run_async(race())

    assert kept == (start + timedelta(seconds=9), frozenset({"otp", "pwd"}))
    assert len(step_up_rows()) == 1


@pytest.mark.anyio
async def test_a_live_recheck_closes_once_the_step_up_is_too_old(
    use_auth: UseAuth, frozen: FrozenClock
) -> None:
    """``Require(...).allows`` re-asks the rule: it closes past ``max_age``."""
    use_auth()
    required = Require(StepUp(max_age=timedelta(minutes=5)))
    open_socket = connection(
        Credentials(
            KNOWN, scheme="jwt", auth_time=frozen.tick(0), amr=frozenset({"otp"})
        ),
        user=KNOWN,
    )

    assert await required.allows(open_socket) is True
    frozen.tick(timedelta(minutes=5, seconds=1))
    assert await required.allows(open_socket) is False


def test_a_browser_session_open_live_idles_out_and_closes(identity: Api) -> None:
    """Re-checks of an open connection write no last use (SEC-15c-2)."""
    with freeze_time("2027-01-04T10:00:00Z") as clock:
        client = identity(IDLE_TIMEOUT=timedelta(minutes=10))
        create_user("alice")
        browser = TestClient(client.app, base_url=SITE)
        browser.post(
            "/auth/session",
            json={"username": "alice", "password": PASSWORD},
            headers=TRUSTED,
        )
        cookie = "; ".join(f"{name}={value}" for name, value in browser.cookies.items())
        socket = {
            **principal_scope({"Cookie": cookie, "Origin": SITE}),
            "type": "websocket",
        }
        middleware = AuthenticationMiddleware(app=lambda *args: None)  # type: ignore[arg-type]
        assert run_async(middleware.reauthenticate(socket)) is False  # No auth yet.
        socket["auth"] = run_async(_credentials_of(socket))
        socket[PROOF] = None  # A step-up read before: read afresh after.
        held = []
        for _ in range(4):
            clock.tick(timedelta(minutes=4))
            held.append(run_async(middleware.reauthenticate(socket)))
            if held == [True]:
                assert PROOF not in socket

        idle = browser.get("/whoami")

    assert held == [True, True, False, False]
    assert RECHECK not in socket
    assert idle.json() == {"user": "", "scheme": None}


async def _credentials_of(socket: dict[str, Any]) -> Any:  # noqa: ANN401
    """What the configured classes make of ``socket``."""
    for authenticator in get_authenticators():
        found = await authenticator.authenticate(HTTPConnection(socket))
        if found is not None:
            return found
    return None


def test_a_jwt_issued_longer_ago_than_the_idle_timeout_is_refused(
    identity: Api,
) -> None:
    """A JSON Web Token login's use is its refreshes (SEC-15c-1)."""
    with freeze_time("2027-01-04T10:00:00Z") as clock:
        long_lived = {**JWT["JWT"], "ACCESS_TOKEN_TTL": timedelta(hours=1)}
        client = identity(
            **{**JWT, "JWT": long_lived}, IDLE_TIMEOUT=timedelta(minutes=10)
        )
        create_user("alice")
        issued = login(client, "alice")
        clock.tick(timedelta(minutes=10, seconds=59))
        fresh = client.get("/whoami", headers=bearer(issued["access_token"]))
        clock.tick(timedelta(seconds=2))
        idle = client.get("/whoami", headers=bearer(issued["access_token"]))
        refreshed = client.post(
            "/auth/refresh", json={"refresh_token": issued["refresh_token"]}
        )

    assert fresh.json()["user"] == "alice"
    assert (idle.status_code, idle.json()["detail"]) == (
        401,
        "Session ended after inactivity; log in again.",
    )
    assert refreshed.status_code == 401


def test_a_jwt_issued_at_no_date_is_refused(identity: Api) -> None:
    """Signed by the key, with an ``iat`` no date holds: invalid, never a 500."""
    client = identity(**JWT, IDLE_TIMEOUT=timedelta(minutes=10))
    create_user("alice")
    login(client, "alice")
    signer = next(
        each for each in get_authenticators() if isinstance(each, JWTAuthentication)
    )
    claims = jwt.decode(
        login(client, "alice")["access_token"], options={"verify_signature": False}
    )
    forged = signer.jwt.encode(
        {**claims, "iat": -(10**20)},
        signer.signing_key,
        algorithm=signer.config.ALGORITHM,
        headers={"kid": signer.key_id},
    )

    refused = client.get("/whoami", headers=bearer(forged))

    assert (refused.status_code, refused.json()["detail"]) == (401, "Invalid token.")


class NoObject(BasePermission):
    """Refuses every object plainly."""

    async def has_object_permission(
        self,
        request: HTTPConnection,
        user: Any,  # noqa: ANN401
        obj: object,
    ) -> bool:
        """Never."""
        return False


@pytest.mark.anyio
async def test_a_plain_refusal_wins_wherever_rules_are_checked(
    use_auth: UseAuth,
) -> None:
    """``&``, ``Require``'s rules, and objects: a step-up answers only alone."""
    use_auth()
    stepped = StepUp(max_age=timedelta(minutes=5))
    request = connection(None, user=KNOWN)

    combined = await AllOf(stepped, HasPermissions("a.b")).refusal(request, KNOWN)
    alone = await AllOf(stepped, BasePermission()).refusal(request, KNOWN)
    with pytest.raises(PermissionDenied):
        await check_object_permissions(request, object(), stepped, NoObject())
    with pytest.raises(StepUpRequired):
        await check_object_permissions(request, object(), stepped, BasePermission())

    assert combined is None
    assert isinstance(alone, StepUpRequired)
