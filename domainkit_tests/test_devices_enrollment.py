"""Enrollment: a user with no bound device binds the first one with no login."""

import json
import re
import time
from collections.abc import Iterator
from typing import Any
from uuid import UUID

import pytest
from fastapi.testclient import TestClient

from domainkit.clock import now
from domainkit.conf import override_settings
from domainkit.contrib import mail
from domainkit.contrib.notifications.backends import sms_outbox
from domainkit_tests.conftest import IDENTITY, MIDDLEWARE
from domainkit_tests.support.auth import PASSWORD, audited, bearer, create_user, login
from domainkit_tests.support.passwords import BREACHED_ON, LEAKED, pwned
from domainkit_tests.test_auth_login_origins import origins
from domainkit_tests.test_auth_mfa import clock, code_at, enrol
from domainkit_tests.test_auth_recovery import run_worker
from domainkit_tests.test_auth_reset_codes import LOCALE, drain
from domainkit_tests.test_ciba import PHONE, SITE, App, Phone, app
from domainkit_tests.test_devices import Device

CODE = re.compile(r"\b[0-9]{6}\b")
ON = {"ENROLLMENT": True}
NUMBER = "+33612345678"
AUTH = {
    **IDENTITY.settings["AUTH"],
    "IDENTIFIERS": ("username", "email", "mobile_number"),
}
TEXTS = {"SMS_BACKEND": "domainkit.contrib.notifications.backends.MemorySmsBackend"}

__all__ = ["app", "clock"]  # The fixtures the tests take.


def ask_code(client: TestClient, identifier: str) -> Any:  # noqa: ANN401 - a response.
    """``POST /auth/devices/enroll/code``: the response."""
    return client.post("/auth/devices/enroll/code", json={"identifier": identifier})


def mailed_code() -> str:
    """The code in the last mail sent, once the worker ran."""
    run_worker()
    found = CODE.search(mail.outbox[-1].body)
    assert found is not None
    return found[0]


def enroll(
    client: TestClient,
    device: Device,
    *,
    identifier: str = "alice@example.com",
    code: str,
    password: str = PASSWORD,
    signer: Device | None = None,
    when: int | None = None,
    **more: str,
) -> Any:  # noqa: ANN401 - a response.
    """``POST /auth/devices/enroll`` for ``device``, signed by ``signer`` (it).

    ``more`` adds ``otp`` or ``new_password``.
    """
    body = json.dumps(
        {
            "identifier": identifier,
            "code": code,
            "password": password,
            "name": "Phone",
            "public_key": device.public(),
            **more,
        }
    ).encode()
    moment = when if when is not None else int(now().timestamp() * 1000)
    headers = (signer or device).headers(
        "POST", "/auth/devices/enroll", body, when=moment
    )
    return client.post(
        "/auth/devices/enroll",
        content=body,
        headers={**headers, "Content-Type": "application/json"},
    )


@pytest.fixture(autouse=True)
def _identifiers(app: App) -> Iterator[None]:
    """Users name themselves by username, email address, or mobile number.

    Takes ``app`` so that it sets these over the project's settings.
    """
    mail.outbox.clear()
    sms_outbox.clear()
    with override_settings(AUTH=AUTH, NOTIFICATIONS=TEXTS):
        yield


def test_a_first_device_enrolls_and_logs_in(app: App) -> None:
    """Code by mail, password, signed by the key: bound, tokens, listed."""
    client = app(PHONE)
    alice = create_user("alice", email="alice@example.com", proved=True)
    with override_settings(DEVICES=ON):
        answer = ask_code(client, "alice@example.com")
        assert answer.status_code == 202
        code = mailed_code()
        assert mail.outbox[-1].subject == "Your code to enroll your phone"
        device = Device()
        enrolled = enroll(client, device, code=code)
        assert enrolled.status_code == 201, enrolled.json()
        assert enrolled.headers["Cache-Control"] == "no-store"
        out = enrolled.json()
        assert out["device"]["name"] == "Phone"
        headers = bearer(out["access_token"])
        assert client.get("/auth/me", headers=headers).json()["id"] == str(alice)
        listed = client.get("/auth/devices", headers=headers).json()
        assert [each["id"] for each in listed] == [out["device"]["id"]]
        assert origins() == [("device", UUID(out["device"]["id"]))]
        assert len(audited("devices.device_bound")) == 1

        again = enroll(client, Device(), code=code)
        assert again.status_code == 401  # The code worked once.
        assert ask_code(client, "alice@example.com").status_code == 202
        run_worker()
        assert [each.subject for each in mail.outbox] == [
            "Your code to enroll your phone",
            "A device was bound to your account",  # And no code for another.
        ]
        assert "bound to your account: Phone." in mail.outbox[-1].body


def test_an_enrolled_phone_passes_the_strictest_gate(app: App) -> None:
    """With APPROVE_LOGINS, the enrollment's own login is the device's."""
    client = app(PHONE, APPROVE_LOGINS=True)
    create_user("alice", email="alice@example.com", proved=True)
    with override_settings(DEVICES=ON):
        ask_code(client, "alice@example.com")
        enrolled = enroll(client, Device(), code=mailed_code())
    assert enrolled.status_code == 201, enrolled.json()


def test_codes_go_by_text_to_a_number(app: App) -> None:
    """A mobile number of the account gets the code by text message."""
    client = app(PHONE)
    create_user("hal", mobile_number=NUMBER, proved=True)
    with override_settings(DEVICES=ON):
        ask_code(client, NUMBER)
        drain()
    [text] = sms_outbox
    assert text.to == NUMBER
    assert "is your code to enroll your phone." in text.body
    assert mail.outbox == []


def test_codes_go_in_the_language_of_the_request(app: App) -> None:
    """A page asking in Arabic gets its code texted in Arabic (user report A11)."""
    with override_settings(DEVICES=ON, MIDDLEWARE=[LOCALE, *MIDDLEWARE]):
        client = app(PHONE)
        create_user("hal", mobile_number=NUMBER, proved=True)
        answer = client.post(
            "/auth/devices/enroll/code",
            json={"identifier": NUMBER},
            headers={"Accept-Language": "ar"},
        )
        assert answer.status_code == 202, answer.json()
        drain()
    [text] = sms_outbox
    assert "هو رمزك لتسجيل هاتفك." in text.body


def test_the_mails_are_written_in_arabic(app: App) -> None:
    """The enrollment code and the notice of the bound device (user report I7)."""
    client = app(PHONE)
    create_user("alice", email="alice@example.com", proved=True)
    with override_settings(DEVICES=ON, LANGUAGE_CODE="ar"):
        ask_code(client, "alice@example.com")
        enrolled = enroll(client, Device(), code=mailed_code())
        assert enrolled.status_code == 201, enrolled.json()
        run_worker()

    coded, bound = mail.outbox
    assert coded.subject == "رمز تسجيل هاتفك"
    assert coded.body.startswith("مرحبًا alice،\n\nرمز تسجيل هاتفك هو:")
    assert "يعمل لمدة " in coded.body
    assert bound.subject == "رُبط جهاز بحسابك"
    assert "رُبط جهاز بحسابك: Phone." in bound.body


def test_enrollment_codes_go_only_to_a_proved_number(app: App) -> None:
    """A number its user never proved gets no enrollment code either."""
    client = app(PHONE)
    create_user(
        "mallory",
        email="mallory@example.com",
        email_verified=True,
        mobile_number=NUMBER,
    )
    with override_settings(DEVICES=ON):
        ask_code(client, NUMBER)
        drain()
    assert sms_outbox == []


@pytest.mark.parametrize("who", ["nobody", "inactive", "passwordless", "unmailable"])
def test_accounts_that_cannot_enroll_get_no_code(app: App, who: str) -> None:
    """The same 202, and nothing sent."""
    client = app(PHONE)
    if who == "inactive":
        create_user("bob", email="bob@example.com", is_active=False)
    if who == "passwordless":
        create_user("bob", None, email="bob@example.com")
    if who == "unmailable":
        create_user("bob@example.com")
    with override_settings(DEVICES=ON):
        assert ask_code(client, "bob@example.com").status_code == 202
        drain()
    assert mail.outbox == []
    assert sms_outbox == []


def test_enrollment_needs_the_code_the_password_and_the_key(app: App) -> None:
    """Each missing proof is one refusal: never which, and audited."""
    client = app(PHONE)
    create_user("alice", email="alice@example.com", proved=True)
    with override_settings(DEVICES=ON):
        ask_code(client, "alice@example.com")
        code = mailed_code()
        device = Device()
        stale = int(time.time() * 1000) - 10 * 60 * 1000
        refused = [
            enroll(client, device, code="000000" if code != "000000" else "111111"),
            enroll(client, device, code=code, password="wrong-password"),
            enroll(client, device, code=code, signer=Device()),
            enroll(client, device, code=code, when=stale),
            enroll(client, device, identifier="bob@example.com", code=code),
        ]
        for answer in refused:
            assert answer.status_code == 401
            assert answer.json()["code"] == "enrollment_refused"
        assert len(audited("devices.enroll")) == len(refused)
        unsigned = client.post(
            "/auth/devices/enroll",
            json={
                "identifier": "alice@example.com",
                "code": code,
                "password": PASSWORD,
                "name": "Phone",
                "public_key": device.public(),
            },
            headers={
                "X-Device-Time": str(int(time.time() * 1000)),
                "X-Device-Signature": "%" * 300,
            },
        )
        assert unsigned.status_code == 401


def test_a_second_factor_is_needed_too(app: App, clock: list[float]) -> None:
    """Asked for before the code is spent; a wrong one refuses, a right one binds."""
    client = app(PHONE)
    create_user("alice", email="alice@example.com", proved=True)
    secret, _ = enrol(client, bearer(login(client, "alice")["access_token"]), clock)
    with override_settings(DEVICES=ON):
        ask_code(client, "alice@example.com")
        code = mailed_code()
        asked = enroll(client, Device(), code=code)
        wrong = enroll(client, Device(), code=code, otp="000000")
        ask_code(client, "alice@example.com")
        code = mailed_code()
        clock[0] += 30
        bound = enroll(client, Device(), code=code, otp=code_at(secret, clock[0]))
    assert (asked.status_code, asked.json()["code"]) == (401, "second_factor_required")
    assert (wrong.status_code, wrong.json()["code"]) == (401, "enrollment_refused")
    assert bound.status_code == 201, bound.json()


def test_a_password_staff_set_is_changed_first(app: App) -> None:
    """``AUTH.PASSWORD_CHANGE_ON_FIRST_USE``: 403 until a new one comes along.

    The code is not spent by the refusal, nor by a new password a rule
    refuses: the user sends again with one, and needs no second message.
    """
    from domainkit.contrib.auth import AuthUnitOfWork
    from domainkit.contrib.auth.models import UserId
    from domainkit.management.base import run_async

    client = app(PHONE)
    alice = create_user("alice", email="alice@example.com", proved=True)
    staff_set = "Kq7-zv93-Lmw2"
    chosen = "Pw4-tt81-Xcv9"

    async def set_by_staff() -> None:
        async with AuthUnitOfWork() as uow:
            stored = await uow.users.get(UserId(alice))
            await uow.users.set_password(stored, staff_set, temporary=True)
            await uow.commit()

    first_use = {**AUTH, "PASSWORD_CHANGE_ON_FIRST_USE": True}
    with override_settings(DEVICES=ON, AUTH=first_use):
        run_async(set_by_staff())
        ask_code(client, "alice@example.com")
        code = mailed_code()
        phone = Device()
        asked = enroll(client, phone, code=code, password=staff_set)
        weak = enroll(client, phone, code=code, password=staff_set, new_password="1")
        changed = enroll(
            client, phone, code=code, password=staff_set, new_password=chosen
        )
        relogged = client.post(
            "/auth/login", json={"username": "alice", "password": chosen}
        )
    assert (asked.status_code, asked.json()["code"]) == (
        403,
        "password_change_required",
    )
    assert (weak.status_code, weak.json()["code"]) == (422, "invalid_password")
    # Neither refusal spent the code: one message is enough.
    assert changed.status_code == 201, changed.json()
    assert relogged.status_code == 200, relogged.json()


def test_a_breached_new_password_is_refused_before_the_code(app: App) -> None:
    """The breached-password check runs where the new password is checked."""
    from domainkit.contrib.auth import AuthUnitOfWork
    from domainkit.contrib.auth.models import UserId
    from domainkit.management.base import run_async
    from domainkit.outbound.client import stub

    client = app(PHONE)
    alice = create_user("alice", email="alice@example.com", proved=True)
    staff_set = "Kq7-zv93-Lmw2"

    async def set_by_staff() -> None:
        async with AuthUnitOfWork() as uow:
            stored = await uow.users.get(UserId(alice))
            await uow.users.set_password(stored, staff_set, temporary=True)
            await uow.commit()

    first_use = {**AUTH, **BREACHED_ON, "PASSWORD_CHANGE_ON_FIRST_USE": True}
    with override_settings(DEVICES=ON, AUTH=first_use), stub(pwned(LEAKED)):
        run_async(set_by_staff())
        ask_code(client, "alice@example.com")
        code = mailed_code()
        phone = Device()
        leaked = enroll(
            client, phone, code=code, password=staff_set, new_password=LEAKED
        )
        changed = enroll(
            client, phone, code=code, password=staff_set, new_password="Pw4-tt81-Xcv9"
        )
    assert (leaked.status_code, leaked.json()["code"]) == (422, "invalid_password")
    assert changed.status_code == 201, changed.json()


def test_a_user_with_a_bound_device_enrolls_no_other(app: App) -> None:
    """A code that slipped through still binds nothing: approve from the phone."""
    from domainkit.contrib.auth import AuthUnitOfWork
    from domainkit.contrib.auth.integration import target_of
    from domainkit.contrib.devices.enrollment import ENROLLMENT
    from domainkit.management.base import run_async

    client = app(PHONE)
    phone = Phone(client)  # alice, bound.

    async def issue() -> str:
        async with AuthUnitOfWork() as uow:
            code = await uow.verification_codes.issue(
                ENROLLMENT, target_of("alice"), user_id=phone.user_id
            )
            await uow.commit()
        return code

    code = run_async(issue())
    with override_settings(DEVICES=ON):
        answer = enroll(client, Device(), identifier="alice", code=code)
    assert answer.status_code == 409
    assert answer.json()["code"] == "device_already_bound"


def test_enrollment_is_off_unless_turned_on(app: App) -> None:
    """``DEVICES.ENROLLMENT`` defaults to off: 404 for both routes."""
    client = app(PHONE)
    assert ask_code(client, "alice@example.com").json()["code"] == "enrollment_off"
    off = enroll(client, Device(), code="123456")
    assert off.status_code == 404
    assert off.json()["code"] == "enrollment_off"


def test_enrollment_attempts_are_throttled(app: App) -> None:
    """Refused attempts count against AUTH.LOGIN_RATE: then 429."""
    client = app(PHONE)
    create_user("alice", email="alice@example.com", proved=True)
    auth = {**AUTH, "LOGIN_RATE": "2/minute"}
    with override_settings(DEVICES=ON, AUTH=auth):
        for _ in range(2):
            assert enroll(client, Device(), code="000000").status_code == 401
        throttled = enroll(client, Device(), code="000000")
        assert throttled.status_code == 429
        assert ask_code(client, "alice@example.com").status_code == 202
        assert ask_code(client, "alice@example.com").status_code == 202
        assert ask_code(client, "alice@example.com").status_code == 429


def test_enrollment_codes_are_counted_per_recipient(app: App) -> None:
    """Callers from many addresses cannot text one number without end (A1).

    The ways of writing one number share its count (``AUTH.EMAIL_RATE``);
    another recipient still gets its code.
    """
    create_user("alice", email="alice@example.com", mobile_number=NUMBER, proved=True)
    project = app(PHONE).app
    callers = [
        TestClient(project, base_url=SITE, client=(f"198.51.100.{n}", 50000 + n))
        for n in range(1, 4)
    ]
    auth = {**AUTH, "EMAIL_RATE": "2/hour"}
    with override_settings(DEVICES=ON, AUTH=auth):
        first = ask_code(callers[0], NUMBER)
        second = ask_code(callers[1], "+33 6 12 34 56 78")
        third = ask_code(callers[2], NUMBER)
        other = ask_code(callers[2], "bob@example.com")

    assert (first.status_code, second.status_code) == (202, 202)
    assert third.status_code == 429
    assert third.json()["code"] == "rate_limited"
    assert other.status_code == 202


def test_enrollment_needs_messaging(app: App, monkeypatch: pytest.MonkeyPatch) -> None:
    """Without the worker that sends codes, the app refuses to start."""
    from domainkit.apps import registry
    from domainkit.exceptions import ImproperlyConfigured

    app()
    config = registry.get_domain_config("devices")
    installed = registry.is_installed
    monkeypatch.setattr(
        registry,
        "is_installed",
        lambda name: name != "domainkit.contrib.messaging" and installed(name),
    )
    with (
        override_settings(DEVICES=ON),
        pytest.raises(
            ImproperlyConfigured, match=r"needs 'domainkit\.contrib\.messaging'"
        ),
    ):
        config.ready()
