"""Enforce the dependency rules of a domain-driven package layout.

Each domain package has four layers; dependencies point inward only::

    api  ->  infrastructure  ->  application  ->  domain

- ``domain``: the standard library, ``domainkit.ddd``, ``domainkit.clock``,
  ``domainkit.money``, and its own domain layer. No frameworks, no I/O, no
  settings.
- ``application``: no ``infrastructure`` or ``api`` code, and no web or
  database libraries; it reaches storage through repository ports.
- ``infrastructure``: no ``api`` code and no web libraries.
- Another domain may be used only through its ``application`` layer or its
  ``domain.events`` (bounded contexts stay decoupled).

``python manage.py check`` and the test suite run these rules for every
installed domain. The tables below are the defaults; a project extends them
with the ``ARCHITECTURE`` setting (``LayerRules.extend``): packages its domain
layers may import, such as a shared kernel, libraries a layer must not use,
and layers of its own.
"""

import ast
import sys
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from importlib.util import find_spec
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, Final, Self

if TYPE_CHECKING:
    from domainkit.conf.global_settings import ArchitectureConfig

__all__ = [
    "DOMAIN_ALLOWED_PREFIXES",
    "FORBIDDEN_LAYERS",
    "FORBIDDEN_LIBRARIES",
    "FRAMEWORK_PACKAGES",
    "LAYERS",
    "PUBLIC_SURFACE",
    "LayerRules",
    "Violation",
    "check_domain",
    "check_domains",
    "iter_imports",
    "project_rules",
]

LAYERS: Final = ("domain", "application", "infrastructure", "api")
"""Layer packages of a domain, innermost first."""

DOMAIN_ALLOWED_PREFIXES: Final = ("domainkit.ddd", "domainkit.clock", "domainkit.money")
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


def _frozen(table: Mapping[str, Iterable[str]]) -> Mapping[str, tuple[str, ...]]:
    """A read-only copy of ``table`` with tuple values."""
    return MappingProxyType({key: tuple(value) for key, value in table.items()})


@dataclass(frozen=True, slots=True)
class LayerRules:
    """The rules one check applies; the defaults are the tables above.

    Projects do not build these by hand: ``ARCHITECTURE`` extends the
    defaults (``project_rules``), and only extends them, so every rule the
    framework states still holds. The defaults are read from the tables
    when the rules are made, so a project that replaced a table before
    0.9.2 still has its way.
    """

    layers: tuple[str, ...] = field(default_factory=lambda: LAYERS)
    """Layer packages a domain may have, innermost first."""
    domain_allowed: tuple[str, ...] = field(
        default_factory=lambda: DOMAIN_ALLOWED_PREFIXES
    )
    """Non-stdlib modules a domain layer may import (besides its own layer)."""
    forbidden_libraries: Mapping[str, tuple[str, ...]] = field(
        default_factory=lambda: _frozen(FORBIDDEN_LIBRARIES)
    )
    """Libraries each layer must not import."""
    forbidden_layers: Mapping[str, tuple[str, ...]] = field(
        default_factory=lambda: _frozen(FORBIDDEN_LAYERS)
    )
    """Layers of the same domain that each layer must not import."""
    public_surface: tuple[str, ...] = field(default_factory=lambda: PUBLIC_SURFACE)
    """Parts of a domain that other domains may import."""

    def extend(
        self,
        *,
        layers: Iterable[str] = (),
        domain_allowed: Iterable[str] = (),
        forbidden_libraries: Mapping[str, Iterable[str]] | None = None,
        forbidden_layers: Mapping[str, Iterable[str]] | None = None,
        public_surface: Iterable[str] = (),
    ) -> Self:
        """These rules with more entries; nothing already here is removed."""
        return type(self)(
            layers=_merged(self.layers, layers),
            domain_allowed=_merged(self.domain_allowed, domain_allowed),
            forbidden_libraries=_merged_table(
                self.forbidden_libraries, forbidden_libraries or {}
            ),
            forbidden_layers=_merged_table(
                self.forbidden_layers, forbidden_layers or {}
            ),
            public_surface=_merged(self.public_surface, public_surface),
        )

    @classmethod
    def from_config(cls, config: "ArchitectureConfig") -> Self:
        """The default rules extended by an ``ARCHITECTURE`` setting."""
        return cls().extend(
            layers=config.EXTRA_LAYERS,
            domain_allowed=config.DOMAIN_ALLOWED,
            forbidden_libraries=config.FORBIDDEN_LIBRARIES,
            forbidden_layers=config.FORBIDDEN_LAYERS,
            public_surface=config.PUBLIC_SURFACE,
        )


def project_rules() -> LayerRules:
    """The rules of the loaded settings' ``ARCHITECTURE``, or the defaults.

    Without a settings module (a script, a test of the rules themselves) the
    defaults apply.
    """
    from domainkit.conf import settings  # The settings import this module.
    from domainkit.exceptions import ImproperlyConfigured

    try:
        config: ArchitectureConfig = settings.ARCHITECTURE
    except ImproperlyConfigured:
        return LayerRules()
    return LayerRules.from_config(config)


def _merged(first: Iterable[str], more: Iterable[str]) -> tuple[str, ...]:
    """``first`` then the new entries of ``more``, each once, in order."""
    return tuple(dict.fromkeys((*first, *more)))


def _merged_table(
    first: Mapping[str, Iterable[str]], more: Mapping[str, Iterable[str]]
) -> Mapping[str, tuple[str, ...]]:
    """``first`` with the entries of ``more`` added, key by key."""
    keys = dict.fromkeys((*first, *more))
    return _frozen(
        {key: _merged(first.get(key, ()), more.get(key, ())) for key in keys}
    )


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


def check_domain(
    package: str, installed: Sequence[str] = (), rules: LayerRules | None = None
) -> list[Violation]:
    """Return the rule violations in domain ``package``.

    ``installed`` lists the other domain packages, for the cross-domain rule.
    ``rules`` defaults to the project's (``project_rules``).
    """
    rules = project_rules() if rules is None else rules
    others = [
        name
        for name in installed
        if name != package and not _within(name, FRAMEWORK_PACKAGES)
    ]
    violations = []
    for module, imported in iter_imports(package):
        layer = _layer(package, module, rules)
        rule = _cross_domain_rule(imported, others, rules) or _layer_rule(
            package, layer, imported, rules
        )
        if rule:
            violations.append(Violation(module, imported, rule))
    return list(dict.fromkeys(violations))


def check_domains(
    installed: Iterable[str], rules: LayerRules | None = None
) -> list[Violation]:
    """Check every domain in ``installed``, under ``rules`` (the project's)."""
    rules = project_rules() if rules is None else rules
    names = [name for name in installed if not _within(name, FRAMEWORK_PACKAGES)]
    return [
        violation for name in names for violation in check_domain(name, names, rules)
    ]


def _layer_rule(
    package: str, layer: str | None, imported: str, rules: LayerRules
) -> str | None:
    """Return the broken layer rule, if any."""
    if layer == "domain":
        own = f"{package}.domain"
        allowed = (own, *rules.domain_allowed)
        if not _is_stdlib(imported) and not _within(imported, allowed):
            return (
                "the domain layer may import only the standard library, "
                + "".join(f"{prefix}, " for prefix in rules.domain_allowed)
                + "and its own domain layer"
            )
        return None
    if layer is None:
        return None
    forbidden = [f"{package}.{name}" for name in rules.forbidden_layers.get(layer, ())]
    if _within(imported, forbidden):
        return f"the {layer} layer must not depend on outer layers"
    if _within(imported, rules.forbidden_libraries.get(layer, ())):
        return f"the {layer} layer must not use {imported.partition('.')[0]}"
    return None


def _cross_domain_rule(
    imported: str, others: Sequence[str], rules: LayerRules
) -> str | None:
    """Return the broken cross-domain rule, if any."""
    for other in others:
        if _within(imported, (other,)) and imported != other:
            public = tuple(f"{other}.{part}" for part in rules.public_surface)
            if not _within(imported, public):
                return "use another domain only through " + " or ".join(public)
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


def _layer(package: str, module: str, rules: LayerRules) -> str | None:
    """Return the layer ``module`` belongs to, or None for top-level modules."""
    first = module.removeprefix(f"{package}.").partition(".")[0]
    return first if module != package and first in rules.layers else None


def _within(name: str, prefixes: Iterable[str]) -> bool:
    """Report whether ``name`` is one of ``prefixes`` or inside one."""
    return any(name == prefix or name.startswith(f"{prefix}.") for prefix in prefixes)


def _is_stdlib(name: str) -> bool:
    """Report whether ``name`` belongs to the standard library."""
    return name.partition(".")[0] in sys.stdlib_module_names
