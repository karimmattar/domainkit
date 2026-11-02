"""Repository adapter backed by an SQLAlchemy ``AsyncSession``.

Queries compile to SQL (``domainkit.db.query``); every aggregate they load is
tracked, so its events are published on commit. ``get_for_update`` and locking
queries use ``SELECT ... FOR UPDATE`` on PostgreSQL. SQLite has no row locks:
it reads without one, and serializes writers on the whole database instead.

``update_rows`` and ``delete_rows`` change rows in one statement, without
loading aggregates: no invariant is checked, and no event is recorded. They
are an escape hatch for infrastructure code (see ADR 0022).
"""

import operator
from collections.abc import Callable, Hashable, Mapping
from decimal import Decimal
from typing import Any, Final

from sqlalchemy import Delete, Select, Update, delete, func, literal, select, update
from sqlalchemy.engine import Row
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm.attributes import instance_state
from sqlalchemy.sql.compiler import SQLCompiler
from sqlalchemy.sql.elements import ClauseElement, ColumnElement
from sqlalchemy.sql.expression import Executable

from domainkit.db.encrypted import guarded_attributes
from domainkit.db.errors import duplicate_value
from domainkit.db.money import money_columns
from domainkit.db.orm import metadata
from domainkit.db.query import column_of, compile_criteria, compile_ordering, mapper_of
from domainkit.ddd import AggregateRoot, Repository
from domainkit.ddd.fields import FieldInfo, InvalidQuery, base_type, fields_of
from domainkit.ddd.query import (
    ID,
    Arithmetic,
    Criteria,
    CriteriaLike,
    Measure,
    Selection,
    as_query,
)
from domainkit.ddd.repository import LockNotAvailable
from domainkit.money import Money


class Explain(Executable, ClauseElement):
    """``EXPLAIN`` of a statement, its parameters still bound."""

    inherit_cache = False

    def __init__(self, statement: Select[Any], *, analyze: bool) -> None:
        """Plan ``statement``; ``analyze`` runs it too (PostgreSQL)."""
        self.statement = statement
        self.analyze = analyze


@compiles(Explain)
def _explain(element: Explain, compiler: SQLCompiler, **options: Any) -> str:  # noqa: ANN401 - the compiler's options.
    """``EXPLAIN [ANALYZE]`` on PostgreSQL, ``EXPLAIN QUERY PLAN`` on SQLite."""
    if compiler.dialect.name == "sqlite":
        prefix = "EXPLAIN QUERY PLAN"
    else:
        prefix = "EXPLAIN ANALYZE" if element.analyze else "EXPLAIN"
    planned = compiler.process(element.statement, **options)
    # The rows are the plan's, not the SELECT's: read them untyped.
    compiler._result_columns = []
    return f"{prefix} {planned}"


def _plan_line(row: Row[Any]) -> str:
    """One step of a plan: PostgreSQL's line, or SQLite's detail column."""
    return str(row[-1])


_OPERATORS: Final[dict[str, Callable[[Any, Any], Any]]] = {
    "+": operator.add,
    "-": operator.sub,
    "*": operator.mul,
}


def _computed(
    mapper: Any,  # noqa: ANN401 - SQLAlchemy's Mapper.
    fields: Mapping[str, FieldInfo],
    name: str,
    value: Arithmetic,
) -> Any:  # noqa: ANN401 - a SQL expression.
    """``name``'s new value as SQL: ``value.field <operator> value.operand``.

    Raises:
        InvalidQuery: The fields are not numbers stored in columns of their
            own (money, encrypted), or the operand is not a number.
    """
    for each in (name, value.field):
        info = fields.get(each)
        kind = base_type(info.kind) if info is not None else None
        if (
            info is None
            or info.encrypted
            or kind is None
            or kind is bool
            or not issubclass(kind, (int, Decimal))
        ):
            raise InvalidQuery(
                f"update_rows computes only whole numbers and decimals; "
                f"{each!r} is not one."
            )
    operand = value.operand
    if isinstance(operand, bool) or not isinstance(operand, (int, Decimal)):
        raise InvalidQuery(
            f"F({value.field!r}) {value.operator} takes an int or a Decimal, "
            f"not {type(operand).__name__}."
        )
    column = column_of(mapper, value.field)
    return _OPERATORS[value.operator](column, operand)


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

    async def _add_now(self, aggregate: AggregateT) -> None:
        """Insert ``aggregate`` in a savepoint; a unique violation undoes only it.

        Raises:
            DuplicateValue: A unique constraint refused it; it is not stored.
        """
        try:
            async with self.session.begin_nested():
                self.session.add(aggregate)
        except IntegrityError as exc:
            duplicate = duplicate_value(exc, metadata)
            if duplicate is None:
                raise
            raise duplicate from exc

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
        statement = self._select(query)
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

    def _select(self, query: Selection) -> Select[Any]:
        """The ``SELECT`` of the bound ``query``: where, order, offset, limit."""
        model = self.aggregate_type
        return (
            self._where(select(model), query.criteria)
            .order_by(*compile_ordering(query.ordering, model))
            .offset(query.offset or None)
            .limit(query.limit)
        )

    async def explain(
        self, query: Selection | CriteriaLike | None = None, *, analyze: bool = False
    ) -> str:
        """The database's plan for ``filter(query)``, one line per step.

        Django's ``explain()``: ``EXPLAIN`` on PostgreSQL (``analyze=True``
        runs the query and adds what it cost), ``EXPLAIN QUERY PLAN`` on
        SQLite. The values stay bound parameters. Nothing is loaded or
        tracked; a locking query is planned without its lock.

        Raises:
            ValueError: ``analyze`` on a database that cannot.
        """
        dialect = self.session.get_bind().dialect.name
        if analyze and dialect != "postgresql":
            raise ValueError(f"EXPLAIN ANALYZE is PostgreSQL's, not {dialect!r}'s.")
        prepared = self._prepare(as_query(query))
        rows = await self.session.execute(
            Explain(self._select(prepared.replace(lock=None)), analyze=analyze)
        )
        return "\n".join(_plan_line(row) for row in rows)

    async def _aggregate(
        self, criteria: Criteria | None, measures: dict[str, Measure]
    ) -> dict[str, object]:
        """One ``SELECT`` of every measure; a mean as its total over its count.

        Totals, smallest, and largest values come back in the column's type,
        exact on SQLite too (``ExactDecimal``); a mean is divided here, so a
        ``Decimal`` field's stays a ``Decimal``.
        """
        model = self.aggregate_type
        mapper = mapper_of(model)
        columns: list[ColumnElement[Any]] = []
        for measure in measures.values():
            if measure.field is None:
                columns.append(func.count())
                continue
            money = money_columns(mapper, measure.field)  # Counted by its amount.
            column = money[0] if money else column_of(mapper, measure.field)
            if measure.function == "avg":
                columns += [func.sum(column), func.count(column)]
            else:
                columns.append(getattr(func, measure.function)(column))
        statement = self._where(select(*columns).select_from(model), criteria)
        row = iter((await self.session.execute(statement)).one())
        answer: dict[str, object] = {}
        for name, measure in measures.items():
            value = next(row)
            if measure.function == "avg":
                count = next(row)
                value = value / count if count else None
            answer[name] = measure.present(model, value)
        return answer

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

        A value of an encrypted field, or of the field naming its subject,
        is set on each matching aggregate and flushed through the ORM
        instead, so it is sealed for its row and subject and its blind
        index follows (SEC-15a-1); still without invariants or events.

        Raises:
            InvalidQuery: A value names no mapped column, the id or the
                version, or does not fit its field.
        """
        model = self.aggregate_type
        mapper = mapper_of(model)
        fields = fields_of(model)
        changes: dict[str, Any] = {}
        given: dict[str, Any] = {}
        for name, value in values.items():
            if name in {ID, "version"} or name not in fields:
                raise InvalidQuery(f"update_rows cannot set {name!r}.")
            if isinstance(value, Arithmetic):
                changes[name] = _computed(mapper, fields, name, value)
                continue
            if (money := money_columns(mapper, name)) is None:
                column_of(mapper, name)
                changes[name] = given[name] = fields[name].coerce(value)
                continue
            coerced = given[name] = fields[name].coerce(value)
            amount, code = (
                coerced.__composite_values__()
                if isinstance(coerced, Money)
                else (None, None)
            )
            changes[mapper.get_property_by_column(money[0]).key] = amount
            changes[mapper.get_property_by_column(money[1]).key] = code
        if not changes:
            raise InvalidQuery("update_rows needs at least one value to set.")
        scope = self._scope(criteria, include_deleted=include_deleted)
        if set(given) & guarded_attributes(mapper):
            if len(given) < len(values):
                raise InvalidQuery(
                    "update_rows cannot compute a value (F) while it sets an "
                    "encrypted field: set them in two calls."
                )
            found = list(await self.session.scalars(self._where(select(model), scope)))
            for aggregate in found:
                for name, value in given.items():
                    setattr(aggregate, name, value)
            await self.session.flush()
            return len(found)
        version = mapper.version_id_col
        if version is not None:
            changes[mapper.get_property_by_column(version).key] = version + 1
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
