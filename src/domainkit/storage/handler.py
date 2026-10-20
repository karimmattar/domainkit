"""Storages per ``STORAGES`` alias (Django's ``storages``)."""

from typing import Final

from domainkit.conf import settings
from domainkit.conf.global_settings import StorageConfig
from domainkit.exceptions import ImproperlyConfigured
from domainkit.storage.base import Storage
from domainkit.utils.module_loading import import_string


class StorageHandler:
    """Make each configured storage on first use; one per alias and process."""

    def __init__(self) -> None:
        """Start with none made."""
        self._storages: dict[str, tuple[StorageConfig, Storage]] = {}

    def __getitem__(self, alias: str) -> Storage:
        """The storage of ``alias``; a new one if its settings changed.

        Raises:
            ImproperlyConfigured: ``STORAGES`` has no such alias, or its
                ``BACKEND`` is not a ``Storage``.
        """
        try:
            config = settings.STORAGES[alias]
        except KeyError:
            raise ImproperlyConfigured(f"STORAGES has no {alias!r} alias.") from None
        made = self._storages.get(alias)
        if made is not None and made[0] == config:
            return made[1]
        storage = _make(alias, config)
        self._storages[alias] = (config, storage)
        return storage

    def reset(self) -> None:
        """Forget the storages made (tests); memory storages lose their files."""
        self._storages.clear()


def _make(alias: str, config: StorageConfig) -> Storage:
    """Instantiate ``BACKEND`` with ``OPTIONS``."""
    try:
        backend = import_string(config.BACKEND)
    except ImportError as exc:
        raise ImproperlyConfigured(f"STORAGES[{alias!r}].BACKEND: {exc}") from exc
    try:
        storage = backend(**config.OPTIONS)
    except TypeError as exc:
        raise ImproperlyConfigured(f"STORAGES[{alias!r}].OPTIONS: {exc}") from exc
    if not isinstance(storage, Storage):
        raise ImproperlyConfigured(
            f"STORAGES[{alias!r}].BACKEND {config.BACKEND!r} is not a Storage."
        )
    return storage


storages: Final = StorageHandler()
"""The process-wide storages: ``storages["default"]``."""
