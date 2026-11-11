"""Phone verification: a code by text message, tried, limited, and kept."""

import re
from collections.abc import Callable, Iterator
from datetime import timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import select

import domainkit.contrib.phones as phones_module
from domainkit import signing
from domainkit.conf import override_settings
from domainkit.contrib.auth.integration import utc_now
from domainkit.contrib.messaging.tables import messages
from domainkit.contrib.notifications.delivery import SMS_TASK
from domainkit.contrib.phones import (
    InvalidPhone,
    PhoneAlreadyVerified,
    PhonesSettings,
    PhonesUnitOfWork,
    confirm_code,
    normalize_phone,
)
from domainkit.contrib.phones.apps import PhonesConfig, forget_phones
from domainkit.contrib.phones.services import PURPOSE, code_digest
from domainkit.contrib.phones.tables import phone_codes, phones
from domainkit.db import connections
from domainkit.exceptions import ImproperlyConfigured
from domainkit.management.base import run_async
from domainkit.testing import freeze_time
from domainkit_tests.conftest import (
    IDENTITY,
    IDENTITY_DOMAINS,
    ExitStackClients,
    Migrated,
    Shape,
)
from domainkit_tests.support.auth import bearer, create_user, login

PHONES = Shape(
    "phones",
    "phones",
    {
        **IDENTITY.settings,
        "INSTALLED_DOMAINS": [
            *IDENTITY_DOMAINS,
            "domainkit.contrib.notifications",
            "domainkit.contrib.phones",
        ],
        "ROOT_ROUTER": "domainkit_tests.sample_phones_routes.router",
    },
)
NUMBER = "+33612345678"
HOUR = timedelta(hours=1)
type App = Callable[..., TestClient]


@pytest.fixture
def app(migrated: Migrated) -> Iterator[App]:
    """``app(**PHONES)``: a client of the project with phones installed."""
    with migrated(PHONES), ExitStackClients() as clients:

        def make(**phones: Any) -> TestClient:  # noqa: ANN401 - PHONES keys.
            clients.enter(override_settings(PHONES=phones))
            return clients.client(("testclient", 50000))

        yield make


def signed_in(client: TestClient, username: str = "ann") -> dict[str, str]:
    """``username`` made and logged in: the headers of their requests."""
    create_user(username)
    return bearer(login(client, username)["access_token"])


def texts() -> list[dict[str, Any]]:
    """The text messages queued in the outbox, oldest first."""

    async def read() -> list[dict[str, Any]]:
        async with connections.engine().connect() as connection:
            rows = await connection.execute(
                select(messages.c.payload)
                .where(messages.c.target == SMS_TASK)
                .order_by(messages.c.created_at)
            )
            return [row.payload["message"] for row in rows]

    return run_async(read())


async def stored_codes() -> list[Any]:
    """The rows of the code table."""
    async with connections.engine().connect() as connection:
        return list(await connection.execute(select(phone_codes)))


async def stored_phones() -> list[Any]:
    """The rows of the verified numbers' table."""
    async with connections.engine().connect() as connection:
        return list(await connection.execute(select(phones)))


def last_code() -> str:
    """The code in the last text message sent."""
    [code] = re.findall(r"\d{6,10}", str(texts()[-1]["body"]))
    return str(code)


def test_a_code_proves_a_number(app: App) -> None:
    """Asked for, sent through the outbox, typed back: the number is kept."""
    client = app()
    headers = signed_in(client)

    asked = client.post(
        "/auth/phones/codes", json={"phone": "+33 6 12-34.56 78"}, headers=headers
    )
    [text] = texts()
    verified = client.post(
        "/auth/phones",
        json={"phone": NUMBER, "code": f" {last_code()} "},
        headers=headers,
    )
    listed = client.get("/auth/phones", headers=headers)

    assert asked.status_code == 202
    assert asked.json()["phone"] == NUMBER
    assert text["to"] == NUMBER
    assert text["body"].startswith("Your verification code is ")
    assert verified.status_code == 201
    assert verified.json()["phone"] == NUMBER
    assert listed.json() == [verified.json()]
    again = client.post("/auth/phones/codes", json={"phone": NUMBER}, headers=headers)
    assert again.json()["code"] == "phone_already_verified"
    removed = client.delete(f"/auth/phones/{verified.json()['id']}", headers=headers)
    assert removed.status_code == 204
    assert client.get("/auth/phones", headers=headers).json() == []
    gone = client.delete(f"/auth/phones/{verified.json()['id']}", headers=headers)
    assert gone.json()["code"] == "phone_not_found"


def test_codes_are_tried_a_few_times_only(app: App) -> None:
    """A wrong code counts; after MAX_ATTEMPTS the code is spent, even if right."""
    client = app(MAX_ATTEMPTS=2)
    headers = signed_in(client)
    client.post("/auth/phones/codes", json={"phone": NUMBER}, headers=headers)
    right = last_code()
    wrong = "000000" if right != "000000" else "111111"

    answers = [
        client.post(
            "/auth/phones", json={"phone": NUMBER, "code": code}, headers=headers
        )
        for code in (wrong, wrong, right)
    ]

    assert [a.json()["code"] for a in answers] == [
        "wrong_code",
        "wrong_code",
        "code_expired",
    ]
    never = client.post(
        "/auth/phones", json={"phone": "+4915112345678", "code": right}, headers=headers
    )
    assert never.json()["code"] == "code_expired"


def test_codes_expire_and_sends_are_limited(app: App) -> None:
    """Old codes fail; a resend waits; a number and a user get MAX_SENDS a day."""
    client = app(MAX_SENDS=2, RESEND_AFTER=timedelta(minutes=1))
    headers = signed_in(client)
    with freeze_time("2026-01-01T09:00:00Z") as clock:
        client.post("/auth/phones/codes", json={"phone": NUMBER}, headers=headers)
        code = last_code()
        soon = client.post(
            "/auth/phones/codes", json={"phone": NUMBER}, headers=headers
        )
        clock.tick(timedelta(minutes=11))
        late = client.post(
            "/auth/phones", json={"phone": NUMBER, "code": code}, headers=headers
        )
        client.post("/auth/phones/codes", json={"phone": NUMBER}, headers=headers)
        clock.tick(timedelta(minutes=2))
        third = client.post(
            "/auth/phones/codes", json={"phone": NUMBER}, headers=headers
        )
        other = client.post(
            "/auth/phones/codes", json={"phone": "+4915112345678"}, headers=headers
        )
        clock.tick(timedelta(days=1))
        tomorrow = client.post(
            "/auth/phones/codes", json={"phone": NUMBER}, headers=headers
        )

    assert (soon.status_code, soon.json()["code"]) == (429, "rate_limited")
    assert soon.json()["detail"].startswith("A code was sent moments ago")
    assert soon.headers["retry-after"] == "60"
    assert late.json()["code"] == "code_expired"
    assert third.json()["code"] == "rate_limited"
    assert third.json()["detail"].startswith("Too many codes")
    # Until the first of the two leaves the day's window: 13 minutes in.
    assert third.headers["retry-after"] == str(24 * 3600 - 13 * 60)
    assert other.json()["code"] == "rate_limited"  # The user's sends count too.
    assert tomorrow.status_code == 202


def test_numbers_are_checked_and_capped(app: App) -> None:
    """A number not in international form is refused; MAX_PHONES are kept."""
    client = app(MAX_PHONES=1, RESEND_AFTER=timedelta(0))
    headers = signed_in(client)
    bad = client.post(
        "/auth/phones/codes", json={"phone": "0612345678"}, headers=headers
    )
    for number in (NUMBER, "+4915112345678"):
        client.post("/auth/phones/codes", json={"phone": number}, headers=headers)
        last = client.post(
            "/auth/phones",
            json={"phone": number, "code": last_code()},
            headers=headers,
        )

    assert bad.json()["code"] == "invalid_phone"
    assert last.json()["code"] == "too_many_phones"
    assert client.post("/auth/phones/codes", json={"phone": NUMBER}).status_code == 401


def test_the_code_is_kept_only_as_a_keyed_digest(app: App) -> None:
    """The table holds an HMAC, never the code; a deleted user's rows go."""
    client = app()
    headers = signed_in(client)
    client.post("/auth/phones/codes", json={"phone": NUMBER}, headers=headers)
    code = last_code()

    [row] = run_async(stored_codes())
    assert code not in row.digest
    assert len(row.digest) == 64
    assert "digest" not in repr(row)

    class Deleted:
        user_id: UUID = row.user_id

    run_async(forget_phones(Deleted()))
    assert run_async(stored_codes()) == []

    async def held() -> int:
        async with PhonesUnitOfWork() as uow:
            return await uow.phones.count(row.user_id)

    assert run_async(held()) == 0


def test_old_codes_are_forgotten_and_only_the_last_code_works(app: App) -> None:
    """A send forgets codes past the window; of two codes, the last one works."""
    client = app(RESEND_AFTER=timedelta(0))
    headers = signed_in(client)
    with freeze_time("2026-01-01T09:00:00Z") as clock:
        client.post("/auth/phones/codes", json={"phone": NUMBER}, headers=headers)
        clock.tick(timedelta(days=2))
        client.post(
            "/auth/phones/codes", json={"phone": "+4915112345678"}, headers=headers
        )
        kept = run_async(stored_codes())
        client.post("/auth/phones/codes", json={"phone": NUMBER}, headers=headers)
        first = last_code()
        client.post("/auth/phones/codes", json={"phone": NUMBER}, headers=headers)
        second = last_code()
        older = client.post(
            "/auth/phones", json={"phone": NUMBER, "code": first}, headers=headers
        )
        verified = client.post(
            "/auth/phones", json={"phone": NUMBER, "code": second}, headers=headers
        )

    assert [row.phone for row in kept] == ["+4915112345678"]
    if first != second:  # One chance in a million they are the same.
        assert older.json()["code"] == "wrong_code"
        assert verified.status_code == 201


def test_a_code_for_a_number_kept_already_is_refused(app: App) -> None:
    """Should a code outlive its number's verification, it verifies nothing."""
    client = app()
    headers = signed_in(client)
    client.post("/auth/phones/codes", json={"phone": NUMBER}, headers=headers)
    client.post(
        "/auth/phones", json={"phone": NUMBER, "code": last_code()}, headers=headers
    )
    [user_id] = {row.user_id for row in run_async(stored_phones())}

    async def again() -> None:
        async with PhonesUnitOfWork() as uow:
            now = utc_now()
            code_id = uuid4()
            digest = code_digest(code_id, "123456", signing.derive_key(PURPOSE))
            await uow.codes.add(
                code_id, user_id, NUMBER, digest, now=now, expires_at=now + HOUR
            )
            stored = await uow.codes.latest(user_id, NUMBER)
            await uow.commit()
        assert "digest" not in repr(stored)
        async with PhonesUnitOfWork() as uow:
            await confirm_code(uow, user_id, NUMBER, "123456")

    with pytest.raises(PhoneAlreadyVerified):
        run_async(again())


def test_phones_live_with_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    """Routed to another database than auth's, the app refuses to start."""
    from domainkit.db.routing import router

    monkeypatch.setattr(router, "target_for", lambda label: label)
    config = PhonesConfig("domainkit.contrib.phones", phones_module)
    with pytest.raises(ImproperlyConfigured, match="routed with 'auth'"):
        config.ready()


def test_numbers_and_settings_are_checked() -> None:
    """International form only; a message must hold the code once."""
    assert normalize_phone(" +1 (415) 555-0100 ") == "+14155550100"
    for text in ("0612345678", "+0612345678", "+1", "+33 6 12 34 56 78 90 12 34"):
        with pytest.raises(InvalidPhone):
            normalize_phone(text)
    for message in ("No code here", "{code} {code}", "{code} {other}"):
        with pytest.raises(ValidationError, match="holds"):
            PhonesSettings(MESSAGE=message)
    assert PhonesSettings(MESSAGE="Code: {code}").MESSAGE == "Code: {code}"
