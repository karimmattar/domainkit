"""Typed hooks a domain registers for what it takes part in.

A ``HookPoint`` is one thing the framework lets domains join: a commit, a
migration, an erasure. A domain registers a function on it in
``DomainConfig.ready()``, and only the domains that registered are called::

    from domainkit.db.hooks import on_committed

    class LedgerConfig(DomainConfig):
        def ready(self) -> None:
            on_committed(publish_entries)

Hooks run in ``INSTALLED_DOMAINS`` order, each domain's in the order it
registered them. A point may name the ``DomainConfig`` method that served
the same purpose before it (``method``): a domain whose configuration
overrides that method is called through it too, after its registered
hooks, so method-style hooks keep working.

Registrations belong to the domains loaded at the time: ``registry.clear()``
and the next ``populate`` forget them, and the domains register again in
``ready()``.
"""

import threading
from collections.abc import Callable, Iterator
from typing import TYPE_CHECKING, NamedTuple, cast

from domainkit.exceptions import ImproperlyConfigured

if TYPE_CHECKING:
    from domainkit.apps.registry import DomainRegistry

__all__ = ["Hook", "HookPoint"]


class Hook[F: Callable[..., object]](NamedTuple):
    """One registered function, and the domain that registered it."""

    domain: str
    """The domain's label, for logs and error reports."""
    call: F


class HookPoint[F: Callable[..., object]]:
    """Functions installed domains registered for one purpose, in domain order.

    Call the point with a function to register it, from
    ``DomainConfig.ready()``; iterate it to get the ``Hook`` of each. The
    registering domain is the one whose ``ready()`` is running, unless
    ``domain`` names another label. Event loops on several threads read the
    hooks; registering takes a lock, and reading takes none.
    """

    def __init__(self, name: str, *, method: str | None = None) -> None:
        """Name the point (``stage``), and the ``DomainConfig`` method it replaces.

        Args:
            name: How errors name the point.
            method: The method a domain may still override instead of
                registering; None if there is none.
        """
        self.name = name
        self.method = method
        self._lock = threading.Lock()
        self._generation = -1
        self._registered: dict[str, list[F]] = {}
        self._hooks: tuple[int, tuple[Hook[F], ...]] | None = None
        self._registry: DomainRegistry | None = None

    def __repr__(self) -> str:
        """Show the point's name."""
        return f"<HookPoint {self.name}>"

    def __call__(self, function: F, *, domain: str | None = None) -> F:
        """Register ``function`` for the domain being made ready; return it.

        Raises:
            ImproperlyConfigured: Called outside a domain's ``ready()``
                without ``domain``.
        """
        registry = _registry()
        if domain is None:
            readying = registry.readying
            if readying is None:
                raise ImproperlyConfigured(
                    f"Register {self.name} hooks in DomainConfig.ready(), or "
                    "name the domain: domain='label'."
                )
            domain = readying.label
        with self._lock:
            self._forget_stale(registry.generation)
            self._registered.setdefault(domain, []).append(function)
            self._hooks = None
        return function

    def __iter__(self) -> Iterator[Hook[F]]:
        """The hooks of the installed domains, in ``INSTALLED_DOMAINS`` order."""
        return iter(self.hooks())

    def hooks(self) -> tuple[Hook[F], ...]:
        """The hooks of the installed domains, worked out once per change.

        Each commit reads them: when nothing changed, that is one lookup.
        """
        registry = self._registry or _registry()
        self._registry = registry
        known = self._hooks
        if known is not None and known[0] == registry.generation:
            return known[1]
        with self._lock:
            generation = registry.generation
            self._forget_stale(generation)
            hooks = tuple(self._collect(registry))
            if registry.generation == generation:
                self._hooks = (generation, hooks)
        return hooks

    def _collect(self, registry: "DomainRegistry") -> Iterator[Hook[F]]:
        """Each domain's registered hooks, then its overriding method, if any."""
        from domainkit.apps.config import DomainConfig

        for config in registry:
            for function in self._registered.get(config.label, ()):
                yield Hook(config.label, function)
            if self.method is not None and getattr(
                type(config), self.method
            ) is not getattr(DomainConfig, self.method):
                yield Hook(config.label, cast(F, getattr(config, self.method)))

    def _forget_stale(self, generation: int) -> None:
        """Drop what domains registered before the registry last changed."""
        if generation != self._generation:
            self._registered.clear()
            self._hooks = None
            self._generation = generation


def _registry() -> "DomainRegistry":
    """The process's domain registry (imported late: ``domainkit.apps`` loads it)."""
    from domainkit.apps.registry import registry

    return registry
