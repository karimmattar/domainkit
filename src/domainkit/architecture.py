"""Enforce the dependency rules of a domain-driven package layout.

Each domain package has four layers; dependencies point inward only::

    api  ->  infrastructure  ->  application  ->  domain

A layer is a package (``domain/``, ``application/``, ...) or, in the simple
layout ``startdomain`` writes by default, one module: ``domain.py`` (the
domain layer), ``controllers.py`` (application), ``models.py``
(infrastructure), and ``routes.py`` (api). The same rules hold either way.

- ``domain``: the standard library, ``domainkit.ddd``, ``domainkit.clock``,
  ``domainkit.money``, ``domainkit.i18n.lazy`` (to mark its texts for
  translation), and its own domain layer. No frameworks, no I/O, no
  settings.
- ``application``: no ``infrastructure`` or ``api`` code, no web or
  database libraries, and no outgoing calls (``domainkit.outbound``); it
  reaches storage and other services through ports.
- ``infrastructure``: no ``api`` code and no web libraries. It calls other
  services through ``domainkit.outbound`` (the outgoing HTTP client):
  calling another service is what an adapter does. Of ``domainkit.http``
  it may import the client's 2.8 path (``domainkit.http.client``, ADR
  0195); the checks and storing of uploads (``domainkit.http.uploads``),
  which a file gateway runs on what its route hands it, typed as
  ``Upload`` rather than Starlette's ``UploadFile`` (ADR 0177); and the
  problems a route raises (``domainkit.http.problems``: ``APIError``,
  ``FieldError``), so a file gateway refuses a file with the code a route
  would (ADR 0182), and only the names its ``__all__`` lists. The rest of
  ``domainkit.http.errors``, which builds responses, stays out.
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
from functools import cache
from importlib import import_module
from importlib.util import find_spec
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, Final, Self

from domainkit.conf.global_settings import LAYERS as LAYERS

if TYPE_CHECKING:
    from domainkit.conf.global_settings import ArchitectureConfig

__all__ = [
    "ALLOWED_MODULES",
    "FORBIDDEN_LAYERS",
    "FORBIDDEN_LIBRARIES",
    "LAYER_MODULES",
    "PUBLIC_SURFACE",
    "LayerRules",
    "Violation",
    "check_domain",
    "check_domains",
    "layer_warnings",
]

LAYER_MODULES: Final = MappingProxyType(
    {
        "domain": "domain",
        "controllers": "application",
        "models": "infrastructure",
        "routes": "api",
    }
)
"""The simple layout's modules, and the layer each one is."""

DOMAIN_ALLOWED_PREFIXES: Final = (
    "domainkit.ddd",
    "domainkit.clock",
    "domainkit.money",
    "domainkit.i18n.lazy",
)
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
        "domainkit.outbound",
    ),
    "infrastructure": ("fastapi", "starlette", "domainkit.http"),
}
"""Libraries each layer must not import."""

ALLOWED_MODULES: Final = {
    "infrastructure": (
        "domainkit.http.client",
        "domainkit.http.uploads",
        "domainkit.http.problems",
    )
}
"""Modules a layer may import although a library it must not use holds them.

An entry lifts only a library entry less specific than itself: a project
that forbids ``domainkit.http.client`` by name forbids it. That module is
the client's path of 2.8; the client lives in ``domainkit.outbound``,
which the infrastructure layer imports with no exception (ADR 0195).
"""

_PUBLIC_ONLY: Final = ("domainkit.http.problems",)
"""Allowed modules a layer may import only the names of their ``__all__`` from.

``from domainkit.http.problems import HTTPException`` would pass otherwise:
the rules read names, and a module holds every name it imports. The 2.7
modules (``client``, ``uploads``) are not held to it yet (ADR 0182): they
are warned of (``_PUBLIC_FROM_3_0``).
"""

_PUBLIC_FROM_3_0: Final = ("domainkit.http.client", "domainkit.http.uploads")
"""Allowed modules whose other names ``layer_warnings`` warns of; 3.0 refuses them.

``from domainkit.http.uploads import UploadFile`` passed in 2.8, and so did
Starlette's ``StreamingResponse`` and every name either module imports
(ADR 0188).
"""

_SERVING: Final = MappingProxyType({"domainkit.http.uploads": frozenset({"serve"})})
"""Names of an allowed module's ``__all__`` that answer a request with a response.

A route answers; a gateway hands it what to answer with. Warned of by
``layer_warnings``, refused by 3.0.
"""

FORBIDDEN_LAYERS: Final = {
    "application": ("infrastructure", "api"),
    "infrastructure": ("api",),
}
"""Layers of the same domain that each layer must not import."""

PUBLIC_SURFACE: Final = ("application", "domain.events", "controllers")
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
    allowed_modules: Mapping[str, tuple[str, ...]] = field(
        default_factory=lambda: _frozen(ALLOWED_MODULES)
    )
    """Modules each layer may import although a forbidden library holds them."""
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
        """These rules with more entries; nothing already here is removed.

        Nothing extends ``allowed_modules``: a project adds rules, never
        exceptions to them.
        """
        return type(self)(
            layers=_merged(self.layers, layers),
            domain_allowed=_merged(self.domain_allowed, domain_allowed),
            forbidden_libraries=_merged_table(
                self.forbidden_libraries, forbidden_libraries or {}
            ),
            allowed_modules=self.allowed_modules,
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
    from domainkit.conf import settings  # Read only when the rules are asked for.
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


def layer_warnings(
    installed: Iterable[str], rules: LayerRules | None = None
) -> list[Violation]:
    """Imports the rules let through in 2.x and 3.0 refuses (``architecture.W001``).

    Of each domain in ``installed``, under ``rules`` (the project's): a
    name a layer takes from an allowed module (``ALLOWED_MODULES``) that
    its ``__all__`` does not list, such as Starlette's ``UploadFile`` from
    ``domainkit.http.uploads``, and ``serve``, which builds a route's
    response. ``check`` lists them as warnings, so ``check --deploy``
    fails until the import changes (ADR 0188).
    """
    rules = project_rules() if rules is None else rules
    names = [name for name in installed if not _within(name, FRAMEWORK_PACKAGES)]
    found = []
    for name in names:
        for module, imported in iter_imports(name):
            layer = _layer(name, module, rules)
            if layer is not None and (rule := _refused_in_3_0(layer, imported, rules)):
                found.append(Violation(module, imported, rule))
    return list(dict.fromkeys(found))


def _refused_in_3_0(layer: str, imported: str, rules: LayerRules) -> str | None:
    """The rule 3.0 adds that ``imported`` breaks in ``layer``, if any."""
    library = _closest(imported, rules.forbidden_libraries.get(layer, ()))
    exception = _closest(imported, rules.allowed_modules.get(layer, ()))
    if (
        library is None
        or exception is None
        or exception not in _PUBLIC_FROM_3_0
        or len(exception) <= len(library)
        or imported == exception
    ):
        return None
    name = imported.removeprefix(f"{exception}.").partition(".")[0]
    if name in _SERVING.get(exception, ()):
        return (
            f"the {layer} layer may not answer a request with {exception}.{name}, "
            "which is its route's; 3.0 refuses it"
        )
    if name not in _public_names(exception):
        return (
            f"the {layer} layer may import only the __all__ of {exception}; "
            "3.0 refuses the rest"
        )
    return None


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
    forbidden = [
        f"{package}.{name}"
        for outer in rules.forbidden_layers.get(layer, ())
        for name in _names_of(outer)
    ]
    if _within(imported, forbidden):
        return f"the {layer} layer must not depend on outer layers"
    library = _closest(imported, rules.forbidden_libraries.get(layer, ()))
    exception = _closest(imported, rules.allowed_modules.get(layer, ()))
    if library is not None and (exception is None or len(exception) <= len(library)):
        return f"the {layer} layer must not use {imported.partition('.')[0]}"
    if library is not None and exception in _PUBLIC_ONLY and imported != exception:
        name = imported.removeprefix(f"{exception}.").partition(".")[0]
        if name not in _public_names(exception):
            return f"the {layer} layer may import only the __all__ of {exception}"
    return None


@cache
def _public_names(module: str) -> frozenset[str]:
    """The names ``module``, one of domainkit's own, lists in its ``__all__``."""
    return frozenset(import_module(module).__all__)


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
    if module == package:
        return None
    first = module.removeprefix(f"{package}.").partition(".")[0]
    if first in rules.layers:
        return first
    return LAYER_MODULES.get(first)


def _names_of(layer: str) -> tuple[str, ...]:
    """The package name of ``layer``, and its simple layout module's."""
    return (layer, *(name for name, of in LAYER_MODULES.items() if of == layer))


def _within(name: str, prefixes: Iterable[str]) -> bool:
    """Report whether ``name`` is one of ``prefixes`` or inside one."""
    return any(name == prefix or name.startswith(f"{prefix}.") for prefix in prefixes)


def _closest(name: str, prefixes: Iterable[str]) -> str | None:
    """The longest of ``prefixes`` that ``name`` is or is inside; None if none."""
    return max(
        (prefix for prefix in prefixes if _within(name, (prefix,))),
        key=len,
        default=None,
    )


def _is_stdlib(name: str) -> bool:
    """Report whether ``name`` belongs to the standard library."""
    return name.partition(".")[0] in sys.stdlib_module_names
