"""What phones keep about a person: the numbers they proved, and codes sent.

``privacy export`` lists a user's numbers; ``privacy erase`` removes them
and any code still pending, as deleting the user does. The subject is the
user's id.
"""

from collections.abc import Mapping
from uuid import UUID

from sqlalchemy import delete, select

from .stores import PhonesUnitOfWork
from .tables import phone_codes, phones


def _user_id(subject: str) -> UUID | None:
    """The user ``subject`` names, if it is a user's id."""
    try:
        return UUID(subject)
    except ValueError:
        return None


async def export_subject(subject: str) -> Mapping[str, object]:
    """The numbers ``subject`` proved, and when."""
    user_id = _user_id(subject)
    if user_id is None:
        return {}
    async with PhonesUnitOfWork() as uow:
        rows = (
            await uow.session.execute(
                select(phones.c.phone, phones.c.verified_at)
                .where(phones.c.user_id == user_id)
                .order_by(phones.c.verified_at)
            )
        ).all()
    if not rows:
        return {}
    return {
        "phones": [
            {"phone": phone, "verified_at": verified.isoformat()}
            for phone, verified in rows
        ]
    }


async def erase_subject(subject: str) -> Mapping[str, int]:
    """Remove the numbers ``subject`` proved and the codes sent to them."""
    user_id = _user_id(subject)
    if user_id is None:
        return {}
    async with PhonesUnitOfWork() as uow:
        numbers = await uow.session.execute(
            delete(phones).where(phones.c.user_id == user_id)
        )
        codes = await uow.session.execute(
            delete(phone_codes).where(phone_codes.c.user_id == user_id)
        )
        await uow.commit()
    counts = {"phones": numbers.rowcount, "phone_codes": codes.rowcount}  # type: ignore[attr-defined]
    return {kind: count for kind, count in counts.items() if count}
