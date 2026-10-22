"""Principals: who credentials name, kept between requests (ADR 0058).

Authenticating a request takes its credential (an opaque token, a session
cookie, a JSON Web Token's login, a client certificate) to the user it
names, with the user's groups and permissions. The first request of a
credential reads them in two queries (``PrincipalStore``); later ones find
them here, in memory, without a query (``AUTH.PRINCIPAL_CACHE``):

- **What is kept.** A snapshot of the user: its columns, its groups, and
  its permissions (``Principal``); and what the token table says of the
  credential: its kind, expiry, scopes, and login (``Credential``). Never a
  second factor's secret, recovery codes, or the raw token: entries are
  keyed by the class's scheme and the credential's digest. The password
  hash is kept only for ``request.user.password``, deprecated and removed
  in 0.10, which warns (``DomainkitDeprecationWarning``); it is never in a
  ``repr``, a log, a key, or a stamp. Each request gets a user built afresh
  from the snapshot, the same as a query builds.
- **What is checked on every request.** A token's expiry, on
  ``domainkit.clock``; a cookie's CSRF checks; a JSON Web Token's signature
  and times.
- **Bounds.** ``MAX_ENTRIES`` entries at most (the least recently used go
  first), each used for ``TTL`` at most (5 seconds by default, 5 minutes at
  most).
- **In this process, at once.** A commit that changes what a principal holds
  drops it (``invalidate_after_commit``): the repositories mark what they
  delete or update (tokens, logins), and the ORM marks the users, groups,
  permissions, and tokens it changes. A principal read while a change
  commits is not kept.
- **In other processes.** Within the ``TTL``; or at once, when ``STAMPS``
  names a Redis cache: a commit stamps what it changed there, and each
  request compares the stamps of its principal (one round trip). A cache
  that fails is never trusted: the principal is read from the database.
"""

import copy
import logging
import secrets
import threading
import warnings
from collections import OrderedDict
from collections.abc import Callable, Collection, Iterable, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from functools import cache
from pathlib import Path
from typing import Any, Final, Self, cast
from uuid import UUID

import sqlalchemy
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import (
    Mapper,
    Session,
    class_mapper,
    make_transient_to_detached,
    object_session,
)
from sqlalchemy.orm.attributes import instance_state, set_committed_value
from sqlalchemy.sql.elements import ColumnElement

from domainkit import clock
from domainkit.cache import CacheError, caches
from domainkit.contrib.contenttypes.models import ContentType
from domainkit.deprecation import DomainkitDeprecationWarning
from domainkit.log import SECURITY_LOGGER

from . import tokens
from .models import USABLE_PASSWORD, AbstractBaseUser, Group, PasswordHash, Permission
from .settings import MAX_PRINCIPAL_TTL, PrincipalCacheSettings
from .tokens import LAST_USED_EVERY, AuthToken

try:
    from .metrics import observe_principals
except ImportError:  # pragma: no cover - only without the [metrics] extra.

    def observe_principals(authentication: str, event: str, count: int = 1) -> None:
        """Record nothing: ``prometheus_client`` is not installed."""


security_logger = logging.getLogger(SECURITY_LOGGER)

type Key = tuple[str, str]
"""``(scheme, credential)``: the class's scheme, and the credential's digest."""

ALL: Final = "all"
"""The target every principal rests on: invalidating it drops them all."""
STAMP_PREFIX: Final = "principals:"
"""Starts the keys of stamps in the shared cache (after its ``KEY_PREFIX``)."""
STAMP_TTL: Final = 2 * MAX_PRINCIPAL_TTL
"""How long a stamp lives: longer than any principal read before it is used."""
RECENT_INVALIDATIONS: Final = 4096
"""Targets whose last invalidation is remembered, to refuse principals read
during it; an older one refuses every principal read before it."""
PASSWORD: Final = "password"  # noqa: S105 - a field name, not a secret.
"""The user field a request's user reads with a warning (``lend_password``)."""
PASSWORD_DEPRECATED: Final = (
    "request.user.password is deprecated and removed in domainkit 0.10: check "  # noqa: S105 - a message, not a password.
    "a password with domainkit.contrib.auth.api.password_holds(user, password), "
    "or read the hash with await uow.users.password_hash(user.id)."
)
"""What reading ``password`` of a request's user warns."""
_INTERNAL: Final = (str(Path(sqlalchemy.__file__).parent), __file__)
"""Where the warning is not pinned: it names the code that read the password."""
SNAPSHOT_RELATIONSHIPS: Final = frozenset({"groups", "user_permissions"})
"""The user's relationships a snapshot holds."""
UNLOADED: Final = frozenset({"raise", "raise_on_sql", "noload"})
"""Loading strategies that leave a relationship unloaded, in a query too."""
IMMUTABLE: Final = (
    str,
    int,
    float,
    bytes,
    type(None),
    UUID,
    date,
    time,
    timedelta,
    Decimal,
)
"""Column value types each user may share; others are copied for each request.

Subclasses count (asyncpg's ``UUID``, ``datetime``, ``bool``)."""
PENDING: Final = "domainkit.auth.principals.pending"
"""``Session.info`` key: what to drop when the session commits."""
COMMITTED: Final = "domainkit.auth.principals.committed"
"""``Session.info`` key: what the last commit dropped, still to stamp."""


def user_target(user_id: UUID) -> str:
    """What every principal of a user rests on."""
    return f"user:{user_id}"


def token_target(digest: str) -> str:
    """What the principal of a stored token (or certificate) rests on."""
    return f"token:{digest}"


def family_target(family: UUID) -> str:
    """What the principal of a JSON Web Token's login rests on."""
    return f"family:{family}"


@dataclass(frozen=True, slots=True)
class GrantedPermission:
    """A permission and its content type, as a snapshot holds them."""

    id: int
    codename: str
    name: str
    content_type_id: int
    domain: str
    model: str


@dataclass(frozen=True, slots=True)
class Membership:
    """One of the user's groups, with its permissions."""

    id: UUID
    name: str
    version: int
    permissions: tuple[GrantedPermission, ...]


@dataclass(frozen=True, slots=True, repr=False)
class Principal:
    """A user as authorization needs them.

    ``user()`` builds a user from it, detached from any session, as a query
    would: every column, the groups with their permissions, and the
    permissions given directly. The ``repr`` names no field: some are
    personal data, and one is the password hash.
    """

    model: type[AbstractBaseUser]
    is_active: bool
    columns: tuple[tuple[str, object], ...]
    groups: tuple[Membership, ...]
    permissions: tuple[GrantedPermission, ...]
    usable_password: bool
    """Whether some password can log the user in (``has_usable_password``)."""
    password: PasswordHash
    """Only for ``request.user.password``, deprecated: removed in 0.10."""

    def __repr__(self) -> str:
        """Name the user model only."""
        return f"<Principal of a {self.model.__name__}>"

    def user(self) -> AbstractBaseUser:
        """A new user of ``model``, detached from any session.

        Reading its ``password`` warns (``lend_password``): check a password
        with ``password_holds``, or read the hash in a unit of work.
        """
        content_types: dict[int, ContentType] = {}
        permissions: dict[int, Permission] = {}

        def permission(granted: GrantedPermission) -> Permission:
            found = permissions.get(granted.id)
            if found is None:
                kind = content_types.get(granted.content_type_id)
                if kind is None:
                    kind = content_types[granted.content_type_id] = _detached(
                        ContentType,
                        {
                            "id": granted.content_type_id,
                            "domain": granted.domain,
                            "model": granted.model,
                        },
                    )
                found = permissions[granted.id] = _detached(
                    Permission,
                    {
                        "id": granted.id,
                        "codename": granted.codename,
                        "name": granted.name,
                        "content_type_id": granted.content_type_id,
                    },
                    content_type=kind,
                )
            return found

        groups = {
            _detached(
                Group,
                {"id": group.id, "name": group.name, "version": group.version},
                permissions={permission(each) for each in group.permissions},
            )
            for group in self.groups
        }
        user: AbstractBaseUser = _detached(
            self.model,
            {
                name: value if isinstance(value, IMMUTABLE) else copy.deepcopy(value)
                for name, value in self.columns
            },
            groups=groups,
            user_permissions={permission(each) for each in self.permissions},
        )
        lend_password(user, self.password, usable=self.usable_password)
        return user


_FACTORIES: Final[dict[type[Any], Callable[[], Any]]] = {}
"""Each mapped class's factory of instances, as a query makes them."""


def _factory(cls: type[Any]) -> Callable[[], Any]:
    """The mapper's factory of ``cls``: instances without ``__init__``, as loaded."""
    factory = _FACTORIES.get(cls)
    if factory is None:
        factory = _FACTORIES[cls] = class_mapper(cls).class_manager.new_instance
    return factory


def _detached[T](cls: type[T], columns: dict[str, object], **relations: object) -> T:
    """An instance of mapped ``cls``, detached, as a closed session leaves it.

    ``columns`` are its loaded column values; ``relations`` its loaded
    relationships. Whatever is left out is unloaded.
    """
    instance = cast(T, _factory(cls)())
    instance.__dict__.update(columns)
    for name, value in relations.items():
        set_committed_value(instance, name, value)
    make_transient_to_detached(instance)
    return instance


class _LentPassword:
    """Loads ``password`` of a request's user: the hash read with it, and a warning."""

    __slots__ = ("_hashed",)

    def __init__(self, hashed: PasswordHash) -> None:
        """Lend ``hashed``."""
        self._hashed = hashed

    def __repr__(self) -> str:
        """Hide the hash."""
        return "<password lent to a request's user>"

    def __call__(self, state: object, passive: object) -> PasswordHash:
        """Warn (``PASSWORD_DEPRECATED``); SQLAlchemy keeps what this returns."""
        warnings.warn(
            PASSWORD_DEPRECATED,
            DomainkitDeprecationWarning,
            skip_file_prefixes=_INTERNAL,
        )
        return self._hashed


def lend_password(
    user: AbstractBaseUser, hashed: PasswordHash, *, usable: bool
) -> None:
    """Let ``user`` read ``hashed`` as its ``password``, warning the first time.

    ``request.user.password`` is deprecated (removed in 0.10). Until then it
    reads the hash the user was built with, as SQLAlchemy loads a deferred
    column; ``has_usable_password`` answers ``usable`` without it.
    """
    state = instance_state(user)
    state.dict.pop(PASSWORD, None)
    state.dict[USABLE_PASSWORD] = usable
    state.expired_attributes.discard(PASSWORD)
    if "callables" not in state.__dict__:
        state.callables = {}
    state.callables[PASSWORD] = _LentPassword(hashed)


@dataclass(frozen=True, slots=True)
class UserShape:
    """How a user model is read into a ``Principal``: its loaded columns."""

    columns: tuple[tuple[str, ColumnElement[Any]], ...]
    """``(attribute, column)`` of each column a query loads, but the password."""
    password: ColumnElement[Any]
    """The password's column, read apart (``lend_password``)."""


@cache
def user_shape(model: type[AbstractBaseUser]) -> UserShape | None:
    """How to snapshot ``model``; None when a snapshot cannot hold it.

    A model that loads relationships of its own with the user (``selectin``,
    ``joined``) is read as before, by the ORM, and never kept.
    """
    mapper = class_mapper(model)
    for relationship in mapper.relationships:
        if relationship.key in SNAPSHOT_RELATIONSHIPS:
            continue
        if relationship.lazy not in UNLOADED:
            return None
    return UserShape(
        tuple(
            (prop.key, prop.columns[0])
            for prop in mapper.column_attrs
            if prop.key != PASSWORD and not prop.deferred
        ),
        mapper.column_attrs[PASSWORD].columns[0],
    )


@dataclass(frozen=True, slots=True)
class Credential:
    """What the token table says of a credential (never the token itself)."""

    id: str
    """The token's digest (a certificate's SHA-256)."""
    user_id: UUID
    kind: str
    family: UUID | None
    expires_at: datetime | None
    scopes: frozenset[str] | None
    last_used_at: datetime | None

    def expired(self, now: datetime | None = None) -> bool:
        """Whether the token's lifetime is over, as ``AuthToken.expired``."""
        return self.expires_at is not None and (
            (now or tokens.utc_now()) >= self.expires_at
        )


@dataclass(frozen=True, slots=True)
class Login:
    """A JSON Web Token's login: whether its tokens still work, and until when."""

    active: bool
    """Whether the login has a token at all."""
    until: datetime | None
    """When its last token expires; None: one never does."""

    def active_at(self, now: datetime) -> bool:
        """Whether the login has an unexpired token at ``now``."""
        return self.active and (self.until is None or now < self.until)


@dataclass(frozen=True, slots=True)
class Snapshot:
    """What ``PrincipalStore.principal`` read, and whether it still held."""

    principal: Principal | None = None
    loaded: AbstractBaseUser | None = None
    """For a model a snapshot cannot hold: the user the ORM loaded."""
    current: bool = True
    """Whether the credential checked with it was still stored."""


@dataclass(slots=True, eq=False)
class Entry:
    """A credential, the user it names, and what keeps the pair valid.

    One per key while kept. A loaded entry is kept (``remember``) only once
    its class accepted the credential, and only if nothing it rests on
    changed while it was read.
    """

    key: Key
    principal: Principal | None
    """None: the user is gone, or a model a snapshot cannot hold (``loaded``)."""
    credential: Credential | None = None
    login: Login | None = None
    targets: tuple[str, ...] = ()
    """What it rests on: invalidating one of them drops it."""
    stamps: tuple[str | None, ...] | None = None
    """The shared stamps of ``ALL`` and ``targets`` when it was read."""
    started: int = 0
    """``PrincipalCache.begin()`` before it was read."""
    keep: bool = False
    """Whether it may be kept once accepted (a read that raced a change may not)."""
    loaded: AbstractBaseUser | None = None
    """The user the ORM loaded, for a model a snapshot cannot hold."""
    last_used: datetime | None = None
    """When ``last_used_at`` was last written, as far as this process knows."""
    deadline: float = 0.0
    """``clock.monotonic()`` past which it is read again."""
    fresh: bool = True
    """Read from the database by this request, not kept yet."""

    @property
    def scheme(self) -> str:
        """The authentication class's scheme (the metrics' label)."""
        return self.key[0]

    def holding(self, snapshot: Snapshot) -> Self:
        """This entry, with what ``PrincipalStore.principal`` read.

        A credential gone meanwhile, or a user the ORM loaded, is never kept.
        """
        self.principal = snapshot.principal
        self.loaded = snapshot.loaded
        self.keep = self.keep and snapshot.current and snapshot.loaded is None
        return self

    def user(self) -> AbstractBaseUser | None:
        """A new user built from the principal, if they exist and are active."""
        if self.loaded is not None:
            return self.loaded if self.loaded.is_active else None
        if self.principal is None or not self.principal.is_active:
            return None
        return self.principal.user()


class PrincipalCache:
    """Entries by key, bounded in number and age, dropped by what they rest on.

    Every event loop of the process shares it (free-threaded Python too):
    its state is only touched under its lock.
    """

    def __init__(self, *, recent: int = RECENT_INVALIDATIONS) -> None:
        """Start empty; remember the last ``recent`` targets invalidated."""
        self._lock = threading.Lock()
        self._entries: OrderedDict[Key, Entry] = OrderedDict()
        self._by_target: dict[str, set[Key]] = {}
        self._sequence = 0
        """Counts invalidations; ``begin`` hands out its value."""
        self._invalidated: OrderedDict[str, int] = OrderedDict()
        """Targets, each with the sequence of its last invalidation."""
        self._floor = 0
        """The sequence of the last invalidation no longer in ``_invalidated``."""
        self._recent = recent

    def __len__(self) -> int:
        """How many entries are kept."""
        with self._lock:
            return len(self._entries)

    def begin(self) -> int:
        """A mark taken before reading a principal, for ``put``.

        An invalidation after it, of anything the principal rests on, keeps
        ``put`` from keeping what was read: it may predate the change.
        """
        with self._lock:
            return self._sequence

    def get(self, key: Key, now: float) -> Entry | None:
        """The entry of ``key``, unless it is missing or past its deadline."""
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                return None
            if entry.deadline <= now:
                self._remove(key)
                return None
            self._entries.move_to_end(key)
            return entry

    def put(self, entry: Entry, *, limit: int) -> list[Entry] | None:
        """Keep ``entry``; return the entries evicted for room.

        None: it was not kept, as something it rests on was invalidated
        after ``entry.started``.
        """
        with self._lock:
            if entry.started < self._floor or any(
                self._invalidated.get(target, -1) > entry.started
                for target in (ALL, *entry.targets)
            ):
                return None
            self._remove(entry.key)
            self._entries[entry.key] = entry
            for target in entry.targets:
                self._by_target.setdefault(target, set()).add(entry.key)
            evicted = []
            while len(self._entries) > limit:
                oldest = next(iter(self._entries))
                evicted.append(self._remove(oldest))
            entry.fresh = False
            return [each for each in evicted if each is not None]

    def discard(self, entry: Entry) -> bool:
        """Drop ``entry`` if it is still the one kept under its key."""
        with self._lock:
            if self._entries.get(entry.key) is not entry:
                return False
            self._remove(entry.key)
            return True

    def invalidate(self, targets: Collection[str]) -> list[Entry]:
        """Drop the entries that rest on any of ``targets``; return them.

        ``ALL`` drops every entry. Entries read before now are refused by
        ``put`` from now on.
        """
        with self._lock:
            self._sequence += 1
            for target in targets:
                self._invalidated[target] = self._sequence
                self._invalidated.move_to_end(target)
            while len(self._invalidated) > self._recent:
                _, sequence = self._invalidated.popitem(last=False)
                self._floor = max(self._floor, sequence)
            if ALL in targets:
                dropped = list(self._entries.values())
                self._entries.clear()
                self._by_target.clear()
                return dropped
            keys = {
                key for target in targets for key in self._by_target.get(target, ())
            }
            return [entry for key in keys if (entry := self._remove(key)) is not None]

    def claim_touch(self, entry: Entry, now: datetime) -> bool:
        """Whether this request writes the credential's ``last_used_at``.

        Once per ``LAST_USED_EVERY`` per entry: the first request to find it
        due claims it, so concurrent requests do not all write.
        """
        with self._lock:
            if entry.last_used is not None and now - entry.last_used < LAST_USED_EVERY:
                return False
            entry.last_used = now
            return True

    def _remove(self, key: Key) -> Entry | None:
        """Forget the entry of ``key`` and its targets; the lock is held."""
        entry = self._entries.pop(key, None)
        if entry is None:
            return None
        for target in entry.targets:
            keys = self._by_target.get(target, set())
            keys.discard(key)
            if not keys:
                self._by_target.pop(target, None)
        return entry


principal_cache: Final = PrincipalCache()
"""The process's principals."""


def invalidate(targets: Collection[str]) -> int:
    """Drop the principals that rest on ``targets`` in this process; how many.

    For changes made outside the auth repositories and the ORM, such as
    a ``DELETE`` in SQL: ``invalidate([user_target(user_id)])``. Prefer
    ``invalidate_after_commit``, which waits for the change to commit.
    """
    dropped = principal_cache.invalidate(targets)
    _count_dropped(dropped, "invalidation")
    return len(dropped)


def invalidate_all() -> int:
    """Drop every principal of this process; how many."""
    return invalidate([ALL])


async def invalidate_everywhere(
    targets: Collection[str], config: PrincipalCacheSettings
) -> int:
    """Drop ``targets`` here, and stamp them for every other process.

    For a change made in SQL, after it commits. Without ``STAMPS``, other
    processes see it within ``TTL``. Returns how many were dropped here.

    Raises:
        CacheError: The shared cache failed: other processes see it within
            ``TTL``.
    """
    dropped = invalidate(targets)
    if config.STAMPS is not None:
        await write_stamps(config.STAMPS, targets)
    return dropped


def invalidate_after_commit(
    session: Session | AsyncSession, targets: Iterable[str]
) -> None:
    """Drop what ``targets`` name once ``session`` commits, and stamp them.

    Dropping before the commit would let a request read the old rows again
    in between and keep them.
    """
    session.info.setdefault(PENDING, set()).update(targets)


def _committed(session: Session) -> None:
    """Drop what the commit changed (``Session`` ``after_commit``)."""
    targets = session.info.pop(PENDING, None)
    if targets:
        invalidate(targets)
        session.info.setdefault(COMMITTED, set()).update(targets)


async def stamp_committed(
    session: AsyncSession, config: PrincipalCacheSettings
) -> None:
    """Stamp, in the shared cache, what ``session``'s commits changed.

    Without ``STAMPS`` there is nothing to stamp. A cache that fails is
    logged: other processes then see the change within ``TTL``.
    """
    targets = session.info.pop(COMMITTED, None)
    if not targets or config.STAMPS is None:
        return
    try:
        await write_stamps(config.STAMPS, targets)
    except CacheError as exc:
        security_logger.warning(
            "Could not stamp %d changed principals in cache %r (%s); other "
            "processes keep theirs for up to %s",
            len(targets),
            config.STAMPS,
            type(exc).__name__,
            config.TTL,
        )


def stamp_key(target: str) -> str:
    """The shared cache's key of ``target``'s stamp."""
    return STAMP_PREFIX + target


async def read_stamps(alias: str, targets: Sequence[str]) -> tuple[str | None, ...]:
    """The stamps of ``ALL`` and ``targets``, in one round trip.

    Raises:
        CacheError: The cache failed; nothing it says can be trusted.
    """
    keys = [stamp_key(ALL), *map(stamp_key, targets)]
    found = await caches[alias].get_many(keys, str)
    return tuple(found.get(key) for key in keys)


async def write_stamps(alias: str, targets: Collection[str]) -> None:
    """Give each of ``targets`` a new random stamp, for ``STAMP_TTL``.

    Random, not counted: a stamp that expired and came back can never equal
    one read before.

    Raises:
        CacheError: The cache failed.
    """
    await caches[alias].set_many(
        {stamp_key(target): secrets.token_hex(8) for target in targets},
        ttl=STAMP_TTL,
    )


async def recall(key: Key, config: PrincipalCacheSettings) -> Entry | None:
    """The kept entry of ``key``, if it still holds here, and everywhere.

    With ``STAMPS``, its stamps must be unchanged; a cache that fails
    confirms nothing, so the entry is not used.
    """
    if not config.ENABLED:
        return None
    entry = principal_cache.get(key, clock.monotonic())
    if entry is not None and config.STAMPS is not None:
        try:
            current = await read_stamps(config.STAMPS, entry.targets)
        except CacheError:
            entry = None
        else:
            if current != entry.stamps:
                if principal_cache.discard(entry):
                    observe_principals(entry.scheme, "invalidation")
                entry = None
    observe_principals(key[0], "miss" if entry is None else "hit")
    return entry


async def stamps_before_reading(entry: Entry, config: PrincipalCacheSettings) -> None:
    """Read ``entry``'s stamps now, before its rows; mark whether it may be kept.

    A principal is kept only with the stamps read before it, so a change
    stamped after that tells it apart. Disabled, or with a cache that
    fails, it is not kept.
    """
    if not config.ENABLED:
        return
    if config.STAMPS is not None:
        try:
            entry.stamps = await read_stamps(config.STAMPS, entry.targets)
        except CacheError:
            return
    entry.keep = True


def remember(entry: Entry, config: PrincipalCacheSettings) -> None:
    """Keep a loaded ``entry`` its class accepted, for ``TTL``."""
    if not (entry.fresh and entry.keep and config.ENABLED):
        return
    entry.deadline = clock.monotonic() + config.TTL.total_seconds()
    evicted = principal_cache.put(entry, limit=config.MAX_ENTRIES)
    if evicted is not None:
        _count_dropped(evicted, "eviction")


def forget(entry: Entry) -> None:
    """Drop a kept ``entry`` that no longer holds (its token expired)."""
    if not entry.fresh and principal_cache.discard(entry):
        observe_principals(entry.scheme, "invalidation")


def _count_dropped(entries: Iterable[Entry], event: str) -> None:
    """Count dropped entries, by their authentication class."""
    counts: dict[str, int] = {}
    for entry in entries:
        counts[entry.scheme] = counts.get(entry.scheme, 0) + 1
    for scheme, count in counts.items():
        observe_principals(scheme, event, count)


def watch(user_model: type[AbstractBaseUser]) -> None:
    """Drop principals when the ORM changes what they hold (idempotent).

    A user updated (any field, a group joined or left, a permission given
    or taken) or deleted drops that user's principals; a group or a
    permission changed or deleted drops every principal; a token updated
    or deleted, its own. Each takes effect when its session commits.
    """
    listeners: list[tuple[type, str, Any]] = [
        (user_model, "after_update", _user_changed),
        (user_model, "after_delete", _user_changed),
        (Group, "after_update", _everyone_changed),
        (Group, "after_delete", _everyone_changed),
        (Permission, "after_update", _everyone_changed),
        (Permission, "after_delete", _everyone_changed),
        (AuthToken, "after_update", _token_changed),
        (AuthToken, "after_delete", _token_changed),
        (Session, "after_commit", _committed),
    ]
    for target, name, listener in listeners:
        if not event.contains(target, name, listener):
            event.listen(target, name, listener, propagate=True)


def _user_changed(mapper: Mapper[Any], connection: object, user: Any) -> None:  # noqa: ANN401 - the mapped user.
    """A user's row or links changed: drop their principals at commit."""
    _after_commit_of(user, [user_target(user.id)])


def _everyone_changed(mapper: Mapper[Any], connection: object, target: object) -> None:
    """A group or a permission changed: drop every principal at commit."""
    _after_commit_of(target, [ALL])


def _token_changed(mapper: Mapper[Any], connection: object, token: AuthToken) -> None:
    """A stored token changed: drop its principal (and its login's) at commit."""
    targets = [token_target(token.id)]
    if token.family is not None:
        targets.append(family_target(token.family))
    _after_commit_of(token, targets)


def _after_commit_of(instance: object, targets: list[str]) -> None:
    """Drop ``targets`` when the session of ``instance`` commits."""
    session = object_session(instance)
    if session is not None:
        invalidate_after_commit(session, targets)
