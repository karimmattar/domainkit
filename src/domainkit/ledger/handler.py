"""Ledgers per ``LEDGERS`` alias (the ledger counterpart of ``connections``)."""

from collections.abc import Callable, Mapping
from functools import partial
from typing import Final

from domainkit.conf import LedgerConfig, settings
from domainkit.exceptions import ImproperlyConfigured
from domainkit.ledger.base import Ledger
from domainkit.ledger.immudb import ImmudbLedger
from domainkit.ledger.memory import MemoryLedger
from domainkit.utils.closing import close_each
from domainkit.utils.readiness import failing_checks

type LedgerFactory = Callable[[LedgerConfig], Ledger]

FACTORIES: Final[Mapping[str, LedgerFactory]] = {
    "immudb": ImmudbLedger,
    "memory": lambda config: MemoryLedger(),
}
"""Adapters by URL scheme."""


class LedgerHandler:
    """Create each configured ledger on first use and keep it for the process.

    Ledgers are not bound to an event loop (immudb calls run on their own
    thread), so one instance per alias serves the whole process. A changed
    configuration (``override_settings`` in tests) gets a new instance.
    """

    def __init__(self, factories: Mapping[str, LedgerFactory] = FACTORIES) -> None:
        """Use ``factories`` to build ledgers by URL scheme."""
        self._factories = factories
        self._ledgers: dict[tuple[str, LedgerConfig], Ledger] = {}

    def config(self, alias: str) -> LedgerConfig:
        """Return the ``LEDGERS`` entry of ``alias``."""
        try:
            return settings.LEDGERS[alias]
        except KeyError:
            hint = "; it is a SQL database" if alias in settings.DATABASES else ""
            raise ImproperlyConfigured(
                f"LEDGERS has no {alias!r} alias{hint}."
            ) from None

    def __getitem__(self, alias: str) -> Ledger:
        """The ledger of ``alias``."""
        config = self.config(alias)
        key = (alias, config)
        if key not in self._ledgers:
            self._ledgers[key] = self._factories[config.scheme](config)
        return self._ledgers[key]

    async def check_all(self, seconds: float) -> list[str]:
        """Check every configured ledger; return ``ledger:<alias>`` for failures."""
        return await failing_checks(
            {
                f"ledger:{alias}": partial(self._check, alias)
                for alias in settings.LEDGERS
            },
            seconds,
        )

    async def _check(self, alias: str) -> None:
        """Run one ledger's readiness check."""
        await self[alias].check()

    def reset(self) -> None:
        """Forget the memory ledgers made so far: the next use starts empty (tests).

        Ledgers that hold connections (immudb's) stay until ``close_all``.
        The test plugin calls it before each test.
        """
        self._ledgers = {
            key: ledger
            for key, ledger in self._ledgers.items()
            if not isinstance(ledger, MemoryLedger)
        }

    async def close_all(self) -> None:
        """Close every ledger opened so far, even if one fails (it is raised).

        Each ledger's own ``close``: ``Ledger.close`` is the base class's,
        which closes nothing.
        """
        ledgers, self._ledgers = self._ledgers, {}
        await close_each(ledgers.values(), lambda ledger: ledger.close())


ledgers: Final = LedgerHandler()
"""The process-wide ledgers: ``ledgers["evidence"]``."""
