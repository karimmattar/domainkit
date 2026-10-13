"""The registry of installed domains (``django.apps.apps``)."""

import difflib
from collections.abc import AsyncIterator, Iterable, Iterator
from contextlib import AsyncExitStack, asynccontextmanager
from typing import Final, cast

from pydantic import BaseModel, ValidationError

from domainkit.apps.config import DomainConfig
from domainkit.conf import describe_errors, settings, unwrap_settings
from domainkit.exceptions import ImproperlyConfigured
from domainkit.utils.readiness import failing_checks


class DomainRegistry:
    """Installed domains in ``INSTALLED_DOMAINS`` order."""

    def __init__(self) -> None:
        """Start empty; ``populate`` loads the domains."""
        self._configs: dict[str, DomainConfig] = {}
        self._settings_cache: dict[type[BaseModel], tuple[object, BaseModel]] = {}
        self.ready = False

    def populate(self, installed: Iterable[str]) -> None:
        """Load every domain, validate its settings, then call ``ready()``.

        Does nothing once the registry is ready; ``clear()`` starts over.
        """
        if self.ready:
            return
        configs: dict[str, DomainConfig] = {}
        for name in installed:
            config = DomainConfig.create(name)
            if config.label in configs:
                raise ImproperlyConfigured(
                    f"Domain label {config.label!r} is not unique "
                    f"({configs[config.label].name!r} and {name!r})."
                )
            configs[config.label] = config
        self._configs = configs
        self._settings_cache.clear()
        self._check_settings()
        for config in configs.values():
            config.ready()
        self.ready = True

    def clear(self) -> None:
        """Forget every domain (tests); the next ``populate`` loads them again."""
        self._configs = {}
        self._settings_cache.clear()
        self.ready = False

    def __iter__(self) -> Iterator[DomainConfig]:
        """Iterate in ``INSTALLED_DOMAINS`` order."""
        return iter(self._configs.values())

    def __len__(self) -> int:
        """Return the number of installed domains."""
        return len(self._configs)

    def get_domain_config(self, label: str) -> DomainConfig:
        """Return the domain with ``label``."""
        try:
            return self._configs[label]
        except KeyError:
            raise LookupError(f"No installed domain with label {label!r}.") from None

    def is_installed(self, name: str) -> bool:
        """Report whether the package ``name`` is an installed domain."""
        return any(config.name == name for config in self)

    def settings[S: BaseModel](self, settings_class: type[S]) -> S:
        """Return the validated settings of the domain that declares ``settings_class``.

        Values are re-validated when ``override_settings`` replaces them.
        """
        config = next((c for c in self if c.settings_class is settings_class), None)
        if config is None:
            raise ImproperlyConfigured(
                f"No installed domain declares {settings_class.__qualname__}."
            )
        raw = getattr(settings, config.settings_name, None)
        cached = self._settings_cache.get(settings_class)
        if cached is not None and cached[0] is raw:
            return cast(S, cached[1])
        try:
            value = settings_class.model_validate({} if raw is None else raw)
        except ValidationError as exc:
            raise ImproperlyConfigured(
                f"Invalid {config.settings_name} settings:\n{describe_errors(exc)}"
            ) from None
        self._settings_cache[settings_class] = (raw, value)
        return value

    @asynccontextmanager
    async def lifespan(self) -> AsyncIterator[None]:
        """Start domains in order and stop started ones in reverse, even on failure."""
        async with AsyncExitStack() as stack:
            for config in self:
                await config.startup()
                stack.push_async_callback(config.shutdown)
            yield

    async def check(self, seconds: float) -> list[str]:
        """Run every domain's readiness check; return the labels that failed."""
        return await failing_checks(
            {config.label: config.check for config in self}, seconds
        )

    def _check_settings(self) -> None:
        """Validate domain settings and reject names nothing declares."""
        claimed = set()
        for config in self:
            if config.settings_class is not None:
                claimed.add(config.settings_name)
                self.settings(config.settings_class)
        current = unwrap_settings()
        if unknown := sorted(set(current.model_extra or {}) - claimed):
            known = [*type(current).model_fields, *claimed]
            raise ImproperlyConfigured(
                f"Unknown settings in {current.SETTINGS_MODULE!r}: "
                + ", ".join(_describe_unknown(name, known) for name in unknown)
                + ". Declare them in a domain's settings_class or SETTINGS_CLASS."
            )


def _describe_unknown(name: str, known: list[str]) -> str:
    """Name an unknown setting and suggest the closest known one."""
    matches = difflib.get_close_matches(name, known, n=1)
    return f"{name} (did you mean {matches[0]}?)" if matches else name


registry: Final = DomainRegistry()
"""The process-wide registry populated by ``domainkit.setup()``."""
