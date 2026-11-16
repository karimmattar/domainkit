"""Superusers an operator makes: proved by a code, or promoted (ADR 0165).

Since 2.4 codes go only to what a user proved (ADR 0141), so a superuser
``createsuperuser`` made, which proved nothing, could never enroll a phone:
under ``CIBA.APPROVE_LOGINS = "always"`` it could never log in (user
report A12).
"""

import io
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Final, cast
from uuid import UUID

import pytest

from domainkit.conf import override_settings
from domainkit.conf.env import project_prefix
from domainkit.contrib import mail
from domainkit.contrib.auth import AuthUnitOfWork, User
from domainkit.contrib.auth.management.commands import createsuperuser
from domainkit.contrib.auth.models import AbstractUser, UserId
from domainkit.contrib.notifications.backends import sms_outbox
from domainkit.management import CommandError, call_command, operators
from domainkit.management.base import run_async
from domainkit_tests.support.auth import PASSWORD, audited, create_user
from domainkit_tests.test_auth_commands import Prompts, prompts
from domainkit_tests.test_auth_recovery import run_worker
from domainkit_tests.test_auth_reset_codes import code_in
from domainkit_tests.test_ciba import PHONE, App, app
from domainkit_tests.test_devices import Device
from domainkit_tests.test_devices_enrollment import (
    AUTH,
    NUMBER,
    ON,
    _identifiers,
    ask_code,
    enroll,
    mailed_code,
)

__all__ = ["_identifiers", "app", "prompts"]  # The fixtures the tests take.

WRONG: Final = "Error: That is not the code sent, or it no longer works."
UNREACHED: Final = (
    "Note: no code reaches the superuser, to reset its password or enroll a "
    "phone, until it proves its email address: run createsuperuser --promote "
    "boss@example.com --verify"
)


def run(*args: str) -> tuple[str, str]:
    """Run ``createsuperuser`` with ``args``: what it wrote to stdout and stderr."""
    out, notes = io.StringIO(), io.StringIO()
    call_command("createsuperuser", *args, stdout=out, stderr=notes)
    return out.getvalue(), notes.getvalue()


def stored(user_id: UUID) -> User:
    """The user, as stored."""

    async def read() -> User:
        async with AuthUnitOfWork() as uow:
            return cast(User, await uow.users.get(UserId(user_id)))

    return run_async(read())


def find(identifier: str) -> User:
    """The user ``identifier`` names."""

    async def read() -> User | None:
        async with AuthUnitOfWork() as uow:
            return cast(User | None, await uow.users.find_by_identifier(identifier))

    found = run_async(read())
    assert found is not None, identifier
    return found


def mailed() -> str:
    """The code in the last mail sent."""
    return code_in(mail.outbox[-1].body)


def texted() -> str:
    """The code in the last text message sent."""
    return code_in(sms_outbox[-1].body)


@pytest.fixture
def password(monkeypatch: pytest.MonkeyPatch) -> None:
    """``createsuperuser --noinput`` reads a password that passes the rules."""
    monkeypatch.setenv(f"{project_prefix()}SUPERUSER_PASSWORD", PASSWORD)


@pytest.mark.usefixtures("password")
def test_the_superuser_the_command_made_enrolls_its_phone_once_proved(
    app: App, prompts: Prompts
) -> None:
    """Made unproved, it got no enrollment code; ``--verify`` proves it.

    The operator types the code mailed at the prompt; then the enrollment
    code reaches the address, and the phone, its first login, binds.
    """
    phone = app(PHONE, APPROVE_LOGINS="always")
    with override_settings(DEVICES=ON):
        made, warned = run(
            "--noinput",
            "--no-temporary",
            "--username",
            "boss",
            "--email",
            "boss@example.com",
        )
        ask_code(phone, "boss@example.com")
        run_worker()
        unsent = list(mail.outbox)

        def replies() -> Iterator[str]:
            yield mailed()  # Mailed before the prompt: no worker runs.

        asked = prompts(createsuperuser.Command, plain=replies())
        proved, found = run("--promote", "boss@example.com", "--verify")
        ask_code(phone, "boss@example.com")
        enrolled = enroll(
            phone, Device(), identifier="boss@example.com", code=mailed_code()
        )

    boss = find("boss")
    assert made == "Superuser created successfully.\n"
    assert warned == f"{UNREACHED}\n"
    assert unsent == []
    assert asked == ["Code sent to boss@example.com: "]
    assert mail.outbox[0].subject == "Your code to verify your email address"
    assert proved.splitlines() == [
        "The email address boss@example.com is proved.",
        "'boss@example.com' is a superuser already.",
    ]
    assert found == f"'boss@example.com' is user {boss.id}, by its email.\n"
    assert boss.email_verified
    assert enrolled.status_code == 201, enrolled.json()


def test_verify_proves_a_new_superusers_address_and_number(
    app: App, prompts: Prompts, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A code to each, sent now; typed in, each is proved, by the operator."""
    app()
    monkeypatch.setenv("DOMAINKIT_OPERATOR", "ops")

    def replies() -> Iterator[str]:
        yield from ("chief", "chief@example.com", NUMBER)
        yield mailed()
        yield texted()

    asked = prompts(
        createsuperuser.Command, plain=replies(), secret=[PASSWORD, PASSWORD]
    )
    made, notes = run("--verify")

    chief = find("chief")
    assert asked[-2:] == [
        "Code sent to chief@example.com: ",
        f"Code sent to {NUMBER}: ",
    ]
    assert made.splitlines() == [
        "Superuser created successfully.",
        "The email address chief@example.com is proved.",
        f"The mobile number {NUMBER} is proved.",
    ]
    assert notes == ""
    assert (chief.email_verified, chief.mobile_number_verified) == (True, True)
    (text,) = sms_outbox
    assert text.to == NUMBER
    assert "is your code to verify your number." in text.body
    assert [row["actor"] for row in audited("auth.email_verified")] == ["operator:ops"]
    assert [row["actor"] for row in audited("auth.mobile_number_verified")] == [
        "operator:ops"
    ]


def test_wrong_codes_leave_the_superuser_unproved(app: App, prompts: Prompts) -> None:
    """``AUTH.CODE_ATTEMPTS`` tries, a blank one too; then the way to try again."""
    app()
    asked = prompts(
        createsuperuser.Command,
        plain=["", "", "nope", "nope", "1", "2"],  # No number; then the codes.
        secret=[PASSWORD, PASSWORD],
    )

    with pytest.raises(CommandError) as refused:
        call_command(
            "createsuperuser",
            "--username",
            "boss",
            "--email",
            "boss@example.com",
            "--verify",
            stdout=io.StringIO(),
            stderr=(notes := io.StringIO()),
        )

    assert asked[3:] == ["Code sent to boss@example.com: "] * 5  # CODE_ATTEMPTS
    assert str(refused.value) == (
        "The email address boss@example.com is not proved. Try again with: "
        "createsuperuser --promote boss@example.com --verify"
    )
    assert notes.getvalue().splitlines() == [WRONG] * 5
    boss = find("boss")
    assert (boss.is_superuser, boss.email_verified) == (True, False)
    prompts(createsuperuser.Command, plain=["^C"])
    with pytest.raises(CommandError) as cancelled:
        run("--promote", "boss", "--verify")
    assert str(cancelled.value) == str(refused.value)
    assert not find("boss").email_verified


def test_promote_makes_an_account_that_proved_its_address_a_superuser(
    app: App,
) -> None:
    """One that signed up in the app, say: audited, naming the operator."""
    app()
    ann = create_user("ann", email="ann@example.com", proved=True)

    now, found = run("--promote", "Ann@Example.com", "--operator", "bob")
    again, _ = run("--promote", "ann", "--operator", "bob")

    assert now == "'Ann@Example.com' is a superuser now.\n"
    assert again == "'ann' is a superuser already.\n"
    assert found == f"'Ann@Example.com' is user {ann}, by its email.\n"
    promoted = stored(ann)
    assert (promoted.is_superuser, promoted.is_staff) == (True, True)
    changes = audited("auth.user_status_changed")
    assert [(row["actor"], row["details"]) for row in changes] == [
        ("operator:bob", {"user_id": str(ann), "field": "is_superuser", "value": True}),
        ("operator:bob", {"user_id": str(ann), "field": "is_staff", "value": True}),
    ]


def test_promote_names_an_account_only_by_what_it_proved(
    app: App, prompts: Prompts
) -> None:
    """An address the account never proved might be anyone's: ``--verify`` it.

    Its username names it as it is, as other commands take it; the
    command then says that no code reaches it.
    """
    app()
    mallory = create_user("mallory", email="ceo@example.com")
    create_user("gone", email="gone@example.com", proved=True, is_active=False)

    with pytest.raises(CommandError) as unproved:
        run("--promote", "ceo@example.com")
    with pytest.raises(CommandError) as inactive:
        run("--promote", "gone@example.com")
    with pytest.raises(CommandError) as unknown:
        run("--promote", "nobody@example.com")
    by_name, warned = run("--promote", "mallory")

    assert str(unproved.value) == (
        "The user never proved the email address 'ceo@example.com': add "
        "--verify to prove it with a code, or name the user by a value it proved."
    )
    assert (
        str(inactive.value) == "User 'gone@example.com' is inactive: it cannot log in."
    )
    assert str(unknown.value) == "User 'nobody@example.com' does not exist."
    assert by_name == "'mallory' is a superuser now.\n"
    assert warned.splitlines()[-1] == (
        "Note: no code reaches the superuser, to reset its password or enroll a "
        "phone, until it proves its email address: run createsuperuser "
        "--promote ceo@example.com --verify"
    )
    assert stored(mallory).is_superuser


def test_promote_takes_the_identifier_alone(app: App) -> None:
    """No field, no password, no ``--noinput`` with ``--verify``."""
    app()
    fields = "--promote changes no field"
    for args, message in [
        (("--promote", "ann", "--email", "a@example.com"), fields),
        (("--promote", "ann", "--no-temporary"), fields),
        (("--promote", "ann", "--verify", "--noinput"), "cannot run with --noinput"),
        (("--verify", "--noinput"), "cannot run with --noinput"),
    ]:
        with pytest.raises(CommandError, match=message):
            run(*args)


def test_promote_needs_a_named_operator_where_deployed(
    app: App, monkeypatch: pytest.MonkeyPatch
) -> None:
    """In staging and production the trail names who made a superuser."""
    app()
    create_user("ann", email="ann@example.com", proved=True)
    monkeypatch.delenv("DOMAINKIT_OPERATOR", raising=False)
    monkeypatch.setattr(
        operators, "settings", SimpleNamespace(ENVIRONMENT="production")
    )

    with pytest.raises(CommandError) as unnamed:
        run("--promote", "ann")
    named, _ = run("--promote", "ann", "--operator", "bob")

    assert str(unnamed.value) == (
        "Name the operator who runs createsuperuser --promote in production: "
        "give --operator NAME, or set DOMAINKIT_OPERATOR."
    )
    assert named == "'ann' is a superuser now.\n"


@pytest.mark.usefixtures("password")
def test_the_note_says_when_no_code_could_ever_reach(app: App) -> None:
    """No address nor number; and nothing to say where codes go unproved."""
    app()

    _, bare = run("--noinput", "--username", "robot")
    with override_settings(AUTH={**AUTH, "CODES_TO_UNPROVED": True}):
        _, unproved = run("--noinput", "--username", "boss", "--email", "b@example.com")

    assert bare == (
        "Note: the superuser has no email address nor mobile number: no code "
        "reaches it, to reset its password or enroll a phone.\n"
    )
    assert unproved == ""


def test_a_code_needs_the_app_that_sends_it(
    app: App, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Mail for an address, notifications for a number."""
    app()
    create_user("ann", email="ann@example.com")
    monkeypatch.setattr(
        createsuperuser, "registry", SimpleNamespace(is_installed=lambda name: False)
    )

    with pytest.raises(CommandError) as missing:
        run("--promote", "ann@example.com", "--verify")

    assert str(missing.value) == (
        "Sending the code needs domainkit.contrib.mail in INSTALLED_DOMAINS."
    )
    assert mail.outbox == []


def test_verify_sends_codes_only_where_the_user_proved_nothing(
    app: App, prompts: Prompts
) -> None:
    """A proved address gets no code; the number it never proved does."""
    app()
    nora = create_user(
        "nora", email="nora@example.com", email_verified=True, mobile_number=NUMBER
    )

    def replies() -> Iterator[str]:
        yield texted()

    asked = prompts(createsuperuser.Command, plain=replies())
    promoted, _ = run("--promote", "nora@example.com", "--verify")

    assert asked == [f"Code sent to {NUMBER}: "]
    assert mail.outbox == []
    assert promoted.splitlines() == [
        f"The mobile number {NUMBER} is proved.",
        "'nora@example.com' is a superuser now.",
    ]
    assert stored(nora).mobile_number_verified


def test_a_user_model_without_staff_is_made_a_superuser_alone(
    app: App, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every permission, as ``create_superuser`` gives, and no staff flag to set."""
    app()
    ann = create_user("ann", email="ann@example.com", proved=True)
    monkeypatch.delattr(AbstractUser, "set_staff")

    promoted, _ = run("--promote", "ann")

    assert promoted == "'ann' is a superuser now.\n"
    assert (stored(ann).is_superuser, stored(ann).is_staff) == (True, False)
