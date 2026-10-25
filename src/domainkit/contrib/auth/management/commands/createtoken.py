"""``createtoken``: a long-lived token for a script or another service."""

from argparse import ArgumentParser
from datetime import timedelta

from domainkit.management.base import BaseCommand, CommandError, run_async

from ...backends import get_all_permissions
from ...events import TokenCreated
from ...settings import AUTHENTICATION, auth_settings
from ...uow import AuthUnitOfWork

TOKEN_AUTHENTICATION = f"{AUTHENTICATION}.TokenAuthentication"
NAME_MAX_LENGTH = 100
"""The length of the tokens table's ``name`` column."""


class Command(BaseCommand):
    """Print a new opaque token for a user; it is shown this once."""

    help = "Create a token for a user's scripts; it is shown once, then only hashed."
    operator_actions = True

    def add_arguments(self, parser: ArgumentParser) -> None:
        """The user, a name for the token, and its lifetime."""
        parser.add_argument("username", help="whose token")
        parser.add_argument("--name", default="cli", help="what it is for (cli)")
        parser.add_argument(
            "--scope",
            dest="scopes",
            action="append",
            metavar="PERMISSION",
            help="limit it to this permission key (repeat for more); the user "
            "must have it",
        )
        lifetime = parser.add_mutually_exclusive_group()
        lifetime.add_argument("--days", type=int, default=90, help="days it works (90)")
        lifetime.add_argument(
            "--no-expiry",
            dest="expires",
            action="store_false",
            help="works until revoked (cleartokens --user)",
        )

    def handle(
        self,
        *,
        username: str,
        name: str,
        days: int,
        expires: bool,
        scopes: list[str] | None,
    ) -> None:
        """Store the token and print it."""
        if expires and days < 1:
            raise CommandError("--days must be at least 1.")
        if len(name) > NAME_MAX_LENGTH:
            raise CommandError(f"--name has at most {NAME_MAX_LENGTH} characters.")
        if TOKEN_AUTHENTICATION not in auth_settings().AUTHENTICATION_CLASSES:
            self.stderr.write(
                f"Note: AUTH.AUTHENTICATION_CLASSES does not list "
                f"{TOKEN_AUTHENTICATION}; requests cannot use this token yet.\n"
            )
        ttl = timedelta(days=days) if expires else None
        limit = None if scopes is None else frozenset(scopes)
        self.write(run_async(self._create(username, name, ttl, limit)))

    async def _create(
        self,
        username: str,
        name: str,
        ttl: timedelta | None,
        scopes: frozenset[str] | None,
    ) -> str:
        """Issue the token; record ``TokenCreated``."""
        async with AuthUnitOfWork() as uow:
            user = await uow.users.get_by_natural_key(username)
            if user is None:
                raise CommandError(f"User {username!r} does not exist.")
            if scopes is not None and (
                missing := sorted(scopes - await get_all_permissions(user))
            ):
                raise CommandError(
                    f"{username!r} lacks these permissions: {', '.join(missing)}."
                )
            raw = await uow.tokens.issue(user.id, ttl=ttl, name=name, scopes=scopes)
            user.record(
                TokenCreated(
                    user_id=user.id,
                    name=name,
                    scopes=None if scopes is None else tuple(sorted(scopes)),
                )
            )
            await uow.commit()
        return raw
