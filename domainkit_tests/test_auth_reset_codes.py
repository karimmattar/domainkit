"""Password resets and mobile numbers verified by code (ADR 0126)."""

import logging
import re
from collections.abc import Callable, Iterator
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from domainkit.conf import override_settings
from domainkit.contrib import mail
from domainkit.contrib.auth import AuthUnitOfWork, User, UserId
from domainkit.contrib.auth.recovery import send_mobile_verification
from domainkit.contrib.notifications.backends import sms_outbox
from domainkit.http import get_asgi_application
from domainkit.http.application import application_problems
from domainkit.management.base import run_async
from domainkit_tests.conftest import (
    IDENTITY,
    IDENTITY_DOMAINS,
    Api,
    ExitStackClients,
    Migrated,
    Shape,
)
from domainkit_tests.test_auth_http import (
    PASSWORD,
    audited,
    bearer,
    create_user,
    login,
)
from domainkit_tests.test_auth_recovery import run_worker

NEW_PASSWORD = "an0ther-Strong-pass!"
NUMBER = "+33612345678"
CODES = {"IDENTIFIERS": ("email", "mobile_number")}
MAILED = {"IDENTIFIERS": ("email",)}
TEXTS = Shape(
    "texts",
    "texts",
    {
        **IDENTITY.settings,
        "INSTALLED_DOMAINS": [*IDENTITY_DOMAINS, "domainkit.contrib.notifications"],
        "NOTIFICATIONS": {
            "SMS_BACKEND": "domainkit.contrib.notifications.backends.MemorySmsBackend"
        },
    },
)
CODE = re.compile(r"\b[0-9]{6}\b")
WRONG = (400, "invalid_verification_code")

type App = Callable[..., TestClient]


@pytest.fixture
def texting(migrated: Migrated) -> Iterator[App]:
    """``texting(**AUTH)``: a client of the project with notifications too."""
    sms_outbox.clear()
    mail.outbox.clear()
    auth = TEXTS.settings["AUTH"]
    with migrated(TEXTS), ExitStackClients() as clients:

        def make(**options: Any) -> TestClient:  # noqa: ANN401 - AUTH keys.
            clients.enter(override_settings(AUTH={**auth, **options}))
            return clients.client(("testclient", 50000))

        yield make


def drain() -> None:
    """Run the worker until nothing is due: tasks, then the texts they queue."""
    while run_worker():
        pass


def code_in(text: str) -> str:
    """The 6 digits in ``text``."""
    found = CODE.search(text)
    assert found is not None, text
    return found[0]


def ask_reset(client: TestClient, **body: str) -> dict[str, Any]:
    """Ask for a reset; return the 202 body."""
    response = client.post("/auth/password-reset", json=body)
    assert response.status_code == 202, response.json()
    answer: dict[str, Any] = response.json()
    return answer


def reset(
    client: TestClient, identifier: str, code: str, password: str = NEW_PASSWORD
) -> tuple[int, Any]:
    """The status and problem code of resetting with ``code``."""
    response = client.post(
        "/auth/password-reset/code",
        json={"identifier": identifier, "code": code, "new_password": password},
    )
    problem = response.json().get("code") if response.content else None
    return response.status_code, problem


def logs_in(client: TestClient, identifier: str, password: str) -> bool:
    """Whether ``identifier`` and ``password`` log in."""
    response = client.post(
        "/auth/login", json={"identifier": identifier, "password": password}
    )
    return response.status_code == 200


def other(code: str) -> str:
    """A code that is not ``code``."""
    return "000000" if code != "000000" else "111111"


def test_a_mailed_code_sets_a_new_password(identity: Api) -> None:
    """A weak password keeps the code; a strong one spends it and ends sessions."""
    client = identity(**MAILED)
    ann = create_user(None, email="ann@example.com")
    old = bearer(login(client, "ann@example.com")["access_token"])

    answer = ask_reset(client, identifier="Ann@Example.com")
    assert answer == {"detail": "If an account uses it, a code is on its way."}
    run_worker()
    (message,) = mail.outbox
    assert (message.subject, message.to) == (
        "Your code to reset your password",
        ("ann@example.com",),
    )
    code = code_in(message.body)

    assert reset(client, "ann@example.com", other(code)) == WRONG
    assert reset(client, "ann@example.com", code, "short") == (422, "invalid_password")
    assert reset(client, "ANN@example.com", code) == (204, None)
    assert reset(client, "ann@example.com", code) == WRONG
    assert logs_in(client, "ann@example.com", NEW_PASSWORD)
    assert not logs_in(client, "ann@example.com", PASSWORD)
    assert client.get("/auth/me", headers=old).status_code == 401
    (event,) = audited("auth.password_reset_requested")
    assert event["details"] == {"user_id": str(ann)}
    refusals = audited("auth.password_reset")
    assert {"reason": "invalid_verification_code"} in [r["details"] for r in refusals]


def test_the_email_field_asks_for_a_code_too(identity: Api) -> None:
    """``{"email": ...}``, as before 1.8, gets a code where codes are on."""
    create_user("bob", email="bob@example.com")
    client = identity()

    ask_reset(client, email="bob@example.com")
    run_worker()
    code = code_in(mail.outbox[0].body)
    assert reset(client, "bob@example.com", code) == (204, None)


def test_an_unknown_identifier_gets_the_same_answer_and_nothing(identity: Api) -> None:
    """No account, an inactive one, or one without a password: nothing is sent."""
    create_user("carol", email="carol@example.com", is_active=False)
    create_user("dave", None, email="dave@example.com")
    client = identity(**MAILED)

    for identifier in ("nobody@example.com", "carol@example.com", "dave@example.com"):
        answer = ask_reset(client, identifier=identifier)
        assert answer == {"detail": "If an account uses it, a code is on its way."}
    run_worker()
    assert mail.outbox == []
    assert reset(client, "nobody@example.com", "123456") == WRONG


def test_a_reset_needs_a_value(identity: Api) -> None:
    """An identifier, or an email address: one value names the account."""
    client = identity()

    response = client.post("/auth/password-reset", json={})
    assert (response.status_code, response.json()["code"]) == (422, "missing_value")


def test_a_texted_code_sets_a_new_password(texting: App) -> None:
    """A number gets a text message; it may be written any way."""
    client = texting(**CODES)
    create_user(None, mobile_number=NUMBER, email="erin@example.com")

    ask_reset(client, identifier="+33 6 12 34 56 78")
    drain()
    (text,) = sms_outbox
    assert text.to == NUMBER
    assert "is your code to reset your password. It works for 10 minutes." in text.body
    assert mail.outbox == []
    code = code_in(text.body)

    assert reset(client, "+33 (6) 12-34-56-78", code) == (204, None)
    assert logs_in(client, NUMBER, NEW_PASSWORD)


def test_a_username_gets_its_code_by_mail(identity: Api) -> None:
    """An identifier that is neither an address nor a number: the address."""
    create_user("frank", email="frank@example.com")
    client = identity(IDENTIFIERS=("username", "email"))

    ask_reset(client, identifier="frank")
    run_worker()
    code = code_in(mail.outbox[0].body)
    assert reset(client, "frank", code) == (204, None)


def test_a_username_without_an_address_gets_nothing(identity: Api) -> None:
    """Nowhere to send the code: nothing is sent, nothing is kept."""
    create_user("jo")
    client = identity(IDENTIFIERS=("username",))

    ask_reset(client, identifier="jo")
    run_worker()
    assert mail.outbox == []
    run_async(send_mobile_verification(uuid4()))  # A user who is gone.


def test_a_number_without_notifications_gets_nothing(
    identity: Api, caplog: pytest.LogCaptureFixture
) -> None:
    """Texts need notifications; a warning names the user."""
    gina = create_user("gina", mobile_number=NUMBER)
    client = identity()
    headers = bearer(login(client, "gina")["access_token"])

    with caplog.at_level(logging.WARNING):
        client.post("/auth/mobile-verification", headers=headers)
        run_worker()
    assert f"User {gina} has a code to text" in caplog.text


def mobile_verified(user_id: UUID) -> bool:
    """Whether the stored user's number is verified."""

    async def read() -> bool:
        async with AuthUnitOfWork() as uow:
            user = await uow.users.get(UserId(user_id))
        return bool(cast(User, user).mobile_number_verified)

    return run_async(read())


def confirm_number(
    client: TestClient, headers: dict[str, str], code: str
) -> tuple[int, Any]:
    """The status and problem code of confirming a number with ``code``."""
    response = client.post(
        "/auth/mobile-verification/code", json={"code": code}, headers=headers
    )
    problem = response.json().get("code") if response.content else None
    return response.status_code, problem


def test_a_texted_code_verifies_the_number_once(texting: App) -> None:
    """Asked for, texted, typed back: the number is verified, once."""
    hal = create_user("hal", mobile_number=NUMBER)
    client = texting()
    headers = bearer(login(client, "hal")["access_token"])

    response = client.post("/auth/mobile-verification", headers=headers)
    assert response.json() == {"detail": "A code to verify your number is on its way."}
    drain()
    (text,) = sms_outbox
    code = code_in(text.body)
    assert "is your code to verify your number." in text.body

    assert confirm_number(client, headers, other(code)) == WRONG
    assert not mobile_verified(hal)
    assert confirm_number(client, headers, code) == (204, None)
    assert mobile_verified(hal)
    assert confirm_number(client, headers, code) == WRONG
    (event,) = audited("auth.mobile_number_verified")
    assert event["details"] == {"user_id": str(hal)}

    client.post("/auth/mobile-verification", headers=headers)
    drain()
    assert len(sms_outbox) == 1  # Verified already: nothing more is sent.


def test_a_user_without_a_number_gets_no_code(texting: App) -> None:
    """Nothing is texted, and any code is refused."""
    create_user("ian")
    client = texting()
    headers = bearer(login(client, "ian")["access_token"])

    client.post("/auth/mobile-verification", headers=headers)
    drain()
    assert list(sms_outbox) == []
    assert confirm_number(client, headers, "123456") == WRONG
    assert client.post("/auth/mobile-verification").status_code == 401


def test_texted_codes_need_notifications(identity: Api) -> None:
    """``auth.E107``: routes that text codes, without notifications."""
    identity()
    with override_settings(AUTH={**IDENTITY.settings["AUTH"], **CODES}):
        problems = application_problems(get_asgi_application())
    with override_settings(AUTH={**IDENTITY.settings["AUTH"], **MAILED}):
        mailed = application_problems(get_asgi_application())

    texts = (
        "send codes to mobile numbers by text message: add "
        "domainkit.contrib.notifications to INSTALLED_DOMAINS."
    )
    assert problems == [
        f"Routes of domainkit.contrib.auth.recovery {texts}",
        f"Routes of domainkit.contrib.auth.signup {texts}",
    ]
    assert mailed == []
