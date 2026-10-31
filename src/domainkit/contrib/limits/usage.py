"""Checking and counting movements against limits, in the caller's transaction.

A use case counts what it moves in its own unit of work, so the count
commits with the movement, or rolls back with it::

    async with payments_uow as uow:
        wallet = await uow.wallets.get_for_update(wallet_id)
        wallet.pay(amount)
        await consume(
            uow,
            Movement(amount, {"holder": str(wallet.owner_id)}),
            key=f"payments:{payment_id}",
        )
        await uow.commit()

``consume`` refuses a movement over a limit (``LimitExceeded``,
``TransactionLimitExceeded``) and counts one within them. Two movements
of one subject that a window bounds are counted one after the other:
each locks the subject's row (``subjects``), then sums its usage in each
window, on the database's clock. Locks are taken in the order of the
subjects' keys; take them after the rows of the use case's own. Every
movement is counted, a subject no window bounds too, without a lock: so
a limit set, or an exemption lifted, partway through a window counts
what moved earlier in it (SEC-15b-5). ``check`` says whether a movement would
pass, counting nothing; ``release`` gives back what was counted under a
key (a refund, a reversal). See ADR 0123.
"""

from collections import defaultdict
from collections.abc import AsyncIterator, Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any, Final, Self
from uuid import UUID, uuid4

from sqlalchemy import (
    ColumnElement,
    Row,
    and_,
    bindparam,
    delete,
    exists,
    func,
    insert,
    or_,
    select,
    update,
)
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.ext.asyncio import AsyncSession

from domainkit import clock
from domainkit.apps import registry
from domainkit.conf import settings
from domainkit.db import SqlAlchemyUnitOfWork, connections, dbclock
from domainkit.ddd import ReadOnlyError
from domainkit.exceptions import ImproperlyConfigured
from domainkit.money import Money

from .exceptions import LimitExceeded, TransactionLimitExceeded
from .models import Direction, Limit, Movement, Window, limit_currency
from .settings import LimitDefault, LimitsSettings
from .tables import limits, subjects, usage
from .uow import limits_alias

LOCK: Final = "domainkit.limits"
"""On SQLite, the name of the lock a count takes: the database's write lock."""
PURGE_BATCH: Final = 5000
"""Usage rows ``purge_usage`` deletes at a time."""

type SubjectKey = tuple[str, Direction, str, str]
"""A subject's key: its kind, the direction, the currency, and the subject."""


@dataclass(frozen=True, slots=True)
class Rule:
    """A limit as it is enforced: a stored one, or a default of the settings."""

    code: str
    kind: str
    direction: Direction
    currency: str
    subject: str
    per_transaction: Decimal | None
    window: Window | None
    max_amount: Decimal | None
    max_count: int | None
    time_zone: str
    enabled: bool
    stored: bool
    """Whether it is a row of the table, not a default of the settings."""

    @classmethod
    def of(cls, limit: Limit) -> Self:
        """A stored limit, as a rule."""
        return cls(
            code=limit.code,
            kind=limit.kind,
            direction=limit.direction,
            currency=limit.currency,
            subject=limit.subject,
            per_transaction=_amount(limit.per_transaction),
            window=limit.window,
            max_amount=_amount(limit.max_amount),
            max_count=limit.max_count,
            time_zone=limit.time_zone,
            enabled=limit.enabled,
            stored=True,
        )

    def covers(self, key: SubjectKey) -> bool:
        """Whether this limit bounds the subject of ``key``."""
        kind, direction, currency, subject = key
        return (self.kind, self.direction, self.currency) == (
            kind,
            direction,
            currency,
        ) and self.subject in ("", subject)

    @property
    def rank(self) -> tuple[bool, bool]:
        """What wins among limits of one code: for one subject, then stored."""
        return self.subject != "", self.stored


@dataclass(frozen=True, slots=True)
class Usage:
    """What one subject used of one limit in its current window.

    ``since`` is when the window started, None for a limit with no window
    (``per_transaction`` alone); ``used`` and ``count`` are then zero.
    """

    code: str
    kind: str
    direction: Direction
    subject: str
    """The limit's subject: the subject's own, or ``""`` for every one."""
    window: Window | None
    since: datetime | None
    used: Money
    count: int
    per_transaction: Money | None
    max_amount: Money | None
    max_count: int | None
    stored: bool

    @property
    def remaining(self) -> Money | None:
        """How much more may move in the window; None without ``max_amount``."""
        if self.max_amount is None:
            return None
        return max(self.max_amount - self.used, Money.zero(self.used.currency))


def _amount(money: Money | None) -> Decimal | None:
    """The amount of ``money``; None stays None."""
    return None if money is None else money.amount


def config() -> LimitsSettings:
    """``LIMITS``'s settings."""
    return registry.settings(LimitsSettings)


def _decimal(text: str | None) -> Decimal | None:
    """A default's amount, from its text."""
    return None if text is None else Decimal(text)


def default_rule(default: LimitDefault) -> Rule:
    """``default`` as a rule; ``LIMITS.DEFAULTS`` were checked by ``setup()``."""
    return Rule(
        code=default.code,
        kind=default.kind,
        direction=Direction(default.direction),
        currency=default.currency,
        subject=default.subject,
        per_transaction=_decimal(default.per_transaction),
        window=None if default.window is None else Window(default.window),
        max_amount=_decimal(default.max_amount),
        max_count=default.max_count,
        time_zone=default.time_zone or settings.TIME_ZONE,
        enabled=True,
        stored=False,
    )


def stored_rule(row: Row[Any]) -> Rule:
    """A row of ``limits`` as a rule."""
    return Rule(
        code=row.code,
        kind=row.kind,
        direction=row.direction,
        currency=row.currency,
        subject=row.subject,
        per_transaction=row.per_transaction,
        window=row.window,
        max_amount=row.max_amount,
        max_count=row.max_count,
        time_zone=row.time_zone,
        enabled=row.enabled,
        stored=True,
    )


def applicable(key: SubjectKey, rules: Iterable[Rule]) -> list[Rule]:
    """The limits that bound the subject of ``key``, one per code, by code.

    Of the limits of one code, one for the subject wins over one for every
    subject, and a stored one over a default of the settings. A disabled
    winner bounds nothing: disabling a limit for one subject exempts it.
    """
    chosen: dict[str, Rule] = {}
    for rule in rules:
        if rule.covers(key):
            current = chosen.get(rule.code)
            if current is None or rule.rank > current.rank:
                chosen[rule.code] = rule
    return sorted((rule for rule in chosen.values() if rule.enabled), key=_code)


def _code(rule: Rule) -> str:
    """A rule's code, to sort by."""
    return rule.code


async def _rules(session: AsyncSession, keys: Iterable[SubjectKey]) -> list[Rule]:
    """The stored limits and the defaults that may bound the subjects of ``keys``."""
    keys = list(keys)
    found = await session.execute(
        select(limits).where(
            or_(
                *(
                    and_(
                        limits.c.kind == kind,
                        limits.c.direction == direction,
                        limits.c.currency == currency,
                        limits.c.subject.in_(("", subject)),
                    )
                    for kind, direction, currency, subject in keys
                )
            )
        )
    )
    stored = [stored_rule(row) for row in found]
    shadowed = {_key_of(rule) for rule in stored}
    defaults = [
        rule
        for rule in map(default_rule, config().DEFAULTS)
        if _key_of(rule) not in shadowed and any(rule.covers(key) for key in keys)
    ]
    return stored + defaults


def _key_of(rule: Rule) -> tuple[str, str, Direction, str, str]:
    """A limit's key: its code, kind, direction, currency, and subject."""
    return rule.code, rule.kind, rule.direction, rule.currency, rule.subject


def _tally(movements: Sequence[Movement]) -> dict[SubjectKey, list[Decimal]]:
    """Each subject's movements: their amounts, by the subject's key.

    Raises:
        TypeError: A movement is not a ``Movement``.
        ValueError: There are none.
        InvalidLimit: One's currency has more places than limits keep.
    """
    if not movements:
        raise ValueError("Give at least one movement.")
    amounts: defaultdict[SubjectKey, list[Decimal]] = defaultdict(list)
    for movement in movements:
        if not isinstance(movement, Movement):
            raise TypeError(f"Expected a Movement, not {type(movement).__name__}.")
        code = limit_currency(movement.amount.currency.code).code
        for kind, subject in movement.subjects.items():
            amounts[kind, Direction(movement.direction), code, subject].append(
                movement.amount.amount
            )
    return dict(sorted(amounts.items()))


def _session(uow: SqlAlchemyUnitOfWork, *, writes: bool) -> AsyncSession:
    """The open session of ``uow``, if it is on the limits' database.

    Raises:
        ImproperlyConfigured: ``uow`` is on another database.
        ReadOnlyError: ``writes``, and ``uow`` is read-only.
        RuntimeError: ``uow`` is not open (``async with uow:``).
    """
    alias = limits_alias()
    if uow.alias != alias and uow.alias not in settings.DATABASES[alias].REPLICAS:
        raise ImproperlyConfigured(
            f"Limits live in {alias!r} (DATABASE_ROUTING['limits']), and the unit "
            f"of work in {uow.alias!r}: count movements in a unit of work on the "
            "limits' database."
        )
    if writes and uow.readonly:
        raise ReadOnlyError(
            f"{type(uow).__name__} is read-only; movements are counted in one "
            "that writes."
        )
    session: AsyncSession | None = getattr(uow, "session", None)
    if session is None:
        raise RuntimeError("Count movements inside the unit of work: async with uow.")
    return session


def _transactions(tallies: dict[SubjectKey, list[Decimal]], rules: list[Rule]) -> None:
    """Refuse a movement over a limit's ``per_transaction``.

    Raises:
        TransactionLimitExceeded: One is.
    """
    for key, amounts in tallies.items():
        for rule in applicable(key, rules):
            if rule.per_transaction is not None and max(amounts) > rule.per_transaction:
                raise TransactionLimitExceeded(
                    "The amount is more than one movement may be.", limit=rule.code
                )


def _windowed(key: SubjectKey, rules: list[Rule]) -> list[Rule]:
    """The limits of ``key`` that count over a window."""
    return [rule for rule in applicable(key, rules) if rule.window is not None]


async def _used(
    session: AsyncSession, subject_id: UUID, now: datetime, rules: list[Rule]
) -> list[tuple[Decimal, int]]:
    """What the subject used in each rule's window: the sum and the count."""
    windows = [rule.window for rule in rules if rule.window is not None]
    starts = [
        window.start(now, rule.time_zone)
        for window, rule in zip(windows, rules, strict=True)
    ]
    columns: list[ColumnElement[Any]] = []
    for window, start in zip(windows, starts, strict=True):
        since = usage.c.at > start if window.rolling else usage.c.at >= start
        columns.append(func.coalesce(func.sum(usage.c.amount).filter(since), 0))
        columns.append(func.count().filter(since))
    row = (
        await session.execute(
            select(*columns).where(
                usage.c.subject_id == subject_id,
                usage.c.released_at.is_(None),
                usage.c.at >= min(starts),
            )
        )
    ).one()
    return [(Decimal(row[2 * n]), int(row[2 * n + 1])) for n in range(len(rules))]


def _judge(
    rules: list[Rule], used: list[tuple[Decimal, int]], amounts: list[Decimal]
) -> None:
    """Refuse ``amounts`` if any rule's window cannot take them on top of ``used``.

    Raises:
        LimitExceeded: One cannot.
    """
    total = sum(amounts, Decimal(0))
    for rule, (spent, count) in zip(rules, used, strict=True):
        window = str(rule.window)
        if rule.max_amount is not None and spent + total > rule.max_amount:
            raise LimitExceeded(
                "The amount is more than a limit allows in its period.",
                limit=rule.code,
                window=window,
            )
        if rule.max_count is not None and count + len(amounts) > rule.max_count:
            raise LimitExceeded(
                "There have been as many of these as a limit allows in its period.",
                limit=rule.code,
                window=window,
            )


def _upsert(dialect: str) -> Any:  # noqa: ANN401 - the dialect's insert.
    """The ``insert`` of ``dialect`` that has ``ON CONFLICT``."""
    return (postgresql if dialect == "postgresql" else sqlite).insert


async def _lock(session: AsyncSession, key: SubjectKey) -> tuple[UUID, datetime]:
    """Lock the subject's row, made if new; its id, and now on the database's clock.

    One statement: an insert that, when the row is there, updates its
    ``touched_at`` instead, which locks it until the transaction ends.
    """
    dialect = session.get_bind().dialect.name
    kind, direction, currency, subject = key
    now = dbclock.now(dialect)
    statement = _upsert(dialect)(subjects).values(
        id=uuid4(),
        kind=kind,
        direction=direction,
        currency=currency,
        subject=subject,
        touched_at=now,
    )
    statement = statement.on_conflict_do_update(
        index_elements=[
            subjects.c.kind,
            subjects.c.direction,
            subjects.c.currency,
            subjects.c.subject,
        ],
        set_={"touched_at": statement.excluded.touched_at},
    ).returning(subjects.c.id, subjects.c.touched_at)
    row = (await session.execute(statement)).one()
    return row.id, row.touched_at


async def _ensure(session: AsyncSession, keys: list[SubjectKey]) -> None:
    """Make the rows of the subjects of ``keys`` that have none, locking none.

    One statement; a row that is there stays as it is, unlocked: no window
    bounds these subjects, so their movements need not wait for each other.
    """
    if not keys:
        return
    dialect = session.get_bind().dialect.name
    now = dbclock.now(dialect)
    statement = _upsert(dialect)(subjects).values(
        [
            {
                "id": uuid4(),
                "kind": kind,
                "direction": direction,
                "currency": currency,
                "subject": subject,
                "touched_at": now,
            }
            for kind, direction, currency, subject in keys
        ]
    )
    await session.execute(
        statement.on_conflict_do_nothing(
            index_elements=[
                subjects.c.kind,
                subjects.c.direction,
                subjects.c.currency,
                subjects.c.subject,
            ]
        )
    )


async def _record(
    session: AsyncSession, tallies: dict[SubjectKey, list[Decimal]], key: str | None
) -> None:
    """Count each movement of each subject, under ``key``, at now: one statement.

    Each row finds its subject's id by the subject's key, in the statement.
    """
    owner = (
        select(subjects.c.id)
        .where(
            subjects.c.kind == bindparam("s_kind"),
            subjects.c.direction == bindparam("s_direction"),
            subjects.c.currency == bindparam("s_currency"),
            subjects.c.subject == bindparam("s_subject"),
        )
        .scalar_subquery()
    )
    statement = insert(usage).values(
        id=bindparam("u_id"),
        subject_id=owner,
        amount=bindparam("u_amount"),
        at=dbclock.now(session.get_bind().dialect.name),
        key=key,
    )
    await session.execute(
        statement,
        [
            {
                "u_id": uuid4(),
                "s_kind": kind,
                "s_direction": direction,
                "s_currency": currency,
                "s_subject": subject,
                "u_amount": amount,
            }
            for (kind, direction, currency, subject), amounts in tallies.items()
            for amount in amounts
        ],
    )


async def consume(
    uow: SqlAlchemyUnitOfWork, *movements: Movement, key: str | None = None
) -> None:
    """Count ``movements`` against their subjects' limits, or refuse them.

    Call it inside ``uow``, on the limits' database, before its commit: the
    count commits with the use case's changes, or rolls back with them. A
    movement within every limit is counted under ``key``, which ``release``
    gives back; every movement is counted, whether a window bounds its
    subject now or not.

    Raises:
        TransactionLimitExceeded: A movement is more than one may be.
        LimitExceeded: A window cannot take a movement: its amount, or one
            more of them.
        InvalidLimit: A movement's currency has more than four places.
        ImproperlyConfigured, ReadOnlyError, RuntimeError: ``uow`` is on
            another database, read-only, or not open.
    """
    session = _session(uow, writes=True)
    tallies = _tally(movements)
    if session.get_bind().dialect.name != "postgresql":
        await uow.lock(LOCK)
    rules = await _rules(session, tallies)
    _transactions(tallies, rules)
    unbounded: list[SubjectKey] = []
    for subject_key, amounts in tallies.items():
        windowed = _windowed(subject_key, rules)
        if not windowed:
            unbounded.append(subject_key)
            continue
        subject_id, now = await _lock(session, subject_key)
        _judge(windowed, await _used(session, subject_id, now, windowed), amounts)
    await _ensure(session, unbounded)
    await _record(session, tallies, key)


async def check(uow: SqlAlchemyUnitOfWork, *movements: Movement) -> None:
    """Refuse ``movements`` as ``consume`` would now, counting nothing.

    It takes no lock: a movement that passes may still be refused by
    ``consume``, if another was counted meanwhile.

    Raises:
        TransactionLimitExceeded, LimitExceeded, InvalidLimit: As ``consume``.
        ImproperlyConfigured, RuntimeError: As ``consume``; a read-only
            unit of work may check.
    """
    session = _session(uow, writes=False)
    tallies = _tally(movements)
    rules = await _rules(session, tallies)
    _transactions(tallies, rules)
    for subject_key, amounts in tallies.items():
        windowed = _windowed(subject_key, rules)
        if not windowed:
            continue
        found = await _subject(session, subject_key)
        used = (
            [(Decimal(0), 0)] * len(windowed)
            if found is None
            else await _used(session, found[0], found[1], windowed)
        )
        _judge(windowed, used, amounts)


async def _subject(
    session: AsyncSession, key: SubjectKey
) -> tuple[UUID, datetime] | None:
    """The subject's id, and now on the database's clock; None if it never moved."""
    dialect = session.get_bind().dialect.name
    kind, direction, currency, subject = key
    now = dbclock.now(dialect)
    columns: list[Any] = [subjects.c.id]
    if isinstance(now, ColumnElement):
        columns.append(now.label("now"))
    row = (
        await session.execute(
            select(*columns).where(
                subjects.c.kind == kind,
                subjects.c.direction == direction,
                subjects.c.currency == currency,
                subjects.c.subject == subject,
            )
        )
    ).one_or_none()
    if row is None:
        return None
    moment = row.now if isinstance(now, ColumnElement) else now
    return row.id, moment


async def release(uow: SqlAlchemyUnitOfWork, key: str) -> int:
    """Give back what was counted under ``key``; how many movements it gave back.

    Released usage counts against no window, so a refund or a reversal
    frees what its movement used. Releasing again releases nothing.

    Raises:
        ImproperlyConfigured, ReadOnlyError, RuntimeError: As ``consume``.
    """
    session = _session(uow, writes=True)
    now = dbclock.now(session.get_bind().dialect.name)
    done = await session.execute(
        update(usage)
        .where(usage.c.key == key, usage.c.released_at.is_(None))
        .values(released_at=now)
    )
    return int(done.rowcount)  # type: ignore[attr-defined]


async def usage_of(
    kind: str,
    subject: str,
    *,
    currency: str | None = None,
    direction: Direction | None = None,
) -> list[Usage]:
    """What ``subject`` of ``kind`` used of each limit that bounds it, now.

    By currency, direction, and code; ``currency`` and ``direction`` keep
    to one of each.
    """
    async with connections.session(limits_alias(), readonly=True) as session:
        conditions = [limits.c.kind == kind, limits.c.subject.in_(("", subject))]
        if currency is not None:
            conditions.append(limits.c.currency == currency)
        if direction is not None:
            conditions.append(limits.c.direction == direction)
        found = await session.execute(select(limits).where(*conditions))
        stored = [stored_rule(row) for row in found]
        shadowed = {_key_of(rule) for rule in stored}
        rules = stored + [
            rule
            for rule in map(default_rule, config().DEFAULTS)
            if _key_of(rule) not in shadowed
            and rule.kind == kind
            and rule.subject in ("", subject)
            and currency in (None, rule.currency)
            and direction in (None, rule.direction)
        ]
        keys = sorted(
            {(kind, rule.direction, rule.currency, subject) for rule in rules}
        )
        return [each async for each in _usages(session, keys, rules)]


async def _usages(
    session: AsyncSession, keys: list[SubjectKey], rules: list[Rule]
) -> AsyncIterator[Usage]:
    """Each key's usage of each limit that bounds it."""
    for key in keys:
        bounding = applicable(key, rules)
        windowed = [rule for rule in bounding if rule.window is not None]
        found = await _subject(session, key)
        now = clock.now() if found is None else found[1]
        used = (
            [(Decimal(0), 0)] * len(windowed)
            if found is None or not windowed
            else await _used(session, found[0], now, windowed)
        )
        spent = dict(zip(map(_code, windowed), used, strict=True))
        for rule in bounding:
            amount, count = spent.get(rule.code, (Decimal(0), 0))
            yield _usage(rule, now, amount, count)


def _usage(rule: Rule, now: datetime, amount: Decimal, count: int) -> Usage:
    """``rule``'s usage, ``amount`` and ``count`` in its window at ``now``."""

    def money(value: Decimal | None) -> Money | None:
        return None if value is None else Money.of(value, rule.currency)

    return Usage(
        code=rule.code,
        kind=rule.kind,
        direction=rule.direction,
        subject=rule.subject,
        window=rule.window,
        since=None if rule.window is None else rule.window.start(now, rule.time_zone),
        used=Money.of(amount, rule.currency),
        count=count,
        per_transaction=money(rule.per_transaction),
        max_amount=money(rule.max_amount),
        max_count=rule.max_count,
        stored=rule.stored,
    )


async def purge_usage() -> int:
    """Delete usage older than ``LIMITS.KEEP_USAGE``, and subjects idle as long.

    Returns:
        How many movements' usage this call deleted.
    """
    seconds = -config().KEEP_USAGE.total_seconds()
    deleted = 0
    engine = connections.engine(limits_alias())
    while True:
        async with engine.begin() as connection:
            cutoff = dbclock.at(connection.dialect.name, seconds)
            batch = (
                select(usage.c.id).where(usage.c.at < cutoff).limit(PURGE_BATCH)
            ).scalar_subquery()
            done = await connection.execute(delete(usage).where(usage.c.id.in_(batch)))
        deleted += done.rowcount
        if done.rowcount < PURGE_BATCH:
            break
    async with engine.begin() as connection:
        cutoff = dbclock.at(connection.dialect.name, seconds)
        await connection.execute(
            delete(subjects).where(
                subjects.c.touched_at < cutoff,
                ~exists(select(usage.c.id).where(usage.c.subject_id == subjects.c.id)),
            )
        )
    return deleted
