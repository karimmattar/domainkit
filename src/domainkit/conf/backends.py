"""The backend classes a setting names, loaded one way (ADR 0196).

A ``BackendHandler`` stands for one setting. It imports what the setting
names, by dotted path or by the name an installed package offers it by
(``domainkit.utils.module_loading.import_backend``), and checks that each
is a class of its kind. It makes the instances a setting of several names
once per settings object: ``override_settings``, ``use_settings``, and
``reset_settings`` each put another object in place, so the next call
makes them again::

    hashers: Final[BackendHandler[PasswordHasher]] = BackendHandler(
        "AUTH.PASSWORD_HASHERS",
        PasswordHasher,
        lambda: auth_settings().PASSWORD_HASHERS,
    )

    hashers.instances()  # One of each class, the same until the settings change.

A backend that keeps state of its own, such as the counts of a rate
limiter, is made where it is used, with ``create``. Loops on several
threads (free-threaded Python) ask at once, so the instances are made
under a lock, and every thread gets the same ones.
"""

import threading
from collections.abc import Callable, Mapping, Sequence
from typing import Any, cast

from domainkit.conf import settings, unwrap_settings
from domainkit.exceptions import ImproperlyConfigured
from domainkit.utils.module_loading import backend_class

__all__ = ["BackendHandler"]

type Named = str | tuple[str, Mapping[str, Any]]
"""A class a setting names: its path, or its path and its keyword arguments."""


class BackendHandler[T]:
    """The classes one setting names, imported, checked, and made once per settings."""

    def __init__(
        self,
        setting: str,
        base: type,
        read: Callable[[], str | Sequence[Named]] | None = None,
    ) -> None:
        """Load what ``read`` returns for ``setting``: each a ``base``.

        ``read`` returns the setting's value: one path, or a sequence of
        paths, each alone or with its keyword arguments (``(path,
        options)``). By default it reads ``settings.<setting>``. ``base`` is
        a class, or a protocol marked ``runtime_checkable`` whose members
        are methods.
        """
        self.setting = setting
        self.base = base
        self._read = read or (lambda: cast(str, getattr(settings, setting)))
        self._kept: tuple[object, tuple[T, ...]] | None = None
        self._lock = threading.Lock()
        """Held to make the instances: loops on other threads ask too."""

    def backend_class(self) -> type[T]:
        """The class the setting names (a setting of one).

        Raises:
            ImproperlyConfigured: It cannot be imported, or is not a ``base``.
        """
        (named,) = self._each()
        path, _ = _named(named)
        return cast(type[T], backend_class(self.setting, path, self.base))

    def create(self) -> T:
        """A new instance of the class the setting names (a setting of one).

        Raises:
            ImproperlyConfigured: It cannot be imported, is not a ``base``,
                or refuses its keyword arguments.
        """
        (named,) = self._each()
        return self._make(named)

    def instances(self) -> tuple[T, ...]:
        """One instance of each class the setting names, in order: kept per settings.

        Raises:
            ImproperlyConfigured: A class cannot be imported, is not a
                ``base``, or refuses its keyword arguments.
        """
        source = unwrap_settings()
        kept = self._kept
        if kept is not None and kept[0] is source:
            return kept[1]
        with self._lock:
            kept = self._kept
            if kept is not None and kept[0] is source:
                return kept[1]
            made = self.load(self._read())
            if unwrap_settings() is source:  # Not settings replaced meanwhile.
                self._kept = (source, made)
            return made

    def load(self, value: str | Sequence[Named]) -> tuple[T, ...]:
        """One new instance of each class ``value`` names, as the setting would.

        For a value that is not the setting's own, such as one a check
        weighs; nothing is kept.

        Raises:
            ImproperlyConfigured: A class cannot be imported, is not a
                ``base``, or refuses its keyword arguments.
        """
        return tuple(self._make(named) for named in _each(value))

    def _each(self) -> tuple[Named, ...]:
        """What the setting names, as a sequence."""
        return _each(self._read())

    def _make(self, named: Named) -> T:
        """An instance of the class ``named`` names, given its keyword arguments.

        Raises:
            ImproperlyConfigured: The class cannot be imported, is not a
                ``base``, or refuses its keyword arguments.
        """
        path, options = _named(named)
        found = backend_class(self.setting, path, self.base)
        try:
            return cast(T, found(**options))
        except TypeError as exc:
            raise ImproperlyConfigured(f"{self.setting}: {path}: {exc}") from exc


def _each(value: str | Sequence[Named]) -> tuple[Named, ...]:
    """``value`` as a sequence of what it names."""
    if isinstance(value, str):
        return (value,)
    return tuple(value)


def _named(named: Named) -> tuple[str, Mapping[str, Any]]:
    """The path and the keyword arguments of ``named``."""
    if isinstance(named, str):
        return named, {}
    return named
