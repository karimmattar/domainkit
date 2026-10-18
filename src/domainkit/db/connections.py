"""Async SQLAlchemy engines per ``DATABASES`` alias (``django.db.connections``).

Engines are created on first use and disposed by the application lifespan, so
importing a module never opens a connection. Async drivers bind connections
to the event loop that opened them, so each running loop gets its own engines:
a management command and a test client in one process never share a pool.
"""

import asyncio
from functools import partial
from typing import Any, Final
from weakref import WeakKeyDictionary

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from domainkit.conf import DEFAULT_DB_ALIAS, DatabaseConfig, settings
from domainkit.db.drivers import connect_args
from domainkit.exceptions import ImproperlyConfigured
from domainkit.utils.closing import close_each
from domainkit.utils.readiness import failing_checks

type _Pool = tuple[AsyncEngine, async_sessionmaker[AsyncSession]]


def create_engine(config: DatabaseConfig) -> AsyncEngine:
    """Build a pooled engine; bound values stay out of logs and errors by default."""
    pool = {
        "pool_size": config.POOL_SIZE,
        "max_overflow": config.MAX_OVERFLOW,
        "pool_timeout": config.POOL_TIMEOUT,
        "pool_recycle": config.POOL_RECYCLE,
    }
    options: dict[str, Any] = {k: v for k, v in pool.items() if v is not None}
    return create_async_engine(
        config.URL.get_secret_value(),
        echo=config.ECHO,
        hide_parameters=not config.LOG_PARAMETERS,
        pool_pre_ping=config.POOL_PRE_PING,
        connect_args=connect_args(config),
        **options,
    )


def create_owner_engine(config: DatabaseConfig) -> AsyncEngine:
    """An unpooled engine for the schema owner (``MIGRATION_URL``), without timeouts.

    Migrations and partition maintenance change the schema, which may take
    longer than requests are allowed to, and needs the owner's privileges.
    """
    url = (config.MIGRATION_URL or config.URL).get_secret_value()
    return create_async_engine(
        url,
        poolclass=NullPool,
        hide_parameters=not config.LOG_PARAMETERS,
        connect_args=connect_args(config, url, timeouts=False),
    )


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

    def session(self, alias: str = DEFAULT_DB_ALIAS) -> AsyncSession:
        """Open a session; objects stay readable after commit."""
        return self._pool(alias)[1]()

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
