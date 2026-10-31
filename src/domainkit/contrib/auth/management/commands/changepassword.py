"""``changepassword``: set a user's password (Django's)."""

import getpass
from argparse import ArgumentParser
from collections.abc import Callable

from domainkit.management.base import BaseCommand, CommandError, run_async

from ...exceptions import InvalidPassword
from ...models import AbstractBaseUser
from ...uow import AuthUnitOfWork
from ...validators import validate_password


class Command(BaseCommand):
    """Ask twice for a new password that passes the validators, then store it."""

    help = "Change a user's password (default user: your login name here)."
    operator_actions = True

    ask_secret: Callable[[str], str] = staticmethod(getpass.getpass)
    attempts = 3

    def add_arguments(self, parser: ArgumentParser) -> None:
        """Accept the username."""
        parser.add_argument(
            "username", nargs="?", help="whose password (default: your login name)"
        )

    def handle(self, *, username: str | None, **options: object) -> None:
        """Change the password, or stop after three failed attempts."""
        username = username or getpass.getuser()
        user = run_async(self._find(username))
        if user is None:
            raise CommandError(f"User {username!r} does not exist.")
        self.write(f"Changing password for user {username!r}")
        try:
            for _ in range(self.attempts):
                password = self.ask_secret("Password: ")
                if password != self.ask_secret("Password (again): "):
                    self.stderr.write("Passwords do not match. Please try again.\n")
                    continue
                try:
                    validate_password(password, user)
                    run_async(self._save(username, password))
                except InvalidPassword as exc:  # A rule, or the history.
                    self.stderr.write("\n".join(exc.errors) + "\n")
                    continue
                self.write(f"Password changed successfully for user {username!r}")
                return
        except (KeyboardInterrupt, EOFError):
            raise CommandError("Operation cancelled.") from None
        raise CommandError(
            f"Aborting password change for user {username!r} after "
            f"{self.attempts} attempts."
        )

    async def _find(self, username: str) -> AbstractBaseUser | None:
        """The user called ``username``."""
        async with AuthUnitOfWork() as uow:
            return await uow.users.get_by_natural_key(username)

    async def _save(self, username: str, password: str) -> None:
        """Hash and store the password; the user's tokens stop working."""
        async with AuthUnitOfWork() as uow:
            user = await uow.users.get_by_natural_key(username)
            if user is None:
                raise CommandError(f"User {username!r} does not exist.")
            await uow.users.set_password(user, password)
            await uow.tokens.revoke_user(user.id)
            await uow.commit()
