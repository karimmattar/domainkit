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
as one that signed up in the application; an address or a number names
the user only once it proved it, as codes go. In staging and production
it needs a named operator.
"""

import getpass
from argparse import ArgumentParser, BooleanOptionalAction
from collections.abc import Callable
from types import SimpleNamespace
from typing import Final
from uuid import UUID

from domainkit import setup
from domainkit.apps import registry
from domainkit.conf import settings
from domainkit.conf.env import Env
from domainkit.conf.global_settings import DEPLOYED_ENVIRONMENTS
from domainkit.i18n import override
from domainkit.management.base import BaseCommand, CommandError, run_async
from domainkit.management.operators import require_named

from ...codes import EMAIL_VERIFICATION, MOBILE_VERIFICATION, target_of
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
from ...models import AbstractBaseUser, UserId
from ...recovery import code_context, code_text, language_of
from ...settings import auth_settings, identifier_fields
from ...uow import AuthUnitOfWork
from ...users import get_user_model
from .. import check_password_rules, match_for_staff

MAIL: Final = "domainkit.contrib.mail"
NOTIFICATIONS: Final = "domainkit.contrib.notifications"
PROOFS: Final = {
    "email": ("email_verified", "verify_email", EMAIL_VERIFICATION),
    "mobile_number": (
        "mobile_number_verified",
        "verify_mobile_number",
        MOBILE_VERIFICATION,
    ),
}
"""For each field codes go to: its proof's flag, the method that sets it, and
the purpose of the code that proves it."""
LABELS: Final = {"email": "email address", "mobile_number": "mobile number"}


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


def _reached(user: AbstractBaseUser) -> list[tuple[str, str, bool]]:
    """Each address or number codes would go to: the field, the value, proved.

    A user model without a field's ``*_verified`` flag counts it proved, as
    ``reaches_proved`` does.
    """
    found = []
    for field, (flag, _, _) in PROOFS.items():
        value = getattr(user, field, None)
        if value:
            found.append((field, str(value), bool(getattr(user, flag, True))))
    return found


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
            self._promote(promote, verify=verify)
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

    def _promote(self, identifier: str, *, verify: bool) -> None:
        """Make the user ``identifier`` names a superuser, and staff.

        Its address or number, when that is what names it, must be proved,
        or proved now with ``verify``: a value names the user who proved it
        (ADR 0141). It is made a superuser once every code asked for came
        back.
        """
        require_named(self.operator, "createsuperuser --promote")
        user_id, field = run_async(self._named(identifier))
        if field is not None and not verify:
            raise CommandError(
                f"The user never proved the {LABELS[field]} {identifier!r}: "
                "add --verify to prove it with a code, or name the user by "
                "a value it proved."
            )
        if verify:
            self._prove(user_id)
        if run_async(self._make_superuser(user_id)):
            self.write(f"{identifier!r} is a superuser now.")
        else:
            self.write(f"{identifier!r} is a superuser already.")
        self._tell_unreached(user_id)

    async def _named(self, identifier: str) -> tuple[UUID, str | None]:
        """The user ``identifier`` names, and its field, if the user never proved it.

        Raises:
            CommandError: No user has it, or the user is inactive.
        """
        async with AuthUnitOfWork() as uow:
            user, field = await match_for_staff(uow.users, identifier, self.note)
        if not user.is_active:
            raise CommandError(f"User {identifier!r} is inactive: it cannot log in.")
        flag = PROOFS[field][0] if field in PROOFS else None
        unproved = flag is not None and not getattr(user, flag, True)
        return user.id, field if unproved else None

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
                ``AUTH.CODE_ATTEMPTS`` tries, or cannot be sent.
        """
        user = run_async(_user(user_id))
        for field, value, proved in _reached(user):
            if not proved:
                self._prove_one(user_id, field, value)

    def _prove_one(self, user_id: UUID, field: str, value: str) -> None:
        """Send a code to ``value`` now, and ask for it until it comes back.

        A cancelled prompt leaves ``value`` unproved, as wrong codes do.
        """
        run_async(_send_proof(user_id, field))
        for _ in range(auth_settings().CODE_ATTEMPTS):
            try:
                code = self.ask(f"Code sent to {value}: ").strip()
            except (KeyboardInterrupt, EOFError):
                break
            if code and run_async(_take_proof(user_id, field, code)):
                self.write(f"The {LABELS[field]} {value} is proved.")
                return
            self._error("That is not the code sent, or it no longer works.")
        raise CommandError(
            f"The {LABELS[field]} {value} is not proved. Try again with: "
            f"createsuperuser --promote {value} --verify"
        )

    def _tell_unreached(self, user_id: UUID) -> None:
        """Say so on stderr when no code can reach the superuser (ADR 0141)."""
        if auth_settings().CODES_TO_UNPROVED:
            return
        reached = _reached(run_async(_user(user_id)))
        if any(proved for _, _, proved in reached):
            return
        if not reached:
            self.note(
                "Note: the superuser has no email address nor mobile number: "
                "no code reaches it, to reset its password or enroll a phone."
            )
            return
        field, value, _ = reached[0]
        self.note(
            f"Note: no code reaches the superuser, to reset its password or "
            f"enroll a phone, until it proves its {LABELS[field]}: run "
            f"createsuperuser --promote {value} --verify"
        )

    def _error(self, message: str) -> None:
        """Explain what to fix before asking again."""
        self.stderr.write(f"Error: {message}\n")


async def _user(user_id: UUID) -> AbstractBaseUser:
    """The stored user."""
    async with AuthUnitOfWork() as uow:
        return await uow.users.get(UserId(user_id))


async def _send_proof(user_id: UUID, field: str) -> None:
    """Issue a code that proves the user's ``field``, and send it now.

    Mailed, or texted, at once, not through the outbox: the operator waits
    for it, and no worker may run yet. In the user's language.

    Raises:
        CommandError: The app that sends it is not installed.
    """
    sender = MAIL if field == "email" else NOTIFICATIONS
    if not registry.is_installed(sender):
        raise CommandError(f"Sending the code needs {sender} in INSTALLED_DOMAINS.")
    async with AuthUnitOfWork() as uow:
        user = await uow.users.get(UserId(user_id))
        value = str(getattr(user, field))
        code = await uow.verification_codes.issue(
            PROOFS[field][2], target_of(value), user_id=user.id
        )
        await uow.commit()
    with override(language_of(user)):
        if field == "email":
            from domainkit.contrib.mail import deliver, render_mail

            context = code_context(code, user)
            mail = render_mail("auth/email_verification_code", context, to=[value])
            await deliver([mail])
        else:
            from domainkit.contrib.notifications import SmsMessage, deliver_sms

            body = code_text(code, "verify your number")
            await deliver_sms([SmsMessage(to=value, body=body)])


async def _take_proof(user_id: UUID, field: str, code: str) -> bool:
    """Whether ``code`` is the one sent to the user's ``field``: then it is proved.

    A wrong code counts as a try (``AUTH.CODE_ATTEMPTS``).
    """
    _, verify, purpose = PROOFS[field]
    async with AuthUnitOfWork() as uow:
        user = await uow.users.get(UserId(user_id))
        value = str(getattr(user, field))
        proven = await uow.verification_codes.check(purpose, target_of(value), code)
        right = proven is not None and proven.user_id == user.id
        if right:
            getattr(user, verify)()
        await uow.commit()
    return right
