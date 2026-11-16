"""An approval is worth one act, with its arguments, once (ADR 0144, user report A6).

The sample routes rename the user (an act on auth's database, spent with
its commit) and archive a note (kept elsewhere, spent before the act).
Each route asks ``StepUp(for_=...)``; the client asks for an
authorization, confirms it with a code, and sends it with the act.
"""

import asyncio
import base64
import os
import threading
from collections.abc import Callable, Iterator
from datetime import timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, update

from domainkit.clock import FrozenClock, freeze_time
from domainkit.conf import override_settings
from domainkit.contrib.auth import AuthUnitOfWork, UserId
from domainkit.contrib.auth.actions import (
    ActionAuthorization,
    ActionConfirmation,
    AuthorizationChanged,
    AuthorizationNeedsOtherFactors,
    AuthorizationNotFound,
    Shown,
    confirm_action_with,
    link_action_authorization,
    on_action_confirmation,
    register_action,
)
from domainkit.contrib.auth.erasure import export_logins
from domainkit.contrib.auth.tables import authorizations
from domainkit.contrib.evidence import evidence_store
from domainkit.contrib.evidence.tasks import record_evidence
from domainkit.contrib.messaging import Worker
from domainkit.db import connections
from domainkit.ddd import ConcurrentUpdate
from domainkit.management import call_command
from domainkit.management.base import run_async
from domainkit_tests import sample_action_routes as acts
from domainkit_tests.conftest import IDENTITY, POSTGRES_VARIABLE, Api, Migrated, Shape
from domainkit_tests.support.auth import (
    PASSWORD,
    audited,
    bearer,
    create_user,
    login,
)
from domainkit_tests.test_auth_stepup import enrol, hourly_purge, now_code

START = "2027-01-04T10:00:00Z"
ROUTES = "domainkit_tests.sample_action_routes.router"
EVIDENCE = "domainkit.contrib.evidence"
NOTE = UUID("00000000-0000-4000-8000-000000000042")
ON_POSTGRES = bool(os.environ.get(POSTGRES_VARIABLE))


@pytest.fixture
def frozen() -> Iterator[FrozenClock]:
    """One clock for codes and expiries."""
    with freeze_time(START) as clock:
        yield clock


class Web:
    """A signed-in session of a user, with a second factor, at the sample API."""

    def __init__(self, client: TestClient, clock: FrozenClock, user: str) -> None:
        """Log ``user`` in, without a second factor yet."""
        self.client = client
        self.clock = clock
        self.headers = bearer(login(client, user)["access_token"])
        self.secret = b""

    def again(self, user: str = "alice") -> "Web":
        """Another login of ``user``, with the second factor: another session."""
        other = object.__new__(Web)
        other.client, other.clock, other.secret = self.client, self.clock, self.secret
        started = self.client.post(
            "/auth/login", json={"username": user, "password": PASSWORD}
        ).json()
        logged_in = self.client.post(
            "/auth/mfa/login",
            json={
                "mfa_token": started["mfa_token"],
                "code": now_code(self.secret, self.clock),
            },
        )
        assert logged_in.status_code == 200, logged_in.json()
        other.headers = bearer(logged_in.json()["access_token"])
        return other

    def enrolled(self) -> "Web":
        """The same session, with the second factor turned on."""
        self.secret = enrol(self.client, self.headers, self.clock)
        return self

    def ask(self, name: str = "Al", **body: str) -> Any:  # noqa: ANN401 - a response.
        """Ask for an authorization of renaming oneself ``name``."""
        return self.client.post(
            "/me/name/authorization",
            json={"first_name": name, **body},
            headers=self.headers,
        )

    def confirm(self, authorization_id: str, **body: str) -> Any:  # noqa: ANN401
        """Confirm it with now's code (and more)."""
        return self.client.post(
            f"/auth/authorizations/{authorization_id}/confirm",
            json={"code": now_code(self.secret, self.clock), **body},
            headers=self.headers,
        )

    def authorized(self, name: str = "Al", **body: str) -> str:
        """``<id>.<code>``: a confirmed authorization of renaming oneself ``name``."""
        asked = self.ask(name, **body)
        assert asked.status_code == 201, asked.json()
        confirmed = self.confirm(asked.json()["id"])
        assert confirmed.status_code == 200, confirmed.json()
        authorization: str = confirmed.json()["authorization"]
        return authorization

    def rename(
        self, name: str = "Al", authorization: str | None = None, **body: str
    ) -> Any:  # noqa: ANN401 - a response.
        """Rename oneself ``name``, sending ``authorization`` if given."""
        headers = dict(self.headers)
        if authorization is not None:
            headers["Action-Authorization"] = authorization
        return self.client.post(
            "/me/name", json={"first_name": name, **body}, headers=headers
        )


@pytest.fixture
def web(identity: Api, frozen: FrozenClock) -> Iterator[Web]:
    """Alice's session at the sample API, the actions registered."""
    acts.FAILURES.clear()
    acts.ARCHIVED.clear()
    acts.BARRIER.clear()
    with override_settings(ROOT_ROUTER=ROUTES):
        register_action(acts.RENAME, domain="shop")
        register_action(acts.ARCHIVE, domain="shop")
        create_user("alice")
        yield Web(identity(), frozen, "alice").enrolled()
    acts.FAILURES.clear()
    acts.BARRIER.clear()


def rows() -> list[dict[str, Any]]:
    """Every stored authorization."""

    async def read() -> list[dict[str, Any]]:
        async with connections.engine().connect() as connection:
            found = await connection.execute(
                select(authorizations).order_by(authorizations.c.created_at)
            )
            return [dict(row._mapping) for row in found]

    return run_async(read())


def first_name(username: str = "alice") -> str:
    """The stored first name of ``username``."""

    async def read() -> str:
        async with AuthUnitOfWork() as uow:
            user = await uow.users.get_by_natural_key(username)
        assert user is not None
        return str(user.first_name)  # type: ignore[attr-defined]

    return run_async(read())


def code_of(response: Any) -> tuple[int, str]:  # noqa: ANN401 - a response.
    """The status and the error code of ``response``."""
    return response.status_code, response.json().get("code")


def reasons() -> list[str]:
    """The reasons of the refused authorizations the trail kept, in order."""
    return [row["details"]["reason"] for row in audited("auth.authorization")]


def test_an_act_is_asked_shown_confirmed_with_a_code_and_spent_once(web: Web) -> None:
    """Refused without one; asked, shown, confirmed, spent; then a replay fails.

    The server builds what is shown, and keeps it; the code is never
    stored; the act commits, and the authorization is spent with it.
    """
    before = web.rename("Al")
    asked = web.ask("Al")
    authorization_id = asked.json()["id"]
    shown = web.client.get(
        f"/auth/authorizations/{authorization_id}", headers=web.headers
    )
    confirmed = web.confirm(authorization_id)
    authorization = confirmed.json()["authorization"]
    renamed = web.rename("Al", authorization)
    replayed = web.rename("Al", authorization)

    assert code_of(before) == (401, "authorization_required")
    challenge = before.headers["WWW-Authenticate"]
    assert 'error="insufficient_user_authentication"' in challenge
    assert 'purpose="accounts.rename"' in challenge
    assert before.json()["errors"][0]["msg"] == "accounts.rename"
    assert asked.status_code == 201, asked.json()
    assert asked.json()["purpose"] == "accounts.rename"
    assert asked.json()["shown"] == {
        "title": "Change your first name",
        "details": [{"label": "New first name", "value": "Al"}],
    }
    assert shown.json()["confirmed"] is False
    assert shown.json()["methods"] == ["otp"]
    assert shown.headers["Cache-Control"] == "no-store"
    assert confirmed.status_code == 200, confirmed.json()
    assert authorization.startswith(f"{authorization_id}.")
    assert renamed.status_code == 200, renamed.json()
    assert first_name() == "Al"
    assert code_of(replayed) == (403, "action_not_authorized")
    [row] = rows()
    assert (row["amr"], row["consumed_at"] is not None) == ("otp", True)
    assert authorization.split(".")[1] not in repr(row)  # The code is never kept.


def test_the_trail_names_the_act_and_never_its_arguments(web: Web) -> None:
    """Confirmed and spent: the purpose, subject, digest, methods; no name, no words."""
    authorization = web.authorized("Wilhelmina")
    web.rename("Wilhelmina", authorization)
    web.rename("Wilhelmina", "nonsense")

    [authorized] = audited("auth.action_authorized")
    [used] = audited("auth.action_authorization_used")
    for row in (authorized, used):
        assert row["details"]["purpose"] == "accounts.rename"
        assert row["details"]["subject"].startswith("auth.user:")
        assert row["details"]["methods"] == ["otp"]
        assert len(row["details"]["digest"]) == 64
        assert "Wilhelmina" not in repr(row)
        assert "Change your first name" not in repr(row)
    assert reasons() == ["invalid_authorization"]


def test_other_arguments_void_the_authorization(web: Web) -> None:
    """Confirmed for one name, sent with another: refused, and gone for both."""
    authorization = web.authorized("Al")

    other = web.rename("Bob", authorization)
    confirmed_one = web.rename("Al", authorization)

    assert code_of(other) == (403, "action_not_authorized")
    assert code_of(confirmed_one) == (403, "action_not_authorized")
    assert first_name() == ""
    assert reasons() == ["changed", "invalid_authorization"]


def test_a_field_the_action_ignores_may_differ(web: Web) -> None:
    """``ignore=("asked_at",)``: a field that changes on every call binds nothing."""
    authorization = web.authorized("Al", asked_at="10:00")

    renamed = web.rename("Al", authorization, asked_at="10:01")

    assert renamed.status_code == 200, renamed.json()


def test_another_login_of_the_same_user_cannot_spend_it(web: Web) -> None:
    """Another token of alice's is refused, and refused to see it; the asker's holds."""
    authorization = web.authorized("Al")
    other = web.again()
    authorization_id = authorization.split(".")[0]

    spent_elsewhere = other.rename("Al", authorization)
    seen_elsewhere = other.client.get(
        f"/auth/authorizations/{authorization_id}", headers=other.headers
    )
    own = web.rename("Al", authorization)

    assert code_of(spent_elsewhere) == (403, "action_not_authorized")
    assert code_of(seen_elsewhere) == (404, "authorization_not_found")
    assert own.status_code == 200, own.json()


def test_an_authorization_expires_with_its_ttl(web: Web) -> None:
    """Five minutes from asking, confirmed or not: neither spent nor confirmed later."""
    authorization = web.authorized("Al")
    unconfirmed = web.ask("Bo").json()["id"]
    web.clock.tick(timedelta(minutes=5, seconds=1))

    late_act = web.rename("Al", authorization)
    late_confirmation = web.confirm(unconfirmed)

    assert code_of(late_act) == (403, "action_not_authorized")
    assert code_of(late_confirmation) == (409, "authorization_expired")


def test_an_act_spent_with_its_commit_can_be_sent_again_after_it_failed(
    web: Web,
) -> None:
    """On auth's database: refused on the way (409), it holds for one more try."""
    authorization = web.authorized("Al")
    acts.FAILURES.append(ConcurrentUpdate("Someone else changed it first."))

    lost = web.rename("Al", authorization)
    again = web.rename("Al", authorization)
    third = web.rename("Al", authorization)

    assert code_of(lost) == (409, "concurrent_update")
    assert again.status_code == 200, again.json()
    assert code_of(third) == (403, "action_not_authorized")


def test_an_act_in_a_unit_of_work_the_route_opened_spends_it_with_its_commit(
    web: Web,
) -> None:
    """A resource's route lends its open unit of work (``Joined``): spent with it."""
    authorization = web.authorized("Al")
    headers = {**web.headers, "Action-Authorization": authorization}
    acts.FAILURES.append(ConcurrentUpdate("Someone else changed it first."))

    def joined() -> Any:  # noqa: ANN401 - a response.
        return web.client.post(
            "/me/name/joined", json={"first_name": "Al"}, headers=headers
        )

    lost, again, third = joined(), joined(), joined()

    assert code_of(lost) == (409, "concurrent_update")
    assert again.status_code == 200, again.json()
    assert code_of(third) == (403, "action_not_authorized")
    assert first_name() == "Al"


def test_code_that_spends_it_itself_commits_the_spend_with_its_change(
    web: Web,
) -> None:
    """``spend_authorization`` in an open unit of work: spent if it commits."""
    authorization = web.authorized("Al")
    headers = {**web.headers, "Action-Authorization": authorization}
    acts.FAILURES.append(ConcurrentUpdate("Someone else changed it first."))

    def in_code() -> Any:  # noqa: ANN401 - a response.
        return web.client.post(
            "/me/name/in-code", json={"first_name": "Al"}, headers=headers
        )

    lost, again, third = in_code(), in_code(), in_code()

    assert code_of(lost) == (409, "concurrent_update")
    assert again.status_code == 200, again.json()
    assert code_of(third) == (403, "action_not_authorized")
    assert len(audited("auth.action_authorization_used")) == 1


def test_a_wrong_code_confirms_nothing_and_is_counted(web: Web) -> None:
    """``401 invalid_code``, told to the trail as ``auth.authorization``."""
    asked = web.ask("Al").json()

    wrong = web.client.post(
        f"/auth/authorizations/{asked['id']}/confirm",
        json={"code": "000000"},
        headers=web.headers,
    )
    right = web.confirm(asked["id"])

    assert code_of(wrong) == (401, "invalid_code")
    assert right.status_code == 200, right.json()
    assert reasons() == ["invalid_code"]


def test_a_row_changed_after_its_confirmation_fails_its_code(web: Web) -> None:
    """The code covers what was confirmed, not what is stored now."""
    authorization = web.authorized("Al")

    async def change() -> None:
        async with connections.engine().begin() as connection:
            await connection.execute(
                update(authorizations).values(subject="auth.user:someone-else")
            )

    run_async(change())
    renamed = web.rename("Al", authorization)

    assert code_of(renamed) == (403, "action_not_authorized")
    assert reasons() == ["invalid_authorization"]


def test_an_act_elsewhere_spends_it_before_and_a_failure_needs_a_new_one(
    web: Web,
) -> None:
    """Off auth's database: spent alone, just before the act; at most once."""
    asked = web.client.post(
        f"/notes/{NOTE}/archive/authorization", headers=web.headers
    ).json()
    confirmed = web.confirm(asked["id"]).json()["authorization"]
    headers = {**web.headers, "Action-Authorization": confirmed}
    acts.FAILURES.append(ConcurrentUpdate("Someone else changed it first."))

    lost = web.client.post(f"/notes/{NOTE}/archive", headers=headers)
    again = web.client.post(f"/notes/{NOTE}/archive", headers=headers)

    assert asked["shown"]["details"] == [{"label": "Note", "value": str(NOTE)}]
    assert code_of(lost) == (409, "concurrent_update")
    assert code_of(again) == (403, "action_not_authorized")
    assert acts.ARCHIVED == []
    assert reasons() == ["invalid_authorization"]  # The rule saw it spent.


def test_an_authorization_no_service_spent_fails_the_request_in_tests(
    web: Web,
) -> None:
    """A service left undecorated: the act ran, the authorization is void, 500.

    In tests (and under ``DEBUG``) the request fails, so the forgotten
    decorator shows; the trail says ``unspent``.
    """
    authorization = web.authorized("Al")
    headers = {**web.headers, "Action-Authorization": authorization}

    forgotten = web.client.post(
        "/me/name/forgotten", json={"first_name": "Al"}, headers=headers
    )
    again = web.rename("Al", authorization)

    assert code_of(forgotten) == (500, "authorization_unspent")
    assert code_of(again) == (403, "action_not_authorized")
    assert reasons() == ["unspent", "invalid_authorization"]


def test_an_authorization_no_service_spent_is_voided_and_logged_in_production(
    web: Web, caplog: pytest.LogCaptureFixture
) -> None:
    """Outside tests and ``DEBUG`` the answer stands; the authorization is void."""
    authorization = web.authorized("Al")
    headers = {**web.headers, "Action-Authorization": authorization}

    with override_settings(ENVIRONMENT="local", DEBUG=False):
        forgotten = web.client.post(
            "/me/name/forgotten", json={"first_name": "Al"}, headers=headers
        )
    again = web.rename("Al", authorization)

    assert forgotten.status_code == 200, forgotten.json()
    assert code_of(again) == (403, "action_not_authorized")
    assert "auth.authorization_unspent: POST /me/name/forgotten" in caplog.text


def test_a_refused_request_leaves_its_authorization_for_the_act(web: Web) -> None:
    """A body that does not validate: no act ran, so the authorization still holds."""
    authorization = web.authorized("Al")
    headers = {**web.headers, "Action-Authorization": authorization}

    invalid = web.client.post("/me/name", json={}, headers=headers)
    renamed = web.rename("Al", authorization)

    assert invalid.status_code == 422
    assert renamed.status_code == 200, renamed.json()


def at_once(count: int, act: Callable[[], Any]) -> list[int]:
    """The statuses of ``count`` calls of ``act``, from as many threads at once."""
    answers: list[int] = []

    def call() -> None:
        answers.append(act().status_code)

    threads = [threading.Thread(target=call) for _ in range(count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    return answers


@pytest.mark.skipif(not ON_POSTGRES, reason=f"set {POSTGRES_VARIABLE}")
def test_two_acts_at_once_spend_it_with_one_commit(web: Web) -> None:
    """Both pass the rule, both commit at once: one wins, the other is refused.

    On auth's database, the spend is the commit's first write: the second
    act waits on the row, then finds it spent (SQLite cannot hold two such
    transactions at once, so PostgreSQL alone runs it).
    """
    authorization = web.authorized("Al")
    acts.BARRIER.append(asyncio.Barrier(2))

    answers = at_once(2, lambda: web.rename("Al", authorization))

    assert sorted(answers) == [200, 403]
    assert len(audited("auth.action_authorization_used")) == 1
    assert reasons() == ["used"]


def test_ten_acts_at_once_spend_it_before_the_act_once(web: Web) -> None:
    """Off auth's database, one statement spends it: of ten at once, one acts."""
    asked = web.client.post(
        f"/notes/{NOTE}/archive/authorization", headers=web.headers
    ).json()
    confirmed = web.confirm(asked["id"]).json()["authorization"]
    headers = {**web.headers, "Action-Authorization": confirmed}

    answers = at_once(
        10, lambda: web.client.post(f"/notes/{NOTE}/archive", headers=headers)
    )

    assert sorted(answers) == [204, *[403] * 9]
    assert acts.ARCHIVED == [NOTE]


def test_asking_needs_a_way_to_confirm_what_the_act_asks(
    identity: Api, frozen: FrozenClock
) -> None:
    """A user with no second factor cannot ask: 409 ``no_second_factor``."""
    with override_settings(ROOT_ROUTER=ROUTES):
        register_action(acts.RENAME, domain="shop")
        create_user("bob")
        bob = Web(identity(), frozen, "bob")
        unconfirmable = bob.ask()
        bob.enrolled()
        asked = bob.ask()

    assert code_of(unconfirmable) == (409, "no_second_factor")
    assert asked.status_code == 201, asked.json()


def test_a_login_holds_ten_unconfirmed_authorizations(web: Web) -> None:
    """The eleventh ends the oldest: it can no longer be confirmed."""
    asked = []
    for number in range(11):
        asked.append(web.ask(f"Name {number}").json()["id"])
        web.clock.tick(1)

    oldest = web.confirm(asked[0])
    newest = web.confirm(asked[-1])

    waiting = [row for row in rows() if row["consumed_at"] is None]
    assert code_of(oldest) == (409, "authorization_confirmed")
    assert newest.status_code == 200, newest.json()
    assert len(waiting) == 10


def test_confirming_twice_or_another_users_is_refused(web: Web) -> None:
    """Confirmed already: 409; unknown or another session's: 404."""
    authorization_id = web.authorized("Al").split(".")[0]

    twice = web.confirm(authorization_id)
    unknown = web.confirm(str(uuid4()))

    assert code_of(twice) == (409, "authorization_confirmed")
    assert code_of(unknown) == (404, "authorization_not_found")


def test_a_code_never_proves_the_phone_nor_does_a_passkey(web: Web) -> None:
    """``METHODS = ["phone"]``: a code is refused, a passkey too; the phone confirms.

    The phone is an app of its own (phase 2); here, a stand-in registers
    what it proves, ``hwk`` and ``phone``, and confirms through
    ``confirm_action_with`` as it will.
    """

    async def always(user_id: UUID) -> bool:
        return True

    phone = ActionConfirmation("phone", frozenset({"hwk", "phone"}), always)
    on_action_confirmation(phone, domain="shop")
    user_id = run_async(_user_id("alice"))
    with override_settings(AUTH={"AUTHORIZATIONS": {"METHODS": ["phone"]}}):
        asked = web.ask("Al").json()
        by_code = web.confirm(asked["id"])
        shown = web.client.get(
            f"/auth/authorizations/{asked['id']}", headers=web.headers
        ).json()
        authorization_id = UUID(asked["id"])
        with pytest.raises(AuthorizationNeedsOtherFactors):
            run_async(
                confirm_action_with(authorization_id, {"hwk", "mfa"}, user_id=user_id)
            )
        by_phone = run_async(
            confirm_action_with(authorization_id, {"hwk", "phone"}, user_id=user_id)
        )
        renamed = web.rename("Al", by_phone)

    assert shown["methods"] == ["phone"]
    assert code_of(by_code) == (409, "authorization_needs_other_factors")
    assert renamed.status_code == 200, renamed.json()
    assert rows()[0]["amr"] == "hwk phone"


async def _user_id(username: str) -> UUID:
    """The id of ``username``."""
    async with AuthUnitOfWork() as uow:
        user = await uow.users.get_by_natural_key(username)
    assert user is not None
    return UUID(str(user.id))


def test_a_device_confirms_with_its_signature_kept_and_its_digest_checked(
    web: Web,
) -> None:
    """The device's key, what it signed, and its signature stay on the row.

    Another digest than the act's is refused (``authorization_changed``),
    and the phone's request is linked to the row before it confirms.
    """
    user_id = run_async(_user_id("alice"))
    asked = web.ask("Al").json()
    authorization_id = UUID(asked["id"])
    request_id = uuid4()
    digest = rows()[0]["digest"]
    linked = run_async(
        link_action_authorization(
            authorization_id, user_id=user_id, sign_in_request_id=request_id
        )
    )
    with pytest.raises(AuthorizationChanged):
        run_async(
            confirm_action_with(
                authorization_id, {"hwk"}, user_id=user_id, digest="0" * 64
            )
        )
    with pytest.raises(AuthorizationNotFound):
        run_async(confirm_action_with(authorization_id, {"hwk"}, user_id=uuid4()))
    device_id = uuid4()
    run_async(
        confirm_action_with(
            authorization_id,
            {"hwk"},
            user_id=user_id,
            digest=digest,
            device_id=device_id,
            algorithm=-7,
            public_key=b"key",
            signature=b"signature",
            signed=b"signed message",
            signed_at=web.clock.tick(0),
        )
    )

    [row] = rows()
    assert linked.sign_in_request_id == request_id
    assert row["sign_in_request_id"] == request_id
    assert (row["device_id"], row["algorithm"]) == (device_id, -7)
    assert (row["public_key"], row["signature"]) == (b"key", b"signature")
    assert row["signed"] == b"signed message"
    assert row["signed_at"] is not None
    [authorized] = audited("auth.action_authorized")
    assert authorized["details"]["device_id"] == str(device_id)


def test_past_expiry_they_are_purged_by_the_hour_and_by_command(web: Web) -> None:
    """Live ones stay; expired ones go, hourly and with ``cleartokens``."""
    web.ask("Al")
    web.clock.tick(timedelta(minutes=6))
    web.ask("Bo")

    run_async(hourly_purge())
    kept = rows()
    web.clock.tick(timedelta(minutes=6))
    call_command("cleartokens", stdout=None)

    assert [row["shown"]["details"][0][1] for row in kept] == ["Bo"]
    assert rows() == []


def test_privacy_export_lists_them_and_deleting_the_user_deletes_them(
    web: Web,
) -> None:
    """The purpose, subject, and times; never what was shown, nor a digest."""
    user_id = run_async(_user_id("alice"))
    web.authorized("Wilhelmina")

    exported: dict[str, Any] = dict(run_async(export_logins(str(user_id))))

    async def delete() -> None:
        async with AuthUnitOfWork() as uow:
            await uow.users.remove(await uow.users.get(UserId(user_id)))
            await uow.commit()

    run_async(delete())

    [listed] = exported["authorizations"]
    assert listed["purpose"] == "accounts.rename"
    assert listed["subject"] == f"auth.user:{user_id}"
    assert listed["confirmed_at"] is not None
    assert "Wilhelmina" not in repr(exported)
    assert rows() == []


EVIDENCE_PROJECT = Shape(
    "identity_evidence",
    "identity evidence",
    {
        **IDENTITY.settings,
        "INSTALLED_DOMAINS": [*IDENTITY.settings["INSTALLED_DOMAINS"], EVIDENCE],
        "LEDGERS": {"evidence": {"URL": "memory://"}},
    },
)


def test_a_signed_confirmation_goes_into_the_evidence_ledger_too(
    migrated: Migrated, frozen: FrozenClock
) -> None:
    """With the ledger installed, the device's key and signature outlive the row.

    The record names the authorization, its purpose, subject, and digest,
    the methods, and the device's id, key, what it signed, its signature,
    and when; never what was shown.
    """
    with migrated(EVIDENCE_PROJECT):
        user_id = create_user("alice")
        authorization = stored_authorization(user_id, frozen)
        run_async(
            confirm_action_with(
                authorization.id,
                {"hwk", "phone"},
                user_id=user_id,
                device_id=UUID(int=5),
                algorithm=-8,
                public_key=b"public key",
                signature=b"signature",
                signed=b"signed message",
                signed_at=frozen.tick(0),
            )
        )

        async def record() -> tuple[int, list[Any]]:
            processed = await Worker().run_once()  # The ledger lives in one loop.
            return processed, await evidence_store().for_subject(f"auth.user:{user_id}")

        processed, [recorded] = run_async(record())

    facts = recorded.evidence.facts
    assert record_evidence.__name__ == "record_evidence"  # Registered at import.
    assert processed >= 1  # The evidence's; handlers other tests import add theirs.
    assert recorded.evidence.kind == "auth.action_authorized"
    assert facts["authorization_id"] == str(authorization.id)
    assert (facts["purpose"], facts["digest"]) == ("accounts.close", "c" * 64)
    assert facts["methods"] == ["hwk", "phone"]
    assert (facts["device_id"], facts["algorithm"]) == (str(UUID(int=5)), -8)
    assert base64.urlsafe_b64decode(facts["public_key"]) == b"public key"
    assert base64.urlsafe_b64decode(facts["signature"]) == b"signature"
    assert base64.urlsafe_b64decode(facts["signed"]) == b"signed message"
    assert "Close an account" not in repr(recorded)


def stored_authorization(user_id: UUID, clock: FrozenClock) -> ActionAuthorization:
    """An unconfirmed authorization of ``user_id``'s, stored as asking stores one."""
    now = clock.tick(0)
    authorization = ActionAuthorization(
        id=uuid4(),
        user_id=user_id,
        login="b" * 64,
        purpose="accounts.close",
        subject="accounts.account:7",
        digest="c" * 64,
        shown=Shown("Close an account", {"Reason": "moving"}),
        created_at=now,
        expires_at=now + timedelta(minutes=5),
    )

    async def store() -> None:
        async with AuthUnitOfWork() as uow:
            await uow.authorizations.add(authorization)
            await uow.commit()

    run_async(store())
    return authorization
