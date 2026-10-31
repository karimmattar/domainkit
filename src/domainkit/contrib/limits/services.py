"""Changing the limits: each change a command run by a service, and audited.

Each records ``LimitSet`` or ``LimitRemoved``, which ``contrib.audit``
writes to the trail with the actor, so every change to what a subject may
move says who made it::

    await set_limit(
        SetLimit(
            code="wallet.daily",
            kind="holder",
            currency="EGP",
            window=Window.DAY,
            max_amount=Money.of("50000", "EGP"),
        ),
        LimitsUnitOfWork(),
    )
"""

from uuid import UUID

from domainkit.conf import settings
from domainkit.ddd import Command
from domainkit.money import Money

from .models import Direction, Limit, LimitId, Window, check_kind, limit_currency
from .uow import LimitsUnitOfWork


class SetLimit(Command):
    """Make the limit of this key, or set the bounds of the one there is.

    ``subject`` ``""`` is every subject of ``kind``; ``time_zone`` None is
    ``TIME_ZONE`` for a new limit, and the limit's own for one there is.
    """

    code: str
    kind: str
    currency: str
    subject: str = ""
    direction: Direction = Direction.OUT
    per_transaction: Money | None = None
    window: Window | None = None
    max_amount: Money | None = None
    max_count: int | None = None
    time_zone: str | None = None
    enabled: bool = True
    note: str = ""


class ChangeLimit(Command):
    """Set a limit's bounds, all of them: one left out is None (no bound)."""

    limit_id: UUID
    per_transaction: Money | None = None
    window: Window | None = None
    max_amount: Money | None = None
    max_count: int | None = None
    time_zone: str | None = None
    """None keeps the limit's."""
    enabled: bool = True
    note: str = ""


class RemoveLimit(Command):
    """Remove a limit: a default of the settings with its key applies again."""

    limit_id: UUID


def _bounds(command: SetLimit | ChangeLimit, zone: str) -> dict[str, object]:
    """The bounds ``command`` sets, as ``Limit.change`` takes them."""
    return {
        "per_transaction": command.per_transaction,
        "window": command.window,
        "max_amount": command.max_amount,
        "max_count": command.max_count,
        "time_zone": command.time_zone or zone,
        "enabled": command.enabled,
        "note": command.note,
    }


async def define_limit(command: SetLimit, uow: LimitsUnitOfWork) -> Limit:
    """Make a limit; another of its key is ``limit_taken`` (409).

    Raises:
        LimitTaken: A limit has its code, kind, direction, currency, and
            subject.
        InvalidLimit, UnknownCurrency: As ``Limit.define``.
    """
    limit = Limit.define(
        command.code,
        command.kind,
        command.currency,
        direction=command.direction,
        subject=command.subject,
        **_bounds(command, settings.TIME_ZONE),  # type: ignore[arg-type]
    )
    async with uow:
        await uow.limits.add(limit)
        await uow.commit()
    return limit


async def set_limit(command: SetLimit, uow: LimitsUnitOfWork) -> Limit:
    """Make the limit of the command's key, or set the bounds of the one there is.

    Raises:
        InvalidLimit, UnknownCurrency: As ``Limit.define``.
        LimitTaken: Another request made it meanwhile.
    """
    unit = limit_currency(command.currency)
    async with uow:
        limit = await uow.limits.by_key(
            command.code,
            check_kind(command.kind),
            Direction(command.direction),
            unit.code,
            command.subject,
        )
        if limit is None:
            limit = Limit.define(
                command.code,
                command.kind,
                unit.code,
                direction=command.direction,
                subject=command.subject,
                **_bounds(command, settings.TIME_ZONE),  # type: ignore[arg-type]
            )
            await uow.limits.add(limit)
        else:
            limit.change(**_bounds(command, limit.time_zone))  # type: ignore[arg-type]
        await uow.commit()
    return limit


async def change_limit(command: ChangeLimit, uow: LimitsUnitOfWork) -> Limit:
    """Set a limit's bounds.

    Raises:
        LimitNotFound: No limit has that id.
        InvalidLimit: As ``Limit.change``.
    """
    async with uow:
        limit = await uow.limits.get(LimitId(command.limit_id))
        limit.change(**_bounds(command, limit.time_zone))  # type: ignore[arg-type]
        await uow.commit()
    return limit


async def remove_limit(command: RemoveLimit, uow: LimitsUnitOfWork) -> None:
    """Remove a limit.

    Raises:
        LimitNotFound: No limit has that id.
    """
    async with uow:
        limit = await uow.limits.get(LimitId(command.limit_id))
        limit.remove()
        await uow.limits.remove(limit)
        await uow.commit()
