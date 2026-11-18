"""Superusers an operator makes: proved by a code, or promoted (ADR 0165).

Since 2.4 codes go only to what a user proved (ADR 0141), so a superuser
``createsuperuser`` made, which proved nothing, could never enroll a phone:
under ``CIBA.APPROVE_LOGINS = "always"`` it could never log in (user
report A12).
"""

import io
from collections.abc import Iterator
from dataclasses import dataclass
from types import SimpleNamespace
from typing import ClassVar, Final, cast
from uuid import UUID

import pytest

from domainkit.conf import override_settings
from domainkit.conf.env import project_prefix
from domainkit.contrib import mail
from domainkit.contrib.auth import AuthUnitOfWork, User
from domainkit.contrib.auth.codes import EMAIL_VERIFICATION, target_of
from domainkit.contrib.auth.management.commands import createsuperuser
from domainkit.contrib.auth.models import AbstractUser, UserId
from domainkit.contrib.auth.repositories import StaffMatch
from domainkit.contrib.mail.backends import MailDeliveryFailed
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
    "Note: no verification code can be sent to the superuser (to reset its "
    "password or enroll a phone) until its email address is verified: run "
    "createsuperuser --promote boss --verify"
)
UNKNOWN: Final = (
    "No user proved {!r}: name the user by its username or its id, or by an "
    "email address or mobile number it proved."
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


def code_works(user_id: UUID, address: str, code: str) -> bool:
    """Whether ``code`` still proves ``address`` for the user."""

    async def check() -> bool:
        async with AuthUnitOfWork() as uow:
            proven = await uow.verification_codes.check(
                EMAIL_VERIFICATION, target_of(address), code
            )
            await uow.rollback()
        return proven is not None and proven.user_id == user_id

    return run_async(check())


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
            yield "y"
            yield mailed()  # Mailed before the prompt: no worker runs.

        asked = prompts(createsuperuser.Command, plain=replies())
        proved, found = run("--promote", "boss", "--verify")
        ask_code(phone, "boss@example.com")
        enrolled = enroll(
            phone, Device(), identifier="boss@example.com", code=mailed_code()
        )

    boss = find("boss")
    assert made == "Superuser created successfully.\n"
    assert warned == f"{UNREACHED}\n"
    assert unsent == []
    assert asked == [
        "Make this user a superuser? [y/N]: ",
        "Code sent to boss@example.com: ",
    ]
    assert mail.outbox[0].subject == "Your code to verify your email address"
    assert proved.splitlines() == [
        "The email address boss@example.com is proved.",
        "'boss' is a superuser already.",
    ]
    assert found.splitlines() == [
        f"'boss' is user {boss.id}, by its username.",
        (
            f"User {boss.id}: username 'boss', email address 'boss@example.com' "
            f"(not proved), no mobile number, joined {boss.date_joined.date()}."
        ),
    ]
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
    """``AUTH.CODE_ATTEMPTS`` tries, a blank one too; then the way to try again.

    A cancelled prompt ends the same way, and the code it was waiting for
    stops working.
    """
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
    boss = find("boss")
    prompts(createsuperuser.Command, plain=["y", "^C"])
    with pytest.raises(CommandError) as cancelled:
        run("--promote", "boss", "--verify")

    assert asked[3:] == ["Code sent to boss@example.com: "] * 5  # CODE_ATTEMPTS
    assert str(refused.value) == (
        "The email address boss@example.com is not proved. Try again with: "
        "createsuperuser --promote boss --verify"
    )
    assert notes.getvalue().splitlines() == [WRONG] * 5
    assert (boss.is_superuser, boss.email_verified) == (True, False)
    assert str(cancelled.value) == str(refused.value)
    assert not find("boss").email_verified
    assert not code_works(boss.id, "boss@example.com", mailed())


def test_promote_makes_an_account_that_proved_its_address_a_superuser(
    app: App,
) -> None:
    """One that signed up in the app, say: audited, naming the operator."""
    app()
    ann = create_user("ann", email="ann@example.com", proved=True)

    now, found = run("--promote", "Ann@Example.com", "--noinput", "--operator", "bob")
    again, _ = run("--promote", str(ann), "--noinput", "--operator", "bob")

    assert now == "'Ann@Example.com' is a superuser now.\n"
    assert again == f"{str(ann)!r} is a superuser already.\n"
    assert found.splitlines()[0] == (
        f"'Ann@Example.com' is user {ann}, by its email address."
    )
    promoted = stored(ann)
    assert (promoted.is_superuser, promoted.is_staff) == (True, True)
    changes = audited("auth.user_status_changed")
    assert [(row["actor"], row["details"]) for row in changes] == [
        ("operator:bob", {"user_id": str(ann), "field": "is_superuser", "value": True}),
        ("operator:bob", {"user_id": str(ann), "field": "is_staff", "value": True}),
    ]


def test_a_value_put_on_an_account_unproved_never_names_it(
    app: App, prompts: Prompts
) -> None:
    """Mallory put the operator's number on her account, unproved.

    The operator's code would prove only that the operator reads the
    number, not that the account is theirs: ``--verify`` names by such a
    value only a user that is a superuser already. No code is sent.
    """
    app()
    mallory = create_user(
        None, email="mallory@evil.test", email_verified=True, mobile_number=NUMBER
    )
    prompts(createsuperuser.Command, plain=["y", "123456"])

    with pytest.raises(CommandError) as refused:
        run("--promote", NUMBER, "--verify", "--operator", "ops")
    with pytest.raises(CommandError) as unverified:
        run("--promote", NUMBER, "--noinput")

    assert str(refused.value) == str(unverified.value) == UNKNOWN.format(NUMBER)
    assert sms_outbox == []
    assert not stored(mallory).is_superuser


def test_an_address_names_the_user_that_proved_it_not_a_username(
    app: App,
) -> None:
    """Mallory chose the username ``ceo@example.com``; usernames are tried first.

    By the address, the command finds the CEO, who proved it; Mallory's
    username names no one by an address.
    """
    app()
    ceo = create_user("ceo", email="ceo@example.com", proved=True)
    mallory = create_user("ceo@example.com", email="mallory@evil.test", proved=True)
    nobody = create_user("nobody@example.com", email="x@evil.test", proved=True)

    promoted, found = run("--promote", "ceo@example.com", "--noinput")
    with pytest.raises(CommandError) as refused:
        run("--promote", "nobody@example.com", "--noinput")

    assert promoted == "'ceo@example.com' is a superuser now.\n"
    assert found.splitlines()[0] == (
        f"'ceo@example.com' is user {ceo}, by its email address."
    )
    assert (stored(ceo).is_superuser, stored(mallory).is_superuser) == (True, False)
    assert str(refused.value) == UNKNOWN.format("nobody@example.com")
    assert not stored(nobody).is_superuser


def test_promote_shows_the_user_and_asks_first(app: App, prompts: Prompts) -> None:
    """Any answer but ``y`` changes nothing; ``--noinput`` asks nothing."""
    app()
    ann = create_user("ann", email="ann@example.com", mobile_number=NUMBER)
    asked = prompts(createsuperuser.Command, plain=["", "n"])

    for _ in range(2):
        with pytest.raises(CommandError, match=r"^Operation cancelled\.$"):
            run("--promote", "ann")
    _, shown = run("--promote", "ann", "--noinput")

    assert asked == ["Make this user a superuser? [y/N]: "] * 2
    assert shown.splitlines()[1] == (
        f"User {ann}: username 'ann', email address 'ann@example.com' (not "
        f"proved), mobile number '{NUMBER}' (not proved), joined "
        f"{stored(ann).date_joined.date()}."
    )
    assert stored(ann).is_superuser


def test_noinput_promotes_only_a_user_named_exactly(
    app: App, monkeypatch: pytest.MonkeyPatch
) -> None:
    """By another field of ``AUTH.IDENTIFIERS``, the operator is asked."""
    app()
    ann = create_user("ann", email="ann@example.com", proved=True)

    async def by_badge(users: object, value: str, write: object) -> StaffMatch:
        async with AuthUnitOfWork() as uow:
            user = await uow.users.get(UserId(ann))
        return StaffMatch(user, "badge")

    monkeypatch.setattr(createsuperuser, "match_for_staff", by_badge)
    with pytest.raises(CommandError) as refused:
        run("--promote", "B-77", "--noinput")

    assert str(refused.value) == (
        "--noinput promotes a user named by its username, its id, or a value "
        "it proved, not by its badge: run it without --noinput to be asked."
    )
    assert not stored(ann).is_superuser


def test_promote_refuses_inactive_unknown_and_shared_values(app: App) -> None:
    """An inactive user, an id nobody has, a value two users proved."""
    app()
    create_user("gone", email="gone@example.com", proved=True, is_active=False)
    with override_settings(AUTH={**AUTH, "IDENTIFIERS": ("username",)}):
        create_user("twin1", email="twin@example.com", proved=True)
        create_user("twin2", email="twin@example.com", proved=True)
        with pytest.raises(CommandError) as shared:
            run("--promote", "twin@example.com", "--noinput")
    missing = "00000000-0000-4000-8000-000000000000"

    with pytest.raises(CommandError) as inactive:
        run("--promote", "gone@example.com", "--noinput")
    with pytest.raises(CommandError) as unknown:
        run("--promote", missing, "--noinput")

    assert str(inactive.value) == (
        "User 'gone@example.com' is inactive: it cannot log in."
    )
    assert str(unknown.value) == f"No user has the id {missing}."
    assert str(shared.value) == (
        "Several users proved 'twin@example.com': name the user by its username "
        "or its id."
    )


def test_the_note_names_the_user_by_what_promote_takes(
    app: App, prompts: Prompts
) -> None:
    """Its username; its id where it has none, or one written as an address.

    Usernames alone identify users by default, so the address the note
    named was refused; and a username written as an address would be read
    as one.
    """
    app()
    with override_settings(AUTH={**AUTH, "IDENTIFIERS": None}):
        prompts(
            createsuperuser.Command,
            plain=["admin", "admin@example.com"],
            secret=[PASSWORD, PASSWORD],
        )
        _, by_name = run()

        def replies() -> Iterator[str]:
            yield "y"
            yield mailed()

        prompts(createsuperuser.Command, plain=replies())
        proved, _ = run("--promote", "admin", "--verify")
    with override_settings(AUTH={**AUTH, "IDENTIFIERS": ("email", "mobile_number")}):
        prompts(
            createsuperuser.Command,
            plain=["", "", NUMBER],  # No username, no address.
            secret=[PASSWORD, PASSWORD],
        )
        _, by_id = run()
        numbered = find(NUMBER)

    assert by_name.endswith("run createsuperuser --promote admin --verify\n")
    assert proved.splitlines()[0] == "The email address admin@example.com is proved."
    assert by_id.endswith(f"run createsuperuser --promote {numbered.id} --verify\n")


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
        run("--promote", "ann", "--noinput")
    named, _ = run("--promote", "ann", "--noinput", "--operator", "bob")

    assert str(unnamed.value) == (
        "Name the operator who runs createsuperuser --promote in production: "
        "give --operator NAME, or set DOMAINKIT_OPERATOR."
    )
    assert named == "'ann' is a superuser now.\n"


@pytest.mark.usefixtures("password")
def test_the_note_says_when_no_code_could_ever_reach(
    app: App, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No address nor number; nothing to say where codes go unproved.

    The address is the model's ``EMAIL_FIELD``: a model that names none
    has no address codes go to, whatever its ``email`` holds.
    """
    app()

    _, bare = run("--noinput", "--username", "robot")
    with override_settings(AUTH={**AUTH, "CODES_TO_UNPROVED": True}):
        _, unproved = run("--noinput", "--username", "boss", "--email", "b@example.com")
    monkeypatch.setattr(User, "EMAIL_FIELD", None)
    _, no_field = run("--noinput", "--username", "carl", "--email", "c@example.com")

    none = (
        "Note: the superuser has no email address nor mobile number: no "
        "verification code can be sent to it (to reset its password or enroll a "
        "phone).\n"
    )
    assert bare == none
    assert unproved == ""
    assert no_field == none


def test_a_code_needs_the_app_that_sends_it(
    app: App, prompts: Prompts, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Checked before the user is made; a failed delivery is a plain refusal.

    The code that could not go stops working, and the refusal says how to
    try again.
    """
    app()
    prompts(createsuperuser.Command, plain=[""], secret=[PASSWORD, PASSWORD])
    with monkeypatch.context() as patched:
        patched.setattr(
            createsuperuser,
            "registry",
            SimpleNamespace(is_installed=lambda name: False),
        )
        with pytest.raises(CommandError) as missing:
            run("--username", "boss", "--email", "boss@example.com", "--verify")
    made = run_async(_count())

    async def refuse(messages: object) -> int:
        raise MailDeliveryFailed("The relay is down.")

    monkeypatch.setattr(mail, "deliver", refuse)
    prompts(createsuperuser.Command, plain=[""], secret=[PASSWORD, PASSWORD])
    with pytest.raises(CommandError) as undelivered:
        run("--username", "boss", "--email", "boss@example.com", "--verify")

    assert str(missing.value) == (
        "Sending the code needs domainkit.contrib.mail in INSTALLED_DOMAINS."
    )
    assert made == 0
    assert str(undelivered.value) == (
        "The code could not be sent (MailDeliveryFailed). The email address "
        "boss@example.com is not proved. Try again with: createsuperuser "
        "--promote boss --verify"
    )
    assert run_async(_codes()) == 0


async def _count() -> int:
    """How many users are stored."""
    async with AuthUnitOfWork() as uow:
        page = await uow.users.page(limit=100)
    return len(page.items)


async def _codes() -> int:
    """How many codes are stored."""
    from sqlalchemy import func, select

    from domainkit.contrib.auth.tables import auth_codes

    async with AuthUnitOfWork() as uow:
        return int(
            await uow.session.scalar(select(func.count()).select_from(auth_codes)) or 0
        )


def test_verify_sends_codes_only_where_the_user_proved_nothing(
    app: App, prompts: Prompts
) -> None:
    """A proved address gets no code; the number it never proved does."""
    app()
    nora = create_user(
        "nora", email="nora@example.com", email_verified=True, mobile_number=NUMBER
    )

    def replies() -> Iterator[str]:
        yield "y"
        yield texted()

    asked = prompts(createsuperuser.Command, plain=replies())
    promoted, _ = run("--promote", "nora@example.com", "--verify")

    assert asked == ["Make this user a superuser? [y/N]: ", f"Code sent to {NUMBER}: "]
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

    promoted, _ = run("--promote", "ann", "--noinput")

    assert promoted == "'ann' is a superuser now.\n"
    assert (stored(ann).is_superuser, stored(ann).is_staff) == (True, False)


@pytest.mark.usefixtures("password")
def test_a_superuser_is_named_by_its_unproved_address_to_prove_it(
    app: App, prompts: Prompts
) -> None:
    """The way out for a superuser made before 2.5: ``--verify`` by the address.

    Not by any other holder of it, nor without ``--verify``; a cancelled
    question changes nothing.
    """
    app()
    run("--noinput", "--username", "boss", "--email", "boss@example.com")
    with override_settings(AUTH={**AUTH, "IDENTIFIERS": ("username",)}):
        create_user("other", email="boss@example.com")  # Unproved there too.

    def replies() -> Iterator[str]:
        yield "y"
        yield mailed()

    prompts(createsuperuser.Command, plain=["^C"])
    with pytest.raises(CommandError, match=r"^Operation cancelled\.$"):
        run("--promote", "boss@example.com", "--verify")
    with pytest.raises(CommandError) as unverified:
        run("--promote", "boss@example.com", "--noinput")
    prompts(createsuperuser.Command, plain=replies())
    proved, found = run("--promote", "boss@example.com", "--verify")

    boss = find("boss")
    assert str(unverified.value) == UNKNOWN.format("boss@example.com")
    assert found.splitlines()[0] == (
        f"'boss@example.com' is user {boss.id}, by its email address."
    )
    assert proved.splitlines() == [
        "The email address boss@example.com is proved.",
        "'boss@example.com' is a superuser already.",
    ]
    assert boss.email_verified


def test_an_id_in_any_form_names_by_id_never_a_username(app: App) -> None:
    """Mallory's username is the operator's id written as 32 hex digits.

    A value that reads as an id names the user with that id, whatever
    users chose as usernames (second review of user report A12).
    """
    app()
    ops = create_user("ops", email="ops@corp.test", proved=True)
    mallory = create_user(ops.hex, email="mallory@evil.test", proved=True)

    named, found = run("--promote", ops.hex, "--noinput")
    upper, _ = run("--promote", f"{{{str(ops).upper()}}}", "--noinput")
    missing = "00000000000040008000000000000000"
    with pytest.raises(CommandError) as unknown:
        run("--promote", missing, "--noinput")

    assert named == f"{ops.hex!r} is a superuser now.\n"
    assert found.splitlines()[0] == f"{ops.hex!r} is user {ops}, by its id."
    assert upper.endswith("is a superuser already.\n")
    assert (stored(ops).is_superuser, stored(mallory).is_superuser) == (True, False)
    assert str(unknown.value) == f"No user has the id {missing}."


@pytest.mark.parametrize(
    "typed",
    [
        "0612345678",
        "0033612345678",
        "33612345678",
        "06.12.34.56.78",
        "\u0660\u0666\u0661\u0662\u0663\u0664\u0665\u0666\u0667\u0668",
    ],
)
def test_digits_not_in_international_form_name_no_one(app: App, typed: str) -> None:
    """Mallory's username is the operator's number written otherwise.

    Such digits name no number, and are never read as a username: the
    operator gives the number in international form, or the id. Arabic-
    Indic digits too.
    """
    app()
    create_user("ops", email="ops@corp.test", mobile_number=NUMBER, proved=True)
    mallory = create_user(f"m{len(typed)}", email="mallory@evil.test", proved=True)
    run_async(_rename(mallory, typed))

    with pytest.raises(CommandError) as refused:
        run("--promote", typed, "--noinput", "--operator", "ops")

    assert str(refused.value) == (
        f"{typed!r} is written as a number: give the number in international "
        "form (+33612345678), or the user's id."
    )
    assert not stored(mallory).is_superuser


async def _rename(user_id: UUID, username: str) -> None:
    """Give the user ``username``, as a signup could have."""
    from sqlalchemy import update

    from domainkit.contrib.auth.users import get_user_table

    async with AuthUnitOfWork() as uow:
        table = get_user_table()
        await uow.session.execute(
            update(table).where(table.c.id == user_id).values(username=username)
        )
        await uow.commit()


def test_look_alike_characters_are_folded_first(app: App) -> None:
    """A full-width plus is a plus: the number names the operator, who proved it."""
    app()
    ops = create_user("ops", email="ops@corp.test", mobile_number=NUMBER, proved=True)
    mallory = create_user("mallory", email="mallory@evil.test", proved=True)
    run_async(_rename(mallory, "+33612345678"))

    run("--promote", "\uff0b33612345678", "--noinput")

    assert (stored(ops).is_superuser, stored(mallory).is_superuser) == (True, False)


def test_a_model_without_numbers_is_named_by_none(
    app: App, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A custom user model with no ``mobile_number``: a refusal, not a crash."""
    app()

    @dataclass
    class Badge:
        USERNAME_FIELD: ClassVar[str] = "username"
        EMAIL_FIELD: ClassVar[None] = None
        REQUIRED_FIELDS: ClassVar[tuple[str, ...]] = ()
        id: UUID
        username: str

    monkeypatch.setattr(createsuperuser, "get_user_model", lambda: Badge)
    with pytest.raises(CommandError) as refused:
        run("--promote", NUMBER, "--noinput")

    assert str(refused.value) == (
        "Users have no mobile number here: name the user by its username or its id."
    )


def test_the_user_shown_may_have_nothing_to_show() -> None:
    """A user model with no username, address, number, or date joined."""
    bare = SimpleNamespace(
        id="u1", USERNAME_FIELD="username", EMAIL_FIELD=None, username=None
    )

    assert createsuperuser._described(cast(User, bare)) == (
        "User u1: no username, no email address, no mobile number."
    )
