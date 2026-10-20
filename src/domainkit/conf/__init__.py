"""Django-style settings access.

Select a module with ``<PREFIX>SETTINGS_MODULE`` (for example
``BILLING_API_SETTINGS_MODULE=config.settings.local``; the prefix comes
from the project name), in the environment or the ``.env`` file, then read
values anywhere::

    from domainkit.conf import settings

    settings.DEBUG
    settings.DATABASES["default"].ECHO

Settings are loaded on first access, so importing this module has no side
effects.
"""

from typing import Final, cast

from domainkit.conf.env import Env
from domainkit.conf.global_settings import (
    DEFAULT_CACHE_ALIAS,
    DEFAULT_DB_ALIAS,
    BrokerConfig,
    CacheConfig,
    DatabaseConfig,
    LedgerConfig,
    Settings,
    ShardConfig,
    StorageConfig,
)
from domainkit.conf.lazy import (
    SETTINGS_MODULE_VARIABLE,
    UNSET,
    LazySettings,
    describe_errors,
    load_settings,
)

_lazy: Final = LazySettings(Env())

settings: Final = cast(Settings, _lazy)
"""Typed, lazily loaded settings for the selected module."""

configure: Final = _lazy.configure
override_settings: Final = _lazy.override
reset_settings: Final = _lazy.reset
unwrap_settings: Final = _lazy.unwrap
use_settings: Final = _lazy.use

__all__ = [
    "DEFAULT_CACHE_ALIAS",
    "DEFAULT_DB_ALIAS",
    "SETTINGS_MODULE_VARIABLE",
    "UNSET",
    "BrokerConfig",
    "CacheConfig",
    "DatabaseConfig",
    "LedgerConfig",
    "Settings",
    "ShardConfig",
    "StorageConfig",
    "configure",
    "describe_errors",
    "load_settings",
    "override_settings",
    "reset_settings",
    "settings",
    "unwrap_settings",
    "use_settings",
]
