"""Whose an Idempotency-Key is: anonymous callers (DATA-09)."""

import asyncio
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from typing import Any, Final

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import select
from starlette.datastructures import Headers
from starlette.types import ASGIApp, Receive, Scope, Send

from domainkit import setup
from domainkit.apps import registry
from domainkit.conf import override_settings
from domainkit.context import bind_request, current_request
from domainkit.contrib.idempotency.settings import IdempotencySettings
from domainkit.contrib.idempotency.tables import idempotency_keys
from domainkit.db import connections
from domainkit.http import get_asgi_application
from domainkit_tests import sample_routes

ACTOR_HEADER: Final = "x-test-actor"
NEEDS_CALLER: Final = {
    "detail": "An Idempotency-Key needs a caller this service knows: "
    "authenticate, or send the request without the key.",
    "code": "idempotency_key_needs_caller",
}


class ActorFromHeader:
    """Act as the actor ``X-Test-Actor`` names, as authentication would."""

    def __init__(self, app: ASGIApp) -> None:
        """Wrap ``app``."""
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Bind the actor, if the request names one."""
        request = current_request()
        actor = Headers(scope=scope).get(ACTOR_HEADER) if request else None
        if actor is None or request is None:
            await self.app(scope, receive, send)
            return
        with bind_request(replace(request, actor=actor)):
            await self.app(scope, receive, send)


@contextmanager
def keyed_app(database_url: str, **idempotency: object) -> Iterator[TestClient]:
    """The idempotency middleware, keys and payments on an empty database."""
    sample_routes.created.clear()
    registry.clear()
    with override_settings(
        INSTALLED_DOMAINS=[
            "domainkit.contrib.idempotency",
            "domainkit_tests.sample_domains.alpha",
        ],
        DATABASES={"default": {"URL": database_url}},
        IDEMPOTENCY=idempotency,
        MIDDLEWARE=[
            "domainkit.http.middleware.RequestContextMiddleware",
            "domainkit.http.middleware.RequestBodyLimitMiddleware",
            f"{__name__}.ActorFromHeader",
            "domainkit.contrib.idempotency.middleware.IdempotencyMiddleware",
        ],
    ):
        setup()

        async def create() -> None:
            async with connections.engine().begin() as connection:
                await connection.run_sync(idempotency_keys.create)
            await connections.close_all()

        asyncio.run(create())
        with TestClient(
            get_asgi_application(), raise_server_exceptions=False
        ) as client:
            yield client
    registry.clear()


def stored() -> list[dict[str, Any]]:
    """The rows of the keys table."""

    async def read() -> list[dict[str, Any]]:
        async with connections.engine().connect() as connection:
            rows = (await connection.execute(select(idempotency_keys))).all()
        await connections.close_all()
        return [dict(row._mapping) for row in rows]

    return asyncio.run(read())


def post(client: TestClient, body: object, **headers: str) -> Any:  # noqa: ANN401 - a response.
    """POST ``body`` to ``/counter`` with the key ``order-1`` and ``headers``."""
    return client.post(
        "/counter",
        json=body,
        headers={"Idempotency-Key": "order-1", **headers},
    )


def test_anonymous_callers_behind_one_address_share_keys(database_url: str) -> None:
    """The PoC of DATA-09: by default, one caller gets another's response.

    Two people behind one NAT (the test client's one address), neither
    authenticated, send the same key: the second is replayed the first's
    response, its ``Location`` and body, and a different body tells the
    second the key exists. This is ``per-address``, the default.
    """
    with keyed_app(database_url) as client:
        first = post(client, {"item": "ann's order"})
        second = post(client, {"item": "ann's order"})
        probe = post(client, {"item": "bob's order"})

    assert (first.status_code, first.json()) == (201, {"count": 1})
    assert second.headers["Idempotent-Replayed"] == "true"
    assert (second.json(), second.headers["Location"]) == ({"count": 1}, "/counter/1")
    assert probe.json()["code"] == "idempotency_key_reused"
    assert sample_routes.created == [{"item": "ann's order"}]


def test_refuse_answers_anonymous_keys_400(database_url: str) -> None:
    """``refuse``: no actor and no credential header is a 400, and nothing runs."""
    with keyed_app(database_url, ANONYMOUS="refuse") as client:
        refused = post(client, {"item": "ann's order"})
        unkeyed = client.post("/counter", json={"item": "plain"})
        keys = stored()

    assert refused.status_code == 400
    assert refused.json().items() >= NEEDS_CALLER.items()
    assert keys == []
    assert unkeyed.status_code == 201
    assert sample_routes.created == [{"item": "plain"}]


def test_refuse_keeps_keys_of_known_callers(database_url: str) -> None:
    """An actor, or a credential header, still scopes a key, so it is kept.

    Two callers with different credentials never share a key: the second
    runs its own request.
    """
    with keyed_app(database_url, ANONYMOUS="refuse") as client:
        alice = post(client, {"n": 1}, **{ACTOR_HEADER: "alice"})
        again = post(client, {"n": 1}, **{ACTOR_HEADER: "alice"})
        bob = post(client, {"n": 1}, **{ACTOR_HEADER: "bob"})
        token = post(client, {"n": 1}, Authorization="Bearer one")
        other = post(client, {"n": 1}, Authorization="Bearer two")
        cookie = post(client, {"n": 1}, Cookie="session=s")
        keys = stored()

    assert [r.json()["count"] for r in (alice, again, bob, token, other, cookie)] == [
        1,
        1,
        2,
        3,
        4,
        5,
    ]
    assert again.headers["Idempotent-Replayed"] == "true"
    assert len(keys) == 5


def test_anonymous_is_one_of_two_policies() -> None:
    """Any other value is refused when settings load."""
    assert IdempotencySettings().ANONYMOUS == "per-address"
    assert IdempotencySettings(ANONYMOUS="refuse").ANONYMOUS == "refuse"
    with pytest.raises(ValidationError, match="'per-address' or 'refuse'"):
        IdempotencySettings(ANONYMOUS="per-client")


def test_kept_responses_name_whom_they_are_about(database_url: str) -> None:
    """The actor, and the subjects of committed events; nobody for an anonymous one.

    ``privacy erase`` finds a kept response by these (``subjects``).
    """
    from datetime import UTC, datetime, timedelta

    from sqlalchemy import insert

    from domainkit.contrib.idempotency import middleware
    from domainkit.contrib.idempotency.operations import Operation

    async def finish(operation: Operation) -> object:
        now = datetime.now(UTC)
        async with connections.engine().begin() as connection:
            await connection.execute(
                insert(idempotency_keys).values(
                    key_hash=operation.key_hash,
                    fingerprint="f" * 64,
                    created_at=now,
                    expires_at=now + timedelta(hours=1),
                    token=operation.token,
                    locked_until=now + timedelta(minutes=1),
                )
            )
        await middleware._finish(
            IdempotencySettings(), operation, {"status": 201, "headers": []}, b"{}"
        )
        async with connections.engine().connect() as connection:
            found = await connection.scalar(
                select(idempotency_keys.c.subjects).where(
                    idempotency_keys.c.key_hash == operation.key_hash
                )
            )
        await connections.close_all()
        return found

    with keyed_app(database_url) as client:
        post(client, {"n": 1}, **{ACTOR_HEADER: "alice"})
        post(client, {"n": 1})
        tags = sorted(str(row["subjects"]) for row in stored())
        both = asyncio.run(
            finish(Operation("a" * 64, "t" * 32, actor="alice", subjects={"ann"}))
        )
        events = asyncio.run(finish(Operation("b" * 64, "t" * 32, subjects={"bob"})))

    assert tags == ["\x1falice\x1f", "None"]
    assert both == "\x1falice\x1fann\x1f"
    assert events == "\x1fbob\x1f"
