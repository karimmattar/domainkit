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
prepared ``;`` outside any transaction, where SQLAlchemy's own test takes
three (``BEGIN``, the ``;``, ``ROLLBACK``); through PgBouncer, which needs
the transaction, it keeps SQLAlchemy's.

Read-only sessions read in one of three ways (``Reads``). ``STATEMENTS``
runs each statement on its own, without ``BEGIN`` or ``ROLLBACK``, on an
engine of its own whose connections PostgreSQL holds read-only; each
statement is bounded by the deadline. Its connections are checked when
made, and reset before another caller gets them (``_reset_session``).
``SNAPSHOT`` reads in one ``REPEATABLE READ`` transaction.
"""

import asyncio
import re
import threading
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from enum import StrEnum
from functools import cache, lru_cache, partial
from typing import Any, Final
from weakref import WeakKeyDictionary

from sqlalchemy import Select, TextClause, event, literal_column, make_url, select, text
from sqlalchemy.engine import Connection, ExceptionContext, ExecutionContext
from sqlalchemy.exc import DisconnectionError
from sqlalchemy.ext.asyncio import (
    AsyncConnection,
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import ORMExecuteState, Session, UOWTransaction
from sqlalchemy.pool import (
    ConnectionPoolEntry,
    NullPool,
    PoolProxiedConnection,
    PoolResetState,
)
from sqlalchemy.sql import Executable
from sqlalchemy.sql.ddl import ExecutableDDLElement
from sqlalchemy.sql.dml import UpdateBase
from sqlalchemy.util import await_only

from domainkit import deadlines
from domainkit.conf import DEFAULT_DB_ALIAS, DatabaseConfig, settings
from domainkit.conf.global_settings import DEPLOYED_ENVIRONMENTS, is_network_database
from domainkit.db.drivers import connect_args, read_only_args, transaction_settings
from domainkit.ddd.uow import ReadOnlyError
from domainkit.exceptions import ImproperlyConfigured
from domainkit.utils.closing import close_each
from domainkit.utils.readiness import failing_checks

type _Pool = tuple[AsyncEngine, async_sessionmaker[AsyncSession]]
type _Execute = Callable[[Any], None]

DEFAULT_POOL_TIMEOUT: Final = 10.0
"""Seconds a request waits for a connection to a network database."""
DEFAULT_POOL_SIZE: Final = 5
"""SQLAlchemy's ``pool_size`` when ``POOL_SIZE`` is None."""
DEFAULT_MAX_OVERFLOW: Final = 10
"""SQLAlchemy's ``max_overflow`` when ``MAX_OVERFLOW`` is None."""
REQUESTS_PER_CONNECTION: Final = 4
"""Requests a pooled connection serves at once, roughly, past which ``check``
warns: a request holds a connection for part of its time, not all of it."""
INTERNAL: Final = "domainkit_internal"
"""Execution option of the framework's own statements, such as the timeouts
a transaction sets; ``assert_num_queries`` does not count them."""
SNAPSHOT_BEGIN: Final = "domainkit_snapshot"
"""Execution option asking SQLite to begin a transaction before reading, for
every read-only session."""
READS_SUFFIX: Final = ":reads"
"""Added to an alias to name its ``STATEMENTS`` engine in ``pool_stats``."""
_DETAIL: Final = re.compile(
    r"\n(?:DETAIL:  .*?(?=\n(?:HINT|QUERY|CONTEXT):  |\n\[SQL: |\Z)"
    r"|CONTEXT:  .*?(?=\n\[SQL: |\Z))",
    re.DOTALL,
)
"""The fields of a libpq message (psycopg's) that repeat values: a ``DETAIL``
(``Failing row contains (...)``) up to the next field, and the ``CONTEXT``
(``JSON data, line 1: ...``), which comes last."""
HIDDEN: Final = "<hidden>"
"""What a value is replaced with in an error's text."""
_REFUSED_ARGUMENT: Final = re.compile(
    r"(?P<encoding>invalid input for )?query argument \$[0-9]+"
    r"(?: in element #[0-9]+ of executemany\(\) sequence)?: "
)
"""How asyncpg starts its refusal of a value it cannot send. The value's
``repr`` follows, then the reason in parentheses; or, without ``invalid
input for``, a type's own message."""
_QUOTED: Final = re.compile(
    r"""'(?:[^'\\]|\\.)*'(?! object)|"(?:[^"\\]|\\.)*"(?! object)"""
)
"""A quoted value in Python's messages; not a type's name (``'str' object``)."""
_DATA_EXCEPTION: Final = "22"
"""SQLSTATE class of PostgreSQL's refusals of a value: its input functions
quote the value (``invalid input syntax for type integer: "x"``)."""
_FIELD: Final = re.compile(r"\n(?:DETAIL|HINT|QUERY|CONTEXT|LINE [0-9]+):? ")
_ALONE_DRIVERS: Final = frozenset({"asyncpg", "psycopg"})
"""PostgreSQL drivers whose connections can be held read-only at connect time."""
_SEES_CLOSING: Final = frozenset({"asyncpg"})
"""Drivers that notice the server closing an idle connection, without a round
trip; the ``STATEMENTS`` pool pings the others' connections as it hands them
out (psycopg reads the server's goodbye only with the next query, whose
failure looks like a statement cut off as it ran)."""
RESET_SESSION: Final = (
    "SET SESSION AUTHORIZATION DEFAULT; RESET ROLE; RESET ALL; CLOSE ALL; "
    "SELECT pg_advisory_unlock_all(); DISCARD TEMP; DISCARD SEQUENCES; "
    "DO $$BEGIN PERFORM * FROM pg_listening_channels() LIMIT 1; "
    "IF FOUND THEN UNLISTEN *; END IF; END$$"
)
"""What a ``STATEMENTS`` connection is reset with before another caller gets
it, in one round trip (the simple query protocol): the session user and
role, every setting (``default_transaction_read_only`` back to ``on``
among them), cursors, session advisory locks, temporary tables, sequence
values, and ``LISTEN``. ``UNLISTEN`` runs only if something listens, as a
standby refuses it."""
_DIRTY: Final = "domainkit_reads_dirty"
"""A ``STATEMENTS`` connection's ``info`` key: it ran something that may
have changed its session since it was last reset."""
_SENT: Final = "domainkit_reads_sent"
"""Its ``info`` key for the driver's count of queries after the last
statement domainkit saw: a higher count means something else ran."""
_SQL_TOKEN: Final = re.compile(
    r"""
    (?P<space>[ \t\n\r\f\v]+)
    | (?P<comment>--|/\*)
    | (?P<word>[A-Za-z_][A-Za-z0-9_]*)
    | (?P<quoted>"[^"]*")
    | (?P<parameter>\$[0-9]+|%\([A-Za-z0-9_]+\)s)
    | (?P<number>[0-9]+)
    | (?P<symbol>::|<>|<=|>=|!=|[(),.*=<>+\-/\[\]])
    """,
    re.VERBOSE,
)
"""The tokens of SQL a plain ``SELECT`` is made of; anything else is not one."""
_BEFORE_PARENTHESIS: Final = frozenset(
    {
        "SELECT", "FROM", "JOIN", "ON", "WHERE", "AND", "OR", "NOT", "IN",
        "EXISTS", "ANY", "ALL", "SOME", "AS", "BY", "HAVING", "WHEN", "THEN",
        "ELSE", "DISTINCT", "LATERAL", "UNION", "INTERSECT", "EXCEPT", "LIKE",
        "ILIKE", "BETWEEN", "IS",
    }
)  # fmt: skip
"""Keywords a ``(`` may follow without a function being called."""
_NOT_A_READ: Final = frozenset({"INTO", "TEMP", "TEMPORARY"})
"""Words of a ``SELECT`` that makes a table."""
_TYPE_WORDS: Final = frozenset(
    {"PRECISION", "VARYING", "WITH", "WITHOUT", "TIME", "ZONE"}
)
"""Words that continue a type's name: ``DOUBLE PRECISION``, ``TIME ZONE``."""
_SELECT_ONE: Final[Executable] = select(literal_column("1"))
"""A readiness probe a ``STATEMENTS`` connection needs no reset after."""


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


def pool_capacity(config: DatabaseConfig) -> int:
    """Connections one event loop's pool of ``config`` holds at most."""
    size = DEFAULT_POOL_SIZE if config.POOL_SIZE is None else config.POOL_SIZE
    overflow = (
        DEFAULT_MAX_OVERFLOW if config.MAX_OVERFLOW is None else config.MAX_OVERFLOW
    )
    return size + overflow


def pool_sizing_warnings() -> list[str]:
    """Pools too small for the requests each event loop admits (PERF-14).

    In staging and production, for each network database: past
    ``REQUESTS_PER_CONNECTION`` requests per pooled connection, requests
    admitted under ``MAX_CONCURRENT_REQUESTS`` wait for a connection,
    unseen by the shedding queue, up to the pool's timeout, then fail with
    503, where a request past the cap is refused within
    ``REQUEST_QUEUE_TIMEOUT``. ``check`` lists them, with the connections
    the database must accept.
    """
    cap = settings.MAX_CONCURRENT_REQUESTS
    if settings.ENVIRONMENT not in DEPLOYED_ENVIRONMENTS or cap is None:
        return []
    warnings = []
    for alias, config in settings.DATABASES.items():
        url = config.URL.get_secret_value()
        capacity = pool_capacity(config)
        if not is_network_database(url) or cap <= capacity * REQUESTS_PER_CONNECTION:
            continue
        timeout = config.POOL_TIMEOUT or DEFAULT_POOL_TIMEOUT
        warnings.append(
            f"MAX_CONCURRENT_REQUESTS ({cap}) admits more requests than the "
            f"pool of DATABASES[{alias!r}] serves: it holds {capacity} "
            f"connections per event loop (POOL_SIZE + MAX_OVERFLOW), so past "
            f"about {capacity * REQUESTS_PER_CONNECTION} requests at once they "
            f"wait for a connection, unseen by the shedding queue, for up to "
            f"{timeout:g}s, then fail with 503. Lower MAX_CONCURRENT_REQUESTS "
            f"to about {capacity * REQUESTS_PER_CONNECTION}, or raise the pool "
            f"within what the database accepts: each process opens up to "
            f"{capacity} connections per pool (twice that when it reads "
            f"through Reads.STATEMENTS), times processes and replicas."
        )
    return warnings


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
        _listen(engine.sync_engine, "begin", _begin_snapshot)
    if backend.get_driver_name() == "asyncpg" and not config.PGBOUNCER:
        engine.sync_engine.dialect.do_ping = _ping  # type: ignore[method-assign]
    return _hiding_details(engine, config)


def create_read_engine(config: DatabaseConfig) -> AsyncEngine:
    """Build the pooled engine of ``Reads.STATEMENTS``, on PostgreSQL.

    Its connections run each statement on its own (``AUTOCOMMIT``), and
    PostgreSQL holds them read-only from the start, so a write is refused
    however it is sent; a connection a pooler made without that is refused
    (``_require_read_only``). Its pool is sized as ``config`` says, like the
    alias's own. On asyncpg it does not ping connections before use: one
    the driver knows is closed (a restart, a failover) is replaced as it is
    taken from the pool, or before a statement is sent on it. psycopg
    cannot tell without a round trip, so its connections are pinged. A
    connection is reset before another caller gets it, unless all it ran
    was plain ``SELECT`` statements (``_reset_session``). Each statement is
    bounded by the deadline (``domainkit.deadlines``), since ``SET LOCAL``
    needs a transaction.
    """
    url = config.URL.get_secret_value()
    engine = create_async_engine(
        url,
        echo=config.ECHO,
        hide_parameters=not config.LOG_PARAMETERS,
        isolation_level="AUTOCOMMIT",
        pool_pre_ping=make_url(url).get_driver_name() not in _SEES_CLOSING,
        connect_args=read_only_args(config),
        **_pool_options(config, url),
    )
    sync = engine.sync_engine
    _listen(sync, "do_execute", _read_alone)
    _listen(sync, "do_execute_no_params", _read_alone_no_params)
    _listen(sync.pool, "connect", _require_read_only)
    _listen(sync.pool, "checkout", _take_open)
    _listen(sync.pool, "reset", _reset_session)
    return _hiding_details(engine, config)


def _listen(target: Any, name: str, listener: Callable[..., Any]) -> None:  # noqa: ANN401 - an engine or a pool.
    """Add ``listener`` to one engine's or pool's ``name``, as a callable of its own.

    SQLAlchemy keys its listener registry by the ids of the target and the
    function, and a disposed engine's new pool keeps its listeners under the
    old pool's id. With one function for every engine, a key repeats once a
    freed pool's id is reused, and the registry, which is not thread-safe,
    then races between threads making and dropping engines (a ``KeyError`` in
    its weakref callback). A callable for each engine keeps the keys apart
    while its listeners live.
    """
    event.listen(target, name, partial(listener))


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
    """SQLAlchemy's test of an asyncpg connection, outside a transaction.

    SQLAlchemy sends a prepared ``;`` between ``BEGIN`` and ``ROLLBACK``,
    which only PgBouncer's transaction pooling needs; outside it, the
    connection is put in ``AUTOCOMMIT`` for the test, and back. Its errors,
    and whether they mean the connection is gone, are judged as before.
    """
    was = dbapi_connection.autocommit
    dbapi_connection.autocommit = True
    try:
        dbapi_connection.ping()
    finally:
        dbapi_connection.autocommit = was
    return True


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

    A connection the driver knows is closed is replaced first, as the pool
    would on checkout: the statement was never sent, so it runs once. Not
    if an earlier statement of this checkout may have set something in the
    session, which a fresh connection would not have: then it fails. A
    connection closed while the statement runs is never retried, since
    the statement may have done something outside its transaction (through
    ``dblink``, say); SQLAlchemy then replaces every connection of the pool.

    The connection is marked for a reset (``_reset_session``) unless the
    statement is a plain ``SELECT`` (``_plain_select``) and nothing else
    reached the server through it since the last statement seen here:
    statements sent many at a time (``executemany``), which do not come
    here, or on the driver's own connection.
    """
    connection = context.root_connection
    proxied = connection.connection
    info = _info(proxied)
    sent = _sent(proxied.driver_connection)
    if sent is None or info.get(_SENT) != sent:
        info[_DIRTY] = True  # Something reached the server unseen.
    if _closed(proxied.driver_connection) and not info.get(_DIRTY):
        connection.invalidate()
        connection.rollback()
        proxied = connection.connection
        info = _info(proxied)
        context.cursor = proxied.cursor()
    if not (
        isinstance(getattr(context.compiled, "statement", None), Select)
        and _plain_select(context.statement)
    ):
        info[_DIRTY] = True
    try:
        _within_deadline(execute, context.cursor)
    finally:
        info[_SENT] = _sent(proxied.driver_connection)


def _info(proxied: PoolProxiedConnection) -> dict[Any, Any]:
    """The pooled connection's ``info``; a throwaway one for SQLAlchemy's first look.

    The dialect reads the server's version and settings on a connection
    that is not checked out yet, whose ``info`` cannot be read; those
    statements change nothing.
    """
    try:
        return proxied.info
    except NotImplementedError:
        return {}


@lru_cache(maxsize=512)
def _plain_select(statement: str) -> bool:
    """Whether ``statement`` is a ``SELECT`` that calls no function, provably.

    It is read lexeme by lexeme: only names, bound parameters, numbers, casts
    and the usual operators may appear, and a ``(`` only after an operator,
    a keyword such as ``IN`` or ``EXISTS``, or a cast's type. A name before
    ``(`` is a function call, which may change the session
    (``set_config``, ``pg_advisory_lock``). Quotes, comments, ``;`` and
    any other character, a space outside ASCII among them (PostgreSQL reads
    those as part of a name), make it something else. Views, row security
    policies, the functions of types and operators it uses, and computed
    fields (``t.f`` for a function ``f(t)``) are not seen: they are the
    schema's.
    """
    previous: tuple[str, str] = ("", "")
    cast = ""  # "type" after ::, "name" after its name, "modifier" in its (n, m).
    position = 0
    for match in _SQL_TOKEN.finditer(statement):
        kind, lexeme = match.lastgroup or "", match.group().upper()
        if match.start() != position or kind == "comment" or lexeme in _NOT_A_READ:
            return False
        position = match.end()
        if kind == "space":
            continue
        if not previous[0] and lexeme != "SELECT":
            return False
        if cast == "modifier":
            if lexeme == ")":
                cast = ""
            elif kind != "number" and lexeme != ",":
                return False
        elif cast == "type" and kind in {"word", "quoted"}:
            cast = "name"
        elif cast == "name" and lexeme == ".":
            cast = "type"
        elif cast == "name" and lexeme in _TYPE_WORDS:
            pass
        elif cast == "name" and lexeme == "(":
            cast = "modifier"
        else:
            cast = ""
            if lexeme == "::":
                cast = "type"
            elif lexeme == "(" and not _may_open(*previous):
                return False
        previous = (kind, lexeme)
    return position == len(statement) and bool(previous[0])


def _may_open(kind: str, lexeme: str) -> bool:
    """Whether ``(`` may follow this lexeme without calling a function."""
    if kind == "word":
        return lexeme in _BEFORE_PARENTHESIS
    return kind == "symbol" and lexeme not in {")", "]", "."}


def _sent(driver: Any) -> int | None:  # noqa: ANN401 - asyncpg's or psycopg's.
    """How many queries the driver has run on this connection; None if unknown.

    asyncpg counts them (the count its own pool uses); psycopg does not.
    """
    count = getattr(getattr(driver, "_protocol", None), "queries_count", None)
    return count if isinstance(count, int) else None


def _closed(driver: Any) -> bool:  # noqa: ANN401 - asyncpg's or psycopg's.
    """Whether the driver knows the connection is closed, without a round trip.

    asyncpg notices as soon as the server closes it; psycopg when an
    operation fails.
    """
    is_closed = getattr(driver, "is_closed", None)
    return bool(is_closed()) if callable(is_closed) else bool(driver.closed)


def _require_read_only(
    dbapi_connection: Any,  # noqa: ANN401 - SQLAlchemy's adapter.
    record: ConnectionPoolEntry,
) -> None:
    """Refuse a new ``STATEMENTS`` connection that PostgreSQL does not hold read-only.

    A pooler that drops the startup parameters would let a read write.

    Raises:
        ImproperlyConfigured: Its transactions are not read-only.
    """
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("SHOW transaction_read_only")
        (read_only,) = cursor.fetchone()
    finally:
        cursor.close()
    if read_only != "on":
        raise ImproperlyConfigured(
            "A connection for reads on their own (Reads.STATEMENTS) is not "
            f"read-only (transaction_read_only is {read_only!r}): something "
            "between the application and PostgreSQL dropped "
            "default_transaction_read_only. Set PGBOUNCER if it is PgBouncer, "
            "or read in a transaction."
        )


def _take_open(
    dbapi_connection: Any,  # noqa: ANN401 - SQLAlchemy's adapter.
    record: ConnectionPoolEntry,
    proxied: PoolProxiedConnection,
) -> None:
    """Replace a connection the driver knows is closed, as it leaves the pool.

    Raises:
        DisconnectionError: It is closed; the pool connects afresh.
    """
    driver = dbapi_connection.driver_connection
    if _closed(driver):
        raise DisconnectionError("The server closed this connection.")
    record.info[_SENT] = _sent(driver)


def _reset_session(
    dbapi_connection: Any,  # noqa: ANN401 - SQLAlchemy's adapter.
    record: ConnectionPoolEntry,
    state: PoolResetState,
) -> None:
    """Reset a ``STATEMENTS`` connection's session as it goes back to the pool.

    Unless all it ran was plain ``SELECT`` statements, seen here: anything
    else may have changed the session (``SET``, ``set_config``,
    ``SET ROLE``, ``LISTEN``, advisory locks), which the next caller would
    inherit. ``RESET_SESSION`` takes one round trip; if it fails, the pool
    closes the connection. A connection being closed anyway is left alone.
    """
    if state.terminate_only or not state.asyncio_safe:
        return
    driver = dbapi_connection.driver_connection
    sent = _sent(driver)
    if record.info.get(_DIRTY) or sent is None or record.info.get(_SENT) != sent:
        if sent is None:  # psycopg: never prepared, so sent as a simple query.
            await_only(driver.execute(RESET_SESSION, prepare=False))
        else:
            await_only(driver.execute(RESET_SESSION))
        record.info[_DIRTY] = False


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
    """Begin the transaction now for a read-only session, outside PostgreSQL.

    SQLite's driver begins one only before ``INSERT``, ``UPDATE``,
    ``DELETE`` or ``REPLACE``: reads would not share a snapshot without it,
    and DDL or a ``WITH ... INSERT`` sent as SQL text would commit at once,
    beyond the rollback that ends a read-only session.
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
    # Names of settings domainkit chose; the values are bound.
    return text(f"SELECT {calls}")  # nosemgrep: avoid-sqlalchemy-text


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
    """Keep values out of errors, from PostgreSQL or the driver, unless they may show.

    A constraint's error carries a ``DETAIL`` with the values of the row or
    key (``Failing row contains (1, alice@example.com, ...)``), which would
    reach logs and error reporters although bound values are hidden. So do
    a value the driver cannot send (``invalid input for query argument $1:
    '4111...'``) and one PostgreSQL cannot read (``invalid input syntax for
    type integer: "4111..."``).
    """
    if not config.LOG_PARAMETERS:
        _listen(engine.sync_engine, "handle_error", _drop_detail)
    return engine


def _drop_detail(context: ExceptionContext) -> None:
    """Remove values from the error, the driver's, and what that wraps.

    The ``DETAIL`` and ``CONTEXT`` go, and a refused value becomes
    ``HIDDEN``; the argument's number, the reason, and the SQLSTATE stay.
    """
    driver = context.original_exception
    hidden = _hidden_values(driver)
    for error in (context.sqlalchemy_exception, driver, driver.__cause__):
        if error is not None and error.args and isinstance(error.args[0], str):
            message = _DETAIL.sub("", error.args[0])
            for shown, kept in hidden:
                message = message.replace(shown, kept)
            error.args = (message, *error.args[1:])
    for error in (driver, driver.__cause__):
        for name in ("detail", "context"):  # asyncpg keeps them apart.
            if getattr(error, name, None) is not None:
                setattr(error, name, None)


def _hidden_values(driver: BaseException) -> list[tuple[str, str]]:
    """Each text of ``driver``'s error that shows a value, and what replaces it.

    asyncpg's refusal of a value it cannot encode names the argument, the
    value, and the reason, raised from the type's own error, whose quoted
    values are masked too. PostgreSQL's refusal of a value (SQLSTATE class
    22, how psycopg's untyped values are refused) quotes it once in the
    message; everything between the first and the last double quote goes.
    """
    refusal = driver.__cause__ or driver
    message = refusal.args[0] if refusal.args else None
    if not isinstance(message, str):
        return []
    if argument := _REFUSED_ARGUMENT.match(message):
        rest = message[argument.end() :]
        if not argument["encoding"]:
            return [(message, argument.group() + _masked(rest))]
        reason = str(refusal.__cause__) if refusal.__cause__ else None
        _mask_causes(refusal)
        if reason and rest.endswith(f" ({reason})"):
            return [(message, f"{argument.group()}{HIDDEN} ({_masked(reason)})")]
        return [(message, argument.group() + HIDDEN)]
    if str(getattr(driver, "sqlstate", "")).startswith(_DATA_EXCEPTION):
        primary = _FIELD.split(message, maxsplit=1)[0]
        first, last = primary.find('"'), primary.rfind('"')
        if first < last:
            return [(primary, f"{primary[: first + 1]}{HIDDEN}{primary[last:]}")]
    return []


def _masked(text: str) -> str:
    """``text`` with each quoted value ``HIDDEN``, but not a type's name."""
    return _QUOTED.sub(lambda quoted: f"{quoted[0][0]}{HIDDEN}{quoted[0][0]}", text)


def _mask_causes(error: BaseException) -> None:
    """Mask the quoted values in the errors ``error`` was raised from."""
    cause = error.__cause__
    while cause is not None:
        if cause.args and isinstance(cause.args[0], str):
            cause.args = (_masked(cause.args[0]), *cause.args[1:])
        cause = cause.__cause__


class _Engines:
    """One event loop's engines: each alias's, and its ``STATEMENTS`` engine.

    ``bound`` keeps, per alias and set of options (snapshot or not), the
    engine with those execution options that read-only sessions bind to,
    made from the alias's engine here: it shares that engine's pool, and
    goes with it.
    """

    __slots__ = ("bound", "pools", "reads")

    def __init__(self) -> None:
        """Start without engines."""
        self.pools: dict[str, _Pool] = {}
        self.reads: dict[str, AsyncEngine] = {}
        self.bound: dict[tuple[str, bool], AsyncEngine] = {}


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
        PostgreSQL, so even a replica's primary refuses its writes. On
        SQLite they begin before the first read, so that SQL text that
        writes is rolled back with them. ``reads`` says how it reads (see
        ``Reads``); with ``STATEMENTS`` or ``SNAPSHOT`` the session itself
        also refuses to flush or to run a statement that writes
        (``ReadOnlyError``).
        """
        engine, sessions = self._pool(alias)
        if not readonly:
            return sessions()
        if reads is Reads.TRANSACTION:
            return AsyncSession(
                self._bound(alias, engine, snapshot=False), expire_on_commit=False
            )
        if reads is Reads.STATEMENTS and reads_alone(self.config(alias)):
            bind = self._read_engine(alias)
        else:
            bind = self._bound(alias, engine, snapshot=reads is Reads.SNAPSHOT)
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
        """Check every configured alias; return ``database:<alias>`` for failures.

        An alias whose ``STATEMENTS`` pool this loop opened is checked
        through it too, as ``database:<alias>:reads``: its connections are
        made apart, and refused if they are not read-only.
        """
        checks = {
            f"database:{alias}": partial(self.check, alias)
            for alias in settings.DATABASES
        }
        engines = self._engines.get(asyncio.get_running_loop())
        for alias, engine in (engines.reads if engines else {}).items():
            checks[f"database:{alias}{READS_SUFFIX}"] = partial(_select_one, engine)
        return await failing_checks(checks, seconds)

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

    def _bound(self, alias: str, engine: AsyncEngine, *, snapshot: bool) -> AsyncEngine:
        """``engine``, this loop's for ``alias``, with read-only (or snapshot) options.

        Made once per engine (an ``OptionEngine`` shares its pool), rather
        than for every read-only session: ``close_all`` drops it with the
        engine, and an engine made since gets one of its own.
        """
        bound = self._loop_engines().bound
        if (alias, snapshot) not in bound:
            options = (
                _snapshot_options(engine) if snapshot else _read_only_options(engine)
            )
            bound[(alias, snapshot)] = engine.execution_options(**options)
        return bound[(alias, snapshot)]

    def _read_engine(self, alias: str) -> AsyncEngine:
        """Return the ``STATEMENTS`` engine of ``alias`` for the running loop."""
        reads = self._loop_engines().reads
        if alias not in reads:
            reads[alias] = create_read_engine(self.config(alias))
        return reads[alias]


async def _select_one(engine: AsyncEngine) -> None:
    """Run ``SELECT 1`` on ``engine``: a plain ``SELECT``, which needs no reset."""
    async with engine.connect() as connection:
        await connection.execute(_SELECT_ONE)


def _snapshot_options(engine: AsyncEngine) -> dict[str, Any]:
    """Execution options for one read snapshot on ``engine``'s database."""
    if engine.dialect.name == "postgresql":
        return {"postgresql_readonly": True, "isolation_level": "REPEATABLE READ"}
    return {SNAPSHOT_BEGIN: True}


def _read_only_options(engine: AsyncEngine) -> dict[str, Any]:
    """Execution options for a read-only transaction on ``engine``'s database.

    ``READ ONLY`` on PostgreSQL, which other databases ignore; elsewhere,
    also a transaction begun before the first read, which the session's
    end rolls back.
    """
    if engine.dialect.name == "postgresql":
        return {"postgresql_readonly": True}
    return {"postgresql_readonly": True, SNAPSHOT_BEGIN: True}


connections: Final = ConnectionHandler()
"""Process-wide connection handler."""
