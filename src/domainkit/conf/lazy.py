"""Lazy settings proxy that imports and validates the selected settings module."""

import importlib
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any, Final

from pydantic import ValidationError

from domainkit.conf.env import Env
from domainkit.conf.global_settings import Settings
from domainkit.exceptions import ImproperlyConfigured
from domainkit.utils.module_loading import import_string

SETTINGS_MODULE_VARIABLE: Final = "SETTINGS_MODULE"
SETTINGS_CLASS_NAME: Final = "SETTINGS_CLASS"


class _Unset:
    """Sentinel type for ``override_settings(NAME=UNSET)``."""

    def __repr__(self) -> str:
        """Name the sentinel in reprs."""
        return "UNSET"


UNSET: Final = _Unset()
"""Pass to ``override_settings`` to remove a setting (Django's ``del settings.X``)."""


def describe_errors(exc: ValidationError) -> str:
    """List failing setting paths and reasons, never the submitted values.

    Pydantic's own message embeds the input (a database URL with its password,
    for example), so it must not reach logs or the console.
    """
    lines = []
    for error in exc.errors(
        include_url=False, include_input=False, include_context=False
    ):
        location = ".".join(str(part) for part in error["loc"]) or "settings"
        lines.append(f"  {location}: {error['msg']}")
    return "\n".join(lines)


def load_settings(module_name: str) -> Settings:
    """Validate every public UPPERCASE name of ``module_name``.

    The module may name its own schema with ``SETTINGS_CLASS`` (a ``Settings``
    subclass or a dotted path to one) to declare project-wide settings.
    """
    try:
        module = importlib.import_module(module_name)
    except ModuleNotFoundError as exc:
        missing = exc.name or ""
        if module_name == missing or module_name.startswith(f"{missing}."):
            raise ImproperlyConfigured(
                f"Settings module {module_name!r} does not exist."
            ) from exc
        raise
    values = {
        name: getattr(module, name)
        for name in dir(module)
        if name.isupper() and not name.startswith("_")
    }
    schema = _schema(values.pop(SETTINGS_CLASS_NAME, Settings))
    values["SETTINGS_MODULE"] = module_name
    try:
        return schema.model_validate(values)
    except ValidationError as exc:
        # "from None": the chained ValidationError would print the inputs.
        raise ImproperlyConfigured(
            f"Invalid settings in {module_name!r}:\n{describe_errors(exc)}"
        ) from None


def _schema(value: object) -> type[Settings]:
    """Resolve ``SETTINGS_CLASS`` to a ``Settings`` subclass."""
    if isinstance(value, str):
        try:
            value = import_string(value)
        except ImportError as exc:
            raise ImproperlyConfigured(f"{SETTINGS_CLASS_NAME}: {exc}") from exc
    if isinstance(value, type) and issubclass(value, Settings):
        return value
    raise ImproperlyConfigured(
        f"{SETTINGS_CLASS_NAME} must be a subclass of domainkit.conf.Settings."
    )


class LazySettings:
    """Load settings on first access, like ``django.conf.settings``."""

    def __init__(self, env: Env) -> None:
        """Read the module name from ``<PREFIX>SETTINGS_MODULE`` through ``env``."""
        self._env = env
        self._wrapped: Settings | None = None

    @property
    def configured(self) -> bool:
        """Report whether a settings module has been loaded."""
        return self._wrapped is not None

    def __getattr__(self, name: str) -> Any:  # noqa: ANN401 - proxies any setting.
        """Load settings on first use, then delegate attribute access."""
        if name.startswith("_"):
            raise AttributeError(name)
        return getattr(self._setup(), name)

    def __setattr__(self, name: str, value: object) -> None:
        """Settings are read-only at runtime; tests use ``override_settings``."""
        if not name.startswith("_"):
            raise AttributeError(
                "Settings are read-only; use override_settings() in tests."
            )
        super().__setattr__(name, value)

    def unwrap(self) -> Settings:
        """Return the loaded ``Settings`` object itself (loading it if needed)."""
        return self._setup()

    def configure(self, module_name: str) -> None:
        """Load ``module_name`` now instead of ``<PREFIX>SETTINGS_MODULE``."""
        self._wrapped = load_settings(module_name)

    def reset(self) -> None:
        """Forget loaded settings; the next access loads them again."""
        self._wrapped = None

    @contextmanager
    def override(self, **changes: object) -> Iterator[None]:
        """Temporarily replace settings, re-running schema validation.

        A value of ``UNSET`` removes the setting, restoring its default.
        """
        current = self._setup()
        values = {**dict(current), **changes}
        values = {name: value for name, value in values.items() if value is not UNSET}
        try:
            replacement = type(current).model_validate(values)
        except ValidationError as exc:
            raise ImproperlyConfigured(
                f"Invalid settings override:\n{describe_errors(exc)}"
            ) from None
        self._wrapped = replacement
        try:
            yield
        finally:
            self._wrapped = current

    @contextmanager
    def use(self, module_name: str) -> Iterator[None]:
        """Temporarily load another settings module, then restore the current one."""
        previous = self._wrapped
        self._wrapped = load_settings(module_name)
        try:
            yield
        finally:
            self._wrapped = previous

    def _setup(self) -> Settings:
        """Return loaded settings, importing the configured module once."""
        if self._wrapped is None:
            self._wrapped = load_settings(self._env.str(SETTINGS_MODULE_VARIABLE))
        return self._wrapped
