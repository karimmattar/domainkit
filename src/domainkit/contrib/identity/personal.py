"""What identity keeps about a person: their accounts at providers.

``privacy export`` lists a user's linked identities; ``privacy erase``
unlinks them, as deleting the user does. The subject is the user's id.
"""

from collections.abc import Mapping
from uuid import UUID

from sqlalchemy import delete, select

from .stores import IdentityUnitOfWork
from .tables import identities


def _user_id(subject: str) -> UUID | None:
    """The user ``subject`` names, if it is a user's id."""
    try:
        return UUID(subject)
    except ValueError:
        return None


async def export_subject(subject: str) -> Mapping[str, object]:
    """The identities linked to ``subject``: provider, its id, email, when."""
    user_id = _user_id(subject)
    if user_id is None:
        return {}
    async with IdentityUnitOfWork() as uow:
        rows = (
            await uow.session.execute(
                select(
                    identities.c.provider,
                    identities.c.subject,
                    identities.c.email,
                    identities.c.created_at,
                )
                .where(identities.c.user_id == user_id)
                .order_by(identities.c.provider)
            )
        ).all()
    if not rows:
        return {}
    return {
        "identities": [
            {
                "provider": provider,
                "subject": at_provider,
                "email": email,
                "linked_at": linked.isoformat(),
            }
            for provider, at_provider, email, linked in rows
        ]
    }


async def erase_subject(subject: str) -> Mapping[str, int]:
    """Unlink every identity of ``subject``."""
    user_id = _user_id(subject)
    if user_id is None:
        return {}
    async with IdentityUnitOfWork() as uow:
        unlinked = await uow.session.execute(
            delete(identities).where(identities.c.user_id == user_id)
        )
        await uow.commit()
    count: int = unlinked.rowcount  # type: ignore[attr-defined]
    return {"identities": count} if count else {}
