"""Authenticating requests: logins, tokens, JWT, access rules, and the actor."""

import base64
import io
import json
import logging
from datetime import timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, update
from starlette.requests import HTTPConnection
from starlette.testclient import WebSocketDenialResponse
from starlette.websockets import WebSocketDisconnect

from domainkit.clock import freeze_time
from domainkit.conf import override_settings, settings
from domainkit.contrib.audit.tables import audit_log
from domainkit.contrib.auth import (
    AnyUser,
    AuthenticationFailed,
    AuthUnitOfWork,
    BaseAuthentication,
    BaseBackend,
    Credentials,
    User,
    authentication,
    hashers,
)
from domainkit.contrib.auth import api as auth_api
from domainkit.contrib.auth import backends as auth_backends
from domainkit.contrib.auth import middleware as auth_middleware
from domainkit.contrib.auth import (
    tokens as token_module,
)
from domainkit.contrib.auth.hashers import HashingPool
from domainkit.contrib.auth.middleware import AuthenticationMiddleware
from domainkit.contrib.auth.repositories import PrincipalStore
from domainkit.contrib.auth.tables import tokens
from domainkit.contrib.idempotency.tables import idempotency_keys
from domainkit.db import connections
from domainkit.ddd import F
from domainkit.exceptions import ImproperlyConfigured
from domainkit.http import get_asgi_application
from domainkit.http.ratelimit import Decision, MemoryBackend, address_key
from domainkit.http.websockets import MIDDLEWARE as WEBSOCKET_LIMITS
from domainkit.log import SECURITY_LOGGER, THROTTLE_INTERVAL, flush_throttled
from domainkit.management import call_command
from domainkit.management.base import run_async
from domainkit_tests.conftest import AUTH, MIDDLEWARE, Api, UseAuth
from domainkit_tests.sample_domains.shop.models import Product

PASSWORD = "vy3!Tq-pX9#mR2"
AUTHENTICATION = "domainkit.contrib.auth.authentication"
JWT: dict[str, Any] = {
    "AUTHENTICATION_CLASSES": [
        f"{AUTHENTICATION}.JWTAuthentication",
        f"{AUTHENTICATION}.TokenAuthentication",
    ],
    "JWT": {"SIGNING_KEY": "k" * 32},
}
INVALID = 'Bearer realm="api", error="invalid_token"'


class OwnerBackend(BaseBackend):
    """Owners may change their own products."""

    async def get_all_permissions(
        self, user: AnyUser, obj: object | None = None
    ) -> frozenset[str]:
        """``shop.change_product`` on the user's products."""
        if isinstance(obj, Product) and obj.name == user.get_username():
            return frozenset({"shop.change_product"})
        return frozenset()


def create_user(
    username: str,
    password: str | None = PASSWORD,
    **fields: Any,  # noqa: ANN401 - user fields.
) -> UUID:
    """Store a user; return the id."""

    async def create() -> UUID:
        async with AuthUnitOfWork() as uow:
            user = await uow.users.create_user(username, password, **fields)
            await uow.commit()
        return user.id

    return run_async(create())


def grant(username: str, *keys: str) -> None:
    """Give ``username`` the permissions ``keys``."""

    async def give() -> None:
        async with AuthUnitOfWork() as uow:
            user = await uow.users.get_by_natural_key(username)
            assert user is not None
            user.grant(*await uow.permissions.get_many(keys))
            await uow.commit()

    run_async(give())


def login(
    client: TestClient, username: str, password: str = PASSWORD
) -> dict[str, Any]:
    """Log in; return the token response."""
    response = client.post(
        "/auth/login", json={"username": username, "password": password}
    )
    assert response.status_code == 200, response.json()
    body: dict[str, Any] = response.json()
    return body


def bearer(token: str) -> dict[str, str]:
    """The ``Authorization`` header for ``token``."""
    return {"Authorization": f"Bearer {token}"}


def audited(action: str) -> list[dict[str, Any]]:
    """Audit rows of ``action``, oldest first."""

    async def read() -> list[dict[str, Any]]:
        async with connections.engine().connect() as connection:
            rows = await connection.execute(
                select(audit_log)
                .where(audit_log.c.action == action)
                .order_by(audit_log.c.occurred_at)
            )
            return [dict(row._mapping) for row in rows]

    return run_async(read())


def stored_tokens(user_id: UUID, *, devices: bool = False) -> list[dict[str, Any]]:
    """The user's stored tokens; with ``devices``, the devices that logged in too."""

    async def read() -> list[dict[str, Any]]:
        async with connections.engine().connect() as connection:
            statement = select(tokens).where(tokens.c.user_id == user_id)
            if not devices:
                statement = statement.where(tokens.c.kind != "device")
            rows = await connection.execute(statement)
            return [dict(row._mapping) for row in rows]

    return run_async(read())


def test_logins_return_opaque_tokens(api: Api) -> None:
    """The token names the user, who becomes the actor of the request."""
    alice = create_user("alice", email="alice@example.com")
    client = api()

    body = login(client, "alice")
    assert set(body) == {"access_token", "token_type", "expires_in"}
    assert body["access_token"].startswith("dkt_")
    assert len(body["access_token"]) == 47
    assert (body["token_type"], body["expires_in"]) == ("Bearer", 14 * 24 * 3600)

    headers = bearer(body["access_token"])
    assert client.get("/whoami", headers=headers).json() == {
        "user": "alice",
        "actor": str(alice),
        "scheme": "token",
    }
    assert client.get("/whoami").json() == {"user": "", "actor": None, "scheme": None}
    me = client.get("/auth/me", headers=headers).json()
    assert me.pop("last_login") is not None
    assert me == {
        "id": str(alice),
        "username": "alice",
        "email": "alice@example.com",
        "email_verified": False,
        "is_staff": False,
        "is_superuser": False,
        "groups": [],
        "permissions": [],
    }
    (logged_in,) = audited("auth.user_logged_in")
    assert (logged_in["actor"], logged_in["details"]) == (
        str(alice),
        {"user_id": str(alice)},
    )
    (token,) = stored_tokens(alice)
    assert (token["kind"], token["name"]) == ("access", "login")
    assert token["id"] != body["access_token"]
    assert token["expires_at"] - token["created_at"] == timedelta(days=14)


def test_tokens_without_an_end(api: Api) -> None:
    """``TOKEN_TTL = None``: login tokens work until revoked."""
    alice = create_user("alice")
    body = login(api(TOKEN_TTL=None), "alice")

    assert set(body) == {"access_token", "token_type"}
    (token,) = stored_tokens(alice)
    assert token["expires_at"] is None


def test_parallel_logins_never_conflict(api: Api) -> None:
    """The login time is stored without bumping the user's version."""
    create_user("alice")
    client = api()
    first, second = login(client, "alice"), login(client, "alice")

    assert first["access_token"] != second["access_token"]

    async def version() -> int:
        async with AuthUnitOfWork() as uow:
            user = await uow.users.get_by_natural_key("alice")
            assert user is not None
            return user.version

    assert run_async(version()) == 1


def test_wrong_credentials_are_refused_and_audited(api: Api) -> None:
    """Unknown users, wrong passwords, and inactive users look alike."""
    create_user("alice")
    create_user("idle", is_active=False)
    client = api()

    for username, password in (
        ("alice", "wrong"),
        ("nobody", PASSWORD),
        ("idle", PASSWORD),
    ):
        response = client.post(
            "/auth/login", json={"username": username, "password": password}
        )
        assert response.status_code == 401
        assert response.json()["code"] == "invalid_credentials"
        assert (
            response.json()["detail"] == "Unable to log in with the given credentials."
        )
    rows = audited("auth.login")
    assert [(row["outcome"], row["actor"], row["target"]) for row in rows] == [
        ("denied", "anonymous", None)
    ] * 3
    assert rows[0]["details"] == {"reason": "invalid_credentials"}
    assert client.post("/auth/login", json={"username": "alice"}).status_code == 422


def test_logins_answer_503_when_hashing_is_saturated(
    api: Api, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A full hashing queue refuses a login at once, for known and unknown users."""
    create_user("alice")
    client = api()
    full = HashingPool(threads=1, queue=0)
    full.pending = full.capacity
    monkeypatch.setattr(hashers, "_pool", lambda: full)

    for username in ("alice", "nobody"):
        refused = client.post(
            "/auth/login", json={"username": username, "password": PASSWORD}
        )
        assert refused.status_code == 503
        assert refused.json()["code"] == "password_checks_busy"
        assert refused.headers["Retry-After"] == "1"


@pytest.mark.parametrize("error_format", ["domainkit", "problem+json"])
def test_a_saturated_login_answers_the_same_503_in_both_error_formats(
    api: Api, monkeypatch: pytest.MonkeyPatch, error_format: str
) -> None:
    """The hashers raise no HTTP error; the HTTP layer maps theirs to this answer.

    The status, headers, and body are what they were while the hashers
    raised a 503 ``APIError`` themselves (0.9; DSGN-06).
    """
    create_user("alice")
    full = HashingPool(threads=1, queue=0)
    full.pending = full.capacity
    monkeypatch.setattr(hashers, "_pool", lambda: full)
    detail = "Too many password checks at once; try again in a moment."

    with override_settings(ERROR_FORMAT=error_format):
        refused = api().post(
            "/auth/login",
            json={"username": "alice", "password": PASSWORD},
        )

    assert refused.status_code == 503
    assert refused.headers["Retry-After"] == "1"
    if error_format == "domainkit":
        assert refused.headers["Content-Type"] == "application/json"
        assert refused.json() == {
            "detail": detail,
            "code": "password_checks_busy",
            "request_id": refused.headers["X-Request-ID"],
        }
    else:
        assert refused.headers["Content-Type"] == "application/problem+json"
        assert refused.json() == {
            "type": "about:blank",
            "title": "Service Unavailable",
            "status": 503,
            "detail": detail,
            "instance": "/auth/login",
            "code": "password_checks_busy",
            "request_id": refused.headers["X-Request-ID"],
        }


def test_signed_in_users_change_passwords_while_logins_fill_the_pool(
    api: Api, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The reserved place takes a password change; a login is refused (N7)."""
    create_user("alice")
    client = api()
    alice = bearer(login(client, "alice")["access_token"])
    busy = HashingPool(threads=1, queue=1, reserved=1)
    busy.pending = busy.capacity - busy.reserved  # Logins took every other place.
    monkeypatch.setattr(hashers, "_pool", lambda: busy)

    refused = client.post(
        "/auth/login", json={"username": "alice", "password": PASSWORD}
    )
    changed = client.post(
        "/auth/password",
        headers=alice,
        json={"old_password": PASSWORD, "new_password": "n3w-Passw0rd!"},
    )

    assert (refused.status_code, refused.json()["code"]) == (
        503,
        "password_checks_busy",
    )
    assert changed.status_code == 204
    assert busy.pending == 1


def test_logins_are_throttled_per_address_and_per_username(api: Api) -> None:
    """``AUTH.LOGIN_RATE`` counts attempts from an address, and for a name."""
    create_user("alice")
    client = api(LOGIN_RATE="2/minute")
    wrong = {"username": "alice", "password": "wrong"}
    codes = [client.post("/auth/login", json=wrong).status_code for _ in range(3)]
    assert codes == [401, 401, 429]
    refused = client.post("/auth/login", json={**wrong, "username": "bob"})
    assert (refused.status_code, refused.json()["code"]) == (429, "login_throttled")
    assert refused.json()["detail"] == "Too many login attempts; try again later."
    assert int(refused.headers["Retry-After"]) > 0
    assert audited("auth.login")[-1]["details"] == {"reason": "throttled"}

    elsewhere = TestClient(client.app, client=("203.0.113.9", 1))
    assert (
        elsewhere.post("/auth/login", json={**wrong, "username": "ALICE"}).status_code
        == 429
    )
    assert (
        elsewhere.post("/auth/login", json={**wrong, "username": "carol"}).status_code
        == 401
    )

    nearby = [
        TestClient(client.app, client=(address, 1))
        .post("/auth/login", json={**wrong, "username": name})
        .status_code
        for address, name in (
            ("2001:db8::1", "dan"),
            ("2001:db8::2", "erin"),
            ("2001:db8::3", "finn"),
        )
    ]
    assert nearby == [401, 401, 429]

    unlimited = api(LOGIN_RATE=None)
    assert {
        unlimited.post("/auth/login", json=wrong).status_code for _ in range(4)
    } == {401}


def test_logins_that_work_use_up_no_count(api: Api) -> None:
    """Only wrong passwords count (AUTH-02).

    A service account that logs in often, or an office behind one address,
    used to be throttled by its own logins.
    """
    create_user("alice")
    create_user("bob")
    client = api(LOGIN_RATE="2/minute")
    right = {"username": "alice", "password": PASSWORD}

    logins = [client.post("/auth/login", json=right).status_code for _ in range(5)]
    wrong = {"username": "bob", "password": "wrong"}
    guesses = [client.post("/auth/login", json=wrong).status_code for _ in range(2)]
    after = client.post("/auth/login", json=right)

    assert logins == [200] * 5
    assert guesses == [401, 401]
    assert (after.status_code, after.json()["code"]) == (429, "login_throttled")


def test_wrong_passwords_from_anywhere_hold_an_accounts_right_one_back(
    api: Api,
) -> None:
    """What the per-account count costs, until 0.10's lockout (SEC-r1-2).

    ``LOGIN_RATE`` wrong passwords for alice, from as many addresses, get
    her right password refused before it is checked, from anywhere, until
    the count frees a place (every ``period / limit`` seconds); a wrong
    password sent in that place keeps her out again. Guessing her password
    from many addresses stays bounded by the same count.
    """
    create_user("alice")
    client = api(LOGIN_RATE="2/minute")
    right = {"username": "alice", "password": PASSWORD}

    def wrong_from(number: int) -> int:
        caller = TestClient(client.app, client=(f"198.51.100.{number}", 1))
        guess = {"username": "alice", "password": f"guess-{number}"}
        return caller.post("/auth/login", json=guess).status_code

    alice = TestClient(client.app, client=("203.0.113.50", 1))
    with freeze_time() as clock:
        guesses = [wrong_from(number) for number in range(1, 4)]
        held = alice.post("/auth/login", json=right)
        clock.tick(30)  # One place freed: taken by another wrong password.
        again = [wrong_from(4), alice.post("/auth/login", json=right).status_code]
        clock.tick(30)  # Freed again: alice gets in, and gives the place back.
        logins = [alice.post("/auth/login", json=right).status_code for _ in range(2)]

    assert guesses == [401, 401, 429]
    assert (held.status_code, held.json()["code"]) == (429, "login_throttled")
    assert again == [401, 429]
    assert logins == [200, 200]


class NoRefunds:
    """A backend of a project's own that cannot give a request back."""

    def __init__(self) -> None:
        """Count in memory."""
        self.memory = MemoryBackend()

    async def hit(self, key: str, limit: int, period: float) -> Decision:
        """Count as ``MemoryBackend`` does."""
        return await self.memory.hit(key, limit, period)


class Uncounted:
    """A backend whose store is down: it lets every request through, uncounted."""

    async def hit(self, key: str, limit: int, period: float) -> Decision:
        """Let the request through without counting it, as ``RedisBackend`` does."""
        return Decision(
            allowed=True,
            remaining=limit - 1,
            reset_after=0.0,
            retry_after=0.0,
            counted=False,
        )


def test_backends_without_refunds_count_every_attempt(api: Api) -> None:
    """A backend without ``refund`` keeps counting logins that work, as before."""
    create_user("alice")
    right = {"username": "alice", "password": PASSWORD}
    with override_settings(RATE_LIMIT_BACKEND=f"{__name__}.NoRefunds"):
        client = api(LOGIN_RATE="2/minute")
        logins = [client.post("/auth/login", json=right).status_code for _ in range(3)]

    assert logins == [200, 200, 429]


def test_attempts_the_counter_cannot_count_may_fail_closed(
    api: Api, monkeypatch: pytest.MonkeyPatch
) -> None:
    """By default they are let through; ``THROTTLE_FAIL_CLOSED`` refuses them."""
    create_user("alice")
    checked: list[str] = []
    real = auth_backends.authenticate

    async def counted(**credentials: str) -> Any:  # noqa: ANN401 - a user.
        checked.append(credentials["username"])
        return await real(**credentials)

    monkeypatch.setattr(auth_api, "authenticate", counted)
    wrong = {"username": "alice", "password": "wrong"}
    with override_settings(RATE_LIMIT_BACKEND=f"{__name__}.Uncounted"):
        client = api(LOGIN_RATE="1/minute")
        let_through = [
            client.post("/auth/login", json=wrong).status_code for _ in range(3)
        ]
        closed = api(LOGIN_RATE="1/minute", THROTTLE_FAIL_CLOSED=True)
        refused = closed.post("/auth/login", json=wrong)
        unlimited = api(LOGIN_RATE=None, THROTTLE_FAIL_CLOSED=True)
        unthrottled = unlimited.post("/auth/login", json=wrong)

    assert let_through == [401] * 3
    assert (refused.status_code, refused.json()["code"]) == (
        503,
        "throttle_unavailable",
    )
    assert refused.json()["detail"] == (
        "Attempts cannot be counted right now; try again later."
    )
    assert refused.headers["Retry-After"] == "1"
    assert unthrottled.status_code == 401  # Nothing to count, nothing refused.
    assert checked == ["alice"] * 4  # The refused one was never checked.


def test_a_nul_in_a_login_is_a_refused_value(api: Api) -> None:
    """PostgreSQL refuses a NUL in text: a 422, not a 500; SQLite takes it."""
    client = api()
    database_url = settings.DATABASES["default"].URL.get_secret_value()

    response = client.post(
        "/auth/login", json={"username": "ad\u0000min", "password": "x"}
    )

    if database_url.startswith("postgresql"):
        assert (response.status_code, response.json()["code"]) == (
            422,
            "invalid_value",
        )
    else:
        assert response.status_code == 401


def test_tokens_are_never_cached_or_kept_for_replay(api: Api) -> None:
    """Login answers ``no-store``, so idempotency keeps no working token.

    A login's caller is not known yet: by default since 0.11 its key is
    refused before the login runs, and a header authentication found no
    one in (a fixed ``X-API-Key``) names no one either (SEC-11a-1). So this
    test keys anonymous callers by their address, as ``"per-address"``
    does.
    """
    create_user("alice")
    client = api()
    keyed = {"Idempotency-Key": "login-1"}
    body = {"username": "alice", "password": PASSWORD}

    with override_settings(IDEMPOTENCY={"ANONYMOUS": "per-address"}):
        first = client.post("/auth/login", json=body, headers=keyed)
        retry = client.post("/auth/login", json=body, headers=keyed)

    assert first.status_code == 200
    assert first.headers["Cache-Control"] == "no-store"
    assert first.headers["Pragma"] == "no-cache"
    assert (retry.status_code, retry.json()["code"]) == (
        409,
        "idempotency_response_unavailable",
    )
    stored = run_async(_stored_bodies())
    assert stored == [None]
    assert first.json()["access_token"].encode() not in repr(stored).encode()


async def _stored_bodies() -> list[bytes | None]:
    """The bodies the idempotency table holds."""
    async with connections.engine().connect() as connection:
        rows = await connection.execute(select(idempotency_keys.c.body))
        return [row.body for row in rows]


FULL_WIDTH = "\uff41\uff4c\uff49\uff43\uff45"  # alice, as NFKC finds it.
BOLD = "\U0001d41a\U0001d425\U0001d422\U0001d41c\U0001d41e"  # alice, in bold.


def test_look_alike_usernames_share_one_count(api: Api) -> None:
    """Names logging in treats as one account are throttled as one."""
    create_user("alice")
    client = api(LOGIN_RATE="2/minute")

    def attempt(number: int, username: str) -> int:
        caller = TestClient(client.app, client=(f"203.0.113.{number}", 1))
        wrong = {"username": username, "password": "wrong"}
        return caller.post("/auth/login", json=wrong).status_code

    assert [attempt(n, name) for n, name in enumerate(("alice", FULL_WIDTH, BOLD))] == [
        401,
        401,
        429,
    ]


def test_password_changes_have_a_count_of_their_own(api: Api) -> None:
    """Throttled logins for a name never stop its user changing the password."""
    create_user("alice")
    client = api(LOGIN_RATE="2/minute")
    alice = bearer(login(client, "alice")["access_token"])
    for number in (1, 2):  # Someone else uses up alice's logins.
        TestClient(client.app, client=(f"203.0.113.{number}", 1)).post(
            "/auth/login", json={"username": "alice", "password": "wrong"}
        )

    def change(old: str) -> int:
        body = {"old_password": old, "new_password": "n3w-Passw0rd!"}
        return client.post("/auth/password", headers=alice, json=body).status_code

    assert [change(PASSWORD), change("wrong"), change("wrong"), change("wrong")] == [
        204,
        400,
        400,
        429,
    ]


def test_a_flood_of_throttled_logins_is_audited_once_per_count(api: Api) -> None:
    """Each count's first refusal in a period is audited; the rest add no rows."""
    create_user("alice")
    client = api(LOGIN_RATE="1/minute")
    wrong = {"username": "alice", "password": "wrong"}

    codes = [client.post("/auth/login", json=wrong).status_code for _ in range(5)]
    first = len(audited("auth.login"))
    elsewhere = TestClient(client.app, client=("203.0.113.9", 1))
    assert elsewhere.post("/auth/login", json=wrong).status_code == 429

    assert codes == [401, 429, 429, 429, 429]
    assert first == 2  # The wrong password, then the address's first refusal.
    assert [row["details"] for row in audited("auth.login")] == [
        {"reason": "invalid_credentials"},
        {"reason": "throttled"},
        {"reason": "throttled"},  # alice's count, refused for the first time.
    ]


def test_refused_credentials_count_against_the_rate_limit(
    api: Api, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Past ``RATE_LIMIT``, an address's credentials are refused unread."""
    create_user("alice")
    lookups: list[str] = []
    find = PrincipalStore.credential

    async def counted(self: PrincipalStore, digest: str) -> Any:  # noqa: ANN401
        lookups.append(digest)
        return await find(self, digest)

    monkeypatch.setattr(PrincipalStore, "credential", counted)
    guess = bearer("dkt_" + "A" * 43)  # Well formed, so it is looked up.
    with override_settings(RATE_LIMIT="3/minute"):
        client = api()
        alice = bearer(login(client, "alice")["access_token"])
        guesses = [client.get("/whoami", headers=guess) for _ in range(4)]
        held = client.get("/whoami", headers=alice)
        with (
            pytest.raises(WebSocketDenialResponse) as socket,
            client.websocket_connect("/socket", headers=alice),
        ):
            pass
        looked_up = len(lookups)
        anonymous = client.get("/whoami")
        elsewhere = TestClient(client.app, client=("203.0.113.9", 1))
        served = elsewhere.get("/whoami", headers=alice)
        guessed_elsewhere = elsewhere.get("/whoami", headers=guess)

    assert [response.status_code for response in guesses] == [401, 401, 401, 429]
    assert guesses[0].json()["detail"] == "Invalid token."
    for refused in (guesses[3], held):
        assert refused.headers["RateLimit-Limit"] == "3"
        assert refused.headers["RateLimit-Remaining"] == "0"
    assert (held.status_code, held.json()["code"]) == (429, "rate_limited")
    assert int(held.headers["Retry-After"]) > 0
    assert guessed_elsewhere.status_code == 401  # Each address has its own count.
    assert socket.value.status_code == 429
    assert looked_up == 4  # The held requests were refused before any lookup.
    assert anonymous.json()["user"] == ""
    assert served.json()["user"] == "alice"


def test_refused_credentials_are_logged_once_per_address_a_while(
    api: Api, caplog: pytest.LogCaptureFixture
) -> None:
    """A flood of bad tokens writes a line per address, not one per request."""
    client = api()
    elsewhere = TestClient(client.app, client=("203.0.113.9", 1))

    for _ in range(3):
        assert client.get("/whoami", headers=bearer("dkt_unknown")).status_code == 401
    assert elsewhere.get("/whoami", headers=bearer("dkt_unknown")).status_code == 401

    assert caplog.messages.count("Refused credentials: Invalid token.") == 2


def test_a_burst_of_refused_credentials_that_stops_is_counted(
    api: Api, caplog: pytest.LogCaptureFixture
) -> None:
    """500 bad tokens from one address, then nothing: the log says 500.

    The hold is logged when it starts, and what it refuses is counted as
    refused credentials are; both counts are written by the next security
    line once their 10 seconds have passed, whoever it is for, or when the
    application shuts down, whichever comes first.
    """
    with override_settings(RATE_LIMIT="100/minute"), freeze_time() as clock:
        client = api()
        # One event loop for all 500: a client used outside ``with`` runs each
        # request on a loop of its own, and so an engine and a connection of
        # its own, more than CI's PostgreSQL (100 connections) allows.
        with TestClient(client.app, client=("203.0.113.9", 1)) as attacker:
            codes = [
                attacker.get("/whoami", headers=bearer(f"dkt_{i:043d}")).status_code
                for i in range(500)
            ]
        clock.tick(THROTTLE_INTERVAL)
        with TestClient(client.app, client=("198.51.100.7", 1)) as other:
            assert (
                other.get("/whoami", headers=bearer("dkt_unknown")).status_code == 401
            )

    assert (codes.count(401), codes.count(429)) == (100, 400)
    assert [
        record.getMessage()
        for record in caplog.records
        if record.name == SECURITY_LOGGER
    ] == [
        "Refused credentials: Invalid token.",
        (
            "Holding back credentials from ip:203.0.113.9 for 1 s: "
            "more refused than RATE_LIMIT allows (100/minute)"
        ),
        # Written when the attacker's client shuts its application down.
        "100 more refused_credentials line(s) for ip:203.0.113.9 were held back",
        "399 more held_credentials line(s) for ip:203.0.113.9 were held back",
        "Refused credentials: Invalid token.",  # The other address's.
    ]


def test_authentication_exempt_paths_hold_behind_a_root_path(api: Api) -> None:
    """Under ``--root-path /api`` probes are exempt as they are without it."""
    client = TestClient(api().app, root_path="/api")

    assert client.get("/api/health", headers=bearer("dkt_unknown")).status_code == 200
    assert client.get("/api/whoami", headers=bearer("dkt_unknown")).status_code == 401


def test_held_addresses_are_let_go(api: Api, monkeypatch: pytest.MonkeyPatch) -> None:
    """After ``Retry-After``, and when too many others are held, first in first out."""
    create_user("alice")
    clock = [1000.0]
    monkeypatch.setattr(auth_middleware, "monotonic", lambda: clock[0])
    monkeypatch.setattr(auth_middleware, "MAX_HELD", 2)
    with override_settings(RATE_LIMIT="1/minute"):
        client = api()
        alice = bearer(login(client, "alice")["access_token"])
        guess = bearer("dkt_" + "A" * 43)
        middleware = _find(client.app, AuthenticationMiddleware)

        assert [client.get("/whoami", headers=guess).status_code for _ in range(2)] == [
            401,
            429,
        ]
        clock[0] += 59.9
        held = client.get("/whoami", headers=alice)
        clock[0] += 0.2
        served = client.get("/whoami", headers=alice)
        for number in (1, 2, 3):
            caller = TestClient(client.app, client=(f"203.0.113.{number}", 1))
            for _ in range(2):
                caller.get("/whoami", headers=guess)

    assert held.status_code == 429
    assert served.json()["user"] == "alice"
    assert list(middleware.held) == ["ip:203.0.113.2", "ip:203.0.113.3"]


def _find[T](app: object, kind: type[T]) -> T:
    """The middleware of ``kind`` in ``app``'s stack."""
    layer: Any = getattr(app, "middleware_stack", None) or app
    while not isinstance(layer, kind):
        layer = layer.app
    return layer


def test_malformed_tokens_are_refused_without_a_lookup(
    api: Api, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only a token of the issued form is looked up."""
    lookups: list[str] = []
    find = PrincipalStore.credential

    async def counted(self: PrincipalStore, digest: str) -> Any:  # noqa: ANN401
        lookups.append(digest)
        return await find(self, digest)

    monkeypatch.setattr(PrincipalStore, "credential", counted)
    client = api()
    for raw in ("dkt_unknown", "dkt_" + "A" * 42, "dkt_" + "A" * 44, "dkt_" + "+" * 43):
        response = client.get("/whoami", headers=bearer(raw))
        assert response.json()["detail"] == "Invalid token."

    assert lookups == []


def test_bad_tokens_are_refused_even_on_public_routes(
    api: Api, caplog: pytest.LogCaptureFixture
) -> None:
    """A token that does not hold is a 401, never an anonymous request."""
    alice = create_user("alice")
    client = api()
    token = login(client, "alice")["access_token"]

    response = client.get("/whoami", headers=bearer("dkt_unknown"))
    assert response.status_code == 401
    assert response.json()["code"] == "authentication_failed"
    assert response.json()["detail"] == "Invalid token."
    assert response.headers["WWW-Authenticate"] == INVALID
    assert "Refused credentials: Invalid token." in caplog.messages

    async def expire() -> None:
        async with connections.engine().begin() as connection:
            await connection.execute(
                update(tokens).values(expires_at=tokens.c.created_at)
            )

    run_async(expire())
    assert (
        client.get("/whoami", headers=bearer(token)).json()["detail"]
        == "Token expired."
    )

    fresh = login(client, "alice")["access_token"]

    async def deactivate() -> None:
        async with AuthUnitOfWork() as uow:
            user = await uow.users.get(alice)  # type: ignore[arg-type]
            user.set_active(False)
            await uow.commit()

    run_async(deactivate())
    assert (
        client.get("/whoami", headers=bearer(fresh)).json()["detail"]
        == "User inactive or deleted."
    )
    # Credentials no class recognizes are left to the route (/metrics has its own).
    for header in ("Basic YWxpY2U6cHc=", "Bearer", "Bearer not-ours"):
        response = client.get("/whoami", headers={"Authorization": header})
        assert response.json()["user"] == ""
    assert client.get("/health", headers=bearer("dkt_unknown")).status_code == 200


def test_routes_check_rules(api: Api) -> None:
    """401 asks anonymous callers to log in; 403 refuses users."""
    create_user("alice")
    create_user("staff", is_staff=True)
    create_user("root", is_superuser=True)
    client = api()
    alice = bearer(login(client, "alice")["access_token"])
    staff = bearer(login(client, "staff")["access_token"])
    root = bearer(login(client, "root")["access_token"])

    anonymous = client.get("/products")
    assert (anonymous.status_code, anonymous.json()["code"]) == (
        401,
        "not_authenticated",
    )
    assert anonymous.json()["detail"] == "Authentication credentials were not provided."
    assert anonymous.headers["WWW-Authenticate"] == 'Bearer realm="api"'
    refused = client.get("/products", headers=alice)
    assert (refused.status_code, refused.json()) == (
        403,
        {
            "detail": "You do not have permission to perform this action.",
            "code": "permission_denied",
            "request_id": refused.headers["X-Request-ID"],
        },
    )
    assert client.request("OPTIONS", "/products", headers=alice).status_code == 200
    assert client.request("OPTIONS", "/products").status_code == 401

    grant("alice", "shop.view_product", "shop.add_product")
    assert client.get("/products", headers=alice).json() == {"user": "alice"}
    assert client.post("/products", headers=alice).status_code == 200
    assert client.put("/products", headers=alice).status_code == 403
    assert client.delete("/products", headers=alice).status_code == 403
    assert client.get("/catalog", headers=alice).status_code == 200
    assert client.get("/products", headers=root).status_code == 200

    assert [
        client.get("/staff", headers=h).status_code for h in (alice, staff, root)
    ] == [
        403,
        200,
        403,
    ]
    assert client.get("/staff").status_code == 401
    assert client.get("/publish", headers=alice).status_code == 403
    assert client.get("/publish", headers=root).status_code == 200
    assert client.get("/either", headers=alice).status_code == 200
    assert client.get("/either", headers=staff).status_code == 403
    assert client.get("/either", headers=root).status_code == 200
    assert client.get("/anonymous").status_code == 200
    assert client.get("/anonymous", headers=alice).status_code == 403
    assert client.get("/notes").status_code == 200
    assert client.post("/notes").status_code == 401
    assert client.post("/notes", headers=alice).status_code == 200
    assert client.get("/open").status_code == 200
    assert client.get("/private").status_code == 401
    assert client.get("/private", headers=alice).json() == {"user": "alice"}


def test_object_permissions_come_from_backends(api: Api) -> None:
    """Owners may change their products; others may not."""
    create_user("alice")
    client = api(
        BACKENDS=[
            f"{AUTH}.backends.ModelBackend",
            "domainkit_tests.test_auth_http.OwnerBackend",
        ]
    )
    alice = bearer(login(client, "alice")["access_token"])

    assert client.get("/products/alice", headers=alice).json() == {"owner": "alice"}
    assert client.get("/products/bob", headers=alice).status_code == 403
    assert client.get("/products/bob").status_code == 401


def test_rate_limits_and_idempotency_keys_follow_the_user(api: Api) -> None:
    """Users behind one address count and retry apart (audit finding M3)."""
    from domainkit_tests import sample_auth_routes

    sample_auth_routes.counted.clear()
    create_user("alice")
    create_user("bob")
    with override_settings(RATE_LIMIT="20/minute"):
        client = api()
        alice = bearer(login(client, "alice")["access_token"])
        bob = bearer(login(client, "bob")["access_token"])

        retry = {"Idempotency-Key": "same"}
        first = client.post("/count", headers={**alice, **retry})
        again = client.post("/count", headers={**alice, **retry})
        other = client.post("/count", headers={**bob, **retry})

        assert (first.json(), again.json(), other.json()) == (
            {"count": 1},
            {"count": 1},
            {"count": 2},
        )
        assert again.headers["Idempotent-Replayed"] == "true"
        assert "Idempotent-Replayed" not in other.headers
        assert sample_auth_routes.counted == ["alice", "bob"]
        remaining = [
            int(client.get("/whoami", headers=h).headers["RateLimit-Remaining"])
            for h in (alice, alice, bob)
        ]
        assert remaining[0] - remaining[1] == 1
        assert remaining[2] > remaining[1]


def test_logging_out_ends_the_session_or_all_of_them(api: Api) -> None:
    """The token stops working; ``everywhere`` ends every token of the user."""
    alice_id = create_user("alice")
    client = api()
    first, second, third = (login(client, "alice")["access_token"] for _ in range(3))

    assert client.post("/auth/logout", headers=bearer(first)).status_code == 204
    assert client.get("/whoami", headers=bearer(first)).status_code == 401
    assert client.get("/private", headers=bearer(second)).status_code == 200
    response = client.post(
        "/auth/logout", headers=bearer(second), json={"everywhere": True}
    )
    assert response.status_code == 204
    assert client.get("/whoami", headers=bearer(third)).status_code == 401
    assert stored_tokens(alice_id) == []
    assert [
        row["details"]["everywhere"] for row in audited("auth.user_logged_out")
    ] == [
        False,
        True,
    ]
    assert client.post("/auth/logout").status_code == 401


def test_password_changes_end_other_sessions(api: Api) -> None:
    """The old password is checked, the new one validated; this session stays."""
    alice_id = create_user("alice")
    client = api()
    current, other = (login(client, "alice")["access_token"] for _ in range(2))
    headers = bearer(current)

    wrong = client.post(
        "/auth/password",
        headers=headers,
        json={"old_password": "nope", "new_password": "n3w-Passw0rd!"},
    )
    assert (wrong.status_code, wrong.json()["code"]) == (400, "wrong_password")
    assert wrong.json()["detail"] == "Your old password was entered incorrectly."
    weak = client.post(
        "/auth/password",
        headers=headers,
        json={"old_password": PASSWORD, "new_password": "alice2026"},
    )
    assert (weak.status_code, weak.json()["detail"]) == (
        422,
        "The password is too similar to the username.",
    )
    changed = client.post(
        "/auth/password",
        headers=headers,
        json={"old_password": PASSWORD, "new_password": "n3w-Passw0rd!"},
    )
    assert changed.status_code == 204
    assert client.get("/private", headers=headers).status_code == 200
    assert client.get("/private", headers=bearer(other)).status_code == 401
    assert len(stored_tokens(alice_id)) == 1
    login(client, "alice", "n3w-Passw0rd!")


def test_changepassword_ends_every_session(
    api: Api, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The command logs the user out everywhere."""
    from domainkit.contrib.auth.management.commands import changepassword

    answers = iter(["An0ther-Passw0rd!"] * 2)
    monkeypatch.setattr(
        changepassword.Command, "ask_secret", staticmethod(lambda prompt: next(answers))
    )
    alice_id = create_user("alice")
    client = api()
    token = login(client, "alice")["access_token"]

    call_command("changepassword", "alice", stdout=io.StringIO())

    assert client.get("/whoami", headers=bearer(token)).status_code == 401
    assert stored_tokens(alice_id) == []


def test_jwt_logins_refresh_and_end(api: Api, caplog: pytest.LogCaptureFixture) -> None:
    """Access tokens are signed; refresh tokens rotate, once each."""
    alice = create_user("alice")
    client = api(**JWT)
    body = login(client, "alice")

    assert set(body) == {"access_token", "token_type", "expires_in", "refresh_token"}
    assert body["access_token"].count(".") == 2
    assert body["expires_in"] == 300
    assert body["refresh_token"].startswith("dkt_")
    headers = bearer(body["access_token"])
    assert client.get("/whoami", headers=headers).json() == {
        "user": "alice",
        "actor": str(alice),
        "scheme": "jwt",
    }
    (issued,) = stored_tokens(alice)
    assert (issued["kind"], issued["name"]) == ("refresh", "login")
    assert issued["expires_at"] - issued["created_at"] == timedelta(days=14)
    refreshed = client.post(
        "/auth/refresh", json={"refresh_token": body["refresh_token"]}
    ).json()
    assert refreshed["refresh_token"] != body["refresh_token"]
    unused = [row for row in stored_tokens(alice) if row["used_at"] is None]
    assert [(row["kind"], row["name"]) for row in unused] == [("refresh", "login")]
    assert (unused[0]["family"], unused[0]["expires_at"]) == (
        issued["family"],
        issued["expires_at"],
    )
    # The new refresh token refreshes in turn; the chain is one login.
    chained = client.post(
        "/auth/refresh", json={"refresh_token": refreshed["refresh_token"]}
    ).json()
    refreshed = {**chained, "refresh_token": chained["refresh_token"]}
    assert (
        client.get("/private", headers=bearer(refreshed["access_token"])).status_code
        == 200
    )

    with caplog.at_level(logging.WARNING, logger=SECURITY_LOGGER):
        reused = client.post(
            "/auth/refresh", json={"refresh_token": body["refresh_token"]}
        )
    assert (reused.status_code, reused.json()["detail"]) == (
        401,
        "Refresh token reused; log in again.",
    )
    family = str(issued["family"])
    assert f"A refresh token came back after use; its login {family} is ended" in [
        record.getMessage() for record in caplog.records
    ]
    (row,) = audited("auth.refresh_token_reused")
    assert (row["actor"], row["details"]) == (
        "anonymous",
        {"user_id": str(alice), "family": family},
    )
    for token in (body["access_token"], refreshed["access_token"]):
        ended = client.get("/whoami", headers=bearer(token))
        assert ended.json()["detail"] == "Session ended; log in again."
    rotated = client.post(
        "/auth/refresh", json={"refresh_token": refreshed["refresh_token"]}
    )
    assert rotated.json()["detail"] == "Invalid refresh token."

    again = login(client, "alice")
    assert (
        client.post("/auth/logout", headers=bearer(again["access_token"])).status_code
        == 204
    )
    assert (
        client.get("/whoami", headers=bearer(again["access_token"])).status_code == 401
    )
    assert (
        client.post(
            "/auth/refresh", json={"refresh_token": again["refresh_token"]}
        ).status_code
        == 401
    )


def test_reused_refresh_tokens_of_deleted_users_still_fail(api: Api) -> None:
    """Tokens have no foreign key: one can outlive a user's row deleted in bulk.

    ``remove`` deletes them with the user; ``delete_rows`` bypasses it.
    """
    create_user("alice")
    client = api(**JWT)
    first = login(client, "alice")["refresh_token"]
    client.post("/auth/refresh", json={"refresh_token": first})

    async def remove() -> None:
        async with AuthUnitOfWork() as uow:
            assert await uow.users.delete_rows(F("username") == "alice") == 1
            await uow.commit()

    run_async(remove())
    reused = client.post("/auth/refresh", json={"refresh_token": first})

    assert (reused.status_code, reused.json()["detail"]) == (
        401,
        "Refresh token reused; log in again.",
    )
    assert audited("auth.refresh_token_reused") == []


def test_jwt_password_changes_keep_this_session(api: Api) -> None:
    """Other logins end; this one, and its refresh token, go on."""
    create_user("alice")
    client = api(**JWT)
    current, other = login(client, "alice"), login(client, "alice")

    changed = client.post(
        "/auth/password",
        headers=bearer(current["access_token"]),
        json={"old_password": PASSWORD, "new_password": "n3w-Passw0rd!"},
    )
    assert changed.status_code == 204
    assert (
        client.get("/private", headers=bearer(current["access_token"])).status_code
        == 200
    )
    assert (
        client.get("/private", headers=bearer(other["access_token"])).status_code == 401
    )
    refreshed = client.post(
        "/auth/refresh", json={"refresh_token": current["refresh_token"]}
    )
    assert refreshed.status_code == 200


def test_opaque_tokens_are_not_refreshed(api: Api) -> None:
    """``/refresh`` explains that the API's tokens are not renewed."""
    client = api()
    response = client.post("/auth/refresh", json={"refresh_token": "dkt_x"})

    assert (response.status_code, response.json()) == (
        400,
        {
            "detail": "Tokens are not refreshed here; log in again.",
            "code": "refresh_not_supported",
            "request_id": response.headers["X-Request-ID"],
        },
    )


def test_api_tokens_work_beside_jwt(api: Api) -> None:
    """``createtoken`` tokens authenticate when JWT is the login scheme."""
    create_user("robot")
    client = api(**JWT)
    output = io.StringIO()
    call_command("createtoken", "robot", stdout=output)

    token = output.getvalue().strip()
    assert client.get("/whoami", headers=bearer(token)).json()["scheme"] == "token"


def test_websockets_see_the_user(api: Api) -> None:
    """The middleware authenticates WebSocket handshakes, and sets the actor.

    The actor is what the audit trail, logs, and outbox headers record, so
    work done over a WebSocket is attributed to its user, not to "system".
    """
    alice = create_user("alice")
    client = api()
    token = login(client, "alice")["access_token"]

    with client.websocket_connect("/socket", headers=bearer(token)) as socket:
        assert socket.receive_json() == {"user": "alice", "actor": str(alice)}
    with client.websocket_connect("/socket") as socket:
        assert socket.receive_json() == {"user": "", "actor": None}
    with (
        pytest.raises(WebSocketDisconnect) as refused,
        client.websocket_connect("/socket", headers=bearer("dkt_unknown")),
    ):
        pass
    assert refused.value.code == 1008


def test_websocket_routes_take_access_rules(api: Api) -> None:
    """``Require`` and ``AuthenticatedUser`` refuse a handshake as they do a request.

    The refusal is the same 401 or 403 response, before the socket opens.
    """
    create_user("alice")
    create_user("boss", is_staff=True)
    client = api()
    alice = bearer(login(client, "alice")["access_token"])
    boss = bearer(login(client, "boss")["access_token"])

    def connect(path: str, headers: dict[str, str]) -> object:
        try:
            with client.websocket_connect(path, headers=headers) as socket:
                return socket.receive_json()
        except WebSocketDenialResponse as denied:
            return denied.status_code, denied.json()["code"]

    assert connect("/user-socket", {}) == (401, "not_authenticated")
    assert connect("/user-socket", alice) == {"user": "alice"}
    assert connect("/guarded-socket", {}) == (401, "not_authenticated")
    assert connect("/guarded-socket", alice) == (403, "permission_denied")
    assert connect("/guarded-socket", boss) == {"user": "boss"}


def test_jwt_tokens_from_elsewhere_are_refused(
    api: Api, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Another key, issuer, or audience, or an expired token, never works."""
    create_user("alice")
    issuing: dict[str, Any] = {
        **JWT,
        "JWT": {"SIGNING_KEY": "k" * 32, "ISSUER": "us", "AUDIENCE": "api"},
    }
    client = api(**issuing)
    token = login(client, "alice")["access_token"]
    assert client.get("/private", headers=bearer(token)).status_code == 200

    for jwt in (
        {"SIGNING_KEY": "o" * 32, "ISSUER": "us", "AUDIENCE": "api"},
        {"SIGNING_KEY": "k" * 32, "ISSUER": "them", "AUDIENCE": "api"},
        {"SIGNING_KEY": "k" * 32, "ISSUER": "us", "AUDIENCE": "web"},
        {"SIGNING_KEY": "k" * 32},
    ):
        other = api(**{**issuing, "JWT": jwt})
        response = other.get("/whoami", headers=bearer(token))
        assert (response.status_code, response.json()["detail"]) == (
            401,
            "Invalid token.",
        )

    client = api(**issuing)
    an_hour_ago = token_module.utc_now() - timedelta(hours=1)
    with monkeypatch.context() as patch:
        patch.setattr(authentication, "utc_now", lambda: an_hour_ago)
        stale = login(client, "alice")["access_token"]
    assert (
        client.get("/whoami", headers=bearer(stale)).json()["detail"]
        == "Token expired."
    )
    lenient = api(
        **{**issuing, "JWT": {**issuing["JWT"], "LEEWAY": timedelta(hours=2)}}
    )
    assert lenient.get("/whoami", headers=bearer(stale)).status_code == 200


def segment(data: dict[str, Any]) -> str:
    """One base64url JSON segment of a JWT."""
    return base64.urlsafe_b64encode(json.dumps(data).encode()).rstrip(b"=").decode()


def test_forged_jwts_are_refused(api: Api) -> None:
    """Edited claims, other algorithms, and key hints never get in."""
    create_user("alice")
    client = api(**JWT)
    token = login(client, "alice")["access_token"]
    header, payload, signature = token.split(".")
    claims = json.loads(base64.urlsafe_b64decode(payload + "=="))
    forged = [
        f"{segment({'alg': 'none', 'typ': 'JWT'})}.{payload}.",
        f"{segment({'alg': 'None'})}.{payload}.{signature}",
        f"{segment({'alg': 'HS512', 'typ': 'JWT'})}.{payload}.{signature}",
        f"{segment({'alg': 'HS256', 'kid': '../../dev/null'})}.{payload}.{signature}",
        f"{segment({'alg': 'HS256', 'jku': 'https://example.org/k'})}.{payload}.{signature}",
        f"{header}.{segment({**claims, 'sub': str(uuid4())})}.{signature}",
        f"{header}.{segment({**claims, 'exp': claims['exp'] + 10**6})}.{signature}",
        f"{header}.{segment({**claims, 'sid': str(uuid4())})}.{signature}",
        f"{header}.{payload}.",
        f"{header}.{payload}.{signature[:-4]}",
    ]

    for jwt in forged:
        response = client.get("/whoami", headers=bearer(jwt))
        assert (response.status_code, response.json()["detail"]) == (
            401,
            "Invalid token.",
        ), jwt
    assert client.get("/private", headers=bearer(token)).status_code == 200


@pytest.mark.parametrize(
    ("middleware", "message"),
    [
        (
            [MIDDLEWARE[2], MIDDLEWARE[0]],
            (f"MIDDLEWARE: list {MIDDLEWARE[0]} before {MIDDLEWARE[2]}."),
        ),
        (  # Without a context, no actor: users would look anonymous.
            [MIDDLEWARE[1], MIDDLEWARE[2]],
            (f"MIDDLEWARE: list {MIDDLEWARE[0]} before {MIDDLEWARE[2]}."),
        ),
        (
            [MIDDLEWARE[0], MIDDLEWARE[3], MIDDLEWARE[2]],
            (
                f"MIDDLEWARE: list {MIDDLEWARE[2]} before {MIDDLEWARE[3]}, so it "
                "counts users rather than client addresses."
            ),
        ),
        (
            [MIDDLEWARE[0], MIDDLEWARE[4], MIDDLEWARE[2]],
            (
                f"MIDDLEWARE: list {MIDDLEWARE[2]} before {MIDDLEWARE[4]}, so it "
                "counts users rather than client addresses."
            ),
        ),
        (  # Its caps would count addresses, so users behind one share them.
            [MIDDLEWARE[0], WEBSOCKET_LIMITS, MIDDLEWARE[2]],
            (
                f"MIDDLEWARE: list {MIDDLEWARE[2]} before {WEBSOCKET_LIMITS}, so "
                "it counts users rather than client addresses."
            ),
        ),
    ],
)
def test_the_middleware_order_is_checked(
    use_auth: UseAuth, middleware: list[str], message: str
) -> None:
    """The actor must be set after the context exists and before it is read."""
    with pytest.raises(ImproperlyConfigured) as refused:
        use_auth(settings={"MIDDLEWARE": middleware})
    assert str(refused.value) == message
    use_auth(
        settings={
            "MIDDLEWARE": [
                MIDDLEWARE[0],
                MIDDLEWARE[2],
                *MIDDLEWARE[3:],
                WEBSOCKET_LIMITS,
            ]
        }
    )
    use_auth(settings={"MIDDLEWARE": [MIDDLEWARE[0]]})
    use_auth(settings={"MIDDLEWARE": []})


def test_users_without_a_request_context(api: Api) -> None:
    """Without ``RequestContextMiddleware`` there is a user but no actor."""
    create_user("alice")
    with override_settings(MIDDLEWARE=[MIDDLEWARE[2]]):
        client = api()
        token = login(client, "alice")["access_token"]
        response = client.get("/whoami", headers=bearer(token))
    assert response.json() == {"user": "alice", "actor": None, "scheme": "token"}


def test_routes_need_the_middleware(api: Api) -> None:
    """Without ``AuthenticationMiddleware``, rules cannot know the user."""
    with override_settings(MIDDLEWARE=[MIDDLEWARE[0]]):
        client = api()
        response = client.get("/private")
    assert (response.status_code, response.json()["code"]) == (500, "internal_error")
    with override_settings(MIDDLEWARE=[]):
        bare = api()
        with pytest.raises(ImproperlyConfigured, match="AuthenticationMiddleware"):
            bare.get("/private")


def test_me_without_an_email_field(api: Api, monkeypatch: pytest.MonkeyPatch) -> None:
    """A user model without ``EMAIL_FIELD`` has no email to show."""
    create_user("alice", email="alice@example.com")
    client = api()
    headers = bearer(login(client, "alice")["access_token"])
    monkeypatch.setattr(User, "EMAIL_FIELD", None)

    assert client.get("/auth/me", headers=headers).json()["email"] is None


def test_superusers_see_every_permission(api: Api) -> None:
    """``/me`` lists what backends grant, all of it for a superuser."""
    create_user("root", is_superuser=True, is_staff=True)
    client = api()
    me = client.get(
        "/auth/me", headers=bearer(login(client, "root")["access_token"])
    ).json()

    assert (me["is_staff"], me["is_superuser"]) == (True, True)
    assert "shop.publish_product" in me["permissions"]
    assert me["permissions"] == sorted(me["permissions"])


class HeaderAuthentication(BaseAuthentication):
    """Users named by a trusted gateway's header (no tokens to revoke)."""

    async def authenticate(self, connection: HTTPConnection) -> Credentials | None:
        """The user in ``X-User``; a 401 for unknown names."""
        username = connection.headers.get("x-user")
        if username is None:
            return None
        async with AuthUnitOfWork() as uow:
            user = await uow.users.get_by_natural_key(username)
        if user is None:
            raise AuthenticationFailed("Unknown user.")
        return Credentials(user, scheme="header")


def test_projects_add_their_own_authentication(api: Api) -> None:
    """A custom class names users; logging out has no token to end."""
    alice = create_user("alice")
    client = api(
        AUTHENTICATION_CLASSES=[
            f"{AUTHENTICATION}.TokenAuthentication",
            "domainkit_tests.test_auth_http.HeaderAuthentication",
        ]
    )

    assert client.get("/whoami", headers={"X-User": "alice"}).json() == {
        "user": "alice",
        "actor": str(alice),
        "scheme": "header",
    }
    unknown = client.get("/whoami", headers={"X-User": "ghost"})
    assert (unknown.status_code, unknown.json()["detail"]) == (401, "Unknown user.")
    assert client.post("/auth/logout", headers={"X-User": "alice"}).status_code == 204
    assert client.get("/private", headers={"X-User": "alice"}).status_code == 200


def test_held_addresses_guess_other_headers_in_vain(
    api: Api, caplog: pytest.LogCaptureFixture
) -> None:
    """A held address's guess that works, in a project's own header, gets 429 too.

    Only requests with ``Authorization`` are refused unread; others are
    read, and the right name used to be served among the 429s. The security
    log counts it against the address's hold.
    """
    create_user("alice")
    with override_settings(RATE_LIMIT="3/minute"):
        client = api(
            AUTHENTICATION_CLASSES=[
                f"{AUTHENTICATION}.TokenAuthentication",
                "domainkit_tests.test_auth_http.HeaderAuthentication",
            ]
        )
        guesses = [
            client.get("/whoami", headers={"X-User": name}).status_code
            for name in ("bob", "carol", "dave", "erin")
        ]
        right = client.get("/whoami", headers={"X-User": "alice"})
        anonymous = client.get("/whoami")
        elsewhere = TestClient(client.app, client=("203.0.113.9", 1))
        served = elsewhere.get("/whoami", headers={"X-User": "alice"})
    flush_throttled()
    held = [
        record.getMessage()
        for record in caplog.records
        if record.name == SECURITY_LOGGER
        and ("Holding" in record.getMessage() or "held_cred" in record.getMessage())
    ]

    assert len(held) == 2
    assert held[0].startswith("Holding back credentials from ip:")
    key = held[0].removeprefix("Holding back credentials from ").split()[0]
    assert held[1] == f"1 more held_credentials line(s) for {key} were held back"
    assert guesses == [401, 401, 401, 429]
    assert (right.status_code, right.json()["code"]) == (429, "rate_limited")
    assert int(right.headers["Retry-After"]) > 0
    assert right.headers["RateLimit-Limit"] == "3"
    assert anonymous.json()["user"] == ""
    assert served.json()["user"] == "alice"


def test_refusals_without_audit(auth_project: str) -> None:
    """Without the audit app, refused logins are only answered."""
    with override_settings(
        ROOT_ROUTER="domainkit_tests.sample_auth_routes.router",
        MIDDLEWARE=MIDDLEWARE[:3],
    ):
        client = TestClient(get_asgi_application())
        response = client.post(
            "/auth/login", json={"username": "ghost", "password": "x"}
        )
    assert response.status_code == 401


def test_openapi_documents_tokens_and_errors(api: Api) -> None:
    """Guarded routes ask for a bearer token; auth errors use the error body."""
    schema = api().get("/openapi.json").json()

    assert schema["components"]["securitySchemes"]["Token"] == {
        "type": "http",
        "scheme": "bearer",
        "description": "A token from logging in: `Authorization: Bearer <token>`.",
    }
    assert schema["paths"]["/private"]["get"]["security"] == [{"Token": []}]
    login_responses = schema["paths"]["/auth/login"]["post"]["responses"]
    for code in ("401", "429"):
        body = login_responses[code]["content"]["application/json"]["schema"]
        assert body == {"$ref": "#/components/schemas/ErrorResponse"}
    assert set(schema["paths"]["/auth/password"]["post"]["responses"]) >= {
        "204",
        "400",
        "401",
        "422",
        "429",
    }


def test_token_commands_are_audited(api: Api, monkeypatch: pytest.MonkeyPatch) -> None:
    """Creating a token and logging a user out everywhere leave events.

    They name the operator who ran the command (SEC-r2-2): an insider who
    mints a token for someone else is named, not ``system``.
    """
    monkeypatch.setenv("DOMAINKIT_OPERATOR", "mallory")
    alice = create_user("alice")
    call_command("createtoken", "alice", "--name", "ci", stdout=io.StringIO())
    call_command(
        "cleartokens", "--user", "alice", "--operator", "bob", stdout=io.StringIO()
    )
    call_command("cleartokens", stdout=io.StringIO())  # Records nothing more.

    (created,) = audited("auth.token_created")
    assert (created["actor"], created["details"]) == (
        "operator:mallory",
        {"user_id": str(alice), "name": "ci", "scopes": None},
    )
    (logged_out,) = audited("auth.user_logged_out")
    assert logged_out["actor"] == "operator:bob"
    assert logged_out["details"] == {"user_id": str(alice), "everywhere": True}


def test_nothing_held_means_no_address_is_looked_at(
    api: Api, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The refused-credential hold reads the address only while one is held."""
    create_user("alice")
    client = api()
    token = bearer(login(client, "alice")["access_token"])
    looked: list[object] = []
    real = address_key

    def counted(scope: Any) -> str:  # noqa: ANN401 - an ASGI scope.
        looked.append(scope)
        return real(scope)

    monkeypatch.setattr(auth_middleware, "address_key", counted)
    assert client.get("/whoami", headers=token).json()["user"] == "alice"
    assert looked == []
    _find(client.app, AuthenticationMiddleware)._hold("ip:203.0.113.250", 60)
    assert client.get("/whoami", headers=token).json()["user"] == "alice"
    assert len(looked) == 1


def test_requests_without_a_token_read_no_settings(
    use_auth: UseAuth, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``TokenAuthentication`` and ``CookieAuthentication`` look at headers first."""
    from domainkit.contrib.auth import cookies

    use_auth()
    read: list[str] = []
    monkeypatch.setattr(authentication, "auth_settings", lambda: read.append("t"))
    monkeypatch.setattr(cookies, "auth_settings", lambda: read.append("c"))
    scope = {"type": "http", "headers": [(b"accept", b"application/json")]}

    async def anonymous() -> tuple[object, object]:
        connection = HTTPConnection(scope)
        return (
            await authentication.TokenAuthentication().authenticate(connection),
            await cookies.CookieAuthentication().authenticate(connection),
        )

    assert run_async(anonymous()) == (None, None)
    assert read == []
