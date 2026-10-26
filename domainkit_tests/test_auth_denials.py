"""Scoped tokens on routes that check no scope (AUTH-01); audited refusals (AUTH-11).

The PoCs of AUTH-01 with auth's own tokens: a personal token limited to
``shop.view_product`` used to write to ``POST /notes``, guarded by
``Require()`` alone, and read ``AuthenticatedUser`` routes. 0.10 lets it
through with a warning by default and refuses it with
``SCOPED_CREDENTIALS = "refuse"``.

A refusal of a user (403) used to leave nothing in the audit trail; it now
records ``auth.access``, denied, with the actor, the route's template, and
the rule, once per actor and route a minute.
"""

import logging
from collections.abc import Iterator

import pytest

from domainkit.clock import freeze_time
from domainkit.conf import override_settings
from domainkit.contrib.audit import apps as audit_apps
from domainkit.deprecation import DomainkitDeprecationWarning
from domainkit.http import access
from domainkit.http.access import SCOPES_UNBOUNDED
from domainkit_tests.conftest import AUTH, Api
from domainkit_tests.test_auth_http import (
    audited,
    bearer,
    create_user,
    grant,
    login,
)
from domainkit_tests.test_auth_personal_tokens import VIEW, make, user_session


@pytest.fixture(autouse=True)
def fresh_samples() -> Iterator[None]:
    """Each test sees the first refusal of each actor and route, and log line."""
    access.denials.clear()
    access.scoped_log.flush()
    yield
    access.denials.clear()


def test_a_read_only_token_writes_where_no_scope_is_checked(identity: Api) -> None:
    """The PoC of AUTH-01, as 0.9 answered it, with 0.10's warning.

    A token limited to viewing products writes a note and reads a route
    guarded by ``AuthenticatedUser`` alone.
    """
    create_user("alice")
    grant("alice", VIEW)
    client = identity()
    scoped = bearer(make(client, user_session(client), scopes=[VIEW]).json()["token"])

    with pytest.warns(DomainkitDeprecationWarning, match="POST /notes"):
        wrote = client.post("/notes", headers=scoped)
    with pytest.warns(DomainkitDeprecationWarning, match="GET /mine"):
        read = client.get("/mine", headers=scoped)

    assert wrote.json() == {"user": "alice", "scheme": "token"}
    assert read.json() == {"user": "alice"}


def test_a_read_only_token_is_refused_where_no_scope_is_checked(
    identity: Api,
) -> None:
    """``SCOPED_CREDENTIALS = "refuse"``: the token does only what its scopes say.

    Logging out and reading who one is say ``AllowScoped``, and still work.
    """
    create_user("alice")
    grant("alice", VIEW)
    client = identity()
    login_headers = user_session(client)
    scoped = bearer(make(client, login_headers, scopes=[VIEW]).json()["token"])

    with override_settings(SCOPED_CREDENTIALS="refuse"):
        for refused in (
            client.post("/notes", headers=scoped),
            client.get("/mine", headers=scoped),
        ):
            assert refused.status_code == 403
            assert refused.json()["detail"] == SCOPES_UNBOUNDED
        assert client.get("/scoped/mine", headers=scoped).json() == {"user": "alice"}
        assert client.get("/auth/me", headers=scoped).json()["permissions"] == [VIEW]
        assert client.post("/notes", headers=login_headers).status_code == 200
        assert client.get("/mine", headers=login_headers).status_code == 200
        assert client.post("/auth/logout", headers=scoped).status_code == 204
        assert client.get("/scoped/mine", headers=scoped).status_code == 401

    rows = audited("auth.access")
    assert [row["details"] for row in rows] == [
        {"route": "POST /notes", "rule": "scopes_unbounded"},
        {"route": "GET /mine", "rule": "scopes_unbounded"},
    ]


def test_refusals_of_users_are_audited_once_per_actor_and_route(api: Api) -> None:
    """403s record the actor, the route's template, and the rule; 401s do not.

    Repeats of one actor on one route within a minute record nothing more,
    so probing many object ids writes one row a minute per route.
    """
    alice = create_user("alice")
    bob = create_user("bob")
    client = api(
        BACKENDS=[
            f"{AUTH}.backends.ModelBackend",
            "domainkit_tests.test_auth_http.OwnerBackend",
        ]
    )
    as_alice = bearer(login(client, "alice")["access_token"])
    as_bob = bearer(login(client, "bob")["access_token"])

    for _ in range(3):
        assert client.get("/products", headers=as_alice).status_code == 403
    assert client.get("/products", headers=as_bob).status_code == 403
    for owner in ("bob", "carol", "dave"):
        assert client.get(f"/products/{owner}", headers=as_alice).status_code == 403
    assert client.get("/staff", headers=as_alice).status_code == 403
    assert client.get("/products").status_code == 401
    assert client.get("/products/alice", headers=as_alice).status_code == 200

    rows = audited("auth.access")
    assert [(row["actor"], row["outcome"], row["details"]) for row in rows] == [
        (str(alice), "denied", {"route": "GET /products", "rule": "ModelPermissions"}),
        (str(bob), "denied", {"route": "GET /products", "rule": "ModelPermissions"}),
        (
            str(alice),
            "denied",
            {"route": "GET /products/{owner}", "rule": "HasObjectPermissions"},
        ),
        (str(alice), "denied", {"route": "GET /staff", "rule": "IsAdminUser"}),
    ]
    assert all(row["target"] is None for row in rows)
    assert "Bearer" not in str(rows)


def test_a_refusal_stands_when_the_trail_cannot_record_it(
    api: Api, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The recorder's failure is logged by its class; the answer is still 403."""
    create_user("alice")
    client = api()
    as_alice = bearer(login(client, "alice")["access_token"])

    async def broken(*args: object, **kwargs: object) -> None:
        raise RuntimeError("database down: secret-detail")

    monkeypatch.setattr(audit_apps, "record", broken)
    with caplog.at_level(logging.ERROR, logger="domainkit.http.access"):
        assert client.get("/products", headers=as_alice).status_code == 403

    (line,) = [r.getMessage() for r in caplog.records if r.name == access.__name__]
    assert line == "Could not record a refused request to GET /products (RuntimeError)"


def test_the_sampler_lets_each_key_once_per_interval_and_stays_bounded() -> None:
    """One per key per interval; the oldest key is forgotten past the bound."""
    sampler = access._Sampler(interval=60.0, keys=2)
    with freeze_time("2026-10-26T10:00:00Z") as clock:
        assert sampler.first(("alice", "GET /a"))
        assert not sampler.first(("alice", "GET /a"))
        assert sampler.first(("alice", "GET /b"))
        clock.tick(59)
        assert not sampler.first(("alice", "GET /a"))
        clock.tick(1)
        assert sampler.first(("alice", "GET /a"))
        assert sampler.first(("bob", "GET /a"))  # Forgets ("alice", "GET /b").
        assert sampler.first(("alice", "GET /b"))
        assert len(sampler._seen) == 2
    sampler.clear()
    assert len(sampler._seen) == 0
