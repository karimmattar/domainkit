"""Contrib apps reach each other, and the core, only through what is public.

An app's public surface is its package (``domainkit.contrib.audit``) and the
modules that declare ``__all__`` (listed in ``docs/api.md``), and of those
only the names in ``__all__``. Anything else is the app's own: another app,
or the core, importing it couples the two so neither can change alone.

``ALLOWED`` lists the imports of that kind that existed when this test was
written (0.9.2). It may only shrink: a new one fails, and so does an entry
that is no longer needed, so that 0.10 can remove them one by one by giving
each app the public names the others use.

The core is held to the same rule from the apps' side: an app imports the
core's public names only, so a project can write an app as the contrib
apps are written. ``ALLOWED_CORE`` lists the core names apps used in 2.8
that no ``__all__`` publishes where they import them from; it only shrinks.

Apps also import no ring of apps: two apps that import each other, or
three or more that reach one another, can be neither read nor replaced
alone (ADR 0140). ``ALLOWED_CYCLES`` lists the one ring left, and why.
"""

import ast
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Final

from domainkit_tests.conftest import MakePackage

SRC: Final = Path(__file__).parents[1] / "src"

ALLOWED: Final = frozenset(
    {
        # The generic admin shows audit history.
        ("domainkit.contrib.admin.sites", "domainkit.contrib.audit.api.HistoryPageOut"),
        ("domainkit.contrib.admin.sites", "domainkit.contrib.audit.api.read_history"),
        ("domainkit.contrib.admin.sites", "domainkit.contrib.audit.history.MAX_LIMIT"),
        # Auth hangs its permissions off content types (it requires them).
        ("domainkit.contrib.auth.apps", "domainkit.contrib.contenttypes.sync"),
        ("domainkit.contrib.auth.principals", "domainkit.contrib.contenttypes.tables"),
        # One import of 0.9.2's repositories module, in the two it became.
        (
            "domainkit.contrib.auth.repositories.accounts",
            "domainkit.contrib.contenttypes.tables",
        ),
        (
            "domainkit.contrib.auth.repositories.principalstore",
            "domainkit.contrib.contenttypes.tables",
        ),
        ("domainkit.contrib.auth.tables", "domainkit.contrib.contenttypes.tables"),
        # E-mail address syntax, which belongs in domainkit.utils.
        ("domainkit.contrib.auth.models", "domainkit.contrib.mail.message"),
        # Admin APIs of flags and webhooks reuse auth's admin plumbing.
        ("domainkit.contrib.flags.admin", "domainkit.contrib.auth.admin.staff"),
        ("domainkit.contrib.webhooks.admin", "domainkit.contrib.auth.admin.staff"),
        # Object permissions extend auth's units of work.
        ("domainkit.contrib.objectpermissions.services", "domainkit.contrib.auth.uow"),
        # Privacy writes the audit trail.
        (
            "domainkit.contrib.privacy.models",
            "domainkit.contrib.audit.tables.audit_log",
        ),
        # Webhook delivery is an outbox task with messaging's retry policy.
        ("domainkit.contrib.webhooks.delivery", "domainkit.contrib.messaging.outbox"),
        ("domainkit.contrib.webhooks.delivery", "domainkit.contrib.messaging.registry"),
        ("domainkit.contrib.webhooks.delivery", "domainkit.contrib.messaging.settings"),
        ("domainkit.contrib.webhooks.settings", "domainkit.contrib.messaging.retry"),
    }
)
"""``(importer, private target)`` pairs that existed in 0.9.2; 0.10 removes them.

The core's were removed in 0.10: the core imports no contrib app at all
(``test_framework_layers.py``).
"""

ALLOWED_CORE: Final = frozenset(
    {
        # Core names apps imported in 2.8 that no __all__ publishes where
        # they are imported from. Publish a name, or import it from the
        # module that publishes it, and remove it here.
        "domainkit.broker.base.publish_with_priority",
        "domainkit.broker.handler.broker_class",
        "domainkit.cloud.http_errors",
        "domainkit.cloud.oauth_token",
        "domainkit.conf.env.Env",
        "domainkit.conf.global_settings.DEADLINE_MIDDLEWARE",
        "domainkit.conf.global_settings.DEPLOYED_ENVIRONMENTS",
        "domainkit.conf.global_settings.NAME_PATTERN",
        "domainkit.conf.global_settings.REDIS_SCHEMES",
        "domainkit.conf.global_settings.is_network_database",
        "domainkit.conf.unwrap_settings",
        "domainkit.db.connections.create_owner_engine",
        "domainkit.db.connections.pool_capacity",
        "domainkit.db.encrypted.context",
        "domainkit.db.encrypted.encrypted_columns",
        "domainkit.db.encrypted.index_context",
        "domainkit.db.encrypted.indexed_tables",
        "domainkit.db.fixtures.FixtureError",
        "domainkit.db.fixtures.tables_of",
        "domainkit.db.orm.children",
        "domainkit.db.orm.map_imperatively",
        "domainkit.db.query.column_of",
        "domainkit.db.query.contains_text",
        "domainkit.db.query.mapper_of",
        "domainkit.db.uow.tenant_schemas",
        "domainkit.ddd.events.inherits_actor",
        "domainkit.ddd.fields.fields_of",
        "domainkit.ddd.personal.SEPARATOR",
        "domainkit.ddd.personal.counted_fields",
        "domainkit.ddd.personal.event_subjects",
        "domainkit.ddd.personal.fields_about",
        "domainkit.ddd.personal.personal_fields",
        "domainkit.ddd.personal.subject_tag",
        "domainkit.ddd.personal.subjects_in",
        "domainkit.ddd.personal.tagged_with",
        "domainkit.ddd.query.And",
        "domainkit.ddd.query.Condition",
        "domainkit.ddd.uow.MAX_ORDERING_KEY",
        "domainkit.ddd.uow.checked_ordering_key",
        "domainkit.ddd.uow.expectations_of",
        "domainkit.ddd.usecases.UseCase",
        "domainkit.http.CURSOR_PATTERN",
        "domainkit.http.access.routes_need_users",
        "domainkit.http.access.served_routes",
        "domainkit.http.blocking.awaited_sync",
        "domainkit.http.errors.sqlstate",
        "domainkit.http.guard",
        "domainkit.http.guard.Address",
        "domainkit.http.guard.MAX_URL_LENGTH",
        "domainkit.http.guard.Network",
        "domainkit.http.guard.UnsafeURL",
        "domainkit.http.guard.lookup",
        "domainkit.http.guard.lookups_for",
        "domainkit.http.guard.tls_context",
        "domainkit.http.live.LONG_LIVED",
        "domainkit.http.live.REAUTHENTICATE",
        "domainkit.http.live.streams",
        "domainkit.http.metrics.register_refresher",
        "domainkit.http.middleware.PEER",
        "domainkit.http.middleware.host_allowed",
        "domainkit.http.middleware.request_path",
        "domainkit.http.ratelimit.address_key",
        "domainkit.http.ratelimit.client_group",
        "domainkit.http.ratelimit.refuse",
        "domainkit.keys.ring.DATA",
        "domainkit.keys.ring.FORMAT",
        "domainkit.keys.ring.SUBJECT",
        "domainkit.keys.ring.forget_held",
        "domainkit.keys.ring.sealed_key",
        "domainkit.management.operators.require_named",
        "domainkit.resources.access.ModelLike",
        "domainkit.resources.access.VIEW",
        "domainkit.resources.access.content_type_of",
        "domainkit.resources.access.rules_for",
        "domainkit.resources.conditional.PreconditionRequired",
        "domainkit.resources.conditional.asked_versions",
        "domainkit.resources.conditional.expected_versions",
        "domainkit.resources.renderers.CSV",
        "domainkit.resources.renderers.JSON",
        "domainkit.resources.renderers.csv_rows",
        "domainkit.resources.renderers.negotiate",
        "domainkit.resources.routing.DEFAULT",
        "domainkit.resources.routing.Handler",
        "domainkit.resources.routing.RepositoryOf",
        "domainkit.resources.routing.Scope",
        "domainkit.resources.routing.Service",
        "domainkit.resources.routing.UnitOfWorkFactory",
        "domainkit.signing.secret_keys",
        "domainkit.utils.decimals.with_decimal_text",
        "domainkit.utils.delivery.ConsoleDelivery",
        "domainkit.utils.delivery.DeliveryBackend",
        "domainkit.utils.delivery.MemoryDelivery",
        "domainkit.utils.delivery.load_backend",
        "domainkit.utils.text.pluralize",
        "domainkit.utils.text.snake_case",
        # The audit's fixes of 2.9 gave the apps these, before this list
        # existed: tenant_alone is the tenancy's own (ADR 0183), the rest
        # are shared definitions. Publish them, or import them from a module
        # that publishes them, and remove them here.
        "domainkit.context.caller_key",
        "domainkit.db.derive.IDENTIFIER_LENGTH",
        "domainkit.ddd.query.CriteriaLike",
        "domainkit.ddd.query.as_criteria",
        "domainkit.ddd.tenancy.tenant_alone",
        "domainkit.utils.text.IGNORABLE",
    }
)
"""Unpublished core names apps import, by the module they import them from.

By name, not by importer: the debt is a name the core must publish (or an
import to point at its public module), whichever app module uses it.
"""

ALLOWED_CYCLES: Final = frozenset(
    {
        # Auth forgets a user's credentials on privacy's PersonalDataErased,
        # which retention purges publish too; on_erase_subject runs only for
        # "privacy erase", with a subject rather than the user erased, so
        # moving to it would keep a purged user's credentials. Privacy
        # writes the audit trail, and audit's history API is guarded by
        # auth's rules.
        ("audit", "auth", "privacy"),
    }
)
"""Rings of apps that import one another, each with its reason; only shrinks."""


class Layout:
    """The modules of one framework-shaped package, read from its files."""

    def __init__(self, root: Path, package: str) -> None:
        """Index every module of ``package``, whose directory is under ``root``."""
        self.package = package
        base = root.joinpath(*package.split("."))
        self.files: dict[str, Path] = {}
        for path in sorted(base.rglob("*.py")):
            if {"templates", "migration_env"} & set(path.parts):
                continue
            parts = path.relative_to(root).with_suffix("").parts
            if parts[-1] == "__init__":
                parts = parts[:-1]
            self.files[".".join(parts)] = path
        contrib = base / "contrib"
        self.apps = {path.parent.name for path in contrib.glob("*/__init__.py")}

    def exports(self, module: str) -> set[str] | None:
        """``module``'s ``__all__``, or None for a module without one."""
        tree = ast.parse(self.files[module].read_text(encoding="utf-8"))
        for node in tree.body:
            if isinstance(node, ast.Assign) and any(
                isinstance(target, ast.Name) and target.id == "__all__"
                for target in node.targets
            ):
                return set(ast.literal_eval(node.value))
        return None

    def app(self, module: str) -> str | None:
        """The contrib app ``module`` belongs to, if any."""
        parts = module.split(".")
        prefix = [*self.package.split("."), "contrib"]
        inside = parts[: len(prefix)] == prefix and len(parts) > len(prefix)
        return (
            parts[len(prefix)] if inside and parts[len(prefix)] in self.apps else None
        )

    def crossing(self, importer: str, module: str, name: str | None) -> str | None:
        """The private target of importing ``name`` from ``module``, if it is one.

        ``name`` None imports the module itself. Within one app, and outside
        the contrib apps, nothing is private.
        """
        owner = self.app(module)
        if module not in self.files or owner is None or owner == self.app(importer):
            return None
        if name is not None and f"{module}.{name}" in self.files:
            module, name = f"{module}.{name}", None
        public = self.exports(module)
        if public is None:
            return module
        if name not in (None, "*") and name not in public:
            return f"{module}.{name}"
        return None

    def core_crossing(self, importer: str, module: str, name: str | None) -> str | None:
        """The core name an app imports that the core does not publish, if it is one.

        The core is every module outside ``<package>.contrib``. What it
        publishes is what its modules' ``__all__`` lists: a module without
        one publishes nothing, and a name is public only from a module
        whose ``__all__`` lists it, not from the module it was moved from.
        """
        contrib = f"{self.package}.contrib"
        inside = module == contrib or module.startswith(f"{contrib}.")
        if self.app(importer) is None or module not in self.files or inside:
            return None
        public = self.exports(module) or set()
        if name in public:
            return None
        if name is not None and f"{module}.{name}" in self.files:
            module, name = f"{module}.{name}", None
            public = self.exports(module) or set()
        if name is None or name == "*":
            return None if public else module
        return f"{module}.{name}"

    def crossings(self) -> Iterator[tuple[str, str]]:
        """Every ``(importer, private target)`` in the package."""
        yield from self._imports(self.crossing)

    def core_crossings(self) -> Iterator[tuple[str, str]]:
        """Every ``(app module, unpublished core name)`` in the package."""
        yield from self._imports(self.core_crossing)

    def _imports(
        self, crossing: Callable[[str, str, str | None], str | None]
    ) -> Iterator[tuple[str, str]]:
        """``(importer, target)`` for each import ``crossing`` finds a target in."""
        for importer, path in self.files.items():
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                targets: list[tuple[str, str | None]]
                if isinstance(node, ast.Import):
                    targets = [(alias.name, None) for alias in node.names]
                elif isinstance(node, ast.ImportFrom):
                    base = _resolve(importer, path, node.level, node.module)
                    targets = [(base, alias.name) for alias in node.names]
                else:
                    continue
                for module, name in targets:
                    if target := crossing(importer, module, name):
                        yield importer, target

    def cycles(self) -> set[tuple[str, ...]]:
        """Each set of apps that reach one another by imports, in alphabetical order.

        A strongly connected component of the graph of apps (Tarjan's
        algorithm): two apps that import each other, or three or more in
        a ring. At any depth, inside functions too: an import a function
        makes when it is called ties the apps as one at the top of a
        module does.
        """
        uses = self.app_imports()
        index: dict[str, int] = {}
        low: dict[str, int] = {}
        stack: list[str] = []
        found: set[tuple[str, ...]] = set()

        def connect(app: str) -> None:
            index[app] = low[app] = len(index)
            stack.append(app)
            for other in sorted(uses.get(app, ())):
                if other not in index:
                    connect(other)
                    low[app] = min(low[app], low[other])
                elif other in stack:
                    low[app] = min(low[app], index[other])
            if low[app] == index[app]:
                component = []
                while (top := stack.pop()) != app:
                    component.append(top)
                if component:
                    found.add(tuple(sorted([app, *component])))

        for app in sorted(uses):
            if app not in index:
                connect(app)
        return found

    def app_imports(self) -> dict[str, set[str]]:
        """The other apps each app imports, at any depth."""
        uses: dict[str, set[str]] = {}
        for importer, path in self.files.items():
            app = self.app(importer)
            if app is None:
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    modules = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom):
                    base = _resolve(importer, path, node.level, node.module)
                    modules = [base, *(f"{base}.{alias.name}" for alias in node.names)]
                else:
                    continue
                for module in modules:
                    other = self.app(module)
                    if other is not None and other != app:
                        uses.setdefault(app, set()).add(other)
        return uses


def _resolve(importer: str, path: Path, level: int, target: str | None) -> str:
    """The absolute module a ``from`` import of ``importer`` names."""
    if level == 0:
        return target or ""
    base = importer.split(".")
    if path.name != "__init__.py":
        base = base[:-1]
    base = base[: len(base) - (level - 1)]
    return ".".join([*base, target] if target else base)


def test_no_new_imports_of_another_apps_internals() -> None:
    """Apps, and the core, use each other's public names; the old list only shrinks."""
    found = set(Layout(SRC, "domainkit").crossings())

    assert sorted(found - ALLOWED) == [], "Import the app's public names instead."
    assert sorted(ALLOWED - found) == [], "No longer imported: remove from ALLOWED."


def test_apps_import_only_the_names_the_core_publishes() -> None:
    """An app uses the core as a project's app would; the old list only shrinks."""
    found = {target for _, target in Layout(SRC, "domainkit").core_crossings()}

    assert sorted(found - ALLOWED_CORE) == [], "Import or publish a public name."
    assert sorted(ALLOWED_CORE - found) == [], "No longer imported: remove it."


def test_no_apps_import_one_another_in_a_ring() -> None:
    """An app asks the apps built on it through hooks, not imports (ADR 0140).

    Two apps that import each other, or three or more in a ring, at any
    depth; the ring left only shrinks.
    """
    found = Layout(SRC, "domainkit").cycles()

    assert sorted(found - ALLOWED_CYCLES) == [], "Ask through a hook instead."
    assert sorted(ALLOWED_CYCLES - found) == [], "No longer a ring: remove it."


def test_the_check_finds_rings_of_any_length(
    make_package: MakePackage, tmp_path: Path
) -> None:
    """``one -> two -> three -> one`` is one ring, though no two import each other."""
    make_package("fakekit.contrib.one", {"__init__.py": "from ..two import x\n"})
    make_package("fakekit.contrib.two", {"__init__.py": "from ..three import x\n"})
    make_package(
        "fakekit.contrib.three",
        {"__init__.py": "def f():\n    import fakekit.contrib.one\n"},
    )
    make_package("fakekit.contrib.four", {"__init__.py": "from ..one import x\n"})

    assert Layout(tmp_path, "fakekit").cycles() == {("one", "three", "two")}


def test_the_check_finds_apps_that_import_each_other(
    make_package: MakePackage, tmp_path: Path
) -> None:
    """One way is a dependency; both ways, even inside a function, is a cycle."""
    make_package(
        "fakekit.contrib.base",
        {
            "__init__.py": "",
            "rules.py": """
                def ask():
                    from fakekit.contrib.trail import record
                    from ..money import ledger
            """,
        },
    )
    make_package(
        "fakekit.contrib.trail",
        {"__init__.py": "record = 1\n", "api.py": "from fakekit.contrib import base\n"},
    )
    make_package(
        "fakekit.contrib.money",
        {"__init__.py": "", "ledger.py": "import fakekit.contrib.base.rules\n"},
    )
    make_package(
        "fakekit.contrib.alone",
        {"__init__.py": "", "uses.py": "from ..base import rules\nimport json\n"},
    )

    assert Layout(tmp_path, "fakekit").cycles() == {("base", "money", "trail")}


def test_the_check_finds_private_imports_and_lets_public_ones_pass(
    make_package: MakePackage, tmp_path: Path
) -> None:
    """Modules without ``__all__``, and names outside it, are another app's own."""
    make_package(
        "fakekit.contrib.one",
        {
            "__init__.py": "from .models import Thing\n__all__ = ['Thing']\n",
            "models.py": "class Thing: ...\nclass Hidden: ...\n",
            "api.py": "router = 1\nhelper = 2\n__all__ = ['router']\n",
            "inner.py": "from . import models\nfrom .models import Hidden\n",
        },
    )
    make_package(
        "fakekit.contrib.two",
        {
            "uses.py": """
                import fakekit.contrib.one
                import fakekit.contrib.one.models
                from fakekit.contrib.one import Thing, models
                from fakekit.contrib.one.api import router, helper
                from fakekit.contrib.one.api import *
                from fakekit.contrib import one
                from fakekit.contrib.one.models import Hidden
                import json
            """,
        },
    )
    make_package("fakekit.core", {"rules.py": "from ..contrib.one.inner import x\n"})

    found = set(Layout(tmp_path, "fakekit").crossings())

    assert found == {
        ("fakekit.contrib.two.uses", "fakekit.contrib.one.models"),
        ("fakekit.contrib.two.uses", "fakekit.contrib.one.api.helper"),
        ("fakekit.core.rules", "fakekit.contrib.one.inner"),
    }


def test_the_check_finds_core_names_the_core_does_not_publish(
    make_package: MakePackage, tmp_path: Path
) -> None:
    """A name outside its module's ``__all__``, or from a module without one."""
    make_package(
        "fakekit.core",
        {
            "__init__.py": "from .rules import Rule\nregistry = 1\n"
            "__all__ = ['Rule', 'registry']\n",
            "rules.py": "class Rule: ...\nclass Hidden: ...\n__all__ = ['Rule']\n",
            "registry.py": "",
            "loose.py": "helper = 1\n",
        },
    )
    make_package(
        "fakekit.contrib.one",
        {
            "uses.py": """
                from fakekit.core import Rule, registry
                from fakekit.core.rules import Rule, Hidden
                from fakekit.core.rules import *
                from fakekit.core.loose import helper
                from fakekit.core import loose
                import fakekit.core.rules
                import fakekit.core.loose
                from ...core import rules
                import json
            """,
        },
    )
    make_package(
        "fakekit.other", {"inner.py": "from fakekit.core.loose import helper\n"}
    )

    found = set(Layout(tmp_path, "fakekit").core_crossings())

    assert found == {
        ("fakekit.contrib.one.uses", "fakekit.core.rules.Hidden"),
        ("fakekit.contrib.one.uses", "fakekit.core.loose.helper"),
        ("fakekit.contrib.one.uses", "fakekit.core.loose"),
    }
