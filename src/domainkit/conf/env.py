"""Typed environment reader for settings modules (django-environ style).

Every variable of a project shares one prefix, derived from the project name
in ``pyproject.toml`` (``billingAPI`` reads ``BILLING_API_DEBUG``); see
``project_prefix``. The framework's own variables use it too:
``<PREFIX>SETTINGS_MODULE`` selects the settings, like Django's
``DJANGO_SETTINGS_MODULE``.

The process environment wins over the project ``.env`` file. Set
``<PREFIX>ENV_FILE`` to another path, or to an empty value to skip the file;
relative paths are resolved from the project root.

Any variable can instead be given as ``<NAME>_FILE`` pointing to a file that
holds the value (Docker and Kubernetes secrets). Prefer files for secrets in
production: environment variables leak through process listings, crash
reports, and ``docker inspect``. ``.env`` values are taken literally; ``${...}``
is not expanded.
"""

import builtins
import os
import re
import tomllib
from collections.abc import Callable
from functools import cache, cached_property
from pathlib import Path
from typing import Any, Final

from dotenv import dotenv_values

from domainkit.exceptions import ImproperlyConfigured
from domainkit.utils.text import snake_case

PROJECT_ROOT_VARIABLE: Final = "DOMAINKIT_PROJECT_ROOT"
"""Set it when the working directory is not inside the project."""
ENV_PREFIX_VARIABLE: Final = "DOMAINKIT_ENV_PREFIX"
"""Set it when the project's ``pyproject.toml`` is not shipped (e.g. wheels)."""
ENV_FILE_VARIABLE: Final = "ENV_FILE"
_PREFIX: Final = re.compile(r"(?:[A-Z][A-Z0-9_]*_)?")
_TRUE: Final = frozenset({"1", "true", "yes", "on"})
_FALSE: Final = frozenset({"0", "false", "no", "off"})


class _Missing:
    """Sentinel type for settings that have no default and must be provided."""


MISSING: Final = _Missing()


@cache
def project_root() -> Path:
    """Return the directory of the project being run (not of domainkit).

    ``DOMAINKIT_PROJECT_ROOT`` wins; otherwise the nearest directory, from the
    working directory upwards, that holds a ``pyproject.toml``; otherwise the
    working directory itself. ``manage.py``, uvicorn, and the Docker image all
    run from the project directory.
    """
    configured = os.environ.get(PROJECT_ROOT_VARIABLE)
    if configured:
        return Path(configured).expanduser().resolve()
    cwd = Path.cwd().resolve()
    return next(
        (path for path in (cwd, *cwd.parents) if (path / "pyproject.toml").is_file()),
        cwd,
    )


def prefix_for(name: str) -> str:
    """Turn a project name into a prefix (``billingAPI``: ``BILLING_API_``)."""
    words = re.sub(r"[^A-Za-z0-9]+", "_", snake_case(name)).strip("_").upper()
    return f"{words}_" if words else ""


@cache
def project_prefix() -> str:
    """Return the prefix shared by this project's environment variables.

    In order: ``DOMAINKIT_ENV_PREFIX``; ``[tool.domainkit] env-prefix`` in the
    project's ``pyproject.toml`` (an empty string means no prefix); the
    project name, ``[project].name``. Renaming the project renames its
    variables.
    """
    explicit = os.environ.get(ENV_PREFIX_VARIABLE)
    if explicit is None:
        pyproject = _read_pyproject()
        options: dict[str, Any] = pyproject.get("tool", {}).get("domainkit", {})
        explicit = options.get("env-prefix")
        if explicit is None:
            name = pyproject.get("project", {}).get("name")
            if not name:
                raise ImproperlyConfigured(
                    f"Cannot derive the environment prefix: set [project].name in "
                    f"{project_root() / 'pyproject.toml'} or {ENV_PREFIX_VARIABLE}."
                )
            explicit = prefix_for(name)
    prefix = str(explicit).upper()
    if prefix and not prefix.endswith("_"):
        prefix = f"{prefix}_"
    if not _PREFIX.fullmatch(prefix):
        raise ImproperlyConfigured(
            f"Environment prefix {prefix!r} must start with a letter and use only "
            "A-Z, 0-9, and underscores."
        )
    return prefix


def _read_pyproject() -> dict[str, Any]:
    """Parse the project's ``pyproject.toml``; a missing file yields ``{}``."""
    path = project_root() / "pyproject.toml"
    if not path.is_file():
        return {}
    try:
        return tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise ImproperlyConfigured(f"{path} is not valid TOML: {exc}") from exc


class Env:
    """Read prefixed variables and convert them, failing with the variable name.

    ``Env().int("WORKERS")`` reads ``<project prefix>WORKERS``; pass
    ``prefix`` to read another family of variables (``""`` for none).
    """

    def __init__(
        self, prefix: builtins.str | None = None, env_file: Path | None = None
    ) -> None:
        """Use ``env_file`` instead of ``<PREFIX>ENV_FILE`` or the project ``.env``."""
        self._prefix = prefix
        self._env_file = env_file

    @property
    def prefix(self) -> builtins.str:
        """The prefix in use; the project prefix unless one was given."""
        return project_prefix() if self._prefix is None else self._prefix

    @cached_property
    def file_values(self) -> dict[builtins.str, builtins.str]:
        """Load the ``.env`` file once; a missing file contributes nothing."""
        path = self._env_file
        if path is None:
            configured = os.environ.get(f"{self.prefix}{ENV_FILE_VARIABLE}")
            if configured == "":
                return {}
            path = _from_root(Path(configured or ".env"))
        if not path.is_file():
            return {}
        values = dotenv_values(path, interpolate=False)
        return {key: value for key, value in values.items() if value is not None}

    def raw(self, name: builtins.str) -> builtins.str | None:
        """Return the unconverted value of ``prefix + name``, or None when unset."""
        key = f"{self.prefix}{name}"
        value = self._lookup(key)
        secret_file = self._lookup(f"{key}_FILE")
        if secret_file is None:
            return value
        if value is not None:
            raise ImproperlyConfigured(f"Set either {key} or {key}_FILE, not both.")
        path = _from_root(Path(secret_file))
        try:
            return path.read_text(encoding="utf-8").rstrip("\r\n")
        except OSError as exc:
            raise ImproperlyConfigured(f"{key}_FILE: cannot read {path}.") from exc

    def str[D](
        self, name: builtins.str, default: D | _Missing = MISSING
    ) -> builtins.str | D:
        """Read text."""
        text = self.raw(name)
        return self._default(name, default) if text is None else text

    def int[D](
        self, name: builtins.str, default: D | _Missing = MISSING
    ) -> builtins.int | D:
        """Read an integer."""
        text = self.raw(name)
        if text is None:
            return self._default(name, default)
        return self._convert(name, text, builtins.int, "an integer")

    def float[D](
        self, name: builtins.str, default: D | _Missing = MISSING
    ) -> builtins.float | D:
        """Read a number."""
        text = self.raw(name)
        if text is None:
            return self._default(name, default)
        return self._convert(name, text, builtins.float, "a number")

    def bool[D](
        self, name: builtins.str, default: D | _Missing = MISSING
    ) -> builtins.bool | D:
        """Read 1/0, true/false, yes/no, or on/off (any case)."""
        text = self.raw(name)
        if text is None:
            return self._default(name, default)
        normalized = text.strip().lower()
        if normalized in _TRUE:
            return True
        if normalized in _FALSE:
            return False
        raise self._invalid(name, text, "a boolean (true/false)")

    def list[D](
        self, name: builtins.str, default: D | _Missing = MISSING
    ) -> builtins.list[builtins.str] | D:
        """Read comma-separated text; blank items are dropped."""
        text = self.raw(name)
        if text is None:
            return self._default(name, default)
        return [item.strip() for item in text.split(",") if item.strip()]

    def path[D](self, name: builtins.str, default: D | _Missing = MISSING) -> Path | D:
        """Read a file system path; relative paths start at the project root."""
        text = self.raw(name)
        if text is None:
            return self._default(name, default)
        return _from_root(Path(text).expanduser())

    def _lookup(self, key: builtins.str) -> builtins.str | None:
        """Return ``key`` from the process environment, else from ``.env``."""
        value = os.environ.get(key)
        return value if value is not None else self.file_values.get(key)

    def _default[D](self, name: builtins.str, default: D | _Missing) -> D:
        """Return the caller's default, or fail when the variable is required."""
        if isinstance(default, _Missing):
            raise ImproperlyConfigured(
                f"Set the {self.prefix}{name} environment variable."
            )
        return default

    def _convert[T](
        self,
        name: builtins.str,
        text: builtins.str,
        cast: Callable[[builtins.str], T],
        expected: builtins.str,
    ) -> T:
        """Convert with ``cast`` or explain which variable is invalid."""
        try:
            return cast(text)
        except ValueError as exc:
            raise self._invalid(name, text, expected) from exc

    def _invalid(
        self, name: builtins.str, text: builtins.str, expected: builtins.str
    ) -> ImproperlyConfigured:
        """Build the error for a value that cannot be converted."""
        return ImproperlyConfigured(
            f"{self.prefix}{name} must be {expected}, got {text!r}."
        )


def _from_root(path: Path) -> Path:
    """Resolve relative paths against the project root."""
    return path if path.is_absolute() else project_root() / path
