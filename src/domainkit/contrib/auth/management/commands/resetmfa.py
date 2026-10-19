"""``resetmfa``: turn a user's second factor off, for one who lost it and its codes."""

from argparse import ArgumentParser

from domainkit.management.base import BaseCommand, CommandError, run_async

from ...events import MfaDisabled, UserLoggedOut
from ...uow import AuthUnitOfWork


class Command(BaseCommand):
    """Remove a user's authenticator and recovery codes, and end their sessions."""

    help = (
        "Turn a user's second factor off (a lost authenticator and lost recovery "
        "codes); every session of the user ends. Check who is asking first."
    )

    def add_arguments(self, parser: ArgumentParser) -> None:
        """The user."""
        parser.add_argument("username", help="whose second factor")

    def handle(self, *, username: str) -> None:
        """Remove the factor and say so."""
        if run_async(self._reset(username)):
            self.write(f"The second factor of {username!r} is off; log in to set one.")
        else:
            self.write(f"{username!r} had no second factor.")

    async def _reset(self, username: str) -> bool:
        """Remove the factor and the sessions; record ``MfaDisabled``."""
        async with AuthUnitOfWork() as uow:
            user = await uow.users.get_by_natural_key(username)
            if user is None:
                raise CommandError(f"User {username!r} does not exist.")
            if not await uow.mfa.remove(user.id):
                return False
            await uow.tokens.revoke_user(user.id)
            user.record(MfaDisabled(user_id=user.id))
            user.record(UserLoggedOut(user_id=user.id, everywhere=True))
            await uow.commit()
        return True
