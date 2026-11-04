"""What devices keep about a person: passkeys, bound devices, push tokens.

``privacy export`` lists them by the names the user gave them (no key, no
token); ``privacy erase`` removes them, as deleting the user does. The
subject is the user's id.
"""

from collections.abc import Mapping
from datetime import datetime
from uuid import UUID

from sqlalchemy import Table, delete, select

from .stores import DevicesUnitOfWork
from .tables import devices, passkeys, push_tokens

KEPT: tuple[tuple[str, Table], ...] = (
    ("passkeys", passkeys),
    ("devices", devices),
    ("push_tokens", push_tokens),
)
"""What a user holds here, by kind."""


def _user_id(subject: str) -> UUID | None:
    """The user ``subject`` names, if it is a user's id."""
    try:
        return UUID(subject)
    except ValueError:
        return None


def _when(value: datetime | None) -> str | None:
    """A time as ISO 8601 text, if any."""
    return None if value is None else value.isoformat()


async def export_subject(subject: str) -> Mapping[str, object]:
    """``subject``'s passkeys and devices by name, and their push platforms."""
    user_id = _user_id(subject)
    if user_id is None:
        return {}
    async with DevicesUnitOfWork() as uow:
        keys = (
            await uow.session.execute(
                select(passkeys.c.name, passkeys.c.created_at, passkeys.c.last_used_at)
                .where(passkeys.c.user_id == user_id)
                .order_by(passkeys.c.created_at)
            )
        ).all()
        bound = (
            await uow.session.execute(
                select(devices.c.name, devices.c.created_at, devices.c.last_used_at)
                .where(devices.c.user_id == user_id)
                .order_by(devices.c.created_at)
            )
        ).all()
        pushed = (
            await uow.session.execute(
                select(push_tokens.c.platform, push_tokens.c.created_at)
                .where(push_tokens.c.user_id == user_id)
                .order_by(push_tokens.c.created_at)
            )
        ).all()
    found: dict[str, object] = {}
    for kind, rows in (("passkeys", keys), ("devices", bound)):
        if rows:
            found[kind] = [
                {"name": name, "created_at": _when(made), "last_used_at": _when(used)}
                for name, made, used in rows
            ]
    if pushed:
        found["push_tokens"] = [
            {"platform": platform, "created_at": _when(made)}
            for platform, made in pushed
        ]
    return found


async def erase_subject(subject: str) -> Mapping[str, int]:
    """Remove every passkey, device, and push token of ``subject``."""
    user_id = _user_id(subject)
    if user_id is None:
        return {}
    counts: dict[str, int] = {}
    async with DevicesUnitOfWork() as uow:
        for kind, table in KEPT:
            removed = await uow.session.execute(
                delete(table).where(table.c.user_id == user_id)
            )
            if removed.rowcount:  # type: ignore[attr-defined]
                counts[kind] = removed.rowcount  # type: ignore[attr-defined]
        await uow.commit()
    return counts
