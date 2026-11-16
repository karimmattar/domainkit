"""Auth commands, and what they share."""

from collections.abc import Callable

from domainkit.exceptions import UpstreamError
from domainkit.management.base import CommandError, run_async

from ..models import AbstractBaseUser
from ..repositories import UserRepository
from ..validators import avalidate_password


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


def check_password_rules(password: str, user: object) -> None:
    """Run the password validators, ``async`` ones too, on a loop of their own.

    Raises:
        InvalidPassword: A rule refuses the password.
        CommandError: A rule could not check it, and refuses it for that
            (``BreachedPasswordsUnavailable`` under ``FAIL_CLOSED``).
    """
    try:
        run_async(avalidate_password(password, user))
    except UpstreamError as exc:
        raise CommandError(exc.detail) from exc
