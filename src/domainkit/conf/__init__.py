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
    ArchitectureConfig,
    BrokerConfig,
    CacheConfig,
    DatabaseConfig,
    DebugToolbarConfig,
    HttpClientConfig,
    LedgerConfig,
    SearchConfig,
    Settings,
    ShardConfig,
    StorageConfig,
    TenancyConfig,
)
from domainkit.conf.lazy import (
    SETTINGS_MODULE_VARIABLE,
    LazySettings,
    load_settings,
)
from domainkit.conf.lazy import (
    describe_errors as describe_errors,
)
from domainkit.utils.unset import UNSET

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
    "ArchitectureConfig",
    "BrokerConfig",
    "CacheConfig",
    "DatabaseConfig",
    "DebugToolbarConfig",
    "Env",
    "HttpClientConfig",
    "LedgerConfig",
    "SearchConfig",
    "Settings",
    "ShardConfig",
    "StorageConfig",
    "TenancyConfig",
    "configure",
    "load_settings",
    "override_settings",
    "reset_settings",
    "settings",
    "use_settings",
]
