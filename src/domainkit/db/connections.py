"""Async SQLAlchemy engines per ``DATABASES`` alias (``django.db.connections``).

Engines are created on first use and disposed by the application lifespan, so
importing a module never opens a connection. Async drivers bind connections
to the event loop that opened them, so each running loop gets its own engines:
a management command and a test client in one process never share a pool.

On PostgreSQL each transaction starts by setting what
``drivers.transaction_settings`` asks for: a ``statement_timeout`` no longer
than the time left before the deadline (``domainkit.deadlines``), and under
PgBouncer every timeout. It runs with the ``INTERNAL`` execution option,
which ``assert_num_queries`` leaves out, as it does ``BEGIN``.

With asyncpg, ``POOL_PRE_PING`` tests a connection with one round trip, a
``SELECT 1`` outside any transaction, where SQLAlchemy's own test takes three
(``BEGIN``, a prepared ``;``, ``ROLLBACK``).

Read-only sessions read in one of three ways (``Reads``). ``STATEMENTS``
runs each statement on its own, without ``BEGIN`` or ``ROLLBACK``, on an
engine of its own whose connections PostgreSQL holds read-only; each
statement is bounded by the deadline, and run once more on a fresh
connection if the server had closed the one it was sent on. ``SNAPSHOT``
reads in one ``REPEATABLE READ`` transaction.
"""

import asyncio
import re
import threading
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from enum import StrEnum
from functools import cache, partial
from typing import Any, Final
from weakref import WeakKeyDictionary

from sqlalchemy import TextClause, event, make_url, text
from sqlalchemy.engine import Connection, ExceptionContext, ExecutionContext
from sqlalchemy.ext.asyncio import (
    AsyncConnection,
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import ORMExecuteState, Session, UOWTransaction
from sqlalchemy.pool import NullPool
from sqlalchemy.sql.ddl import ExecutableDDLElement
from sqlalchemy.sql.dml import UpdateBase
from sqlalchemy.util import await_only

from domainkit import deadlines
from domainkit.conf import DEFAULT_DB_ALIAS, DatabaseConfig, settings
from domainkit.conf.global_settings import is_network_database
from domainkit.db.drivers import connect_args, read_only_args, transaction_settings
from domainkit.ddd.uow import ReadOnlyError
from domainkit.exceptions import ImproperlyConfigured
from domainkit.utils.closing import close_each
from domainkit.utils.readiness import failing_checks

type _Pool = tuple[AsyncEngine, async_sessionmaker[AsyncSession]]
type _Execute = Callable[[Any], None]

DEFAULT_POOL_TIMEOUT: Final = 10.0
"""Seconds a request waits for a connection to a network database."""
INTERNAL: Final = "domainkit_internal"
"""Execution option of the framework's own statements, such as the timeouts
a transaction sets; ``assert_num_queries`` does not count them."""
SNAPSHOT_BEGIN: Final = "domainkit_snapshot"
"""Execution option asking SQLite to begin a transaction before reading."""
READS_SUFFIX: Final = ":reads"
"""Added to an alias to name its ``STATEMENTS`` engine in ``pool_stats``."""
PING: Final = "SELECT 1"
_DETAIL: Final = re.compile(r"\nDETAIL:  [^\n]*")
_ALONE_DRIVERS: Final = frozenset({"asyncpg", "psycopg"})
"""PostgreSQL drivers whose connections can be held read-only at connect time."""


class Reads(StrEnum):
    """How a read-only session reads (``SqlAlchemyUnitOfWork(reads=...)``)."""

    TRANSACTION = "transaction"
    """A ``READ ONLY`` transaction, ``READ COMMITTED`` on PostgreSQL: each
    statement sees what was committed when it started."""
    STATEMENTS = "statements"
    """Each statement on its own, without ``BEGIN`` and ``ROLLBACK``, on
    PostgreSQL outside PgBouncer; elsewhere as ``TRANSACTION``. Statements
    see what was committed when each started, as in ``TRANSACTION``."""
    SNAPSHOT = "snapshot"
    """One transaction whose statements all see the data as it was when the
    first one started: ``REPEATABLE READ``, ``READ ONLY`` on PostgreSQL."""


class ReadOnlySession(Session):
    """A session that refuses to write (``ReadOnlyError``).

    It flushes nothing, and runs no ``INSERT``, ``UPDATE``, ``DELETE`` or
    DDL statement. SQL text is not inspected; on PostgreSQL the server
    refuses the writes in it.
    """


@event.listens_for(ReadOnlySession, "before_flush")
def _refuse_flush(session: Session, flush: UOWTransaction, instances: object) -> None:
    """Refuse to flush changes made in a read-only session."""
    raise ReadOnlyError("A read-only unit of work cannot save changes.")


@event.listens_for(ReadOnlySession, "do_orm_execute")
def _refuse_writes(state: ORMExecuteState) -> None:
    """Refuse a statement that writes, or changes the schema."""
    if isinstance(state.statement, UpdateBase | ExecutableDDLElement):
        raise ReadOnlyError("A read-only unit of work cannot write.")


def _pool_options(config: DatabaseConfig, url: str) -> dict[str, Any]:
    """The pool's size, overflow, timeout, and recycle age, where set."""
    timeout = config.POOL_TIMEOUT
    if timeout is None and is_network_database(url):
        timeout = DEFAULT_POOL_TIMEOUT
    pool = {
        "pool_size": config.POOL_SIZE,
        "max_overflow": config.MAX_OVERFLOW,
        "pool_timeout": timeout,
        "pool_recycle": config.POOL_RECYCLE,
    }
    return {k: v for k, v in pool.items() if v is not None}


def create_engine(config: DatabaseConfig) -> AsyncEngine:
    """Build a pooled engine; bound values stay out of logs and errors by default.

    A network database's pool makes a request wait at most
    ``DEFAULT_POOL_TIMEOUT`` for a connection unless ``POOL_TIMEOUT`` says
    otherwise; past it, the request is refused as busy (503).
    """
    url = config.URL.get_secret_value()
    engine = create_async_engine(
        url,
        echo=config.ECHO,
        hide_parameters=not config.LOG_PARAMETERS,
        pool_pre_ping=config.POOL_PRE_PING,
        connect_args=connect_args(config),
        **_pool_options(config, url),
    )
    backend = make_url(url)
    if backend.get_backend_name() == "postgresql":
        event.listen(engine.sync_engine, "begin", partial(_set_local, config))
    else:
        event.listen(engine.sync_engine, "begin", _begin_snapshot)
    if backend.get_driver_name() == "asyncpg":
        engine.sync_engine.dialect.do_ping = _ping  # type: ignore[method-assign]
    return _hiding_details(engine, config)


def create_read_engine(config: DatabaseConfig) -> AsyncEngine:
    """Build the pooled engine of ``Reads.STATEMENTS``, on PostgreSQL.

    Its connections run each statement on its own (``AUTOCOMMIT``), and
    PostgreSQL holds them read-only from the start, so a write is refused
    however it is sent. Its pool is sized as ``config`` says, like the
    alias's own. It does not test connections before use: a statement whose
    connection the server had closed (a restart, a failover) runs once more
    on a fresh one, which is safe for statements that cannot write. Each
    statement is bounded by the deadline (``domainkit.deadlines``), since
    ``SET LOCAL`` needs a transaction.
    """
    url = config.URL.get_secret_value()
    engine = create_async_engine(
        url,
        echo=config.ECHO,
        hide_parameters=not config.LOG_PARAMETERS,
        isolation_level="AUTOCOMMIT",
        pool_pre_ping=False,
        connect_args=read_only_args(config),
        **_pool_options(config, url),
    )
    event.listen(engine.sync_engine, "do_execute", _read_alone)
    event.listen(engine.sync_engine, "do_execute_no_params", _read_alone_no_params)
    return _hiding_details(engine, config)


def reads_alone(config: DatabaseConfig) -> bool:
    """Whether ``Reads.STATEMENTS`` reads ``config``'s database without a transaction.

    On PostgreSQL through asyncpg or psycopg, which keep the timeouts in the
    session; not through PgBouncer, which keeps nothing there.
    """
    url = make_url(config.URL.get_secret_value())
    return (
        url.get_backend_name() == "postgresql"
        and url.get_driver_name() in _ALONE_DRIVERS
        and not config.PGBOUNCER
    )


def _ping(dbapi_connection: Any) -> bool:  # noqa: ANN401 - SQLAlchemy's adapter.
    """Test an asyncpg connection with one ``SELECT 1``; False if it is unusable.

    The simple query protocol prepares nothing, so it needs no transaction,
    also behind PgBouncer. False makes the pool replace the connection and
    every older idle one, as for a disconnect.
    """
    try:
        dbapi_connection.run_async(_select_one)
    except Exception:  # noqa: BLE001 - any failure of SELECT 1 is the connection's.
        return False
    return True


async def _select_one(connection: Any) -> object:  # noqa: ANN401 - asyncpg's.
    """Run ``PING`` with the simple query protocol."""
    return await connection.execute(PING)


def _read_alone(
    cursor: Any,  # noqa: ANN401 - the driver's.
    statement: str,
    parameters: Any,  # noqa: ANN401 - the driver's.
    context: ExecutionContext,
) -> bool:
    """Run a read of the ``STATEMENTS`` engine (``do_execute``); True: it ran."""

    def execute(cursor: Any) -> None:  # noqa: ANN401 - the driver's.
        context.dialect.do_execute(cursor, statement, parameters, context)

    _run_alone(context, execute)
    return True


def _read_alone_no_params(
    cursor: Any,  # noqa: ANN401 - the driver's.
    statement: str,
    context: ExecutionContext,
) -> bool:
    """Run a read of the ``STATEMENTS`` engine (``do_execute_no_params``)."""

    def execute(cursor: Any) -> None:  # noqa: ANN401 - the driver's.
        context.dialect.do_execute_no_params(cursor, statement, context)

    _run_alone(context, execute)
    return True


def _run_alone(context: ExecutionContext, execute: _Execute) -> None:
    """Run one statement on its own, bounded by the deadline.

    If the server had closed its connection, it runs once more on a fresh
    one; the connection is replaced, as the pool would on checkout.
    """
    try:
        _within_deadline(execute, context.cursor)
    except context.dialect.loaded_dbapi.Error as error:
        connection = context.root_connection
        dbapi = connection.connection.dbapi_connection
        if not context.dialect.is_disconnect(error, dbapi, context.cursor):
            raise
        connection.invalidate()
        connection.rollback()
        context.cursor = connection.connection.cursor()
        _within_deadline(execute, context.cursor)


def _within_deadline(execute: _Execute, cursor: Any) -> None:  # noqa: ANN401
    """Run ``execute(cursor)``, cut off when the deadline passes.

    Without a deadline, or under one a timer already enforces in this task
    (an HTTP request's), it just runs: the timer cancels it. Otherwise a
    ``deadlines.timeout`` block wraps the statement, entered from the
    greenlet the statement runs in, and raises ``DeadlineExceeded`` if it
    cut the statement off; the driver then cancels it on the server. A
    statement started with no time left is refused at once.
    """
    if deadlines.enforced() or deadlines.remaining() is None:
        execute(cursor)
        return
    bound = deadlines.timeout()
    await_only(bound.__aenter__())
    try:
        execute(cursor)
    except BaseException as exc:
        await_only(bound.__aexit__(type(exc), exc, exc.__traceback__))
        raise
    await_only(bound.__aexit__(None, None, None))


def _set_local(config: DatabaseConfig, connection: Connection) -> None:
    """Start a transaction with its own timeouts (``SET LOCAL``), if it needs any.

    The deadline's time is read in the caller's context, which SQLAlchemy
    carries into the event. A connection outside transactions
    (``AUTOCOMMIT``) has none to set them in.
    """
    if connection.get_execution_options().get("isolation_level") == "AUTOCOMMIT":
        return
    values = transaction_settings(config, deadlines.remaining())
    if values:
        connection.execute(
            _set_config(tuple(values)), values, execution_options={INTERNAL: True}
        )


def _begin_snapshot(connection: Connection) -> None:
    """Begin the transaction now for a ``SNAPSHOT`` read, outside PostgreSQL.

    SQLite's driver begins one only before a write, so reads would not
    share a snapshot without it.
    """
    if connection.get_execution_options().get(SNAPSHOT_BEGIN):
        connection.exec_driver_sql("BEGIN", execution_options={INTERNAL: True})


@cache
def _set_config(names: tuple[str, ...]) -> TextClause:
    """``SELECT set_config(name, :name, true), ...``: ``SET LOCAL`` with values bound.

    The text depends only on which settings are set, so each connection
    prepares it once.
    """
    calls = ", ".join(f"set_config('{name}', :{name}, true)" for name in names)
    return text(f"SELECT {calls}")


def create_owner_engine(config: DatabaseConfig) -> AsyncEngine:
    """An unpooled engine for the schema owner (``MIGRATION_URL``), without timeouts.

    Migrations and partition maintenance change the schema, which may take
    longer than requests are allowed to, and needs the owner's privileges.
    """
    url = (config.MIGRATION_URL or config.URL).get_secret_value()
    return _hiding_details(
        create_async_engine(
            url,
            poolclass=NullPool,
            hide_parameters=not config.LOG_PARAMETERS,
            connect_args=connect_args(config, url, timeouts=False),
        ),
        config,
    )


def _hiding_details(engine: AsyncEngine, config: DatabaseConfig) -> AsyncEngine:
    """Keep the values PostgreSQL repeats in errors out, unless they may show.

    A constraint's error carries a ``DETAIL`` with the values of the row or
    key (``Failing row contains (1, alice@example.com, ...)``), which would
    reach logs and error reporters although bound values are hidden.
    """
    if not config.LOG_PARAMETERS:
        event.listen(engine.sync_engine, "handle_error", _drop_detail)
    return engine


def _drop_detail(context: ExceptionContext) -> None:
    """Remove the ``DETAIL`` from the error, the driver's, and what that wraps."""
    driver = context.original_exception
    for error in (context.sqlalchemy_exception, driver):
        if error is not None and error.args and isinstance(error.args[0], str):
            error.args = (_DETAIL.sub("", error.args[0]), *error.args[1:])
    for error in (driver, driver.__cause__):
        if getattr(error, "detail", None) is not None:  # asyncpg keeps it apart.
            error.detail = None  # type: ignore[union-attr]


class _Engines:
    """One event loop's engines: each alias's, and its ``STATEMENTS`` engine."""

    __slots__ = ("pools", "reads")

    def __init__(self) -> None:
        """Start without engines."""
        self.pools: dict[str, _Pool] = {}
        self.reads: dict[str, AsyncEngine] = {}


class ConnectionHandler:
    """Lazily created engines and session factories per alias and event loop.

    Loops on several threads share the handler: the registry of loops is
    changed and walked only with its lock held, while each loop's engines
    are its own.
    """

    def __init__(self) -> None:
        """Start without engines."""
        self._lock = threading.Lock()
        self._engines: WeakKeyDictionary[asyncio.AbstractEventLoop, _Engines] = (
            WeakKeyDictionary()
        )

    def config(self, alias: str = DEFAULT_DB_ALIAS) -> DatabaseConfig:
        """Return the ``DATABASES`` entry for ``alias``."""
        try:
            return settings.DATABASES[alias]
        except KeyError:
            hint = (
                "; it is a ledger: use domainkit.ledger.ledgers"
                if alias in settings.LEDGERS
                else ""
            )
            raise ImproperlyConfigured(
                f"DATABASES has no {alias!r} alias{hint}."
            ) from None

    def engine(self, alias: str = DEFAULT_DB_ALIAS) -> AsyncEngine:
        """Return this event loop's engine for ``alias``, creating it on first use."""
        return self._pool(alias)[0]

    def session(
        self,
        alias: str = DEFAULT_DB_ALIAS,
        *,
        readonly: bool = False,
        reads: Reads = Reads.TRANSACTION,
    ) -> AsyncSession:
        """Open a session; objects stay readable after commit.

        A ``readonly`` session's transactions are ``READ ONLY`` on
        PostgreSQL, so even a replica's primary refuses its writes. ``reads``
        says how it reads (see ``Reads``); with ``STATEMENTS`` or
        ``SNAPSHOT`` the session itself also refuses to flush or to run a
        statement that writes (``ReadOnlyError``).
        """
        engine, sessions = self._pool(alias)
        if not readonly:
            return sessions()
        if reads is Reads.TRANSACTION:
            return AsyncSession(
                engine.execution_options(postgresql_readonly=True),
                expire_on_commit=False,
            )
        if reads is Reads.STATEMENTS and reads_alone(self.config(alias)):
            bind = self._read_engine(alias)
        elif reads is Reads.SNAPSHOT:
            bind = engine.execution_options(**_snapshot_options(engine))
        else:
            bind = engine.execution_options(postgresql_readonly=True)
        return AsyncSession(
            bind, expire_on_commit=False, sync_session_class=ReadOnlySession
        )

    @asynccontextmanager
    async def autocommit(
        self, alias: str = DEFAULT_DB_ALIAS
    ) -> AsyncIterator[AsyncConnection]:
        """A connection whose statements each commit as they run.

        For the framework's own short writes, where each statement stands on
        its own: it saves ``BEGIN`` and ``COMMIT``. The database's session
        keeps the timeouts. Through PgBouncer, which keeps nothing in the
        session, the statements run in one transaction instead, which sets
        them and commits when the block ends.
        """
        engine = self.engine(alias)
        if self.config(alias).PGBOUNCER:
            async with engine.begin() as connection:
                yield connection
            return
        async with engine.connect() as connection:
            yield await connection.execution_options(isolation_level="AUTOCOMMIT")

    async def check(self, alias: str = DEFAULT_DB_ALIAS) -> None:
        """Run ``SELECT 1`` on ``alias``; raises when the database is unreachable."""
        async with self.engine(alias).connect() as connection:
            await connection.execute(text("SELECT 1"))

    async def check_all(self, seconds: float) -> list[str]:
        """Check every configured alias; return ``database:<alias>`` for failures."""
        return await failing_checks(
            {
                f"database:{alias}": partial(self.check, alias)
                for alias in settings.DATABASES
            },
            seconds,
        )

    def pool_stats(self) -> dict[str, dict[str, int]]:
        """Return pool usage per alias for the running loop (for metrics).

        A ``STATEMENTS`` engine's pool is ``<alias>:reads``.
        """
        engines = self._engines.get(asyncio.get_running_loop())
        if engines is None:
            return {}
        named = {alias: engine for alias, (engine, _) in engines.pools.items()}
        named |= {
            f"{alias}{READS_SUFFIX}": engine for alias, engine in engines.reads.items()
        }
        stats = {}
        for name, engine in named.items():
            pool: Any = engine.pool
            stats[name] = {
                "size": int(getattr(pool, "size", lambda: 0)()),
                "checked_out": int(getattr(pool, "checkedout", lambda: 0)()),
                "overflow": max(int(getattr(pool, "overflow", lambda: 0)()), 0),
            }
        return stats

    async def close_all(self) -> None:
        """Dispose the running loop's engines; the next use creates fresh ones.

        Every engine is disposed even if one fails; the first error is raised.
        """
        with self._lock:
            engines = self._engines.pop(asyncio.get_running_loop(), None)
        if engines is None:
            return
        disposed = [engine for engine, _ in engines.pools.values()]
        await close_each([*disposed, *engines.reads.values()], AsyncEngine.dispose)

    def _loop_engines(self) -> _Engines:
        """The running loop's engines; added, under the lock, on first use."""
        loop = asyncio.get_running_loop()
        engines = self._engines.get(loop)
        if engines is None:
            with self._lock:
                engines = self._engines.setdefault(loop, _Engines())
        return engines

    def _pool(self, alias: str) -> _Pool:
        """Return the engine and session factory of ``alias`` for the running loop."""
        pools = self._loop_engines().pools
        if alias not in pools:
            engine = create_engine(self.config(alias))
            pools[alias] = (engine, async_sessionmaker(engine, expire_on_commit=False))
        return pools[alias]

    def _read_engine(self, alias: str) -> AsyncEngine:
        """Return the ``STATEMENTS`` engine of ``alias`` for the running loop."""
        reads = self._loop_engines().reads
        if alias not in reads:
            reads[alias] = create_read_engine(self.config(alias))
        return reads[alias]


def _snapshot_options(engine: AsyncEngine) -> dict[str, Any]:
    """Execution options for one read snapshot on ``engine``'s database."""
    if engine.dialect.name == "postgresql":
        return {"postgresql_readonly": True, "isolation_level": "REPEATABLE READ"}
    return {SNAPSHOT_BEGIN: True}


connections: Final = ConnectionHandler()
"""Process-wide connection handler."""
