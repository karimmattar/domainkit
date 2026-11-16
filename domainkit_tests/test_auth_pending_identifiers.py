"""A new address or number is held apart until its code proves it (ADR 0153).

The report (A2): someone who changes an account's number to a victim's
made the service text the victim, and "a victim who uses a reset code sent
to them sets the password of, and proves their number on, the other
person's account". 2.4 sends account codes to proved identifiers only
(ADR 0141); the open part: "hold a new identifier apart ("pending") until
its code proves it, instead of storing it on the account unproved".

``POST /email-verification`` and ``/mobile-verification`` take the new
address or number: it is kept in ``domainkit_pending_identifiers``, the code
goes to it, and the account keeps the one it has, still logging in and
receiving codes, until the code comes back. Then they swap, and
``EmailChanged`` or ``MobileNumberChanged`` is recorded. Staff still set one
at once, unproved, through the admin API.
"""

import re
from datetime import timedelta
from typing import Any, cast
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from domainkit import clock
from domainkit.clock import freeze_time
from domainkit.conf import override_settings
from domainkit.contrib import mail
from domainkit.contrib.auth import AuthUnitOfWork, PasswordHash, User, UserId
from domainkit.contrib.auth.codes import PASSWORD_RESET, target_of
from domainkit.contrib.auth.events import (
    EmailVerified,
    MobileNumberVerified,
    UserUpdated,
)
from domainkit.contrib.auth.integration import EmailChanged, MobileNumberChanged
from domainkit.contrib.auth.models import AbstractUser
from domainkit.contrib.auth.repositories import PendingIdentifier
from domainkit.contrib.auth.tables import auth_codes, pending_identifiers
from domainkit.contrib.notifications.backends import sms_outbox
from domainkit.db import connections
from domainkit.management.base import run_async
from domainkit_tests.conftest import MIDDLEWARE, Api
from domainkit_tests.support.auth import PASSWORD, audited, bearer, create_user
from domainkit_tests.test_auth_admin import ADMIN, staff
from domainkit_tests.test_auth_recovery import run_worker
from domainkit_tests.test_auth_reset_codes import ARABIC, LOCALE, App, drain, texting
from domainkit_tests.test_ciba import PHONE, Phone, app
from domainkit_tests.test_ciba_logins import refused, web_login

__all__ = ["app", "texting"]  # The fixtures the tests take.

OLD = "ann@example.com"
NEW = "ann@new.example"
NUMBER = "+33612345678"
NEW_NUMBER = "+33698765432"
WRONG = (400, "invalid_verification_code")
BY_EMAIL = {"IDENTIFIERS": ("username", "email")}
BY_NUMBER = {"IDENTIFIERS": ("username", "mobile_number")}


# The model -------------------------------------------------------------------------


def ann(**fields: object) -> User:
    """A user with an address and a number, their creation event taken."""
    user = User.create(
        password=PasswordHash.unusable(),
        username="ann",
        **{"email": OLD, "mobile_number": NUMBER, **fields},
    )
    user.pull_events()
    return user


def test_a_proved_address_replaces_the_old_one_and_names_it() -> None:
    """``change_email``: the new address, proved; the old one in ``EmailChanged``."""
    user = ann(email_verified=True)

    user.change_email("ann@NEW.example")

    updated, changed, verified = user.pull_events()
    assert (user.email, user.email_verified) == (NEW, True)
    assert isinstance(updated, UserUpdated)
    assert updated.fields == ("email",)
    assert isinstance(changed, EmailChanged)
    assert (changed.previous, changed.previous_verified) == (OLD, True)
    assert isinstance(verified, EmailVerified)
    lone = ann(email="")
    lone.change_email(OLD)
    assert [e.previous for e in lone.pull_events() if isinstance(e, EmailChanged)] == [
        None
    ]
    user.change_email(NEW)  # The address it has: nothing changes.
    assert user.pull_events() == []


def test_a_proved_number_replaces_the_old_one_and_names_it() -> None:
    """``change_mobile_number``: the same; the number it has is only proved."""
    user = ann()

    user.change_mobile_number("+33 6 98 76 54 32")

    updated, changed, verified = user.pull_events()
    assert (user.mobile_number, user.mobile_number_verified) == (NEW_NUMBER, True)
    assert isinstance(updated, UserUpdated)
    assert updated.fields == ("mobile_number",)
    assert isinstance(changed, MobileNumberChanged)
    assert (changed.previous, changed.previous_verified) == (NUMBER, False)
    assert isinstance(verified, MobileNumberVerified)
    same = ann()
    same.change_mobile_number(NUMBER)
    assert [type(event) for event in same.pull_events()] == [MobileNumberVerified]


# Helpers ---------------------------------------------------------------------------


def log_in(client: TestClient, identifier: str) -> dict[str, str]:
    """Log ``identifier`` in; its bearer header."""
    answer = client.post(
        "/auth/login", json={"identifier": identifier, "password": PASSWORD}
    )
    assert answer.status_code == 200, answer.json()
    return bearer(answer.json()["access_token"])


def logs_in(client: TestClient, identifier: str) -> bool:
    """Whether ``identifier`` and the password log in."""
    answer = client.post(
        "/auth/login", json={"identifier": identifier, "password": PASSWORD}
    )
    return answer.status_code == 200


def ask(client: TestClient, headers: dict[str, str], **body: str) -> Any:  # noqa: ANN401
    """Ask for an email code, for ``body``'s address if given; the response."""
    if body:
        return client.post("/auth/email-verification", json=body, headers=headers)
    return client.post("/auth/email-verification", headers=headers)


def confirm(
    client: TestClient, headers: dict[str, str], code: str, kind: str = "email"
) -> tuple[int, Any]:
    """The status and problem code of confirming ``code``."""
    response = client.post(
        f"/auth/{kind}-verification/code", json={"code": code}, headers=headers
    )
    problem = response.json().get("code") if response.content else None
    return response.status_code, problem


def code_mailed_to(address: str) -> str:
    """The code in the last mail sent to ``address``."""
    (last, *_) = [m for m in reversed(mail.outbox) if m.to == (address,)]
    found = re.search(r"\b[0-9]{6}\b", last.body)
    assert found is not None, last.body
    return found[0]


def code_texted_to(number: str) -> str:
    """The code in the last text sent to ``number``."""
    (last, *_) = [m for m in reversed(sms_outbox) if m.to == number]
    found = re.search(r"\b[0-9]{6}\b", last.body)
    assert found is not None, last.body
    return found[0]


def stored(user_id: UUID) -> User:
    """The user ``user_id``, as stored."""

    async def read() -> User:
        async with AuthUnitOfWork() as uow:
            return cast(User, await uow.users.get(UserId(user_id)))

    return run_async(read())


def rows(table: Any) -> list[dict[str, Any]]:  # noqa: ANN401 - a table.
    """Every row of ``table``."""

    async def read() -> list[dict[str, Any]]:
        async with connections.engine().connect() as connection:
            return [
                dict(row._mapping) for row in await connection.execute(select(table))
            ]

    return run_async(read())


def other(code: str) -> str:
    """A code that is not ``code``."""
    return "000000" if code != "000000" else "111111"


# Email addresses -------------------------------------------------------------------


def test_a_new_address_is_held_and_the_old_one_stays_in_force(identity: Api) -> None:
    """Until the code comes back, the proved address logs in and gets codes."""
    alice = create_user("alice", email=OLD, proved=True)
    client = identity(**BY_EMAIL)
    headers = log_in(client, "alice")

    answer = ask(client, headers, email="ann@NEW.example")
    assert answer.status_code == 202, answer.json()
    assert answer.json() == {"detail": "A code to verify your address is on its way."}
    run_worker()

    assert [message.to for message in mail.outbox] == [(NEW,)]
    code = code_mailed_to(NEW)
    (held,) = rows(pending_identifiers)
    assert (held["user_id"], held["field"], held["value"]) == (alice, "email", NEW)
    user = stored(alice)
    assert (user.email, user.email_verified) == (OLD, True)
    assert logs_in(client, OLD)
    assert not logs_in(client, NEW)
    client.post("/auth/password-reset", json={"identifier": NEW})
    client.post("/auth/password-reset", json={"identifier": OLD})
    run_worker()
    assert [message.to for message in mail.outbox[1:]] == [(OLD,)]
    reset_code = code_mailed_to(OLD)
    assert audited("auth.email_changed") == []

    assert confirm(client, headers, other(code)) == WRONG
    assert confirm(client, headers, code) == (204, None)

    user = stored(alice)
    assert (user.email, user.email_verified) == (NEW, True)
    assert logs_in(client, NEW)
    assert not logs_in(client, OLD)
    (changed,) = audited("auth.email_changed")
    assert changed["actor"] == str(alice)
    assert changed["details"]["previous_verified"] is True
    assert len(audited("auth.email_verified")) == 1
    assert rows(pending_identifiers) == []
    late_reset = client.post(
        "/auth/password-reset/code",
        json={"identifier": OLD, "code": reset_code, "new_password": "an0ther-Pass!"},
    )
    assert late_reset.status_code == 400  # The replaced address opens nothing.
    assert confirm(client, headers, code) == WRONG


def test_a_held_address_gets_its_code_in_the_requests_language(identity: Api) -> None:
    """The code for a held address is written as any code is (ADR 0149)."""
    create_user("alice", email=OLD, proved=True)
    with override_settings(MIDDLEWARE=[LOCALE, *MIDDLEWARE]):
        client = identity()
        headers = log_in(client, "alice")
        assert ask(client, {**headers, **ARABIC}, email=NEW).status_code == 202
        run_worker()

    (message,) = mail.outbox
    assert (message.to, message.subject) == ((NEW,), "رمز تأكيد عنوان بريدك الإلكتروني")


def test_a_held_address_lasts_as_long_as_its_code(identity: Api) -> None:
    """A code asked for again keeps it; once the last code expired, it is gone."""
    alice = create_user("alice", email=OLD, proved=True)
    client = identity()
    headers = log_in(client, "alice")

    with freeze_time(clock.now()) as frozen:
        ask(client, headers, email=NEW)
        run_worker()
        frozen.tick(timedelta(minutes=6))
        assert ask(client, headers).status_code == 202  # Again, to the held one.
        run_worker()
        assert [message.to for message in mail.outbox] == [(NEW,)] * 2
        frozen.tick(timedelta(minutes=6))
        assert confirm(client, headers, code_mailed_to(NEW)) == (204, None)
        assert stored(alice).email == NEW

        ask(client, headers, email="ann@third.example")
        run_worker()
        late = code_mailed_to("ann@third.example")
        frozen.tick(timedelta(minutes=10))
        assert confirm(client, headers, late) == WRONG
        assert stored(alice).email == NEW
        assert len(rows(pending_identifiers)) == 1  # Expired, until the purge.
        ask(client, headers)  # Expired: the account's own address, proved.
        run_worker()
        assert len(mail.outbox) == 3

        # Imported here: importing at collection would register the task early.
        from domainkit.contrib.auth.tasks import clear_expired_tokens

        run_async(clear_expired_tokens())  # The hourly purge.
        assert rows(pending_identifiers) == []


def test_asking_for_the_address_it_has_drops_the_held_one(identity: Api) -> None:
    """A change undone before its code came back: the code is refused."""
    alice = create_user("alice", email=OLD)
    client = identity()
    headers = log_in(client, "alice")
    ask(client, headers, email=NEW)
    run_worker()
    held = code_mailed_to(NEW)

    ask(client, headers, email="ANN@example.com")
    run_worker()

    assert rows(pending_identifiers) == []
    assert [message.to for message in mail.outbox] == [(NEW,), (OLD,)]
    assert confirm(client, headers, held) == WRONG
    assert confirm(client, headers, code_mailed_to(OLD)) == (204, None)
    assert (stored(alice).email, stored(alice).email_verified) == (OLD, True)


def test_an_address_another_account_proved_stays_theirs(identity: Api) -> None:
    """409 once proved, where addresses identify users; elsewhere both keep it."""
    alice = create_user("alice", email=OLD, proved=True)
    create_user("bob", email=NEW, proved=True)
    client = identity(**BY_EMAIL)
    headers = log_in(client, "alice")
    ask(client, headers, email=NEW)
    run_worker()
    code = code_mailed_to(NEW)

    assert confirm(client, headers, code) == (409, "email_taken")
    assert stored(alice).email == OLD
    assert rows(pending_identifiers) == []
    assert confirm(client, headers, code) == WRONG

    shared = identity(IDENTIFIERS=("username",))  # Two may share an address.
    ask(shared, headers, email=NEW)
    run_worker()
    assert confirm(shared, headers, code_mailed_to(NEW)) == (204, None)
    assert stored(alice).email == NEW


def test_an_address_another_account_never_proved_moves(identity: Api) -> None:
    """Whoever proves it gets it (ADR 0133): the holder gives it up."""
    alice = create_user("alice", email=OLD, proved=True)
    carol = create_user("carol", email=NEW)
    client = identity(**BY_EMAIL)
    headers = log_in(client, "alice")
    ask(client, headers, email=NEW)
    run_worker()

    assert confirm(client, headers, code_mailed_to(NEW)) == (204, None)

    assert stored(alice).email == NEW
    assert (stored(carol).email, stored(carol).is_active) == ("", True)
    (released,) = audited("auth.email_released")
    assert released["details"]["user_id"] == str(carol)


def test_a_change_counts_against_the_rate_of_its_recipient(identity: Api) -> None:
    """Two accounts cannot mail one address more often than one could."""
    create_user("alice", email=OLD)
    create_user("bob", email="bob@example.com")
    client = identity(EMAIL_RATE="1/hour")
    other_client = TestClient(client.app, client=("198.51.100.7", 1))

    assert ask(client, log_in(client, "alice"), email=NEW).status_code == 202
    refused_answer = ask(other_client, log_in(other_client, "bob"), email=NEW)

    assert refused_answer.status_code == 429


# Mobile numbers ---------------------------------------------------------------------


def test_a_new_number_is_held_and_the_old_one_stays_in_force(texting: App) -> None:
    """Texted to the new number; the proved one is the account's until then."""
    hal = create_user("hal", mobile_number=NUMBER, proved=True)
    client = texting(**BY_NUMBER)
    headers = log_in(client, "hal")

    answer = client.post(
        "/auth/mobile-verification",
        json={"mobile_number": "+33 6 98 76 54 32"},
        headers=headers,
    )
    assert answer.status_code == 202, answer.json()
    assert answer.json() == {"detail": "A code to verify your number is on its way."}
    drain()

    assert [text.to for text in sms_outbox] == [NEW_NUMBER]
    code = code_texted_to(NEW_NUMBER)
    user = stored(hal)
    assert (user.mobile_number, user.mobile_number_verified) == (NUMBER, True)
    assert logs_in(client, NUMBER)
    assert not logs_in(client, NEW_NUMBER)
    assert audited("auth.mobile_number_changed") == []

    assert confirm(client, headers, other(code), "mobile") == WRONG
    assert confirm(client, headers, code, "mobile") == (204, None)

    user = stored(hal)
    assert (user.mobile_number, user.mobile_number_verified) == (NEW_NUMBER, True)
    assert logs_in(client, NEW_NUMBER)
    assert not logs_in(client, NUMBER)
    (changed,) = audited("auth.mobile_number_changed")
    assert changed["details"]["previous_verified"] is True
    assert rows(pending_identifiers) == []


def test_a_first_number_is_held_too(texting: App) -> None:
    """An account with no number gets one only once it is proved."""
    ivy = create_user("ivy", email="ivy@example.com")
    client = texting()
    headers = log_in(client, "ivy")

    client.post(
        "/auth/mobile-verification", json={"mobile_number": NUMBER}, headers=headers
    )
    drain()
    assert stored(ivy).mobile_number is None
    assert client.post("/auth/mobile-verification", headers=headers).status_code == 202
    drain()  # Again, to the held number.

    assert [text.to for text in sms_outbox] == [NUMBER] * 2
    assert confirm(client, headers, code_texted_to(NUMBER), "mobile") == (204, None)
    assert stored(ivy).mobile_number == NUMBER
    invalid = client.post(
        "/auth/mobile-verification", json={"mobile_number": "06 12"}, headers=headers
    )
    assert invalid.status_code == 422
    assert invalid.json()["code"] == "invalid_mobile_number"


def test_a_number_another_account_proved_stays_theirs(texting: App) -> None:
    """409 for a number proved elsewhere; one held unproved moves (ADR 0133)."""
    hal = create_user("hal", mobile_number=NUMBER, proved=True)
    create_user("jo", mobile_number=NEW_NUMBER, proved=True)
    kim = create_user("kim", mobile_number="+33611111111")
    client = texting()
    headers = log_in(client, "hal")
    taken = {"mobile_number": NEW_NUMBER}

    client.post("/auth/mobile-verification", json=taken, headers=headers)
    drain()
    code = code_texted_to(NEW_NUMBER)
    assert confirm(client, headers, code, "mobile") == (409, "mobile_number_taken")
    assert stored(hal).mobile_number == NUMBER

    free = {"mobile_number": "+33611111111"}
    client.post("/auth/mobile-verification", json=free, headers=headers)
    drain()
    code = code_texted_to("+33611111111")
    assert confirm(client, headers, code, "mobile") == (204, None)
    assert stored(hal).mobile_number == "+33611111111"
    assert stored(kim).mobile_number is None
    assert len(audited("auth.mobile_number_released")) == 1


def test_a_model_that_cannot_change_a_field_refuses_it(
    identity: Api, monkeypatch: pytest.MonkeyPatch
) -> None:
    """422 ``unknown_user_field``, and nothing held."""
    create_user("alice", email=OLD)
    client = identity()
    headers = log_in(client, "alice")
    monkeypatch.delattr(AbstractUser, "change_email")
    monkeypatch.delattr(AbstractUser, "change_mobile_number")

    by_mail = ask(client, headers, email=NEW)
    by_text = client.post(
        "/auth/mobile-verification", json={"mobile_number": NUMBER}, headers=headers
    )

    assert [by_mail.status_code, by_text.status_code] == [422, 422]
    assert {by_mail.json()["code"], by_text.json()["code"]} == {"unknown_user_field"}
    assert rows(pending_identifiers) == []


# Staff, and what goes with the account -----------------------------------------------


def test_staff_set_an_address_at_once_and_drop_the_held_one(api: Api) -> None:
    """Unproved, recorded at once; the user's held change and old codes go."""
    client = api()
    clerk = staff("clerk", "auth.change_user", "auth.view_user")
    alice = create_user("alice", email=OLD, proved=True)

    async def hold_and_mail() -> None:
        async with AuthUnitOfWork() as uow:
            await uow.pending_identifiers.hold(
                alice, "email", NEW, ttl=timedelta(minutes=10)
            )
            await uow.verification_codes.issue(
                PASSWORD_RESET, target_of(OLD), user_id=alice
            )
            await uow.commit()

    run_async(hold_and_mail())

    answer = client.patch(
        f"{ADMIN}/users/{alice}",
        json={"fields": {"email": "ann@support.example", "first_name": "Ann"}},
        headers=clerk,
    )

    assert answer.status_code == 200, answer.json()
    user = stored(alice)
    assert (user.email, user.email_verified) == ("ann@support.example", False)
    assert len(audited("auth.email_changed")) == 1
    assert rows(pending_identifiers) == []
    assert rows(auth_codes) == []
    named = client.patch(
        f"{ADMIN}/users/{alice}", json={"fields": {"last_name": "Lee"}}, headers=clerk
    )
    assert named.status_code == 200


def test_a_held_value_goes_with_the_accounts_credentials(identity: Api) -> None:
    """Personal data: deleted with the account, erased, never in fixtures."""
    alice = create_user("alice", email=OLD)
    client = identity()
    ask(client, log_in(client, "alice"), email=NEW)
    (row,) = rows(pending_identifiers)
    assert repr(PendingIdentifier(**row)) == (
        f"PendingIdentifier(user_id={alice!r}, field='email')"
    )

    async def forget() -> None:
        async with AuthUnitOfWork() as uow:
            await uow.users.forget_credentials(alice)
            await uow.commit()

    run_async(forget())

    assert rows(pending_identifiers) == []
    assert pending_identifiers.info == {"domain": "auth", "fixtures": False}


# The phone modes -------------------------------------------------------------------


def test_in_the_phone_modes_only_the_phone_holds_a_new_address(app: App) -> None:
    """A web session may verify the address it has, not hold another (ADR 0136)."""
    phone = Phone(app(PHONE, APPROVE_LOGINS=True), email=OLD)
    web = app()
    laptop = web_login(web, phone)

    stolen = web.post("/auth/email-verification", json={"email": NEW}, headers=laptop)
    texted = web.post(
        "/auth/mobile-verification", json={"mobile_number": NUMBER}, headers=laptop
    )
    own = web.post("/auth/email-verification", headers=laptop)
    signed = phone.send("POST", "/auth/email-verification", {"email": NEW})

    assert refused(stolen), stolen.json()
    assert refused(texted), texted.json()
    assert own.status_code == 202, own.json()
    assert signed.status_code == 202, signed.json()
    assert [row["value"] for row in rows(pending_identifiers)] == [NEW]
