"""Repository adapter backed by an SQLAlchemy ``AsyncSession``.

Queries compile to SQL (``domainkit.db.query``); every aggregate they load is
tracked, so its events are published on commit. ``get_for_update`` and locking
queries use ``SELECT ... FOR UPDATE`` on PostgreSQL. SQLite has no row locks:
it reads without one, and serializes writers on the whole database instead.

``update_rows`` and ``delete_rows`` change rows in one statement, without
loading aggregates: no invariant is checked, and no event is recorded. They
are an escape hatch for infrastructure code (see ADR 0022).
"""

from collections.abc import Hashable
from typing import Any, Final

from sqlalchemy import Delete, Select, Update, delete, func, literal, select, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import instance_state

from domainkit.db.money import money_columns
from domainkit.db.query import column_of, compile_criteria, compile_ordering, mapper_of
from domainkit.ddd import AggregateRoot, Repository
from domainkit.ddd.fields import InvalidQuery, fields_of
from domainkit.ddd.query import ID, Criteria, CriteriaLike, Selection
from domainkit.ddd.repository import LockNotAvailable

LOCK_NOT_AVAILABLE: Final = "55P03"
"""PostgreSQL's SQLSTATE for a row lock that ``NOWAIT`` would not wait for."""


class SqlAlchemyRepository[AggregateT: AggregateRoot[Any], IdT: Hashable](
    Repository[AggregateT, IdT]
):
    """Store one imperatively mapped aggregate type; set ``model`` in subclasses."""

    model: type[AggregateT]

    def __init__(self, session: AsyncSession) -> None:
        """Work inside ``session``, which the unit of work owns."""
        super().__init__()
        self.session = session

    async def _add(self, aggregate: AggregateT) -> None:
        """Add ``aggregate`` to the session."""
        self.session.add(aggregate)

    async def _get(self, id_: IdT) -> AggregateT | None:
        """Load by primary key."""
        return await self.session.get(self.aggregate_type, id_)

    async def _remove(self, aggregate: AggregateT) -> None:
        """Mark ``aggregate`` for deletion."""
        await self.session.delete(aggregate)

    async def _filter(self, query: Selection) -> list[AggregateT]:
        """Select, order, skip, limit, and lock in one statement.

        A locking query reloads rows it had loaded already, so the aggregate
        shows what the lock holder committed.
        """
        model = self.aggregate_type
        statement = self._where(select(model), query.criteria)
        statement = (
            statement.order_by(*compile_ordering(query.ordering, model))
            .offset(query.offset or None)
            .limit(query.limit)
        )
        lock = query.lock
        if lock is None:
            return list(await self.session.scalars(statement))
        statement = statement.with_for_update(
            nowait=lock.nowait, skip_locked=lock.skip_locked, of=model
        ).execution_options(populate_existing=True)
        if not lock.nowait:
            return list(await self.session.scalars(statement))
        try:  # A refused NOWAIT aborts the transaction; the savepoint keeps it.
            async with self.session.begin_nested():
                return list(await self.session.scalars(statement))
        except DBAPIError as exc:
            if _sqlstate(exc) == LOCK_NOT_AVAILABLE:
                raise LockNotAvailable(
                    "The data is being changed by another request; try again."
                ) from exc
            raise

    async def _count(self, criteria: Criteria | None) -> int:
        """``SELECT count(*)`` of the matches."""
        counted = select(func.count()).select_from(self.aggregate_type)
        statement = self._where(counted, criteria)
        return int(await self.session.scalar(statement) or 0)

    async def _exists(self, criteria: Criteria | None) -> bool:
        """``SELECT EXISTS (...)`` of the matches."""
        matches = self._where(
            select(literal(1)).select_from(self.aggregate_type), criteria
        )
        return bool(await self.session.scalar(select(matches.exists())))

    async def update_rows(
        self,
        criteria: CriteriaLike | None,
        /,
        *,
        include_deleted: bool = False,
        **values: object,
    ) -> int:
        """Set ``values`` on every matching row in one statement; return how many.

        This bypasses the aggregates: their methods and invariants are not
        run and no event is recorded, so the audit trail and the outbox
        never hear of it. A ``version`` column is still bumped, so a copy
        loaded earlier cannot overwrite the change unseen. Aggregates this
        session holds are updated in place. ``criteria`` None means every
        row (soft-deleted ones only with ``include_deleted``).

        Raises:
            InvalidQuery: A value names no mapped column, the id or the
                version, or does not fit its field.
        """
        model = self.aggregate_type
        mapper = mapper_of(model)
        fields = fields_of(model)
        changes: dict[str, Any] = {}
        for name, value in values.items():
            if name in {ID, "version"} or name not in fields:
                raise InvalidQuery(f"update_rows cannot set {name!r}.")
            if (money := money_columns(mapper, name)) is None:
                column_of(mapper, name)
                changes[name] = fields[name].coerce(value)
                continue
            coerced = fields[name].coerce(value)
            parts = (None, None) if coerced is None else coerced.__composite_values__()  # type: ignore[attr-defined]
            for column, part in zip(money, parts, strict=True):
                changes[mapper.get_property_by_column(column).key] = part
        if not changes:
            raise InvalidQuery("update_rows needs at least one value to set.")
        version = mapper.version_id_col
        if version is not None:
            changes[mapper.get_property_by_column(version).key] = version + 1
        scope = self._scope(criteria, include_deleted=include_deleted)
        statement = self._where(update(model), scope).values(changes)
        result = await self.session.execute(
            statement.execution_options(synchronize_session="fetch")
        )
        await reload_expired(self.session)  # The version was computed in SQL.
        return int(result.rowcount)  # type: ignore[attr-defined]

    async def delete_rows(
        self, criteria: CriteriaLike | None, /, *, include_deleted: bool = False
    ) -> int:
        """Delete every matching row in one statement; return how many.

        This bypasses the aggregates, as ``update_rows`` does, and deletes
        rows for good, also those of soft-deletable aggregates. Child rows
        go only where their foreign key says ``ON DELETE CASCADE``.
        """
        scope = self._scope(criteria, include_deleted=include_deleted)
        statement = self._where(delete(self.aggregate_type), scope)
        result = await self.session.execute(
            statement.execution_options(synchronize_session="fetch")
        )
        return int(result.rowcount)  # type: ignore[attr-defined]

    def _where[StatementT: (Select[Any], Update, Delete)](
        self, statement: StatementT, criteria: Criteria | None
    ) -> StatementT:
        """``statement`` filtered by the bound ``criteria``."""
        if criteria is None:
            return statement
        dialect = self.session.get_bind().dialect.name
        return statement.where(compile_criteria(criteria, self.aggregate_type, dialect))


async def reload_expired(session: AsyncSession) -> None:
    """Read again what SQLAlchemy expired in ``session``.

    Reading an expired attribute later would need I/O outside ``await``,
    which async sessions refuse.
    """
    for instance in list(session.identity_map.values()):
        if instance_state(instance).expired_attributes:
            await session.refresh(instance)


def _sqlstate(exc: DBAPIError) -> str | None:
    """The SQLSTATE of a driver's error, from asyncpg or psycopg."""
    state = getattr(exc.orig, "sqlstate", None) or getattr(exc.orig, "pgcode", None)
    return state if isinstance(state, str) else None
