"""Codes of 6 digits that verify an address (ADR 0126)."""

import re
from datetime import timedelta
from typing import Any
from uuid import UUID

from fastapi.testclient import TestClient
from sqlalchemy import select

from domainkit import clock
from domainkit.clock import freeze_time
from domainkit.contrib import mail
from domainkit.contrib.auth import AuthUnitOfWork, UserId
from domainkit.contrib.auth.codes import EMAIL_VERIFICATION, Proven, target_of
from domainkit.contrib.auth.tables import auth_codes
from domainkit.db import connections
from domainkit.management.base import run_async
from domainkit_tests.conftest import Api
from domainkit_tests.test_auth_http import audited, bearer, create_user, login
from domainkit_tests.test_auth_recovery import run_worker

CODE = re.compile(r"\b[0-9]{6}\b")
WRONG = (400, "invalid_verification_code")


def mailed_code(index: int = -1) -> str:
    """The code in a message of the outbox (the last, by default)."""
    found = CODE.search(mail.outbox[index].body)
    assert found is not None, mail.outbox[index].body
    return found[0]


def ask(client: TestClient, headers: dict[str, str]) -> str:
    """Ask for a code, run the worker; return the code mailed."""
    response = client.post("/auth/email-verification", headers=headers)
    assert response.status_code == 202, response.json()
    run_worker()
    return mailed_code()


def confirm(client: TestClient, headers: dict[str, str], code: str) -> tuple[int, Any]:
    """The status and problem code of confirming ``code``."""
    response = client.post(
        "/auth/email-verification/code", json={"code": code}, headers=headers
    )
    problem = response.json().get("code") if response.content else None
    return response.status_code, problem


def verified(client: TestClient, headers: dict[str, str]) -> bool:
    """Whether ``/me`` says the address is verified."""
    return bool(client.get("/auth/me", headers=headers).json()["email_verified"])


def stored_codes() -> list[dict[str, Any]]:
    """Every row of the codes table."""

    async def read() -> list[dict[str, Any]]:
        async with connections.engine().connect() as connection:
            rows = await connection.execute(select(auth_codes))
            return [dict(row._mapping) for row in rows]

    return run_async(read())


def test_a_mailed_code_verifies_the_address_once(identity: Api) -> None:
    """A wrong code is refused and audited; the right one works, once."""
    alice = create_user("alice", email="alice@example.com")
    client = identity(VERIFICATION="code")
    headers = bearer(login(client, "alice")["access_token"])

    response = client.post("/auth/email-verification", headers=headers)
    assert response.json() == {"detail": "A code to verify your address is on its way."}
    run_worker()
    (message,) = mail.outbox
    assert (message.subject, message.to) == (
        "Your code to verify your email address",
        ("alice@example.com",),
    )
    assert "It works for 10 minutes." in message.body
    code = mailed_code()

    wrong = "000000" if code != "000000" else "111111"
    assert confirm(client, headers, wrong) == WRONG
    assert not verified(client, headers)
    assert confirm(client, headers, f" {code} ") == (204, None)
    assert verified(client, headers)
    assert confirm(client, headers, code) == WRONG
    (event,) = audited("auth.email_verified")
    assert event["details"] == {"user_id": str(alice)}
    refusals = audited("auth.email_verification")
    assert [each["details"] for each in refusals] == [
        {"reason": "invalid_verification_code"}
    ] * len(refusals)
    assert stored_codes() == []


def test_a_code_is_spent_after_its_wrong_tries(identity: Api) -> None:
    """Past ``AUTH.CODE_ATTEMPTS`` tries, even the right code is refused."""
    create_user("bob", email="bob@example.com")
    client = identity(VERIFICATION="code", CODE_ATTEMPTS=2)
    headers = bearer(login(client, "bob")["access_token"])
    code = ask(client, headers)
    wrong = "000000" if code != "000000" else "111111"

    assert confirm(client, headers, wrong) == WRONG
    assert confirm(client, headers, wrong) == WRONG
    assert confirm(client, headers, code) == WRONG
    assert [row["attempts"] for row in stored_codes()] == [2]
    assert confirm(client, headers, ask(client, headers)) == (204, None)


def test_a_new_code_replaces_the_one_before(identity: Api) -> None:
    """Only the last code sent works."""
    create_user("carol", email="carol@example.com")
    client = identity(VERIFICATION="code")
    headers = bearer(login(client, "carol")["access_token"])
    first = ask(client, headers)
    second = ask(client, headers)

    assert len(stored_codes()) == 1
    if first != second:
        assert confirm(client, headers, first) == WRONG
    assert confirm(client, headers, second) == (204, None)


def test_a_code_holds_for_the_address_it_went_to(identity: Api) -> None:
    """Once the address changes, its code no longer works."""
    dave = create_user("dave", email="dave@example.com")
    client = identity(VERIFICATION="code")
    headers = bearer(login(client, "dave")["access_token"])
    code = ask(client, headers)

    async def change_email() -> None:
        async with AuthUnitOfWork() as uow:
            user = await uow.users.get(UserId(dave))
            user.update(email="new@example.com")
            await uow.commit()

    run_async(change_email())
    assert confirm(client, headers, code) == WRONG
    assert not verified(client, headers)


def test_links_still_work_in_link_mode(identity: Api) -> None:
    """``AUTH.VERIFICATION`` is ``"link"`` by default: no code is sent."""
    create_user("erin", email="erin@example.com")
    client = identity()
    headers = bearer(login(client, "erin")["access_token"])
    client.post("/auth/email-verification", headers=headers)
    run_worker()

    assert "/" in mail.outbox[0].body or ":" in mail.outbox[0].body
    assert stored_codes() == []
    assert confirm(client, headers, "123456") == WRONG


def test_a_code_needs_the_user_and_an_address(identity: Api) -> None:
    """Anonymous callers get 401; a user without an address, 400."""
    create_user("noemail")
    client = identity(VERIFICATION="code")
    headers = bearer(login(client, "noemail")["access_token"])

    assert (
        client.post("/auth/email-verification/code", json={"code": "1"}).status_code
        == 401
    )
    assert confirm(client, headers, "123456") == WRONG


def test_codes_are_kept_as_digests_and_expire(identity: Api) -> None:
    """The table keeps neither the code nor the address; codes end on time."""
    identity()
    target = target_of(" Ann@EXAMPLE.com")
    assert target == target_of("ann@example.com") != target_of("bob@example.com")
    assert target_of("+33 6 12 34 56 78") == target_of("+33612345678")

    async def issue() -> str:
        async with AuthUnitOfWork() as uow:
            code = await uow.verification_codes.issue(EMAIL_VERIFICATION, target)
            await uow.commit()
        return code

    async def check(code: str) -> Proven | None:
        async with AuthUnitOfWork() as uow:
            proven = await uow.verification_codes.check(
                EMAIL_VERIFICATION, target, code
            )
            await uow.commit()
        return proven

    async def clear() -> int:
        async with AuthUnitOfWork() as uow:
            count = await uow.verification_codes.clear_expired()
            await uow.commit()
        return count

    with freeze_time(clock.now()) as frozen:
        code = run_async(issue())
        (row,) = stored_codes()
        assert code not in (row["digest"], row["target"])
        assert "ann" not in row["target"]
        assert row["user_id"] is None
        assert row["expires_at"] - row["created_at"] == timedelta(minutes=10)
        assert run_async(clear()) == 0
        frozen.tick(timedelta(minutes=10))
        assert run_async(check(code)) is None
        assert run_async(clear()) == 1

        code = run_async(issue())
        assert run_async(check(code)) == Proven(user_id=None)


def test_forgetting_a_users_credentials_forgets_their_codes(identity: Api) -> None:
    """Erasure and deactivation leave no code of the user behind."""
    frank = create_user("frank", email="frank@example.com")
    identity()

    async def issue(user_id: UUID) -> None:
        async with AuthUnitOfWork() as uow:
            await uow.verification_codes.issue(
                EMAIL_VERIFICATION, target_of("frank@example.com"), user_id=user_id
            )
            await uow.commit()

    async def forget(user_id: UUID) -> None:
        async with AuthUnitOfWork() as uow:
            await uow.users.forget_credentials(user_id)
            await uow.commit()

    run_async(issue(frank))
    assert [row["user_id"] for row in stored_codes()] == [frank]
    run_async(forget(frank))
    assert stored_codes() == []
