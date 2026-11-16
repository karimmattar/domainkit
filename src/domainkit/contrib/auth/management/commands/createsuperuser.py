"""``createsuperuser``: create a user with every permission (Django's).

It asks for the username, the fields of ``AUTH.IDENTIFIERS`` (such as the
email address and the mobile number), the model's ``REQUIRED_FIELDS``, and
a password, unless options give them. Where the username does not identify
users, it is optional; the user needs one identifier at least. With
``--noinput`` it asks nothing and reads what the options leave out from
``<PREFIX>SUPERUSER_<FIELD>`` (or a ``_FILE`` holding it), such as
``BILLING_API_SUPERUSER_PASSWORD``; without a password the user gets an
unusable one.

A password the validators refuse is kept only on request, and only where
no ``AUTH.PRESET`` holds and outside staging and production
(AUTH-18). A password read from the environment, which whoever deploys
holds too, is to change at its first use under
``AUTH.PASSWORD_CHANGE_ON_FIRST_USE`` (PCI DSS v4.0 8.3.5), unless
``--no-temporary``.

Codes go only to an address or a number its user proved (ADR 0141), so a
superuser that proved neither resets no password, and enrolls no phone,
which it needs to log in under ``CIBA.APPROVE_LOGINS = "always"``; the
command says so when it leaves one (ADR 0165). ``--verify`` proves the
user's address and number: it sends a code to each, now, and asks for it.
``--promote IDENTIFIER`` makes an existing user a superuser instead, such
as one that signed up in the application. It takes the user's username,
its id, or an address or a number the user proved: one it never proved
might be anyone's, and names a superuser only, for ``--verify``. It shows
the user and asks before it changes anything; in staging and production
it needs a named operator.
"""

import dataclasses
import getpass
import re
import unicodedata
from argparse import ArgumentParser, BooleanOptionalAction
from collections.abc import Callable
from datetime import datetime
from types import SimpleNamespace
from typing import Final, cast
from uuid import UUID

from domainkit import setup
from domainkit.apps import registry
from domainkit.conf import settings
from domainkit.conf.env import Env
from domainkit.conf.global_settings import DEPLOYED_ENVIRONMENTS
from domainkit.exceptions import DomainkitError
from domainkit.i18n import override
from domainkit.management.base import BaseCommand, CommandError, run_async
from domainkit.management.operators import require_named

from ...codes import EMAIL_VERIFICATION, MOBILE_VERIFICATION, user_target
from ...exceptions import (
    EmailTaken,
    IdentifierRequired,
    InvalidEmail,
    InvalidMobileNumber,
    InvalidPassword,
    InvalidUsername,
    MobileNumberTaken,
    UsernameTaken,
)
from ...models import AbstractBaseUser, UserId, as_mobile_number
from ...onetime import email_of
from ...recovery import code_context, code_text, language_of
from ...settings import auth_settings, identifier_fields
from ...uow import AuthUnitOfWork
from ...users import get_user_model
from .. import check_password_rules, match_for_staff

MAIL: Final = "domainkit.contrib.mail"
NOTIFICATIONS: Final = "domainkit.contrib.notifications"
EMAIL: Final = "email"
NUMBER: Final = "mobile_number"
PROOFS: Final = {
    EMAIL: ("email_verified", "verify_email", EMAIL_VERIFICATION, MAIL),
    NUMBER: (
        "mobile_number_verified",
        "verify_mobile_number",
        MOBILE_VERIFICATION,
        NOTIFICATIONS,
    ),
}
"""For each kind of value codes go to: its proof's flag, the method that
sets it, the purpose of the code that proves it, and the app that sends it.
The address is the model's ``EMAIL_FIELD``."""
LABELS: Final = {EMAIL: "email address", NUMBER: "mobile number"}
ID: Final = "id"
DIGITS: Final = "digits"
SEPARATORS: Final = re.compile(r"[\s().\-/]")
"""What phone numbers are written with besides digits and ``+``."""
EXACT: Final = frozenset({"username", "id", "proved"})
"""How a user may be named for ``--promote --noinput``, asked nothing."""


def _may_bypass() -> bool:
    """Whether a refused password may be kept on request (AUTH-18).

    Not under a preset, whose rules ``auth.E010`` holds, nor in staging
    and production.
    """
    return (
        auth_settings().PRESET is None
        and settings.ENVIRONMENT not in DEPLOYED_ENVIRONMENTS
    )


def _label(field: str) -> str:
    """``first_name`` as ``First name``."""
    return field.replace("_", " ").capitalize()


def _fields() -> list[str]:
    """What it asks for: the username, the identifiers, ``REQUIRED_FIELDS``."""
    model = get_user_model()
    names = [model.USERNAME_FIELD, *identifier_fields(model), *model.REQUIRED_FIELDS]
    return list(dict.fromkeys(names))


def _username_identifies() -> bool:
    """Whether the username is one of ``AUTH.IDENTIFIERS``: then it is required."""
    model = get_user_model()
    return model.USERNAME_FIELD in identifier_fields(model)


def _value(user: AbstractBaseUser, kind: str) -> str | None:
    """The user's address (``EMAIL_FIELD``), or number, if it has one."""
    if kind == EMAIL:
        return email_of(user)
    number = getattr(user, NUMBER, None)
    return str(number) if number else None


def _reached(user: AbstractBaseUser) -> list[tuple[str, str, bool]]:
    """Each address or number codes would go to: its kind, the value, proved.

    A user model without a kind's ``*_verified`` flag counts it proved, as
    ``reaches_proved`` does.
    """
    found = []
    for kind, (flag, *_) in PROOFS.items():
        if value := _value(user, kind):
            found.append((kind, value, bool(getattr(user, flag, True))))
    return found


def _written_as(value: str) -> str | None:
    """What ``value`` is written as, once NFKC folds look-alike characters.

    ``id``: a user id, in any form ``UUID`` reads. ``email``: an address.
    ``mobile_number``: a number in international form. ``digits``: digits
    of any script and phone separators, with a ``+`` or not, that are no
    number in international form (``0612345678``, ``0033…``). None for
    anything else, such as a username.
    """
    value = unicodedata.normalize("NFKC", value).strip()
    try:
        UUID(value)
    except ValueError:
        pass
    else:
        return ID
    if "@" in value:
        return EMAIL
    bare = SEPARATORS.sub("", value).removeprefix("+")
    if bare.isdigit():
        return NUMBER if as_mobile_number(value) is not None else DIGITS
    return None


def _name_of(user: AbstractBaseUser) -> str:
    """What ``--promote`` names the user by, surely: its username, else its id.

    A username written as an id, an address, or a number would be read as
    one.
    """
    username = getattr(user, user.USERNAME_FIELD, None)
    if isinstance(username, str) and username and _written_as(username) is None:
        return username
    return str(user.id)


def _stored_fields() -> set[str]:
    """The fields the user model stores."""
    return {field.name for field in dataclasses.fields(get_user_model())}


def _retry(user: AbstractBaseUser) -> str:
    """The command that proves the user's address and number later."""
    return f"createsuperuser --promote {_name_of(user)} --verify"


def _check_senders(kinds: list[str]) -> None:
    """Refuse before anything changes when a code of ``kinds`` cannot be sent.

    Raises:
        CommandError: The app that sends one is not installed.
    """
    for kind in kinds:
        sender = PROOFS[kind][3]
        if not registry.is_installed(sender):
            raise CommandError(f"Sending the code needs {sender} in INSTALLED_DOMAINS.")


class Command(BaseCommand):
    """Create a superuser, interactively or from options and variables."""

    help = "Create a superuser, asking for whatever the options leave out."
    operator_actions = True

    ask: Callable[[str], str] = staticmethod(input)
    ask_secret: Callable[[str], str] = staticmethod(getpass.getpass)

    def add_arguments(self, parser: ArgumentParser) -> None:
        """One option per field the user model needs, and ``--noinput``."""
        setup()
        for field in _fields():
            parser.add_argument(f"--{field}", help=f"the user's {_label(field)}")
        parser.add_argument(
            "--noinput",
            "--no-input",
            dest="interactive",
            action="store_false",
            help="ask nothing; read missing values from <PREFIX>SUPERUSER_<FIELD>",
        )
        parser.add_argument(
            "--temporary",
            action=BooleanOptionalAction,
            default=None,
            help="the user changes the password at the first login (default: "
            "with --noinput, under AUTH.PASSWORD_CHANGE_ON_FIRST_USE)",
        )
        parser.add_argument(
            "--verify",
            action="store_true",
            help="prove the user's email address and mobile number: send a "
            "code to each now, and ask for it",
        )
        parser.add_argument(
            "--promote",
            metavar="IDENTIFIER",
            help="make the existing user IDENTIFIER names a superuser instead "
            "of creating one; an email address or a mobile number names it "
            "only once it proved it",
        )

    def handle(
        self,
        *,
        interactive: bool,
        temporary: bool | None = None,
        verify: bool = False,
        promote: str | None = None,
        **options: str | None,
    ) -> None:
        """Gather the values, then create the user, or promote the one named."""
        if verify and not interactive:
            raise CommandError(
                "--verify asks for the codes it sends: it cannot run with --noinput."
            )
        if promote is not None:
            if temporary is not None or any(v is not None for v in options.values()):
                raise CommandError(
                    "--promote changes no field: give the identifier alone, "
                    "with --verify if it is to be proved."
                )
            self._promote(promote, verify=verify, interactive=interactive)
            return
        if temporary is None:
            temporary = not interactive and auth_settings().PASSWORD_CHANGE_ON_FIRST_USE
        try:
            values = self._values(interactive, options)
            password = (
                self._ask_password(values)
                if interactive
                else self._env_password(values)
            )
        except (KeyboardInterrupt, EOFError):
            raise CommandError("Operation cancelled.") from None
        if verify:
            model = get_user_model()
            given = {EMAIL: model.EMAIL_FIELD, NUMBER: NUMBER}
            _check_senders([kind for kind, f in given.items() if values.get(f or "")])
        try:
            user_id = run_async(self._create(values, password, temporary=temporary))
        except (
            UsernameTaken,
            InvalidUsername,
            InvalidEmail,
            InvalidMobileNumber,
            IdentifierRequired,
            EmailTaken,
            MobileNumberTaken,
        ) as exc:
            raise CommandError(exc.message) from None
        self.write("Superuser created successfully.")
        if verify:
            self._prove(user_id)
        self._tell_unreached(user_id)

    def _values(
        self, interactive: bool, options: dict[str, str | None]
    ) -> dict[str, str]:
        """The fields to set, from options, variables, or prompts.

        An optional username, or identifier, left blank is left out.
        """
        model = get_user_model()
        env = Env()
        identifiers = identifier_fields(model)
        values: dict[str, str] = {}
        for field in _fields():
            value = options.get(field)
            if value is None and not interactive:
                value = env.str(f"SUPERUSER_{field.upper()}", None)
            if field == model.USERNAME_FIELD and _username_identifies():
                value = self._username(value, interactive, env)
            elif field == model.USERNAME_FIELD:
                value = self._optional_username(value, interactive)
            elif value is None and interactive:
                value = self.ask(f"{_label(field)}: ").strip()
            if value or (field not in identifiers and field != model.USERNAME_FIELD):
                values[field] = value or ""
        return values

    def _optional_username(self, given: str | None, interactive: bool) -> str | None:
        """A free username, or None: asked ones may be left blank."""
        field = get_user_model().USERNAME_FIELD
        while True:
            if given is None and interactive:
                given = self.ask(f"{_label(field)} (optional): ")
            username = (given or "").strip()
            if not username:
                return None
            if not run_async(self._taken(username)):
                return username
            if not interactive:
                raise CommandError(f"The {field} {username!r} is taken.")
            self._error(f"That {field} is already taken.")
            given = None

    def _username(self, given: str | None, interactive: bool, env: Env) -> str:
        """A free username: a given one must be; asked ones are asked again."""
        field = get_user_model().USERNAME_FIELD
        if given is not None:
            username = given.strip()
            if not username:
                raise CommandError(f"The {field} cannot be blank.")
            if run_async(self._taken(username)):
                raise CommandError(f"The {field} {username!r} is taken.")
            return username
        if not interactive:
            raise CommandError(
                f"Give the {field} with --{field} or "
                f"{env.prefix}SUPERUSER_{field.upper()}."
            )
        while True:
            username = self.ask(f"{_label(field)}: ").strip()
            if not username:
                self._error(f"{_label(field)} cannot be blank.")
            elif run_async(self._taken(username)):
                self._error(f"That {field} is already taken.")
            else:
                return username

    def _ask_password(self, values: dict[str, str]) -> str:
        """Ask twice; offer to keep a password the validators refuse."""
        while True:
            password = self.ask_secret("Password: ")
            if password != self.ask_secret("Password (again): "):
                self._error("Your passwords didn't match.")
                continue
            if not password.strip():
                self._error("Blank passwords aren't allowed.")
                continue
            try:
                check_password_rules(password, SimpleNamespace(**values))
            except InvalidPassword as exc:
                self._error("\n".join(exc.errors))
                if not _may_bypass():
                    continue
                answer = self.ask(
                    "Bypass password validation and create user anyway? [y/N]: "
                )
                if answer.strip().lower() != "y":
                    continue
            return password

    def _env_password(self, values: dict[str, str]) -> str | None:
        """``<PREFIX>SUPERUSER_PASSWORD``, which must pass the validators."""
        password = Env().str("SUPERUSER_PASSWORD", None)
        if password is not None:
            try:
                check_password_rules(password, SimpleNamespace(**values))
            except InvalidPassword as exc:
                raise CommandError(" ".join(exc.errors)) from None
        return password

    async def _taken(self, username: str) -> bool:
        """Whether a user already has ``username``."""
        async with AuthUnitOfWork() as uow:
            return await uow.users.get_by_natural_key(username) is not None

    async def _create(
        self, values: dict[str, str], password: str | None, *, temporary: bool
    ) -> UUID:
        """Store the superuser; a ``temporary`` password changes at first use."""
        fields = dict(values)
        username = fields.pop(get_user_model().USERNAME_FIELD, None)
        async with AuthUnitOfWork() as uow:
            user = await uow.users.create_superuser(username, password, **fields)
            if password is not None and temporary:
                await uow.passwords.require_change(user.id)
            await uow.commit()
        return user.id

    def _promote(self, identifier: str, *, verify: bool, interactive: bool) -> None:
        """Make the user ``identifier`` names a superuser, and staff.

        The user is shown, and the operator asked, before anything changes;
        with ``--noinput`` only a user named exactly goes on (``EXACT``).
        With ``verify``, it is made a superuser once every code asked for
        came back.

        Raises:
            CommandError: No user is named so (``_named``), the user is
                inactive, the operator says no, or a code does not come
                back.
        """
        require_named(self.operator, "createsuperuser --promote")
        user, by = run_async(self._named(identifier, verify=verify))
        if not user.is_active:
            raise CommandError(f"User {identifier!r} is inactive: it cannot log in.")
        unproved = [kind for kind, _, proved in _reached(user) if not proved]
        if verify:
            _check_senders(unproved)
        self.note(_described(user))
        if interactive:
            self._confirm()
        elif by not in EXACT:
            raise CommandError(
                f"--noinput promotes a user named by its username, its id, or "
                f"a value it proved, not by its {by}: run it without --noinput "
                "to be asked."
            )
        if verify:
            self._prove(user.id)
        if run_async(self._make_superuser(user.id)):
            self.write(f"{identifier!r} is a superuser now.")
        else:
            self.write(f"{identifier!r} is a superuser already.")
        self._tell_unreached(user.id)

    async def _named(
        self, identifier: str, *, verify: bool
    ) -> tuple[AbstractBaseUser, str]:
        """The user ``identifier`` names for ``--promote``, and how it names it.

        It is read once NFKC folds look-alike characters (``_written_as``).
        A user id, in any form, names its user (``id``), never a username.
        Digits that are no number in international form are refused: they
        name no number, and would name a username written so. An address
        or a number names the
        one user that proved it in its address or number field (``proved``),
        whatever field another user holds it in, such as a username written
        as an address; one nobody proved names nothing, so that a value put
        on an account unproved never names that account. With ``verify``,
        it names the one superuser that holds it unproved (``superuser``):
        a superuser made before 2.5, proving it. Anything else names its
        user as the other operator commands take it (``match_for_staff``):
        the username, or another field of ``AUTH.IDENTIFIERS``.

        Raises:
            CommandError: Nothing names one user so.
        """
        unknown = CommandError(
            f"No user proved {identifier!r}: name the user by its username or "
            "its id, or by an email address or mobile number it proved."
        )
        kind = _written_as(identifier)
        value = unicodedata.normalize("NFKC", identifier).strip()
        if kind == DIGITS:
            raise CommandError(
                f"{identifier!r} is written as a number: give the number in "
                "international form (+33612345678), or the user's id."
            )
        if kind == NUMBER and NUMBER not in _stored_fields():
            raise CommandError(
                "Users have no mobile number here: name the user by its "
                "username or its id."
            )
        async with AuthUnitOfWork() as uow:
            if kind == ID:
                return await self._by_id(uow, value), ID
            if kind is None:
                user, field = await match_for_staff(uow.users, identifier, self.note)
                name = get_user_model().USERNAME_FIELD
                return user, "username" if field == name else field
            if kind == EMAIL:
                holders = await uow.users.find_by_email(value)
            else:
                number = cast(str, as_mobile_number(value))
                found = await uow.users.find_by_mobile_number(number)
                holders = [] if found is None else [found]
        flag = PROOFS[kind][0]
        proved = [user for user in holders if getattr(user, flag, True)]
        if len(proved) > 1:
            raise CommandError(
                f"Several users proved {identifier!r}: name the user by its "
                "username or its id."
            )
        if proved:
            how = "proved"
            (user,) = proved
        else:
            supers = [user for user in holders if user.is_superuser]
            if not verify or len(supers) != 1:
                raise unknown
            how = "superuser"
            (user,) = supers
        self.note(f"{identifier!r} is user {user.id}, by its {LABELS[kind]}.")
        return user, how

    async def _by_id(self, uow: AuthUnitOfWork, identifier: str) -> AbstractBaseUser:
        """The user whose id ``identifier`` is, in any form; say so.

        A value that reads as an id is never a username.

        Raises:
            CommandError: No user has it.
        """
        user = await uow.users.find(UserId(UUID(identifier)))
        if user is None:
            raise CommandError(f"No user has the id {identifier}.")
        self.note(f"{identifier!r} is user {user.id}, by its id.")
        return user

    def _confirm(self) -> None:
        """Ask the operator to go on.

        Raises:
            CommandError: Any answer but ``y``, or none.
        """
        try:
            answer = self.ask("Make this user a superuser? [y/N]: ")
        except (KeyboardInterrupt, EOFError):
            answer = ""
        if answer.strip().lower() != "y":
            raise CommandError("Operation cancelled.")

    async def _make_superuser(self, user_id: UUID) -> bool:
        """Give the user every permission, and staff; False if it had them.

        ``UserStatusChanged`` records each change, with the operator as
        its actor.
        """
        async with AuthUnitOfWork() as uow:
            user = await uow.users.get(UserId(user_id))
            already = user.is_superuser and getattr(user, "is_staff", True)
            user.set_superuser(True)
            if hasattr(user, "set_staff"):
                user.set_staff(True)
            await uow.commit()
        return not already

    def _prove(self, user_id: UUID) -> None:
        """Prove each address and number of the user it has not proved yet.

        Raises:
            CommandError: A code did not come back right within
                ``AUTH.CODE_ATTEMPTS`` tries, or could not be sent.
        """
        user = run_async(_user(user_id))
        for kind, value, proved in _reached(user):
            if not proved:
                self._prove_one(user, kind, value)

    def _prove_one(self, user: AbstractBaseUser, kind: str, value: str) -> None:
        """Send a code to ``value`` now, and ask for it until it comes back.

        A cancelled prompt leaves ``value`` unproved, as wrong codes do, and
        the code no longer works.
        """
        failed = (
            f"The {LABELS[kind]} {value} is not proved. Try again with: {_retry(user)}"
        )
        if (sent := run_async(_send_proof(user.id, kind))) is not None:
            raise CommandError(f"The code could not be sent ({sent}). {failed}")
        for _ in range(auth_settings().CODE_ATTEMPTS):
            try:
                code = self.ask(f"Code sent to {value}: ").strip()
            except (KeyboardInterrupt, EOFError):
                break
            if code and run_async(_take_proof(user.id, kind, code)):
                self.write(f"The {LABELS[kind]} {value} is proved.")
                return
            self._error("That is not the code sent, or it no longer works.")
        run_async(_forget(user.id, value))
        raise CommandError(failed)

    def _tell_unreached(self, user_id: UUID) -> None:
        """Say so on stderr when no code can reach the superuser (ADR 0141)."""
        if auth_settings().CODES_TO_UNPROVED:
            return
        user = run_async(_user(user_id))
        reached = _reached(user)
        if any(proved for _, _, proved in reached):
            return
        if not reached:
            self.note(
                "Note: the superuser has no email address nor mobile number: "
                "no code reaches it, to reset its password or enroll a phone."
            )
            return
        kind, _, _ = reached[0]
        self.note(
            f"Note: no code reaches the superuser, to reset its password or "
            f"enroll a phone, until it proves its {LABELS[kind]}: run "
            f"{_retry(user)}"
        )

    def _error(self, message: str) -> None:
        """Explain what to fix before asking again."""
        self.stderr.write(f"Error: {message}\n")


async def _user(user_id: UUID) -> AbstractBaseUser:
    """The stored user."""
    async with AuthUnitOfWork() as uow:
        return await uow.users.get(UserId(user_id))


def _described(user: AbstractBaseUser) -> str:
    """The user, for the operator to check before promoting it."""
    username = getattr(user, user.USERNAME_FIELD, None)
    parts = [f"username {username!r}" if username else "no username"]
    for kind, (flag, *_) in PROOFS.items():
        value = _value(user, kind)
        proved = "proved" if getattr(user, flag, True) else "not proved"
        parts.append(
            f"{LABELS[kind]} {value!r} ({proved})" if value else f"no {LABELS[kind]}"
        )
    joined = getattr(user, "date_joined", None)
    if isinstance(joined, datetime):
        parts.append(f"joined {joined.date().isoformat()}")
    return f"User {user.id}: {', '.join(parts)}."


async def _send_proof(user_id: UUID, kind: str) -> str | None:
    """Issue a code that proves the user's ``kind`` of value, and send it now.

    Mailed, or texted, at once, not through the outbox: the operator waits
    for it, and no worker may run yet. In the user's language. None once
    sent; else the delivery's error, by its class, and the code is
    forgotten.
    """
    async with AuthUnitOfWork() as uow:
        user = await uow.users.get(UserId(user_id))
        value = cast(str, _value(user, kind))
        code = await uow.verification_codes.issue(
            PROOFS[kind][2], user_target(value, user.id), user_id=user.id
        )
        await uow.commit()
    try:
        with override(language_of(user)):
            await _deliver(user, kind, value, code)
    except DomainkitError as exc:  # The mail or text backend's refusal.
        await _forget(user.id, value)
        return type(exc).__name__
    return None


async def _deliver(user: AbstractBaseUser, kind: str, value: str, code: str) -> None:
    """Mail, or text, ``code`` to ``value``, now."""
    if kind == EMAIL:
        from domainkit.contrib.mail import deliver, render_mail

        context = code_context(code, user)
        await deliver(
            [render_mail("auth/email_verification_code", context, to=[value])]
        )
    else:
        from domainkit.contrib.notifications import SmsMessage, deliver_sms

        body = code_text(code, "verify your number")
        await deliver_sms([SmsMessage(to=value, body=body)])


async def _forget(user_id: UUID, value: str) -> None:
    """Make the codes sent to ``value`` for the user stop working."""
    async with AuthUnitOfWork() as uow:
        await uow.verification_codes.forget(user_id, [user_target(value, user_id)])
        await uow.commit()


async def _take_proof(user_id: UUID, kind: str, code: str) -> bool:
    """Whether ``code`` is the one sent to the user's ``kind``: then it is proved.

    A wrong code counts as a try (``AUTH.CODE_ATTEMPTS``).
    """
    _, verify, purpose, _ = PROOFS[kind]
    async with AuthUnitOfWork() as uow:
        user = await uow.users.get(UserId(user_id))
        value = cast(str, _value(user, kind))
        target = user_target(value, user.id)
        proven = await uow.verification_codes.check(purpose, target, code)
        right = proven is not None and proven.user_id == user.id
        if right:
            getattr(user, verify)()
        await uow.commit()
    return right
