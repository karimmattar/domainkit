"""Privacy for users: their logins exported, their credentials erased.

``privacy export`` lists a user's tokens: what kind, how and from where
each login signed in, and when (ADR 0130); never a digest.

With ``domainkit.contrib.privacy`` installed, ``privacy erase`` erases a
user through ``AbstractUser.erase_personal_data``, which also deactivates
the account and makes its password unusable, in the same commit. Then
``forget_credentials`` deletes the user's tokens (sessions, personal
tokens, refresh tokens, client certificates) and second factor (its
secret and recovery codes): secrets of an account nobody may use.
Deleting a user (``UserRepository.remove``) deletes them the same way.
"""

from collections.abc import Mapping
from datetime import datetime
from typing import TYPE_CHECKING, Final
from uuid import UUID

from sqlalchemy import select

from domainkit.apps import registry
from domainkit.db.models import model_key

from .tables import tokens
from .uow import AuthUnitOfWork
from .users import get_user_model

if TYPE_CHECKING:
    from domainkit.contrib.privacy import PersonalDataErased

AUTH: Final = "domainkit.contrib.auth"


async def forget_credentials(event: "PersonalDataErased") -> None:
    """Delete the tokens and second factor of a user whose data was erased.

    Subscribed to ``PersonalDataErased`` when privacy is installed; events
    about other models are ignored.
    """
    if not registry.is_installed(AUTH):
        return
    try:
        domain, model = model_key(get_user_model())
    except LookupError:
        return  # A user table this project's domains do not hold.
    if event.model != f"{domain}.{model}":
        return
    async with AuthUnitOfWork() as uow:
        await uow.users.forget_credentials(UUID(event.aggregate_id))
        await uow.commit()


async def export_logins(subject: str) -> Mapping[str, object]:
    """The tokens of the user ``subject`` names by id, oldest first.

    Each with its kind, name, sign-in method, agent, address, device, and
    dates; the token itself is a digest, and stays out.
    """
    try:
        user_id = UUID(subject)
    except ValueError:
        return {}
    async with AuthUnitOfWork() as uow:
        rows = (
            await uow.session.execute(
                select(
                    tokens.c.kind,
                    tokens.c.name,
                    tokens.c.method,
                    tokens.c.agent,
                    tokens.c.ip,
                    tokens.c.device_id,
                    tokens.c.created_at,
                    tokens.c.last_used_at,
                    tokens.c.expires_at,
                )
                .where(tokens.c.user_id == user_id)
                .order_by(tokens.c.created_at, tokens.c.id)
            )
        ).all()
    if not rows:
        return {}
    return {
        "tokens": [
            {
                "kind": row.kind,
                "name": row.name,
                "method": row.method,
                "agent": row.agent,
                "ip": row.ip,
                "device_id": None if row.device_id is None else str(row.device_id),
                "created_at": _when(row.created_at),
                "last_used_at": _when(row.last_used_at),
                "expires_at": _when(row.expires_at),
            }
            for row in rows
        ]
    }


def _when(value: datetime | None) -> str | None:
    """A time as ISO 8601 text, if any."""
    return None if value is None else value.isoformat()
