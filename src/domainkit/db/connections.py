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
"""

import asyncio
import re
from functools import cache, partial
from typing import Any, Final
from weakref import WeakKeyDictionary

from sqlalchemy import TextClause, event, make_url, text
from sqlalchemy.engine import Connection, ExceptionContext
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from domainkit.conf import DEFAULT_DB_ALIAS, DatabaseConfig, settings
from domainkit.conf.global_settings import is_network_database
from domainkit.db.drivers import connect_args, transaction_settings
from domainkit.deadlines import remaining
from domainkit.exceptions import ImproperlyConfigured
from domainkit.utils.closing import close_each
from domainkit.utils.readiness import failing_checks

type _Pool = tuple[AsyncEngine, async_sessionmaker[AsyncSession]]

DEFAULT_POOL_TIMEOUT: Final = 10.0
"""Seconds a request waits for a connection to a network database."""
INTERNAL: Final = "domainkit_internal"
"""Execution option of the framework's own statements, such as the timeouts
a transaction sets; ``assert_num_queries`` does not count them."""
_DETAIL: Final = re.compile(r"\nDETAIL:  [^\n]*")


def create_engine(config: DatabaseConfig) -> AsyncEngine:
    """Build a pooled engine; bound values stay out of logs and errors by default.

    A network database's pool makes a request wait at most
    ``DEFAULT_POOL_TIMEOUT`` for a connection unless ``POOL_TIMEOUT`` says
    otherwise; past it, the request is refused as busy (503).
    """
    url = config.URL.get_secret_value()
    timeout = config.POOL_TIMEOUT
    if timeout is None and is_network_database(url):
        timeout = DEFAULT_POOL_TIMEOUT
    pool = {
        "pool_size": config.POOL_SIZE,
        "max_overflow": config.MAX_OVERFLOW,
        "pool_timeout": timeout,
        "pool_recycle": config.POOL_RECYCLE,
    }
    options: dict[str, Any] = {k: v for k, v in pool.items() if v is not None}
    engine = create_async_engine(
        url,
        echo=config.ECHO,
        hide_parameters=not config.LOG_PARAMETERS,
        pool_pre_ping=config.POOL_PRE_PING,
        connect_args=connect_args(config),
        **options,
    )
    if make_url(url).get_backend_name() == "postgresql":
        event.listen(engine.sync_engine, "begin", partial(_set_local, config))
    return _hiding_details(engine, config)


def _set_local(config: DatabaseConfig, connection: Connection) -> None:
    """Start a transaction with its own timeouts (``SET LOCAL``), if it needs any.

    The deadline's time is read in the caller's context, which SQLAlchemy
    carries into the event.
    """
    values = transaction_settings(config, remaining())
    if values:
        connection.execute(
            _set_config(tuple(values)), values, execution_options={INTERNAL: True}
        )


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


class ConnectionHandler:
    """Lazily created engines and session factories per alias and event loop."""

    def __init__(self) -> None:
        """Start without engines."""
        self._pools: WeakKeyDictionary[asyncio.AbstractEventLoop, dict[str, _Pool]] = (
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
        self, alias: str = DEFAULT_DB_ALIAS, *, readonly: bool = False
    ) -> AsyncSession:
        """Open a session; objects stay readable after commit.

        A ``readonly`` session's transactions are ``READ ONLY`` on
        PostgreSQL, so even a replica's primary refuses its writes.
        """
        engine, sessions = self._pool(alias)
        if not readonly:
            return sessions()
        return AsyncSession(
            engine.execution_options(postgresql_readonly=True), expire_on_commit=False
        )

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
        """Return pool usage per alias for the running loop (for metrics)."""
        pools = self._pools.get(asyncio.get_running_loop(), {})
        stats = {}
        for alias, (engine, _) in pools.items():
            pool: Any = engine.pool
            stats[alias] = {
                "size": int(getattr(pool, "size", lambda: 0)()),
                "checked_out": int(getattr(pool, "checkedout", lambda: 0)()),
                "overflow": max(int(getattr(pool, "overflow", lambda: 0)()), 0),
            }
        return stats

    async def close_all(self) -> None:
        """Dispose the running loop's engines; the next use creates fresh ones.

        Every engine is disposed even if one fails; the first error is raised.
        """
        pools = self._pools.pop(asyncio.get_running_loop(), {})
        await close_each([engine for engine, _ in pools.values()], AsyncEngine.dispose)

    def _pool(self, alias: str) -> _Pool:
        """Return the engine and session factory of ``alias`` for the running loop."""
        pools = self._pools.setdefault(asyncio.get_running_loop(), {})
        if alias not in pools:
            engine = create_engine(self.config(alias))
            pools[alias] = (engine, async_sessionmaker(engine, expire_on_commit=False))
        return pools[alias]


connections: Final = ConnectionHandler()
"""Process-wide connection handler."""
