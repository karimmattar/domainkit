"""Typed hooks a domain registers for what it takes part in.

A ``HookPoint`` is one thing the framework lets domains join: a commit, a
migration, an erasure. A domain registers a function on it in
``DomainConfig.ready()``, and only the domains that registered are called::

    from domainkit.apps.hooks import on_startup
    from domainkit.db.hooks import on_committed

    class LedgerConfig(DomainConfig):
        def ready(self) -> None:
            on_startup(open_client)
            on_committed(publish_entries)

This module declares the points of a domain's life; ``domainkit.db.hooks``
those of SQL commits and migrations. Each ``DomainConfig`` hook method
``x`` became the point ``on_x``:

- ``on_startup`` and ``on_shutdown``: open and release process resources
  (the application's lifespan). A domain's shutdown hooks run, in reverse
  domain order, once all its startup hooks ran.
- ``on_check``: raise when the domain cannot serve traffic (``/ready``).
- ``on_seed``: add the rows a fresh database needs (``manage.py seed``).
- ``on_erase_subject`` and ``on_export_subject``: the copies of a person's
  data a domain keeps outside its aggregates (``domainkit.contrib.privacy``).
- ``on_check_application``: problems with the built application.
- ``on_deployment_warnings``: weak but valid settings where deployed.

A registered function is checked when it registers: it must take the
point's arguments, and be a coroutine function where the point awaits it.
A mistake fails ``setup()``, naming the domain, rather than the first
commit or probe that calls it.

Hooks run in ``INSTALLED_DOMAINS`` order, each domain's in the order it
registered them. Only what a domain registered is called: 0.13 removed
the hook methods, and ``setup()`` refuses a ``DomainConfig`` that still
defines one (``apps.E002``).

Registrations belong to the domains loaded at the time: ``registry.clear()``
and the next ``populate`` forget them, and the domains register again in
``ready()``.
"""

import inspect
import threading
from collections.abc import Awaitable, Callable, Iterable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, NamedTuple
from weakref import WeakKeyDictionary

from domainkit.exceptions import ImproperlyConfigured

if TYPE_CHECKING:
    from fastapi import FastAPI

    from domainkit.apps.registry import DomainRegistry

__all__ = [
    "Hook",
    "HookPoint",
    "on_check",
    "on_check_application",
    "on_deployment_warnings",
    "on_erase_subject",
    "on_export_subject",
    "on_seed",
    "on_shutdown",
    "on_startup",
]


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

    def __init__(
        self,
        name: str,
        *,
        arguments: int | None = None,
        awaited: bool | None = None,
    ) -> None:
        """Name the point (``stage``), and say how it calls its hooks.

        Args:
            name: How errors name the point.
            arguments: How many positional arguments the point calls a
                hook with, checked when it registers; None checks nothing.
            awaited: Whether the point awaits a hook (it must be a
                coroutine function) or not (it must not be); None checks
                nothing.
        """
        self.name = name
        self.arguments = arguments
        self.awaited = awaited
        self._lock = threading.Lock()
        self._registered: WeakKeyDictionary[DomainRegistry, _Registered[F]] = (
            WeakKeyDictionary()
        )
        self._hooks: _Known[F] | None = None
        self._registry: DomainRegistry | None = None

    def __repr__(self) -> str:
        """Show the point's name."""
        return f"<HookPoint {self.name}>"

    def __call__(self, function: F, *, domain: str | None = None) -> F:
        """Register ``function`` for the domain being made ready; return it.

        ``domain`` registers for an installed domain from outside its
        ``ready()``, once the domains are loaded (a test, a command).

        Raises:
            ImproperlyConfigured: Called outside a domain's ``ready()``
                without ``domain``; ``domain`` names no domain loaded yet
                (the registration would be forgotten when they load, or
                never be called); or ``function`` cannot take the point's
                arguments, or is not awaitable where the point awaits it
                (or the reverse).
        """
        registry = _populating[-1] if _populating else _registry()
        if domain is None:
            readying = registry.readying
            if readying is None:
                raise ImproperlyConfigured(
                    f"Register {self.name} hooks in DomainConfig.ready(); "
                    "outside it, once the domains are loaded, name an "
                    "installed domain: domain='label'."
                )
            domain = readying.label
        elif not any(config.label == domain for config in registry):
            raise ImproperlyConfigured(
                f"{self.name} hook {_name(function)} names domain {domain!r}, "
                "which is not loaded: register it in that domain's "
                "DomainConfig.ready(), where it is kept as long as the "
                "domain is installed."
            )
        self._check(function, domain)
        with self._lock:
            self._current(registry).functions.setdefault(domain, []).append(function)
            self._hooks = None
        return function

    def _check(self, function: F, domain: str) -> None:
        """Refuse ``function`` if the point cannot call it as it calls hooks.

        Raises:
            ImproperlyConfigured: It takes other arguments, or is a
                coroutine function where the point does not await it (or
                the reverse).
        """
        where = f"{domain}'s {self.name} hook {_name(function)}"
        if self.arguments is not None:
            try:
                signature = inspect.signature(function)
            except (TypeError, ValueError):  # A built-in without one: trust it.
                signature = None
            if signature is not None:
                try:
                    signature.bind(*(None,) * self.arguments)
                except TypeError as exc:
                    raise ImproperlyConfigured(
                        f"{where} must take {self.arguments} positional "
                        f"argument{'' if self.arguments == 1 else 's'} ({exc})."
                    ) from None
        if self.awaited is not None and _is_async(function) is not self.awaited:
            kind = "an async function" if self.awaited else "a plain function"
            raise ImproperlyConfigured(f"{where} must be {kind}.")

    def of(
        self, domain: str, registry: "DomainRegistry | None" = None
    ) -> tuple[Hook[F], ...]:
        """The hooks of the installed domain labelled ``domain``, in order."""
        return tuple(hook for hook in self.hooks(registry) if hook.domain == domain)

    def __iter__(self) -> Iterator[Hook[F]]:
        """The hooks of the installed domains, in ``INSTALLED_DOMAINS`` order."""
        return iter(self.hooks())

    def hooks(self, registry: "DomainRegistry | None" = None) -> tuple[Hook[F], ...]:
        """The hooks of the installed domains, worked out once per change.

        ``registry`` defaults to the process's (``domainkit.apps.registry``).
        Each commit reads them: when nothing changed, that is one lookup.
        """
        if registry is None:
            registry = self._registry or _registry()
            self._registry = registry
        known = self._hooks
        if (
            known is not None
            and known.registry is registry
            and known.generation == registry.generation
        ):
            return known.hooks
        with self._lock:
            generation = registry.generation
            hooks = tuple(self._collect(registry, self._current(registry)))
            if registry.generation == generation:
                self._hooks = _Known(registry, generation, hooks)
        return hooks

    def _collect(
        self, registry: "DomainRegistry", registered: "_Registered[F]"
    ) -> Iterator[Hook[F]]:
        """Each domain's registered hooks, in domain order."""
        for config in registry:
            for function in registered.functions.get(config.label, ()):
                yield Hook(config.label, function)

    def _current(self, registry: "DomainRegistry") -> "_Registered[F]":
        """What ``registry``'s domains registered since it last changed.

        Registrations made before the registry last changed are forgotten.
        """
        registered = self._registered.get(registry)
        if registered is None or registered.generation != registry.generation:
            registered = self._registered[registry] = _Registered(registry.generation)
        return registered


@dataclass(slots=True)
class _Registered[F]:
    """The functions one registry's domains registered, by domain label."""

    generation: int
    """The registry's generation they were registered in."""
    functions: dict[str, list[F]] = field(default_factory=dict)


class _Known[F: Callable[..., object]](NamedTuple):
    """The hooks last worked out, and for which registry and generation."""

    registry: "DomainRegistry"
    generation: int
    hooks: tuple[Hook[F], ...]


_populating: "list[DomainRegistry]" = []
"""The registries running their domains' ``ready()``, innermost last."""


@contextmanager
def populating(registry: "DomainRegistry") -> Iterator[None]:
    """Register the hooks of the domains made ready in the block for ``registry``."""
    _populating.append(registry)
    try:
        yield
    finally:
        _populating.pop()


def _name(function: object) -> str:
    """How errors name a registered function."""
    return getattr(function, "__qualname__", None) or repr(function)


def _is_async(function: object) -> bool:
    """Whether calling ``function`` gives a coroutine: it, or its ``__call__``."""
    return inspect.iscoroutinefunction(function) or inspect.iscoroutinefunction(
        getattr(function, "__call__", None)  # noqa: B004 - an object's own method.
    )


def _registry() -> "DomainRegistry":
    """The process's domain registry (imported late: ``domainkit.apps`` loads it)."""
    from domainkit.apps.registry import registry

    return registry


type LifeHook = Callable[[], Awaitable[None]]
type EraseHook = Callable[[str, str], Awaitable[Mapping[str, int]]]
type ExportHook = Callable[[str], Awaitable[Mapping[str, object]]]
type ApplicationCheck = Callable[["FastAPI"], Iterable[str]]
type DeploymentCheck = Callable[[], Iterable[str]]

on_startup: HookPoint[LifeHook] = HookPoint("startup", arguments=0, awaited=True)
"""Open process resources, such as clients and pools: ``()``."""
on_shutdown: HookPoint[LifeHook] = HookPoint("shutdown", arguments=0, awaited=True)
"""Release what the domain's startup hooks opened: ``()``."""
on_check: HookPoint[LifeHook] = HookPoint("check", arguments=0, awaited=True)
"""Raise when the domain cannot serve traffic (``/ready``): ``()``."""
on_seed: HookPoint[LifeHook] = HookPoint("seed", arguments=0, awaited=True)
"""Add the rows a fresh database needs (``manage.py seed``): ``()``.

Write rows through the domain's own units of work, and make running twice
harmless: rows that exist stay as they are.
"""
on_erase_subject: HookPoint[EraseHook] = HookPoint(
    "erase_subject", arguments=2, awaited=True
)
"""Erase the copies kept of a person's data: ``(subject, reason)``.

Return counts by kind (``{"deliveries": 2}``), never the data itself: they
are audited. Count copies left to finish first under ``pending``.
"""
on_export_subject: HookPoint[ExportHook] = HookPoint(
    "export_subject", arguments=1, awaited=True
)
"""What the domain keeps about a person outside its aggregates: ``(subject)``.

Return JSON-ready data, or nothing when there is nothing.
"""
on_check_application: HookPoint[ApplicationCheck] = HookPoint(
    "check_application", arguments=1, awaited=False
)
"""Problems with the built application: ``(app)``, best as ``CheckMessage``."""
on_deployment_warnings: HookPoint[DeploymentCheck] = HookPoint(
    "deployment_warnings", arguments=0, awaited=False
)
"""Weak but valid settings where deployed: ``()``; none elsewhere."""
