"""What ciba keeps about a person: the sign-ins their phone was asked about.

Each request keeps the address and browser that started it. ``privacy
export`` lists a user's requests; ``privacy erase`` removes them, as
deleting the user does. The subject is the user's id.
"""

from collections.abc import Mapping
from uuid import UUID

from sqlalchemy import select

from .stores import CibaUnitOfWork
from .tables import sign_in_requests


def _user_id(subject: str) -> UUID | None:
    """The user ``subject`` names, if it is a user's id."""
    try:
        return UUID(subject)
    except ValueError:
        return None


async def export_subject(subject: str) -> Mapping[str, object]:
    """The sign-ins ``subject``'s phone was asked about: when, from where, how."""
    user_id = _user_id(subject)
    if user_id is None:
        return {}
    columns = sign_in_requests.c
    async with CibaUnitOfWork() as uow:
        rows = (
            await uow.session.execute(
                select(
                    columns.created_at,
                    columns.client_id,
                    columns.channel,
                    columns.requester_ip,
                    columns.requester_agent,
                    columns.approved_at,
                    columns.denied_at,
                )
                .where(columns.user_id == user_id)
                .order_by(columns.created_at)
            )
        ).all()
    if not rows:
        return {}
    return {
        "sign_in_requests": [
            {
                "created_at": row.created_at.isoformat(),
                "client_id": row.client_id,
                "channel": row.channel,
                "address": row.requester_ip,
                "browser": row.requester_agent,
                "approved_at": _when(row.approved_at),
                "denied_at": _when(row.denied_at),
            }
            for row in rows
        ]
    }


async def erase_subject(subject: str) -> Mapping[str, int]:
    """Remove the sign-in requests of ``subject``."""
    user_id = _user_id(subject)
    if user_id is None:
        return {}
    async with CibaUnitOfWork() as uow:
        removed = await uow.requests.forget_user(user_id)
        await uow.commit()
    return {"sign_in_requests": removed} if removed else {}


def _when(moment: object) -> str | None:
    """``moment`` in ISO 8601, if any."""
    return None if moment is None else moment.isoformat()  # type: ignore[attr-defined]
