"""``cleartokens``: delete expired tokens, or every token of one user."""

from argparse import ArgumentParser

from domainkit.management.base import BaseCommand, CommandError, run_async

from ...events import UserLoggedOut
from ...settings import MAX_STEP_UP_AGE
from ...tokens import utc_now
from ...uow import AuthUnitOfWork


class Command(BaseCommand):
    """Purge expired tokens (Django's ``clearsessions``), or log a user out."""

    help = "Delete expired tokens; with --user, every token of that user."
    operator_actions = True

    def add_arguments(self, parser: ArgumentParser) -> None:
        """Optionally, the user to log out everywhere."""
        parser.add_argument(
            "--user", metavar="USERNAME", help="delete all of this user's tokens"
        )

    def handle(self, *, user: str | None) -> None:
        """Delete the tokens and say how many."""
        if user is None:
            count = run_async(self._expired())
            self.write(f"Deleted {count} expired token(s).")
        else:
            count = run_async(self._of_user(user))
            self.write(f"Deleted {count} token(s) of {user!r}.")

    async def _expired(self) -> int:
        """Delete tokens past their lifetime; step-ups and authorizations too.

        Step-ups older than any ``StepUp`` takes, and transactions whose
        authorization no longer works, go with them (``auth.tasks``).
        """
        async with AuthUnitOfWork() as uow:
            count = await uow.tokens.clear_expired()
            await uow.step_ups.clear_older(utc_now() - MAX_STEP_UP_AGE)
            await uow.sca.clear_expired()
            await uow.commit()
        return count

    async def _of_user(self, username: str) -> int:
        """Delete every token of ``username``; record ``UserLoggedOut``."""
        async with AuthUnitOfWork() as uow:
            user = await uow.users.get_by_natural_key(username)
            if user is None:
                raise CommandError(f"User {username!r} does not exist.")
            count = await uow.tokens.revoke_user(user.id)
            user.record(UserLoggedOut(user_id=user.id, everywhere=True))
            await uow.commit()
        return count
