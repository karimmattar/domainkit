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

import asyncio
import copy
import logging
import secrets
import threading
import warnings
from collections import OrderedDict
from collections.abc import (
    Awaitable,
    Callable,
    Collection,
    Hashable,
    Iterable,
    Mapping,
    Sequence,
)
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from functools import cache, partial
from pathlib import Path
from types import MappingProxyType
from typing import Any, Final, Self, cast
from uuid import UUID

import sqlalchemy
from sqlalchemy import Table, event
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import (
    Mapper,
    ORMExecuteState,
    Session,
    class_mapper,
    make_transient_to_detached,
    object_session,
)
from sqlalchemy.orm.attributes import instance_state, set_committed_value
from sqlalchemy.sql.elements import ColumnElement

from domainkit import clock
from domainkit.cache import CacheError, caches
from domainkit.contrib.contenttypes import ContentType
from domainkit.contrib.contenttypes.tables import content_types
from domainkit.deprecation import DomainkitDeprecationWarning
from domainkit.exceptions import ImproperlyConfigured
from domainkit.log import SECURITY_LOGGER
from domainkit.tasks import spawn
from domainkit.utils.loops import PerLoop

from . import tokens
from .models import (
    EVERY,
    GRANTED,
    USABLE_PASSWORD,
    AbstractBaseUser,
    Group,
    PasswordHash,
    Permission,
)
from .settings import MAX_PRINCIPAL_TTL, PrincipalCacheSettings, auth_settings
from .tables import (
    group_permissions,
    groups,
    permissions,
    user_link_tables,
)
from .tables import tokens as token_rows
from .tokens import LAST_USED_EVERY, AuthToken

try:
    from .metrics import observe_principals
except ImportError:  # pragma: no cover - only without the [metrics] extra.

    def observe_principals(authentication: str, event: str, count: int = 1) -> None:
        """Record nothing: ``prometheus_client`` is not installed."""


__all__ = [
    "invalidate",
    "invalidate_all",
    "invalidate_everywhere",
]


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
REFRESH_SHARE: Final = 0.2
"""The last share of an entry's ``TTL`` in which the first request to use it
reads it again, while the others are served it (``kept_or_read``)."""
PENDING: Final = "domainkit.auth.principals.pending"
"""``Session.info`` key: what to drop when the session commits."""
STAMPING: Final = "domainkit.auth.principals.stamping"
"""``Session.info`` key: the tasks stamping what its commits changed."""
STAMP_WRITE_TIMEOUT: Final = 5.0
"""Seconds a commit's stamps may take to write before they are given up."""
HANDLED: Final = {"domainkit_principals_handled": True}
"""Execution options of a statement whose effect on principals is handled.

The auth repositories' own writes mark what they change themselves (a
revocation's ``RETURNING``), or change nothing a principal holds (a last
use): ``.execution_options(**HANDLED)`` keeps them from dropping every
principal as other bulk statements do (``watch``)."""
_BULK_WATCHED: Final[dict[str, frozenset[str]]] = {}
"""Table names whose bulk ``UPDATE``, ``DELETE`` (or ``INSERT``) through a
session drop every principal, with the statements that do (``watch``)."""


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
    key: str = field(init=False, repr=False, compare=False)
    """``<domain>.<codename>``, as ``Permission.key`` and ``has_perm`` name it."""

    def __post_init__(self) -> None:
        """Work out the key once, for every user built with the permission."""
        object.__setattr__(self, "key", f"{self.domain}.{self.codename}")


@dataclass(frozen=True, slots=True)
class Membership:
    """One of the user's groups, with its permissions."""

    id: UUID
    name: str
    version: int
    permissions: tuple[GrantedPermission, ...]
    keys: frozenset[str] = field(init=False, repr=False, compare=False)
    """The keys of ``permissions`` (``Group.permission_keys``)."""

    def __post_init__(self) -> None:
        """Work out the keys once, for every user built with the group."""
        object.__setattr__(self, "keys", _keys(self.permissions))


NO_KEYS: Final[frozenset[str]] = frozenset()


def _keys(permissions: Iterable[GrantedPermission]) -> frozenset[str]:
    """The keys of ``permissions``, as one shared set (``shared``)."""
    return _union([frozenset(permission.key for permission in permissions)])


def _union(sets: Iterable[frozenset[str]]) -> frozenset[str]:
    """The union of ``sets``, as one shared set (``shared``).

    Users with the same grants share one set, as they share the grants; the
    union of one set and empty ones is that set.
    """
    found = [each for each in sets if each]
    if not found:
        return NO_KEYS
    if len(found) == 1:
        return shared(found[0])
    return shared(frozenset().union(*found))


INTERNED_MAX: Final = 65_536
"""Grants interned per process (``shared``) at most; past it, interning restarts."""
_INTERNED: Final[dict[Hashable, Hashable]] = {}


def shared[T: Hashable](value: T) -> T:
    """The one copy of ``value`` this process's principals hold.

    The users of a group, and each user's credentials, share its grants: a
    principal costs about the same whatever its role holds. Bounded by
    ``INTERNED_MAX``: past it, interning starts afresh (kept principals keep
    what they hold).
    """
    found = _INTERNED.get(value)
    if found is not None:
        return cast(T, found)
    if len(_INTERNED) >= INTERNED_MAX:
        _INTERNED.clear()
    _INTERNED[value] = value
    return value


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
    granted: Mapping[str, frozenset[str]] = field(init=False, compare=False)
    """The keys of what the user holds, by relationship, and ``EVERY`` key
    (``models.GRANTED``): what ``has_perm`` reads of a request's user."""

    def __post_init__(self) -> None:
        """Work out the keys of the grants once, for every user built."""
        direct = _keys(self.permissions)
        by_group = _union(group.keys for group in self.groups)
        every = _union((direct, by_group))
        object.__setattr__(
            self,
            "granted",
            MappingProxyType(
                {
                    "user_permissions": direct,
                    "groups": by_group,
                    EVERY: every,
                }
            ),
        )

    def __repr__(self) -> str:
        """Name the user model only."""
        return f"<Principal of a {self.model.__name__}>"

    def user(self) -> AbstractBaseUser:
        """A new user of ``model``, detached from any session.

        Its groups and permissions are built the first time they are read,
        as new objects of its own, the same a query loads; until then
        ``has_perm`` answers from the keys worked out with the principal
        (ADR 0059's amendment). Reading its ``password`` warns
        (``lend_password``): check a password with ``password_holds``, or
        read the hash in a unit of work.
        """
        built = _Grants()
        user: AbstractBaseUser = _detached(
            self.model,
            {
                name: value if isinstance(value, IMMUTABLE) else copy.deepcopy(value)
                for name, value in self.columns
            },
        )
        _built_later(
            user,
            self.granted,
            groups=partial(built.groups, self.groups),
            user_permissions=partial(built.permissions, self.permissions),
        )
        lend_password(user, self.password, usable=self.usable_password)
        return user


class _Grants:
    """Builds one request's groups and permissions, each object once.

    As the ORM loads them: a permission given directly and by a group, or a
    content type of several permissions, is one object however reached.
    """

    __slots__ = ("_content_types", "_permissions")

    def __init__(self) -> None:
        """Start with nothing built."""
        self._content_types: dict[int, ContentType] = {}
        self._permissions: dict[int, Permission] = {}

    def permission(self, granted: GrantedPermission) -> Permission:
        """The request's ``Permission`` of ``granted``, with its content type."""
        found = self._permissions.get(granted.id)
        if found is None:
            kind = self._content_types.get(granted.content_type_id)
            if kind is None:
                kind = self._content_types[granted.content_type_id] = _detached(
                    ContentType,
                    {
                        "id": granted.content_type_id,
                        "domain": granted.domain,
                        "model": granted.model,
                    },
                )
            found = self._permissions[granted.id] = _detached(
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

    def permissions(
        self, granted: Sequence[GrantedPermission], *_: object
    ) -> list[Permission]:
        """The request's ``Permission`` of each of ``granted`` (a loader)."""
        return [self.permission(each) for each in granted]

    def groups(self, memberships: Sequence[Membership], *_: object) -> list[Group]:
        """A new ``Group`` of each membership, its permissions built later.

        A loader, as ``permissions``.
        """
        found = []
        for membership in memberships:
            group = _detached(
                Group,
                {
                    "id": membership.id,
                    "name": membership.name,
                    "version": membership.version,
                },
            )
            _built_later(
                group,
                {"permissions": membership.keys},
                permissions=partial(self.permissions, membership.permissions),
            )
            found.append(group)
        return found


def _built_later(
    instance: object,
    granted: Mapping[str, frozenset[str]],
    **loaders: Callable[[object, object], object],
) -> None:
    """Build ``instance``'s relationships when first read, by their ``loaders``.

    As SQLAlchemy loads a deferred attribute (``state.callables``); until
    then ``granted`` answers what they hold (``models.GRANTED``).
    """
    state = instance_state(instance)
    state.callables = {**state.callables, **loaders}
    state.dict[GRANTED] = granted


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

    id: str = field(repr=False)
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
    stamps: tuple[str, ...] | None = None
    """The shared stamps of ``ALL`` and ``targets`` when it was read: never
    an absent one, which a lost stamp would match again (``settle_stamps``)."""
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
    refresh_at: float = 0.0
    """``clock.monotonic()`` from which one request reads it again, ahead of
    its ``deadline``, while the others are served it."""
    refreshing: bool = False
    """Whether a request claimed reading it again (``claim_refresh``)."""
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

    def same_as(self, other: "Entry") -> bool:
        """Whether ``other`` says what this entry says: the same rows, read again."""
        return (
            self.principal == other.principal
            and self.credential == other.credential
            and self.login == other.login
        )

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
            if self._changed_since(entry.started, entry.targets):
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

    def changed_since(self, started: int, targets: Iterable[str]) -> bool:
        """Whether ``ALL`` or one of ``targets`` was invalidated after ``started``.

        What ``put`` refuses: an entry read from ``started`` on may predate
        the change.
        """
        with self._lock:
            return self._changed_since(started, targets)

    def claim_refresh(self, entry: Entry) -> bool:
        """Whether this request reads the kept ``entry`` again, ahead of its deadline.

        The first to ask once ``refresh_at`` passed, and only that one; the
        others are served the entry meanwhile. Only the entry still kept
        under its key is read again.
        """
        with self._lock:
            if entry.refreshing or self._entries.get(entry.key) is not entry:
                return False
            entry.refreshing = True
            return True

    def release_refresh(self, entry: Entry) -> None:
        """Let another request read ``entry`` again: this one did not finish."""
        with self._lock:
            entry.refreshing = False

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

    def _changed_since(self, started: int, targets: Iterable[str]) -> bool:
        """``changed_since``; the lock is held."""
        return started < self._floor or any(
            self._invalidated.get(target, -1) > started for target in (ALL, *targets)
        )

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
    """Drop what the commit changed (``Session`` ``after_commit``), and stamp it.

    SQLAlchemy reports a savepoint's release as a commit too. Nothing is
    committed then: the targets wait for the transaction's own commit, or a
    principal read before it would be kept. Any session's commit stamps,
    a unit of work's or a bare one's (``stamp_soon``).
    """
    if session.in_nested_transaction():
        return
    targets = session.info.pop(PENDING, None)
    if targets:
        invalidate(targets)
        stamp_soon(session, frozenset(targets))


def stamp_soon(session: Session, targets: frozenset[str]) -> None:
    """Stamp ``targets`` in a task of their own, once ``session`` committed.

    The task is owned (``domainkit.tasks.spawn``): a request cancelled after
    its commit (a deadline, a client gone) does not cancel it, and it is
    bounded by ``STAMP_WRITE_TIMEOUT``. A unit of work waits for it
    (``stamp_committed``); a bare session's commit leaves it running.
    """
    try:
        config = auth_settings().PRINCIPAL_CACHE
    except ImproperlyConfigured:
        return
    if config.STAMPS is None:
        return
    try:
        task = spawn(stamped(config.STAMPS, targets, config), name="principals.stamps")
    except RuntimeError:  # A synchronous session: no event loop to stamp from.
        stamp_failed(targets, config, "no event loop")
        return
    session.info.setdefault(STAMPING, []).append(task)


async def stamped(
    alias: str, targets: Collection[str], config: PrincipalCacheSettings
) -> None:
    """Give ``targets`` new stamps in cache ``alias``, within ``STAMP_WRITE_TIMEOUT``.

    A failure is logged (``stamp_failed``), never raised.
    """
    try:
        async with asyncio.timeout(STAMP_WRITE_TIMEOUT):
            await write_stamps(alias, targets)
    except (CacheError, TimeoutError) as exc:
        stamp_failed(targets, config, type(exc).__name__)


def stamp_failed(
    targets: Collection[str], config: PrincipalCacheSettings, reason: str
) -> None:
    """Log stamps not written: other processes keep principals for ``TTL``."""
    security_logger.warning(
        "Could not stamp %d changed principals in cache %r (%s); other "
        "processes keep theirs for up to %s",
        len(targets),
        config.STAMPS,
        reason,
        config.TTL,
    )


async def stamp_pending(session: AsyncSession, config: PrincipalCacheSettings) -> None:
    """Stamp what ``session`` is about to commit, before the commit (``stage``).

    Stamped again once committed (``stamp_soon``): a principal read in
    between differs from the second stamp. If that one is lost (the process
    ends between the commit and the write), those read before the
    transaction differ from the first already.
    """
    if config.STAMPS is None:
        return
    await session.flush()
    targets = session.info.get(PENDING)
    if targets:
        await stamped(config.STAMPS, frozenset(targets), config)


async def stamp_committed(session: AsyncSession) -> None:
    """Wait for the stamps of ``session``'s commits (``stamp_soon``).

    Cancelling the wait leaves the writes running.
    """
    tasks = session.info.pop(STAMPING, None)
    if tasks:
        await asyncio.shield(asyncio.gather(*tasks))


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


async def recall(
    key: Key, config: PrincipalCacheSettings, now: float | None = None
) -> Entry | None:
    """The kept entry of ``key``, if it still holds here, and everywhere.

    With ``STAMPS``, its stamps must be unchanged; a cache that fails
    confirms nothing, so the entry is not used. ``now`` is
    ``clock.monotonic()``, read by the caller.
    """
    if not config.ENABLED:
        return None
    entry = principal_cache.get(key, clock.monotonic() if now is None else now)
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


async def settle_stamps(
    alias: str, targets: Sequence[str], known: Mapping[str, str] | None = None
) -> tuple[str, ...]:
    """The stamps of ``ALL`` and ``targets``, giving any that is absent one.

    An absent stamp must never confirm an entry: a stamp the shared cache
    lost (a restart, an eviction, a flush) would be absent again after the
    change it stood for. So a missing one is set now, only if still missing
    (``add``, as ``SET NX``), and the value the cache holds is the one kept.
    ``known`` are stamps read earlier, by key.

    Raises:
        CacheError: The cache failed, or kept no stamp.
    """
    cache = caches[alias]
    keys = [stamp_key(ALL), *map(stamp_key, targets)]
    values = dict(known or {})
    wanted = [key for key in keys if key not in values]
    if wanted:
        values.update(await cache.get_many(wanted, str))
        missing = [key for key in wanted if key not in values]
        for key in missing:
            await cache.add(key, secrets.token_hex(8), ttl=STAMP_TTL)
        if missing:
            values.update(await cache.get_many(missing, str))
    if any(key not in values for key in keys):
        raise CacheError(f"Cache {alias!r} kept no stamp.")
    return tuple(values[key] for key in keys)


NONE_AHEAD: Final[Mapping[str, str]] = MappingProxyType({})
"""No stamp read ahead (``stamps_before_reading``)."""


async def stamps_ahead(
    config: PrincipalCacheSettings, targets: Sequence[str]
) -> dict[str, str] | None:
    """``ALL``'s stamp, and those of ``targets`` that exist, before a unit of work.

    A slow cache then holds no database connection for them. ``ALL`` is
    given a stamp if absent (``settle_stamps``); ``targets`` are only read:
    their stamps are given once the credential proved real, so guesses
    write nothing. Empty without ``STAMPS``; None when the cache failed
    (the principal is not kept).
    """
    if not config.ENABLED or config.STAMPS is None:
        return {}
    keys = list(map(stamp_key, targets))
    try:
        (everyone,) = await settle_stamps(config.STAMPS, ())
        found: dict[str, str] = (
            await caches[config.STAMPS].get_many(keys, str) if keys else {}
        )
    except CacheError:
        return None
    return {stamp_key(ALL): everyone, **found}


async def stamps_before_reading(
    entry: Entry,
    config: PrincipalCacheSettings,
    known: Mapping[str, str] | None = NONE_AHEAD,
) -> None:
    """Settle ``entry``'s stamps now, before its rows; mark whether it may be kept.

    A principal is kept only with the stamps read before it, so a change
    stamped after that tells it apart. ``known`` are stamps read earlier
    (``stamps_ahead``); None when that read failed. Disabled, or with a
    cache that fails, it is not kept.
    """
    if not config.ENABLED or known is None:
        return
    if config.STAMPS is not None:
        try:
            entry.stamps = await settle_stamps(config.STAMPS, entry.targets, known)
        except CacheError:
            return
    entry.keep = True


type Read = Callable[[int], Awaitable[Entry | None]]
"""Reads an entry from the database, given ``PrincipalCache.begin()``'s mark."""


def _holds(entry: Entry) -> bool:
    """Every kept entry may be served (``kept_or_read``'s default)."""
    return True


async def kept_or_read(
    key: Key,
    config: PrincipalCacheSettings,
    read: Read,
    holds: Callable[[Entry], bool] = _holds,
) -> Entry | None:
    """The kept entry of ``key``, or one ``read`` from the database (PERF-04).

    - **One read per key at a time** (``read_once``): requests that miss
      while another request of their event loop reads the same key wait for
      that read, unless something was invalidated since it began.
    - **Read again ahead of time.** In the last ``REFRESH_SHARE`` of its
      ``TTL``, the first request to use an entry reads it again, and the
      others are served it until its deadline, never past it. What the new
      read finds replaces it; if it differs (a change made elsewhere), the
      kept entry is dropped at once. If the database fails, the request is
      served the entry, which no other request reads again before it ends.

    ``holds`` says whether a kept entry may still be served (a login's
    tokens may have expired); one that may not is dropped and read again.
    """
    now = clock.monotonic()
    entry = await recall(key, config, now)
    if entry is None:
        return await read_once(key, read)
    if not holds(entry):
        forget(entry)
        return await read_once(key, read)
    if now < entry.refresh_at or not principal_cache.claim_refresh(entry):
        return entry
    return await _refresh(entry, read)


async def _refresh(entry: Entry, read: Read) -> Entry | None:
    """Read the kept ``entry`` again, for the request that claimed it."""
    try:
        fresh = await read_once(entry.key, read)
    except Exception:
        # The database failed: what is kept still holds, until its deadline.
        observe_principals(entry.scheme, "refresh_failed")
        if entry.deadline > clock.monotonic():
            return entry
        raise
    except BaseException:  # Cancelled: another request may read it again.
        principal_cache.release_refresh(entry)
        raise
    observe_principals(entry.scheme, "refresh")
    if (fresh is None or not fresh.same_as(entry)) and principal_cache.discard(entry):
        observe_principals(entry.scheme, "invalidation")
    return fresh


@dataclass(slots=True, eq=False)
class _Flight:
    """A read of one key under way on an event loop, which others may wait for."""

    started: int
    """``PrincipalCache.begin()`` before it began."""
    done: "asyncio.Future[tuple[bool, Entry | None]]"
    """Whether the read succeeded, and what it read."""


_flights: Final[PerLoop[dict[Key, _Flight]]] = PerLoop()
"""Each event loop's reads under way, by key: futures belong to one loop."""


async def read_once(key: Key, read: Read) -> Entry | None:
    """``read(started)``, or what a read of ``key`` already under way finds.

    A request joins a read only if nothing was invalidated since it began,
    and uses what it read only if nothing it rests on was invalidated by
    the time it ended (``changed_since``): a revocation committed before a
    request arrived is never hidden from it by an older read. A read that
    fails, or whose request is cancelled, leaves each waiting request to
    read for itself. A waiting request that is cancelled leaves the read
    running for the others.
    """
    flights = _flights.get(dict)
    started = principal_cache.begin()
    flight = flights.get(key)
    if flight is not None and flight.started == started:
        succeeded, entry = await asyncio.shield(flight.done)
        if succeeded and (
            entry is None or not principal_cache.changed_since(started, entry.targets)
        ):
            observe_principals(key[0], "coalesced")
            return entry
        started = principal_cache.begin()  # Read anew: from now on.
    flight = flights[key] = _Flight(started, asyncio.get_running_loop().create_future())
    try:
        entry = await read(started)
    except BaseException:
        flight.done.set_result((False, None))
        raise
    finally:
        if flights.get(key) is flight:
            del flights[key]
    flight.done.set_result((True, entry))
    return entry


def remember(entry: Entry, config: PrincipalCacheSettings) -> None:
    """Keep a loaded ``entry`` its class accepted, for ``TTL``."""
    if not (entry.fresh and entry.keep and config.ENABLED):
        return
    ttl = config.TTL.total_seconds()
    entry.deadline = clock.monotonic() + ttl
    entry.refresh_at = entry.deadline - ttl * REFRESH_SHARE
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
    """Drop principals when a session changes what they hold (idempotent).

    A user updated (any field, a group joined or left, a permission given
    or taken) or deleted drops that user's principals; a group, a
    permission, or a content type changed or deleted drops every principal;
    a token updated or deleted, its own. A bulk statement executed through
    a session (``update_rows``, ``delete_rows``, ``update(User)``, a Core
    ``UPDATE``) on the tables principals are read from drops every
    principal. Each takes effect when its session commits. Statements run
    on a connection, outside any session, are not seen.
    """
    user_table = cast(Table, class_mapper(user_model).local_table)
    links = user_link_tables(user_table)
    changes = frozenset({"update", "delete"})
    _BULK_WATCHED.clear()
    _BULK_WATCHED.update(
        {
            table.name: changes
            for table in (user_table, groups, permissions, content_types, token_rows)
        }
    )
    _BULK_WATCHED.update(
        {table.name: changes | {"insert"} for table in (*links, group_permissions)}
    )
    listeners: list[tuple[type, str, Any]] = [
        (user_model, "after_update", _user_changed),
        (user_model, "after_delete", _user_changed),
        (Group, "after_update", _everyone_changed),
        (Group, "after_delete", _everyone_changed),
        (Permission, "after_update", _everyone_changed),
        (Permission, "after_delete", _everyone_changed),
        (ContentType, "after_update", _everyone_changed),
        (ContentType, "after_delete", _everyone_changed),
        (AuthToken, "after_update", _token_changed),
        (AuthToken, "after_delete", _token_changed),
        (Session, "after_commit", _committed),
        (Session, "do_orm_execute", _executed),
    ]
    for target, name, listener in listeners:
        if not event.contains(target, name, listener):
            event.listen(target, name, listener, propagate=True)


def _executed(state: ORMExecuteState) -> None:
    """A bulk statement on what principals hold: drop every principal at commit.

    Which rows it changes is unknown without reading them, and bulk changes
    are rare: every principal is read again.
    """
    if state.is_update:
        change = "update"
    elif state.is_delete:
        change = "delete"
    elif state.is_insert:
        change = "insert"
    else:
        return
    table = getattr(state.statement, "table", None)
    if change not in _BULK_WATCHED.get(getattr(table, "name", ""), ()):
        return
    if state.execution_options.get("domainkit_principals_handled"):
        return
    invalidate_after_commit(state.session, [ALL])


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
