"""Install ``domainkit.contrib.flags`` for feature flags and dynamic settings."""

from domainkit.apps import DomainConfig, registry
from domainkit.conf import settings
from domainkit.db.routing import router
from domainkit.ddd import event_bus
from domainkit.exceptions import ImproperlyConfigured

from .events import FlagChanged, FlagDeleted, SettingChanged, SettingReset
from .settings import FlagsSettings
from .store import invalidate
from .tables import start_mappers


class FlagsConfig(DomainConfig):
    """Map flags and settings; drop the cached copy whenever one changes."""

    label = "flags"
    verbose_name = "Feature flags"
    settings_class = FlagsSettings

    def ready(self) -> None:
        """Check the cache and the database, map, and subscribe."""
        cache = registry.settings(FlagsSettings).CACHE
        if cache not in settings.CACHES:
            raise ImproperlyConfigured(f"FLAGS.CACHE = {cache!r} is not in CACHES.")
        router.require_standalone(
            router.target_for(self.label), "DATABASE_ROUTING['flags']"
        )
        start_mappers()
        for event_type in (FlagChanged, FlagDeleted, SettingChanged, SettingReset):
            event_bus.subscribe(event_type, invalidate)
