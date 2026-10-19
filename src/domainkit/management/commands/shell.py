"""``shell``: an asyncio console with the project loaded; ``await`` works."""

import ast
import asyncio
import code
import inspect
import pkgutil
import types
from argparse import ArgumentParser
from collections.abc import Iterator
from importlib import import_module
from typing import Final

from domainkit.apps import DomainConfig, registry
from domainkit.conf import settings
from domainkit.db.connections import connections
from domainkit.ddd import Entity, Repository, UnitOfWork, ValueObject
from domainkit.lifecycle import close_resources
from domainkit.management.base import BaseCommand

SKIPPED: Final = frozenset({"management", "tests", "migrations"})
"""Parts of a domain package the shell never imports."""
KINDS: Final = (
    ("models", (Entity, ValueObject)),
    ("repositories", (Repository,)),
    ("units of work", (UnitOfWork,)),
)
"""What the shell imports from each domain, and the classes that say so."""


class AsyncConsole(code.InteractiveConsole):
    """A console whose top-level ``await`` runs on one event loop.

    The loop lives as long as the console, so connections opened by one
    line still work in the next.
    """

    def __init__(
        self, namespace: dict[str, object], loop: asyncio.AbstractEventLoop
    ) -> None:
        """Run code in ``namespace``, awaiting it on ``loop``."""
        super().__init__(namespace)
        self.loop = loop
        self.compile.compiler.flags |= ast.PyCF_ALLOW_TOP_LEVEL_AWAIT

    def runcode(self, code: types.CodeType) -> None:
        """Run ``code``, awaiting it if it awaits; show errors, as the console does."""
        try:
            self.execute(code)
        except SystemExit:
            raise
        except BaseException:  # noqa: BLE001 - the console shows every error.
            self.showtraceback()

    def execute(self, code: types.CodeType) -> None:
        """Run ``code`` in the namespace, on the loop when it awaits."""
        result = types.FunctionType(code, self.locals)()
        if inspect.iscoroutine(result):
            self.loop.run_until_complete(result)


class Command(BaseCommand):
    """Open the console with settings, the registry, connections, and models.

    Each installed domain's models, repositories, and units of work are
    imported by name; ``--no-imports`` leaves them out. ``-c`` runs code
    and exits. Top-level ``await`` works in both.
    """

    help = "Open an asyncio Python console with the project loaded; await works."

    def add_arguments(self, parser: ArgumentParser) -> None:
        """Accept a command to run and the import switch."""
        parser.add_argument("-c", "--command", help="run this code instead")
        parser.add_argument(
            "--no-imports",
            dest="imports",
            action="store_false",
            help="import no domain classes",
        )

    def handle(self, *, command: str | None, imports: bool, **options: object) -> None:
        """Run the code or the console, then close what it opened."""
        namespace: dict[str, object] = {
            "asyncio": asyncio,
            "settings": settings,
            "registry": registry,
            "connections": connections,
        }
        skipped: list[str] = []
        if imports:
            for name, value in domain_classes(skipped):
                if name in namespace:
                    skipped.append(f"{value.__module__}.{name} (name taken)")
                else:
                    namespace[name] = value
        loop = asyncio.new_event_loop()
        console = AsyncConsole(namespace, loop)
        try:
            if command is not None:
                source = compile(
                    command, "<command>", "exec", flags=ast.PyCF_ALLOW_TOP_LEVEL_AWAIT
                )
                console.execute(source)
                return
            console.interact(banner=self._banner(namespace, skipped), exitmsg="")
        finally:
            loop.run_until_complete(close_resources())
            loop.close()

    def _banner(self, namespace: dict[str, object], skipped: list[str]) -> str:
        """Name the project and what the console holds."""
        lines = [
            (
                f"{settings.PROJECT_NAME} shell ({settings.SETTINGS_MODULE}); "
                "use await at the top level."
            ),
            f"Available: {', '.join(namespace)}.",
        ]
        lines.extend(f"Not imported: {item}." for item in skipped)
        return "\n".join(lines)


def domain_classes(skipped: list[str]) -> Iterator[tuple[str, type]]:
    """Every installed domain's models, repositories, and units of work.

    Modules that fail to import are added to ``skipped``.
    """
    for config in registry:
        for module in _modules(config, skipped):
            for name, value in vars(module).items():
                if (
                    isinstance(value, type)
                    and value.__module__ == module.__name__
                    and not name.startswith("_")
                    and any(
                        issubclass(value, kind) for _, kinds in KINDS for kind in kinds
                    )
                ):
                    yield name, value


def _modules(config: DomainConfig, skipped: list[str]) -> Iterator[types.ModuleType]:
    """The domain's modules, but its commands, tests, and migrations."""
    yield config.module
    for info in pkgutil.walk_packages(config.module.__path__, f"{config.name}."):
        relative = info.name.removeprefix(f"{config.name}.").split(".")
        if SKIPPED & set(relative):
            continue
        try:
            yield import_module(info.name)
        except Exception as exc:  # noqa: BLE001 - one broken module is reported.
            skipped.append(f"{info.name} ({type(exc).__name__})")
