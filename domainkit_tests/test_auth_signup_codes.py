"""Signing up by code, with an email address or a mobile number (ADR 0126)."""

from datetime import timedelta
from types import SimpleNamespace
from typing import Any, Final, cast
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from domainkit.conf import override_settings
from domainkit.contrib import mail
from domainkit.contrib.auth import AuthUnitOfWork, User
from domainkit.contrib.auth.codes import EMAIL_VERIFICATION, target_of
from domainkit.contrib.auth.models import PasswordHash, UserId
from domainkit.contrib.auth.recovery import (
    EMAIL_PATTERN,
    NewEmailIn,
    PasswordResetIn,
    _prove_reached,
)
from domainkit.contrib.auth.signup import SignupIn, send_signup_code
from domainkit.contrib.notifications.backends import sms_outbox
from domainkit.i18n import gettext, override
from domainkit.management.base import run_async
from domainkit_tests.conftest import MIDDLEWARE, Api
from domainkit_tests.support.auth import PASSWORD, audited, create_user
from domainkit_tests.test_auth_recovery import run_worker
from domainkit_tests.test_auth_reset_codes import (
    ARABIC,
    CODES,
    LOCALE,
    NUMBER,
    OTHER_NUMBER,
    WRONG,
    App,
    code_in,
    drain,
    logs_in,
    other,
    texting,
)
from domainkit_tests.test_auth_signup import pending

__all__ = ["texting"]

BY_MAIL = {"SIGNUP_ENABLED": True, "IDENTIFIERS": ("email",)}
BY_TEXT = {"SIGNUP_ENABLED": True, **CODES}
TAKEN = OTHER_NUMBER


def ask(client: TestClient, **body: object) -> Any:  # noqa: ANN401 - a response.
    """Ask for an account."""
    return client.post("/auth/signup", json=body)


def finish(
    client: TestClient, identifier: str, code: str, password: str = PASSWORD
) -> tuple[int, Any]:
    """The status and problem code of finishing a signup with ``code``."""
    response = client.post(
        "/auth/signup/code",
        json={"identifier": identifier, "code": code, "password": password},
    )
    problem = response.json().get("code") if response.content else None
    return response.status_code, problem


def stored(identifier: str) -> User:
    """The user ``identifier`` names."""

    async def read() -> User:
        async with AuthUnitOfWork() as uow:
            user = await uow.users.find_by_identifier(identifier)
        assert user is not None, identifier
        return cast(User, user)

    return run_async(read())


def test_a_mailed_code_makes_the_account(identity: Api) -> None:
    """No username: the address names the account, verified by the code."""
    client = identity(**BY_MAIL)

    response = ask(client, email="Nina@Example.com")
    assert response.status_code == 202, response.json()
    assert response.json() == {"detail": "Check your email to finish signing up."}
    (row,) = pending()
    assert (row["username"], row["mobile_number"]) == (None, None)
    assert row["expires_at"] - row["created_at"] == timedelta(minutes=10)
    run_worker()
    (message,) = mail.outbox
    assert (message.subject, message.to) == (
        "Your code to finish signing up",
        ("Nina@example.com",),
    )
    code = code_in(message.body)
    assert "never proved" not in message.body
    assert "\n\n\n" not in message.body

    assert finish(client, "nina@example.com", other(code)) == WRONG
    assert finish(client, "nina@example.com", code, "short") == (
        422,
        "invalid_password",
    )
    assert finish(client, "NINA@example.com", code) == (204, None)
    assert finish(client, "nina@example.com", code) == WRONG
    assert pending() == []
    user = stored("nina@example.com")
    assert (user.username, user.email_verified) == (None, True)
    assert logs_in(client, "nina@example.com", PASSWORD)
    (refused, *_) = audited("auth.signup")
    assert refused["details"] == {"reason": "invalid_verification_code"}


def test_a_texted_code_makes_the_account(texting: App) -> None:
    """A number alone, where numbers identify users: the code goes by text."""
    client = texting(**BY_TEXT)

    response = ask(client, mobile_number="+33 6 12 34 56 78")
    assert response.json() == {
        "detail": "Check your text messages to finish signing up."
    }
    drain()
    (text,) = sms_outbox
    assert text.to == NUMBER
    assert "is your code to finish signing up." in text.body
    assert mail.outbox == []

    assert finish(client, "+33 (6) 12 34 56 78", code_in(text.body)) == (204, None)
    user = stored(NUMBER)
    assert (user.mobile_number_verified, user.email, user.username) == (True, "", None)
    assert logs_in(client, NUMBER, PASSWORD)


def test_a_signup_needs_somewhere_to_send_the_code(identity: Api) -> None:
    """An address; a number only with codes, where numbers identify users."""
    client = identity(**BY_MAIL)

    for body in ({}, {"username": "olga"}):
        response = ask(client, **body)
        assert (response.status_code, response.json()["code"]) == (
            422,
            "missing_value",
        )
    response = ask(client, mobile_number=NUMBER)
    assert (response.status_code, response.json()["code"]) == (
        422,
        "unknown_user_field",
    )
    numbers = identity(SIGNUP_ENABLED=True, IDENTIFIERS=("email", "mobile_number"))
    response = ask(numbers, email="pia@example.com", mobile_number="12")
    assert (response.status_code, response.json()["code"]) == (
        422,
        "invalid_mobile_number",
    )
    run_async(send_signup_code(uuid4()))  # A signup gone: nothing happens.
    assert mail.outbox == []


def test_a_taken_address_or_number_is_told_so(texting: App) -> None:
    """Proved by their account: nothing is made; the owner learns of the try."""
    create_user(
        "quinn",
        email="quinn@example.com",
        mobile_number=NUMBER,
        email_verified=True,
        mobile_number_verified=True,
    )
    client = texting(**BY_TEXT)

    ask(client, email="QUINN@example.com")
    ask(client, mobile_number=NUMBER)
    drain()
    (message,) = mail.outbox
    assert "Someone tried to sign up with this email address" in message.body
    (text,) = sms_outbox
    assert text.body.startswith("Someone tried to sign up with this number")
    assert pending() == []
    assert finish(client, NUMBER, "123456") == WRONG


def test_an_address_taken_before_the_code_is_used_answers_409(identity: Api) -> None:
    """An account proved it first: the code is spent, and nothing is made."""
    client = identity(**BY_MAIL)
    ask(client, email="rue@example.com")
    run_worker()
    code = code_in(mail.outbox[0].body)
    create_user("rue", email="rue@example.com", email_verified=True)

    assert finish(client, "rue@example.com", code) == (409, "signup_taken")
    assert finish(client, "rue@example.com", code) == WRONG


def test_a_username_may_come_with_the_address(identity: Api) -> None:
    """The username is kept; the account logs in by address too."""
    client = identity(SIGNUP_ENABLED=True, IDENTIFIERS=("username", "email"))
    ask(client, email="sam@example.com", username="sam")
    run_worker()

    assert finish(client, "sam@example.com", code_in(mail.outbox[0].body)) == (
        204,
        None,
    )
    assert stored("sam").email == "sam@example.com"
    assert logs_in(client, "sam", PASSWORD)


# An account that never proved its address or number (ADR 0133).


def test_an_address_held_unproved_moves_to_the_signup_that_proves_it(
    identity: Api,
) -> None:
    """The holder keeps its username, loses the address and its codes."""
    client = identity(SIGNUP_ENABLED=True, IDENTIFIERS=("username", "email"))
    holder = create_user("ivy", email="Ivy@example.com")
    held_code = run_async(_code_for(holder, "ivy@example.com"))

    ask(client, email="ivy@example.com")
    run_worker()
    (message,) = mail.outbox
    assert "An account already uses this address, but has never proved it" in (
        message.body
    )
    assert "\n\n\n" not in message.body
    assert finish(client, "ivy@example.com", code_in(message.body)) == (204, None)

    signed_up = stored("ivy@example.com")
    assert (signed_up.id != holder, signed_up.email_verified) == (True, True)
    kept = _user(holder)
    assert (kept.email, kept.is_active, kept.username) == ("", True, "ivy")
    assert logs_in(client, "ivy", PASSWORD)
    assert logs_in(client, "ivy@example.com", PASSWORD)  # The new account.
    assert not run_async(_still_works(holder, "ivy@example.com", held_code))
    (released,) = audited("auth.email_released")
    assert released["details"]["user_id"] == str(holder)


def test_a_holder_left_with_nothing_to_log_in_by_is_ended(identity: Api) -> None:
    """Addresses alone log in: the holder is deactivated, its logins end."""
    client = identity(**BY_MAIL)
    holder = create_user(None, email="jo@example.com")
    assert logs_in(client, "jo@example.com", PASSWORD)

    ask(client, email="jo@example.com")
    run_worker()
    assert finish(client, "jo@example.com", code_in(mail.outbox[0].body)) == (
        204,
        None,
    )

    ended = _user(holder)
    assert (ended.email, ended.is_active, ended.has_usable_password()) == (
        "",
        False,
        False,
    )
    assert stored("jo@example.com").id != holder


def test_a_number_held_unproved_moves_too(texting: App) -> None:
    """The number is unique: it moves to the new account, verified."""
    client = texting(**BY_TEXT)
    holder = create_user(None, email="kai@example.com", mobile_number=NUMBER)

    ask(client, mobile_number=NUMBER)
    drain()
    (text,) = sms_outbox
    assert finish(client, NUMBER, code_in(text.body)) == (204, None)

    assert stored(NUMBER).mobile_number_verified is True
    kept = _user(holder)
    assert (kept.mobile_number, kept.is_active, kept.email) == (
        None,
        True,
        "kai@example.com",
    )
    assert len(audited("auth.mobile_number_released")) == 1
    assert run_async(_number_taken(NUMBER)) is True


def test_what_a_user_proved_is_never_released() -> None:
    """``release_email`` and ``release_mobile_number`` leave proof alone."""
    user = User.create(
        password=PasswordHash.unusable(),
        email="nell@example.com",
        mobile_number=NUMBER,
    )
    user.verify_email()
    user.verify_mobile_number()
    user.release_email()
    user.release_mobile_number()
    assert (user.email, user.mobile_number) == ("nell@example.com", NUMBER)


def test_a_reset_proves_nothing_on_a_model_that_keeps_no_proof() -> None:
    """A user model with no ``verify_*`` methods: a reset marks nothing."""
    plain = SimpleNamespace(mobile_number=NUMBER, email="")
    _prove_reached(plain, NUMBER)  # type: ignore[arg-type]
    assert vars(plain) == {"mobile_number": NUMBER, "email": ""}


async def _number_taken(number: str) -> bool:
    """Whether a user has ``number``."""
    async with AuthUnitOfWork() as uow:
        return await uow.users.mobile_number_taken(number)


def test_a_holder_that_proves_it_first_keeps_it(identity: Api) -> None:
    """Proved between the mail and the code: the signup answers 409."""
    client = identity(**BY_MAIL)
    holder = create_user(None, email="lu@example.com")
    ask(client, email="lu@example.com")
    run_worker()
    code = code_in(mail.outbox[0].body)
    run_async(_verify(holder))

    assert finish(client, "lu@example.com", code) == (409, "signup_taken")
    assert _user(holder).email == "lu@example.com"


async def _code_for(user_id: UUID, address: str) -> str:
    """An email verification code for ``user_id``, sent to ``address``."""
    async with AuthUnitOfWork() as uow:
        code = await uow.verification_codes.issue(
            EMAIL_VERIFICATION, target_of(address), user_id=user_id
        )
        await uow.commit()
    return code


async def _still_works(user_id: UUID, address: str, code: str) -> bool:
    """Whether ``code`` still proves ``address`` for ``user_id``."""
    async with AuthUnitOfWork() as uow:
        proven = await uow.verification_codes.check(
            EMAIL_VERIFICATION, target_of(address), code
        )
    return proven is not None and proven.user_id == user_id


async def _verify(user_id: UUID) -> None:
    """Mark ``user_id``'s address proved."""
    async with AuthUnitOfWork() as uow:
        user = cast(User, await uow.users.get(UserId(user_id)))
        user.verify_email()
        await uow.commit()


def _user(user_id: UUID) -> User:
    """The user ``user_id``, as stored."""

    async def read() -> User:
        async with AuthUnitOfWork() as uow:
            return cast(User, await uow.users.get(UserId(user_id)))

    return run_async(read())


def test_signup_codes_go_out_in_the_language_of_the_request(texting: App) -> None:
    """No user yet: the request's language, carried to the task (user report A11)."""
    with override_settings(MIDDLEWARE=[LOCALE, *MIDDLEWARE]):
        client = texting(**BY_TEXT)
        create_user("taken", mobile_number=TAKEN, mobile_number_verified=True)
        for body in ({"mobile_number": NUMBER}, {"mobile_number": TAKEN}):
            assert ask_in_arabic(client, body).status_code == 202
        assert ask_in_arabic(client, {"email": "new@example.com"}).status_code == 202
        drain()

    code, taken = (text.body for text in sorted(sms_outbox, key=lambda text: text.to))
    assert "هو رمزك لإكمال التسجيل. يعمل لمدة 10 دقائق." in code
    assert taken.startswith("حاول أحدهم التسجيل بهذا الرقم، وهو مستخدم في حساب")
    (message,) = mail.outbox
    assert message.subject == "رمز إكمال التسجيل"
    assert "يعمل لمدة 10 دقائق" in message.body


def ask_in_arabic(client: TestClient, body: dict[str, str]) -> Any:  # noqa: ANN401 - a response.
    """Ask for an account, in Arabic."""
    return client.post("/auth/signup", json=body, headers=ARABIC)


# What a refusal says (user report I8) ------------------------------------------------


def test_a_value_shaped_as_no_address_is_refused_in_words(identity: Api) -> None:
    """Not with the pattern that checks it, which reached clients translated.

    ``/signup``, ``/password-reset`` and ``/email-verification`` take
    addresses the same way. OpenAPI still shows the pattern.
    """
    with override_settings(MIDDLEWARE=[LOCALE, *MIDDLEWARE]):
        client = identity(**BY_MAIL)
        english = ask(client, email="not-an-email")
        arabic = client.post(
            "/auth/signup", json={"email": "not-an-email"}, headers=ARABIC
        )
        reset = client.post(
            "/auth/password-reset", json={"email": "a b@example.com"}, headers=ARABIC
        )

    def refusal(response: Any) -> tuple[int, list[str], str]:  # noqa: ANN401 - a response.
        (error,) = response.json()["errors"]
        return response.status_code, error["loc"], error["msg"]

    assert refusal(english) == (
        422,
        ["body", "email"],
        "Value error, Enter a valid email address.",
    )
    assert refusal(arabic) == (
        422,
        ["body", "email"],
        "قيمة غير صالحة: أدخل عنوان بريد إلكتروني صالحًا.",
    )
    assert refusal(reset)[2] == refusal(arabic)[2]
    for model in (SignupIn, PasswordResetIn, NewEmailIn):
        (shown, _) = model.model_json_schema()["properties"]["email"]["anyOf"]
        assert shown["pattern"] == EMAIL_PATTERN


SIGNED_UP_WITH: Final = "Someone tried to sign up with this email address"


@pytest.mark.parametrize(
    ("identifiers", "body", "taken"),
    [
        (
            ("email",),
            {"email": "ann@example.com"},
            (
                "An account already uses this address. If it was you, log in, or "
                "reset your password."
            ),
        ),
        (
            ("email", "mobile_number"),
            {"email": "ann@example.com", "mobile_number": NUMBER},
            (  # The number counts for nothing: its own code proves it (ADR 0171).
                "An account already uses this address. If it was you, log in, or "
                "reset your password."
            ),
        ),
        (
            ("username", "email"),
            {"username": "dave", "email": "ann@example.com"},
            (
                "An account already uses this address, or the username chosen. If "
                "it was you, log in, reset your password, or sign up again with "
                "another username."
            ),
        ),
        (
            ("username", "email", "mobile_number"),
            {"username": "dave", "email": "ann@example.com", "mobile_number": NUMBER},
            (
                "An account already uses this address, or the username chosen. If "
                "it was you, log in, reset your password, or sign up again with "
                "another username."
            ),
        ),
    ],
)
def test_a_taken_signup_is_told_only_what_it_gave(
    texting: App, identifiers: tuple[str, ...], body: dict[str, str], taken: str
) -> None:
    """Another username is suggested only where usernames identify users.

    Elsewhere a signup cannot choose one (``422 unknown_user_field``), yet
    the mail told everyone to sign up again with another username.
    """
    client = texting(SIGNUP_ENABLED=True, IDENTIFIERS=identifiers)
    create_user("ann", email="ann@example.com", email_verified=True)

    assert ask(client, **body).status_code == 202
    drain()

    (message,) = mail.outbox
    assert message.subject == "Your sign-up"
    with override("ar"):
        assert gettext(taken) != taken  # Each is in Arabic too.
    assert message.body.startswith(
        f"Hello,\n\n{SIGNED_UP_WITH} (ann@example.com).\n\n{taken}\n\n"
    )
    assert ("username" in message.body) is ("username" in identifiers)


def test_the_taken_mail_and_answer_speak_the_signups_language(texting: App) -> None:
    """In Arabic for a request in Arabic; the 409 suggests no username either."""
    with override_settings(MIDDLEWARE=[LOCALE, *MIDDLEWARE]):
        client = texting(**BY_MAIL)
        create_user("ann", email="ann@example.com", email_verified=True)
        assert ask_in_arabic(client, {"email": "ann@example.com"}).status_code == 202
        assert ask(client, email="rue@example.com").status_code == 202
        drain()
        (sent,) = [m for m in mail.outbox if m.to == ("rue@example.com",)]
        code = code_in(sent.body)  # The worker runs the two in either order.
        create_user("rue", email="rue@example.com", email_verified=True)
        late = client.post(
            "/auth/signup/code",
            json={"identifier": "rue@example.com", "code": code, "password": PASSWORD},
        )

    told = next(m for m in mail.outbox if m.to == ("ann@example.com",))
    assert told.body.split("\n\n")[:3] == [
        "مرحبًا،",
        "حاول أحدهم التسجيل بعنوان البريد الإلكتروني هذا (ann@example.com).",
        (
            "يستخدم حسابٌ هذا العنوان بالفعل. إن كنت أنت، فسجّل الدخول، أو أعد "
            "تعيين كلمة المرور."
        ),
    ]
    assert (late.status_code, late.json()["code"]) == (409, "signup_taken")
    assert late.json()["detail"] == (
        "An account already uses this address; log in, or reset your password."
    )


@pytest.mark.parametrize(
    ("identifiers", "body", "identifier", "detail"),
    [
        (
            ("email", "mobile_number"),
            {"mobile_number": NUMBER},
            NUMBER,
            "An account already uses this number; log in, or reset your password.",
        ),
        (
            ("username", "mobile_number"),
            {"username": "dave", "mobile_number": NUMBER},
            NUMBER,
            (
                "An account already uses this username or this number; sign up "
                "again with another username, or log in."
            ),
        ),
        (
            ("username", "email", "mobile_number"),
            {"username": "dave", "email": "dave@example.com", "mobile_number": NUMBER},
            "dave@example.com",
            (  # The number, given with an address, is not the signup's to lose.
                "An account already uses this username or this address; sign up "
                "again with another username, or log in."
            ),
        ),
    ],
)
def test_a_signup_taken_meanwhile_is_told_only_what_it_gave(
    texting: App,
    identifiers: tuple[str, ...],
    body: dict[str, str],
    identifier: str,
    detail: str,
) -> None:
    """The 409 names what this signup gave, as the mail does (user report I8)."""
    client = texting(SIGNUP_ENABLED=True, IDENTIFIERS=identifiers)
    assert ask(client, **body).status_code == 202
    drain()
    sent = mail.outbox[-1].body if "@" in identifier else sms_outbox[-1].body
    # The name and the number, taken meanwhile.
    create_user("dave", mobile_number=NUMBER, mobile_number_verified=True)

    late = client.post(
        "/auth/signup/code",
        json={"identifier": identifier, "code": code_in(sent), "password": PASSWORD},
    )

    assert (late.status_code, late.json()["code"]) == (409, "signup_taken")
    assert late.json()["detail"] == detail
