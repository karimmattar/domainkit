"""Base class for management commands (``django.core.management.base``)."""

import asyncio
import inspect
import sys
from argparse import ArgumentParser
from collections.abc import Awaitable, Sequence
from pathlib import Path
from typing import Any, TextIO


class CommandError(Exception):
    """Stop the command and print the message; the process exits with ``returncode``."""

    def __init__(self, message: str, *, returncode: int = 1) -> None:
        """Store the message and exit status."""
        super().__init__(message)
        self.returncode = returncode


class BaseCommand:
    """One ``manage.py`` command, in ``<package>.management.commands.<name>``.

    Subclass it as ``Command``, describe it in ``help``, declare options in
    ``add_arguments``, and implement ``handle``. ``handle`` may be ``async``;
    it then runs in a fresh event loop and database connections are closed
    afterwards.
    """

    help: str = ""
    requires_setup: bool = True
    """Load settings and installed domains before ``handle`` runs."""

    def __init__(
        self, stdout: TextIO | None = None, stderr: TextIO | None = None
    ) -> None:
        """Write output to ``stdout``/``stderr`` (default: the process streams)."""
        self.stdout = stdout or sys.stdout
        self.stderr = stderr or sys.stderr

    def create_parser(self, prog_name: str, command: str) -> ArgumentParser:
        """Build the argument parser for ``prog_name command``."""
        parser = ArgumentParser(
            prog=f"{Path(prog_name).name} {command}", description=self.help or None
        )
        self.add_arguments(parser)
        return parser

    def add_arguments(self, parser: ArgumentParser) -> None:
        """Declare the command's options."""

    def run_from_argv(self, argv: Sequence[str]) -> None:
        """Parse ``[prog, command, *args]`` and execute."""
        parser = self.create_parser(argv[0], argv[1])
        self.execute(**vars(parser.parse_args(argv[2:])))

    def execute(self, **options: object) -> None:
        """Set up the framework if needed, then run ``handle``."""
        if self.requires_setup:
            from domainkit import setup

            setup()
        result = self.handle(**options)
        if inspect.isawaitable(result):
            asyncio.run(_run_async(result))

    def handle(
        self,
        *args: Any,  # noqa: ANN401 - subclasses declare their own arguments.
        **options: Any,  # noqa: ANN401 - subclasses declare their own options.
    ) -> Awaitable[None] | None:
        """Do the command's work with the parsed options as keyword arguments."""
        raise NotImplementedError

    def write(self, message: str) -> None:
        """Print one line to the command's stdout."""
        self.stdout.write(f"{message}\n")


async def _run_async(awaitable: Awaitable[None]) -> None:
    """Await an async ``handle``, then close connections opened in this loop."""
    from domainkit.cache import caches
    from domainkit.db.connections import connections

    try:
        await awaitable
    finally:
        await connections.close_all()
        await caches.close_all()
