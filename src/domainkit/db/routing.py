"""Where each domain's data lives: routing targets, aliases, and shards.

A **target** is a SQL alias or a shard group (``SHARDS``). ``DATABASE_ROUTING``
maps domain labels to targets; unlisted domains use ``default``. Migrations
and units of work read the same routing, so a domain's tables and sessions
are always in the same place.

Inside a shard group a shard key picks one database: it hashes to one of
``BUCKETS`` buckets (BLAKE2b, so every process agrees), and each bucket
belongs to one alias. ``PINNED`` keys skip the hash. See ADRs 0008 and 0009.

An alias may name read replicas (``REPLICAS``): read-only units of work
take turns on them, and everything else uses the alias itself, the primary.
A replica is never a target: nothing writes to it or migrates it (ADR 0024).
"""

import asyncio
import hashlib
import itertools
import uuid
from bisect import bisect_right
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from domainkit.apps import registry
from domainkit.conf import DEFAULT_DB_ALIAS, ShardConfig, settings
from domainkit.exceptions import ImproperlyConfigured

if TYPE_CHECKING:
    from sqlalchemy import Table

    from domainkit.apps import DomainConfig

__all__ = [
    "bucket_for",
    "for_each_shard",
    "router",
]

type ShardKey = str | int | uuid.UUID
"""A value that picks a shard: a tenant id, a customer id, an account number."""

_HASH_BYTES: Final = 8


def shard_text(key: ShardKey) -> str:
    """The canonical text of a shard key, hashed and matched against ``PINNED``."""
    if isinstance(key, bool) or not isinstance(key, str | int | uuid.UUID):
        raise TypeError(
            f"A shard key is a str, int, or UUID, not {type(key).__name__}."
        )
    return str(key)


def bucket_for(key: ShardKey, buckets: int) -> int:
    """The bucket of ``key`` among ``buckets``; stable across processes and hosts."""
    digest = hashlib.blake2b(shard_text(key).encode(), digest_size=_HASH_BYTES)
    return int.from_bytes(digest.digest(), "big") % buckets


@dataclass(frozen=True, slots=True)
class ShardMap:
    """A shard group's layout: which alias owns each bucket, and the pins."""

    name: str
    config: ShardConfig

    def alias_for(self, key: ShardKey) -> str:
        """The alias that holds the data of ``key``."""
        text = shard_text(key)
        if text in self.config.PINNED:
            return self.config.PINNED[text]
        return self.owner_of(bucket_for(text, self.config.BUCKETS))

    def owner_of(self, bucket: int) -> str:
        """The alias that owns ``bucket``."""
        if not 0 <= bucket < self.config.BUCKETS:
            raise ValueError(f"Bucket {bucket} is outside 0-{self.config.BUCKETS - 1}.")
        owners = sorted(
            (low, alias)
            for alias, ranges in self.config.ranges().items()
            for low, _ in ranges
        )
        starts = [low for low, _ in owners]
        return owners[bisect_right(starts, bucket) - 1][1]

    @property
    def aliases(self) -> tuple[str, ...]:
        """The group's aliases, in settings order."""
        return self.config.DATABASES


class DatabaseRouter:
    """Answer where data lives, from ``DATABASES``, ``SHARDS``, and routing."""

    _turns: Final = itertools.count()
    """Read-only units of work take turns on an alias's replicas."""

    def target_for(self, label: str) -> str:
        """The target holding the tables and sessions of domain ``label``."""
        return settings.DATABASE_ROUTING.get(label, DEFAULT_DB_ALIAS)

    def target_for_module(self, module: str) -> str:
        """The target of the installed domain that contains ``module``.

        Code outside every installed domain uses ``default``.
        """
        owners = [
            config
            for config in registry
            if module == config.name or module.startswith(f"{config.name}.")
        ]
        if not owners:
            return DEFAULT_DB_ALIAS
        return self.target_for(max(owners, key=lambda config: len(config.name)).label)

    def is_shard_group(self, target: str) -> bool:
        """Whether ``target`` names a shard group."""
        return target in settings.SHARDS

    def shards(self, group: str) -> ShardMap:
        """The layout of shard group ``group``."""
        try:
            return ShardMap(group, settings.SHARDS[group])
        except KeyError:
            raise ImproperlyConfigured(f"SHARDS has no {group!r} group.") from None

    def aliases_of(self, target: str) -> tuple[str, ...]:
        """The SQL aliases behind ``target``: the group's, or the alias itself.

        A replica is no target: what is written or migrated goes to its primary.
        """
        if self.is_shard_group(target):
            return self.shards(target).aliases
        if target not in settings.DATABASES:
            raise ImproperlyConfigured(
                f"{target!r} is neither a SQL alias in DATABASES nor a shard group."
            )
        if (primary := self.primary_of(target)) is not None:
            raise ImproperlyConfigured(
                f"{target!r} is a read replica of {primary!r}; use {primary!r}, "
                "with readonly=True to read from the replica."
            )
        return (target,)

    def primary_of(self, alias: str) -> str | None:
        """The alias whose ``REPLICAS`` list ``alias``; None for a primary."""
        for primary, config in settings.DATABASES.items():
            if alias in config.REPLICAS:
                return primary
        return None

    def read_alias(self, alias: str) -> str:
        """Where read-only work on ``alias`` goes: its replicas in turn, or itself."""
        replicas = settings.DATABASES[alias].REPLICAS
        if not replicas:
            return alias
        return replicas[next(self._turns) % len(replicas)]

    def target_of_alias(self, alias: str) -> str:
        """The target an alias belongs to: its shard group, or itself."""
        for group, config in settings.SHARDS.items():
            if alias in config.DATABASES:
                return group
        return alias

    def require_standalone(self, alias: str, setting: str) -> str:
        """Check that ``setting`` names a SQL alias outside every shard group.

        Apps keep their home rows there (the home outbox, explicit audit
        records, idempotency keys), so it must be one database.
        """
        if alias in settings.DATABASES and self.target_of_alias(alias) == alias:
            return alias
        raise ImproperlyConfigured(
            f"{setting} = {alias!r} must name a SQL alias in DATABASES that is "
            "not part of a shard group."
        )

    def alias_for(self, target: str, shard_key: ShardKey | None = None) -> str:
        """The one alias a unit of work on ``target`` uses.

        A shard group needs ``shard_key``; a plain alias refuses one, so a
        missing or misplaced key fails at once instead of writing elsewhere.
        """
        if self.is_shard_group(target):
            if shard_key is None:
                raise ImproperlyConfigured(
                    f"{target!r} is a shard group; pass shard_key to pick a database."
                )
            return self.shards(target).alias_for(shard_key)
        if shard_key is not None:
            raise ImproperlyConfigured(
                f"{target!r} is not a shard group; it takes no shard_key."
            )
        return self.aliases_of(target)[0]

    def hosting_targets(self) -> tuple[str, ...]:
        """Targets holding at least one installed domain, ``default`` first."""
        return _unique(
            [DEFAULT_DB_ALIAS, *(self.target_for(config.label) for config in registry)]
        )

    def migration_targets(self) -> tuple[str, ...]:
        """Every target some installed app keeps tables in, ``default`` first."""
        if not settings.DATABASES:
            return ()
        return _unique(
            [
                DEFAULT_DB_ALIAS,
                *(target for config in registry for target in self.targets_of(config)),
            ]
        )

    def targets_of(self, config: "DomainConfig") -> tuple[str, ...]:
        """The targets holding ``config``'s tables, in a stable order."""
        return tuple(sorted(config.databases(self)))

    def holds_tables(self, label: str | None, target: str) -> bool:
        """Whether tables of domain ``label`` belong in ``target``.

        Tables without a domain belong to ``default``. Tables of domains that
        are not installed belong nowhere, so they are never created or dropped.
        """
        if label is None:
            return target == DEFAULT_DB_ALIAS
        try:
            config = registry.get_domain_config(label)
        except LookupError:
            return False
        return target in config.databases(self)

    def holds_table(self, table: "Table", target: str) -> bool:
        """Whether ``table`` belongs in ``target`` and its domain uses it now."""
        label = table.info.get("domain")
        if not self.holds_tables(label, target):
            return False
        return label is None or registry.get_domain_config(label).holds_table(table)


def _unique(items: Iterable[str]) -> tuple[str, ...]:
    """Drop repeats, keeping the first occurrence of each item."""
    return tuple(dict.fromkeys(items))


router: Final = DatabaseRouter()
"""The process-wide router; it reads settings on every call."""


async def for_each_shard[T](
    group: str, function: Callable[[str], Awaitable[T]]
) -> dict[str, T]:
    """Run ``function(alias)`` on every database of ``group`` at once.

    For reports and maintenance: there are no transactions across shards, so
    each call works on its own database.
    """
    aliases = router.shards(group).aliases
    results = await asyncio.gather(*(function(alias) for alias in aliases))
    return dict(zip(aliases, results, strict=True))
