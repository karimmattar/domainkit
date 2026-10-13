"""Run management commands: ``python manage.py <command>``.

Commands come from ``domainkit.management.commands`` and from each installed
domain's ``management.commands`` package; a domain command with the same name
replaces the built-in one.
"""

import difflib
import pkgutil
import sys
from collections.abc import Iterable, Sequence
from importlib import import_module
from importlib.util import find_spec
from pathlib import Path
from typing import TextIO

from domainkit.conf import settings
from domainkit.exceptions import ImproperlyConfigured
from domainkit.management.base import BaseCommand, CommandError

BUILTIN_COMMANDS = "domainkit.management.commands"

__all__ = [
    "BaseCommand",
    "CommandError",
    "call_command",
    "execute_from_command_line",
    "get_commands",
]


def _find_commands(locations: Iterable[str]) -> list[str]:
    """Return the public module names in the given package directories."""
    return [
        name
        for _, name, is_package in pkgutil.iter_modules(list(locations))
        if not is_package and not name.startswith("_")
    ]


def get_commands() -> dict[str, str]:
    """Map each command name to the package that provides it.

    Domain commands are listed only when the settings load; built-in
    commands are always available (``startdomain`` needs no settings).
    """
    commands = dict.fromkeys(
        _find_commands(import_module(BUILTIN_COMMANDS).__path__), BUILTIN_COMMANDS
    )
    try:
        installed = settings.INSTALLED_DOMAINS
    except ImproperlyConfigured:
        return commands
    for domain in installed:
        package = f"{domain}.management.commands"
        try:
            spec = find_spec(package)
        except ModuleNotFoundError:
            continue
        if spec is not None and spec.submodule_search_locations:
            for name in _find_commands(spec.submodule_search_locations):
                commands[name] = package
    return commands


def load_command(
    name: str, stdout: TextIO | None = None, stderr: TextIO | None = None
) -> BaseCommand:
    """Instantiate the ``Command`` class of command ``name``."""
    commands = get_commands()
    if name not in commands:
        suggestion = difflib.get_close_matches(name, list(commands), n=1)
        hint = f" Did you mean {suggestion[0]!r}?" if suggestion else ""
        raise CommandError(f"Unknown command {name!r}.{hint}")
    command: BaseCommand = import_module(f"{commands[name]}.{name}").Command(
        stdout=stdout, stderr=stderr
    )
    return command


def call_command(
    name: str,
    *args: str,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
    **options: object,
) -> None:
    """Run command ``name`` from code; ``options`` override parsed defaults."""
    command = load_command(name, stdout=stdout, stderr=stderr)
    parser = command.create_parser("manage.py", name)
    parsed = vars(parser.parse_args(list(args)))
    unknown = sorted(set(options) - set(parsed))
    if unknown:
        raise TypeError(f"Unknown options for {name!r}: {', '.join(unknown)}")
    command.execute(**{**parsed, **options})


def execute_from_command_line(argv: Sequence[str] | None = None) -> None:
    """Dispatch ``argv`` (default ``sys.argv``) to a command and exit on errors."""
    argv = list(sys.argv if argv is None else argv)
    prog = Path(argv[0]).name
    name = argv[1] if len(argv) > 1 else "help"
    if name in {"help", "--help", "-h"}:
        sys.stdout.write(_usage(prog))
        return
    try:
        load_command(name).run_from_argv(argv)
    except CommandError as exc:
        sys.stderr.write(f"Error: {exc}\n")
        sys.exit(exc.returncode)
    except ImproperlyConfigured as exc:
        sys.stderr.write(f"Configuration error: {exc}\n")
        sys.exit(1)


def _usage(prog: str) -> str:
    """List the available commands grouped by the package that provides them."""
    lines = [f"Usage: {prog} <command> [options]", ""]
    by_package: dict[str, list[str]] = {}
    for name, package in sorted(get_commands().items()):
        by_package.setdefault(package.removesuffix(".management.commands"), []).append(
            name
        )
    for package, names in by_package.items():
        lines.append(f"[{package}]")
        lines.extend(f"    {name}" for name in names)
    lines.append("")
    lines.append(f"Run '{prog} <command> --help' for a command's options.")
    return "\n".join(lines) + "\n"
