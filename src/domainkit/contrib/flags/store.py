"""Reading flags and dynamic settings, through the cache.

Every flag and stored setting is read in one query and cached as one entry
(``FLAGS.CACHE``, for ``FLAGS.CACHE_TTL``). Changes made through
``FlagsUnitOfWork`` delete the entry once they commit; processes with
their own memory cache see them when their entry expires.
"""

import logging
import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any, Final

from pydantic import TypeAdapter, ValidationError
from sqlalchemy import select

from domainkit.apps import registry
from domainkit.cache import caches
from domainkit.db import connections
from domainkit.db.routing import router
from domainkit.exceptions import ImproperlyConfigured

from .exceptions import InvalidSettingValue, UnknownSetting
from .models import Flag
from .settings import FlagsSettings
from .tables import NAME_LENGTH, dynamic_settings, flags

LABEL: Final = "flags"
CACHE_KEY: Final = "domainkit.flags"
NAME: Final = re.compile(rf"[a-z0-9][a-z0-9_.-]{{0,{NAME_LENGTH - 1}}}")
"""Flag names and setting keys: lower case, digits, ``_``, ``.``, ``-``."""
logger = logging.getLogger(__name__)
_MISSING: Final = object()


@dataclass(frozen=True, slots=True)
class Snapshot:
    """Every flag's rules and every stored setting's value, as JSON."""

    flags: dict[str, dict[str, Any]] = field(default_factory=dict)
    settings: dict[str, Any] = field(default_factory=dict)


async def current() -> Snapshot:
    """The flags and settings, from the cache or, on a miss, the database."""
    config = registry.settings(FlagsSettings)
    data = await caches[config.CACHE].get_or_set(CACHE_KEY, _load, ttl=config.CACHE_TTL)
    return Snapshot(flags=data["flags"], settings=data["settings"])


async def invalidate(*_: object) -> None:
    """Drop the cached flags; the next read loads them again."""
    await caches[registry.settings(FlagsSettings).CACHE].delete(CACHE_KEY)


async def _load() -> dict[str, Any]:
    """Read every flag and stored setting."""
    alias = router.alias_for(router.target_for(LABEL))
    async with connections.engine(alias).connect() as connection:
        flag_rows = (await connection.execute(select(flags))).mappings().all()
        setting_rows = (await connection.execute(select(dynamic_settings))).all()
    return {
        "flags": {
            row["id"]: {
                "everyone": row["everyone"],
                "percentage": row["percentage"],
                "users": list(row["users"]),
                "groups": list(row["groups"]),
            }
            for row in flag_rows
        },
        "settings": {row.id: row.value for row in setting_rows},
    }


async def flag_enabled(name: str, user: object = None) -> bool:
    """Whether flag ``name`` is on for ``user`` (anyone without one).

    ``user`` is the request's user (``domainkit.contrib.auth``), or any
    object with an ``id`` and, optionally, ``groups`` with names. A flag
    nobody created is off.
    """
    rules = (await current()).flags.get(name)
    if rules is None:
        return False
    flag = Flag(id=name, **rules)
    return flag.enabled_for(_user_id(user), _group_names(user))


def _user_id(user: object) -> str | None:
    """The user's id as text; None for no user, or an anonymous one."""
    if user is None or getattr(user, "is_authenticated", True) is not True:
        return None
    identifier = getattr(user, "id", None)
    return None if identifier is None else str(identifier)


def _group_names(user: object) -> Iterator[str]:
    """The names of the user's groups."""
    for group in getattr(user, "groups", None) or ():
        yield str(getattr(group, "name", group))


class DynamicSettingDefinition[T]:
    """A setting changed at runtime, declared with ``dynamic_setting``."""

    def __init__(self, key: str, type_: Any, default: T, description: str) -> None:  # noqa: ANN401 - any type form.
        """Check the key and that the default fits the type."""
        if not NAME.fullmatch(key):
            raise ImproperlyConfigured(
                f"Dynamic setting key {key!r} must be lower-case letters, digits, "
                f"'_', '.', or '-' (at most {NAME_LENGTH})."
            )
        self.key = key
        self.type = type_
        self.description = description
        self.adapter: TypeAdapter[T] = TypeAdapter(type_)
        try:
            self.default: T = self.adapter.validate_python(default)
        except ValidationError:
            raise ImproperlyConfigured(
                f"The default of dynamic setting {key!r} does not fit its type."
            ) from None

    async def get(self) -> T:
        """The stored value, or the default when none is stored.

        A stored value the type no longer accepts (the code changed) gives
        the default, and a warning in the log.
        """
        stored = (await current()).settings.get(self.key, _MISSING)
        if stored is _MISSING:
            return self.default
        try:
            return self.adapter.validate_python(stored)
        except ValidationError:
            logger.warning(
                "Dynamic setting %r holds a value its type refuses; using the default",
                self.key,
            )
            return self.default

    def checked(self, value: object) -> object:
        """``value`` validated by the type, as JSON to store.

        Raises:
            InvalidSettingValue: It does not fit; the reasons, not the value.
        """
        try:
            valid = self.adapter.validate_python(value)
        except ValidationError as exc:
            reasons = "; ".join(
                error["msg"]
                for error in exc.errors(include_url=False, include_input=False)
            )
            raise InvalidSettingValue(f"{self.key}: {reasons}.") from None
        return self.adapter.dump_python(valid, mode="json")

    def default_json(self) -> object:
        """The default, as JSON."""
        return self.adapter.dump_python(self.default, mode="json")

    def json_schema(self) -> dict[str, Any]:
        """The JSON schema of the setting's values."""
        return self.adapter.json_schema()


_declared: dict[str, DynamicSettingDefinition[Any]] = {}


def dynamic_setting[T](
    key: str,
    type_: Any,  # noqa: ANN401 - any type form: int, Annotated[...], a model.
    *,
    default: T,
    description: str = "",
) -> DynamicSettingDefinition[T]:
    """Declare a setting staff can change at runtime, typed and validated.

    For example::

        MAX_ITEMS = dynamic_setting(
            "checkout.max_items", Annotated[int, Field(ge=1, le=100)], default=10
        )
        limit = await MAX_ITEMS.get()

    Declaring a key again replaces its definition.
    """
    definition = DynamicSettingDefinition(key, type_, default, description)
    _declared[key] = definition
    return definition


def declared_settings() -> list[DynamicSettingDefinition[Any]]:
    """Every declared dynamic setting, by key."""
    return [_declared[key] for key in sorted(_declared)]


def declared(key: str) -> DynamicSettingDefinition[Any]:
    """The setting declared with ``key``.

    Raises:
        UnknownSetting: None is.
    """
    try:
        return _declared[key]
    except KeyError:
        raise UnknownSetting(f"No dynamic setting is called {key!r}.") from None
