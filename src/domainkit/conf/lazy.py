"""Lazy settings proxy that imports and validates the selected settings module."""

import importlib
import threading
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from types import UnionType
from typing import Annotated, Any, Final, Union, get_args, get_origin

from pydantic import BaseModel, ValidationError

from domainkit.conf.env import Env
from domainkit.conf.global_settings import Settings
from domainkit.exceptions import ImproperlyConfigured
from domainkit.utils.module_loading import import_string
from domainkit.utils.text import did_you_mean
from domainkit.utils.unset import UNSET

SETTINGS_MODULE_VARIABLE: Final = "SETTINGS_MODULE"
SETTINGS_CLASS_NAME: Final = "SETTINGS_CLASS"


def describe_errors(exc: ValidationError, schema: type[BaseModel] | None = None) -> str:
    """List failing setting paths and reasons, never the submitted values.

    Pydantic's own message embeds the input (a database URL with its password,
    for example), so it must not reach logs or the console. With the
    ``schema`` validated, a key it does not know names the one it is near:
    ``CACHES.default.URl: Extra inputs are not permitted (did you mean URL?)``.
    """
    lines = []
    for error in exc.errors(
        include_url=False, include_input=False, include_context=False
    ):
        location = ".".join(str(part) for part in error["loc"]) or "settings"
        message = error["msg"]
        if schema is not None and error["type"] == "extra_forbidden":
            *path, key = error["loc"]
            message += did_you_mean(str(key), _keys_at(schema, path))
        lines.append(f"  {location}: {message}")
    return "\n".join(lines)


def _keys_at(schema: type[BaseModel], path: Sequence[object]) -> set[str]:
    """The keys the models at ``path`` of ``schema`` declare."""
    found: list[object] = [schema]
    for part in path:
        found = [inner for annotation in found for inner in _inside(annotation, part)]
    return {
        name
        for annotation in found
        for kind in _kinds(annotation)
        if isinstance(kind, type) and issubclass(kind, BaseModel)
        for name in kind.model_fields
    }


def _inside(annotation: object, part: object) -> Iterator[object]:
    """What a value of ``annotation`` holds under ``part``: a field, a value."""
    for kind in _kinds(annotation):
        if isinstance(kind, type) and issubclass(kind, BaseModel):
            if (field := kind.model_fields.get(str(part))) is not None:
                yield field.annotation
            continue
        arguments = get_args(kind)
        if get_origin(kind) in {dict, Mapping} and len(arguments) == 2:
            yield arguments[1]
        elif get_origin(kind) in {list, tuple, Sequence} and arguments:
            yield arguments[0]


def _kinds(annotation: object) -> Iterator[object]:
    """``annotation`` without ``Annotated``, and each member of a union."""
    origin = get_origin(annotation)
    if origin is Annotated:
        yield from _kinds(get_args(annotation)[0])
    elif origin in {Union, UnionType}:
        for member in get_args(annotation):
            yield from _kinds(member)
    else:
        yield annotation


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
            f"Invalid settings in {module_name!r}:\n{describe_errors(exc, schema)}"
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


_KEEPING: Final = threading.Lock()
"""Held while a value is kept on the proxy, and while the settings are replaced."""


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
        """Load settings on first use, then delegate attribute access.

        The value is kept on the proxy, as Django keeps it: the next read
        of the setting is an ordinary attribute, with no call. Replacing
        the settings (``override``, ``configure``, ``reset``) forgets them.
        """
        if name.startswith("_"):
            raise AttributeError(name)
        wrapped = self._setup()
        value = getattr(wrapped, name)
        with _KEEPING:
            # Kept only if the settings it came from are still the ones
            # loaded: a thread that read them while another replaced them
            # must not keep an old value past the replacement.
            if self.__dict__.get("_wrapped") is wrapped:
                self.__dict__[name] = value
        return value

    def __setattr__(self, name: str, value: object) -> None:
        """Settings are read-only at runtime; tests use ``override_settings``.

        Replacing the loaded settings forgets every value read from them.
        """
        if not name.startswith("_"):
            raise AttributeError(
                "Settings are read-only; use override_settings() in tests."
            )
        if name != "_wrapped":
            super().__setattr__(name, value)
            return
        with _KEEPING:
            super().__setattr__(name, value)
            for kept in [each for each in self.__dict__ if not each.startswith("_")]:
                del self.__dict__[kept]

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
                f"Invalid settings override:\n{describe_errors(exc, type(current))}"
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
