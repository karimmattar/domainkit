"""Auth commands, and what they share."""

from collections.abc import Callable

from domainkit.management.base import CommandError

from ..models import AbstractBaseUser
from ..repositories import UserRepository


async def user_for_staff(
    users: UserRepository, value: str, write: Callable[[str], object]
) -> AbstractBaseUser:
    """The user an operator names (``match_for_staff``); say which field matched.

    ``write`` takes the line (``BaseCommand.note``: stderr, so a command
    whose stdout is a token keeps it clean). It names the user's id and
    the field, so an operator who gave an address or a number sees whose
    account the command acts on.

    Raises:
        CommandError: No user has that identifier.
    """
    match = await users.match_for_staff(value)
    if match is None:
        raise CommandError(f"User {value!r} does not exist.")
    write(f"{value!r} is user {match.user.id}, by its {match.field}.")
    return match.user
