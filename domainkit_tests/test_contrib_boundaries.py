"""Contrib apps reach each other only through what each app makes public.

An app's public surface is its package (``domainkit.contrib.audit``) and the
modules that declare ``__all__`` (listed in ``docs/api.md``), and of those
only the names in ``__all__``. Anything else is the app's own: another app,
or the core, importing it couples the two so neither can change alone.

``ALLOWED`` lists the imports of that kind that existed when this test was
written (0.9.2). It may only shrink: a new one fails, and so does an entry
that is no longer needed, so that 0.10 can remove them one by one by giving
each app the public names the others use.
"""

import ast
from collections.abc import Iterator
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
        (
            "domainkit.contrib.auth.repositories",
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

    def crossings(self) -> Iterator[tuple[str, str]]:
        """Every ``(importer, private target)`` in the package."""
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
                    if target := self.crossing(importer, module, name):
                        yield importer, target


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
