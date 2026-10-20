"""``createsuperuser``: create a user with every permission (Django's).

It asks for the username, the model's ``REQUIRED_FIELDS``, and a password,
unless options give them. With ``--noinput`` it asks nothing and reads
what the options leave out from ``<PREFIX>SUPERUSER_<FIELD>`` (or a
``_FILE`` holding it), such as ``BILLING_API_SUPERUSER_PASSWORD``; without
a password the user gets an unusable one.
"""

import getpass
from argparse import ArgumentParser
from collections.abc import Callable
from types import SimpleNamespace

from domainkit import setup
from domainkit.conf.env import Env
from domainkit.management.base import BaseCommand, CommandError, run_async

from ...exceptions import (
    InvalidEmail,
    InvalidPassword,
    InvalidUsername,
    UsernameTaken,
)
from ...uow import AuthUnitOfWork
from ...users import get_user_model
from ...validators import validate_password


def _label(field: str) -> str:
    """``first_name`` as ``First name``."""
    return field.replace("_", " ").capitalize()


class Command(BaseCommand):
    """Create a superuser, interactively or from options and variables."""

    help = "Create a superuser, asking for whatever the options leave out."

    ask: Callable[[str], str] = staticmethod(input)
    ask_secret: Callable[[str], str] = staticmethod(getpass.getpass)

    def add_arguments(self, parser: ArgumentParser) -> None:
        """One option per field the user model needs, and ``--noinput``."""
        setup()
        model = get_user_model()
        for field in (model.USERNAME_FIELD, *model.REQUIRED_FIELDS):
            parser.add_argument(f"--{field}", help=f"the user's {_label(field)}")
        parser.add_argument(
            "--noinput",
            "--no-input",
            dest="interactive",
            action="store_false",
            help="ask nothing; read missing values from <PREFIX>SUPERUSER_<FIELD>",
        )

    def handle(self, *, interactive: bool, **options: str | None) -> None:
        """Gather the values, then create the user."""
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
            run_async(self._create(values, password))
        except (UsernameTaken, InvalidUsername, InvalidEmail) as exc:
            raise CommandError(exc.message) from None
        self.write("Superuser created successfully.")

    def _values(
        self, interactive: bool, options: dict[str, str | None]
    ) -> dict[str, str]:
        """The username and required fields, from options, variables, or prompts."""
        model = get_user_model()
        env = Env()
        values: dict[str, str] = {}
        for field in (model.USERNAME_FIELD, *model.REQUIRED_FIELDS):
            value = options.get(field)
            if value is None and not interactive:
                value = env.str(f"SUPERUSER_{field.upper()}", None)
            if field == model.USERNAME_FIELD:
                value = self._username(value, interactive, env)
            elif value is None:
                value = self.ask(f"{_label(field)}: ").strip() if interactive else ""
            values[field] = value
        return values

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
                validate_password(password, SimpleNamespace(**values))
            except InvalidPassword as exc:
                self._error("\n".join(exc.errors))
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
                validate_password(password, SimpleNamespace(**values))
            except InvalidPassword as exc:
                raise CommandError(" ".join(exc.errors)) from None
        return password

    async def _taken(self, username: str) -> bool:
        """Whether a user already has ``username``."""
        async with AuthUnitOfWork() as uow:
            return await uow.users.get_by_natural_key(username) is not None

    async def _create(self, values: dict[str, str], password: str | None) -> None:
        """Store the superuser."""
        fields = dict(values)
        username = fields.pop(get_user_model().USERNAME_FIELD)
        async with AuthUnitOfWork() as uow:
            await uow.users.create_superuser(username, password, **fields)
            await uow.commit()

    def _error(self, message: str) -> None:
        """Explain what to fix before asking again."""
        self.stderr.write(f"Error: {message}\n")
