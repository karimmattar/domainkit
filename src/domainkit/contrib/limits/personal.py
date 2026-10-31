"""What limits keep about a person: limits set for them, and what they moved.

A subject of kind ``holder`` or ``actor`` is a person's id. ``privacy
export`` lists the limits set for them and their counted movements;
``privacy erase`` removes both: the limits through the unit of work, so the
audit trail records each removal, and the usage as rows. The person's
movements stop counting against any window.
"""

from collections.abc import Mapping
from typing import Any

from sqlalchemy import delete, func, select

from domainkit.db import connections
from domainkit.ddd import F

from .tables import limits, subjects, usage
from .uow import LimitsUnitOfWork, limits_alias


async def export_subject(subject: str) -> Mapping[str, object]:
    """The limits set for ``subject``, and its movements counted, by kind."""
    async with connections.engine(limits_alias()).connect() as connection:
        set_for = (
            await connection.execute(
                select(limits)
                .where(limits.c.subject == subject)
                .order_by(limits.c.code)
            )
        ).all()
        counted = (
            await connection.execute(
                select(
                    subjects.c.kind,
                    subjects.c.direction,
                    subjects.c.currency,
                    func.count(usage.c.id),
                    func.min(usage.c.at),
                    func.max(usage.c.at),
                )
                .join(usage, usage.c.subject_id == subjects.c.id)
                .where(subjects.c.subject == subject)
                .group_by(subjects.c.kind, subjects.c.direction, subjects.c.currency)
                .order_by(subjects.c.kind, subjects.c.direction, subjects.c.currency)
            )
        ).all()
    if not set_for and not counted:
        return {}
    found: dict[str, Any] = {
        "limits": [
            {
                "code": row.code,
                "kind": row.kind,
                "direction": row.direction.value,
                "currency": row.currency,
                "window": None if row.window is None else row.window.value,
            }
            for row in set_for
        ],
        "usage": [
            {
                "kind": kind,
                "direction": direction.value,
                "currency": currency,
                "movements": count,
                "first": first.isoformat(),
                "last": last.isoformat(),
            }
            for kind, direction, currency, count, first, last in counted
        ],
    }
    return found


async def erase_subject(subject: str) -> Mapping[str, int]:
    """Remove the limits set for ``subject``, and its counted movements."""
    async with LimitsUnitOfWork() as uow:
        removed = await uow.limits.filter(F("subject") == subject)
        for limit in removed:
            limit.remove()
            await uow.limits.remove(limit)
        await uow.commit()
    owned = select(subjects.c.id).where(subjects.c.subject == subject)
    async with connections.engine(limits_alias()).begin() as connection:
        erased = await connection.execute(
            delete(usage).where(usage.c.subject_id.in_(owned.scalar_subquery()))
        )
        await connection.execute(delete(subjects).where(subjects.c.subject == subject))
    return {"limits": len(removed), "usage": erased.rowcount}
