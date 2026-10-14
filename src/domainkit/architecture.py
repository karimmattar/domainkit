"""Enforce the dependency rules of a domain-driven package layout.

Each domain package has four layers; dependencies point inward only::

    api  ->  infrastructure  ->  application  ->  domain

- ``domain``: the standard library, ``domainkit.ddd``, and its own domain layer.
  No frameworks, no I/O, no settings.
- ``application``: no ``infrastructure`` or ``api`` code, and no web or
  database libraries; it reaches storage through repository ports.
- ``infrastructure``: no ``api`` code and no web libraries.
- Another domain may be used only through its ``application`` layer or its
  ``domain.events`` (bounded contexts stay decoupled).

``python manage.py check`` and the test suite run these rules for every
installed domain. Adjust the tables below to change the policy project-wide.
"""

import ast
import sys
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass
from importlib.util import find_spec
from pathlib import Path
from typing import Final

LAYERS: Final = ("domain", "application", "infrastructure", "api")

DOMAIN_ALLOWED_PREFIXES: Final = ("domainkit.ddd",)
"""Non-stdlib modules a domain layer may import (besides its own layer)."""

FORBIDDEN_LIBRARIES: Final = {
    "application": (
        "fastapi",
        "starlette",
        "sqlalchemy",
        "alembic",
        "redis",
        "domainkit.db",
        "domainkit.http",
    ),
    "infrastructure": ("fastapi", "starlette", "domainkit.http"),
}
"""Libraries each layer must not import."""

FORBIDDEN_LAYERS: Final = {
    "application": ("infrastructure", "api"),
    "infrastructure": ("api",),
}
"""Layers of the same domain that each layer must not import."""

PUBLIC_SURFACE: Final = ("application", "domain.events")
"""Parts of a domain that other domains may import."""

FRAMEWORK_PACKAGES: Final = ("domainkit",)
"""Installed framework apps (``domainkit.contrib.*``) are services, not domains:
they are neither checked nor protected by the cross-domain rule."""


@dataclass(frozen=True, slots=True)
class Violation:
    """One import that breaks a rule."""

    module: str
    imported: str
    rule: str

    def __str__(self) -> str:
        """Render as ``module imports name: rule``."""
        return f"{self.module} imports {self.imported}: {self.rule}"


def iter_imports(package: str) -> Iterator[tuple[str, str]]:
    """Yield ``(module, imported)`` for every import in ``package``'s files.

    Relative imports are resolved, and ``from x import y`` yields both ``x``
    and ``x.y`` because ``y`` may be a submodule. Imports under
    ``TYPE_CHECKING`` count: they are dependencies too.
    """
    for path, module in _modules(package):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        is_package = path.name == "__init__.py"
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    yield module, alias.name
            elif isinstance(node, ast.ImportFrom):
                base = _resolve(module, is_package, node.level, node.module)
                yield module, base
                for alias in node.names:
                    if alias.name != "*":
                        yield module, f"{base}.{alias.name}"


def check_domain(package: str, installed: Sequence[str] = ()) -> list[Violation]:
    """Return the rule violations in domain ``package``.

    ``installed`` lists the other domain packages, for the cross-domain rule.
    """
    others = [
        name
        for name in installed
        if name != package and not _within(name, FRAMEWORK_PACKAGES)
    ]
    violations = []
    for module, imported in iter_imports(package):
        layer = _layer(package, module)
        rule = _cross_domain_rule(imported, others) or _layer_rule(
            package, layer, imported
        )
        if rule:
            violations.append(Violation(module, imported, rule))
    return list(dict.fromkeys(violations))


def check_domains(installed: Iterable[str]) -> list[Violation]:
    """Check every domain in ``installed``."""
    names = [name for name in installed if not _within(name, FRAMEWORK_PACKAGES)]
    return [violation for name in names for violation in check_domain(name, names)]


def _layer_rule(package: str, layer: str | None, imported: str) -> str | None:
    """Return the broken layer rule, if any."""
    if layer == "domain":
        own = f"{package}.domain"
        allowed = (own, *DOMAIN_ALLOWED_PREFIXES)
        if not _is_stdlib(imported) and not _within(imported, allowed):
            return (
                "the domain layer may import only the standard library, "
                "domainkit.ddd, and its own domain layer"
            )
        return None
    if layer is None:
        return None
    forbidden = [f"{package}.{name}" for name in FORBIDDEN_LAYERS.get(layer, ())]
    if _within(imported, forbidden):
        return f"the {layer} layer must not depend on outer layers"
    if _within(imported, FORBIDDEN_LIBRARIES.get(layer, ())):
        return f"the {layer} layer must not use {imported.partition('.')[0]}"
    return None


def _cross_domain_rule(imported: str, others: Sequence[str]) -> str | None:
    """Return the broken cross-domain rule, if any."""
    for other in others:
        if _within(imported, (other,)) and imported != other:
            public = tuple(f"{other}.{part}" for part in PUBLIC_SURFACE)
            if not _within(imported, public):
                return (
                    f"use another domain only through {other}.application "
                    f"or {other}.domain.events"
                )
    return None


def _modules(package: str) -> Iterator[tuple[Path, str]]:
    """Yield ``(path, dotted name)`` for each Python file of ``package``."""
    spec = find_spec(package)
    if spec is None or not spec.submodule_search_locations:
        raise ValueError(f"{package!r} is not an importable package.")
    for location in spec.submodule_search_locations:
        root = Path(location)
        for path in sorted(root.rglob("*.py")):
            parts = path.relative_to(root).with_suffix("").parts
            if parts[-1] == "__init__":
                parts = parts[:-1]
            yield path, ".".join((package, *parts))


def _resolve(module: str, is_package: bool, level: int, target: str | None) -> str:
    """Turn a possibly relative ``from`` import into an absolute module name."""
    if level == 0:
        return target or ""
    base = module.split(".")
    if not is_package:
        base = base[:-1]
    base = base[: len(base) - (level - 1)]
    return ".".join([*base, target] if target else base)


def _layer(package: str, module: str) -> str | None:
    """Return the layer ``module`` belongs to, or None for top-level modules."""
    first = module.removeprefix(f"{package}.").partition(".")[0]
    return first if module != package and first in LAYERS else None


def _within(name: str, prefixes: Iterable[str]) -> bool:
    """Report whether ``name`` is one of ``prefixes`` or inside one."""
    return any(name == prefix or name.startswith(f"{prefix}.") for prefix in prefixes)


def _is_stdlib(name: str) -> bool:
    """Report whether ``name`` belongs to the standard library."""
    return name.partition(".")[0] in sys.stdlib_module_names
