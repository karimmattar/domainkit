"""``logconfig``: ``LOGGING`` as a JSON file, for a server that reads one.

Granian's main process never imports the application, so ``setup()``
never applies ``LOGGING`` there: its own lines (it starts, spawns and
stops workers, a worker died) keep Granian's format. ``granian
--log-config FILE`` reads a ``logging.config.dictConfig`` mapping from a
JSON file; this command writes ``LOGGING`` as one, each class or function
named by its dotted path, which ``dictConfig`` imports again::

    python manage.py logconfig --output /tmp/logging.json
    granian --log-config /tmp/logging.json ...

``manage.py serve`` gives the server the same mapping, with no file.

A value JSON cannot hold, which no dotted path names (an object, a
function defined inside another), is refused with its place in
``LOGGING``. Nothing is loaded but the settings, and nothing is printed
when ``--output`` names the file, so the command can run before the
server, where every line on stdout goes to the log pipeline.
"""

import json
import os
from argparse import ArgumentParser
from collections.abc import Mapping
from logging.config import BaseConfigurator
from pathlib import Path
from typing import Final

from domainkit.conf import settings
from domainkit.management.base import BaseCommand, CommandError

FILE_MODE: Final = 0o600
"""A new file is its owner's alone: ``LOGGING`` may hold a handler's secret."""
_RESOLVER: Final = BaseConfigurator({})
"""How ``dictConfig`` finds a dotted path's object again."""


class Command(BaseCommand):
    """Print ``LOGGING`` as JSON, or write it to a file."""

    help = (
        "Print LOGGING as JSON, classes and functions by their dotted paths, or "
        "write it to a file: granian --log-config reads it."
    )
    requires_setup = False

    def add_arguments(self, parser: ArgumentParser) -> None:
        """Accept an output file."""
        parser.add_argument(
            "-o",
            "--output",
            type=Path,
            help="write it here, readable by its owner only, and print nothing",
        )

    def handle(self, *, output: Path | None = None, **options: object) -> None:
        """Write ``LOGGING`` as JSON to ``output``, or to stdout."""
        text = json.dumps(portable(settings.LOGGING, "LOGGING"), indent=2)
        if output is None:
            self.write(text)
            return
        descriptor = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, FILE_MODE)
        with os.fdopen(descriptor, "w", encoding="utf-8") as file:
            file.write(f"{text}\n")


def portable(value: object, place: str) -> object:
    """``value`` as JSON holds it: classes and functions by their dotted paths.

    Raises:
        CommandError: Some part of ``value`` (at ``place``) is neither JSON
            nor named by a dotted path that leads back to it.
    """
    if isinstance(value, Mapping):
        portable_mapping: dict[str, object] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise CommandError(
                    f"{place} has a key that is not text, {key!r}: a JSON file "
                    "cannot hold it."
                )
            portable_mapping[key] = portable(item, f"{place}[{key!r}]")
        return portable_mapping
    if isinstance(value, list | tuple):
        return [portable(item, f"{place}[{index}]") for index, item in enumerate(value)]
    if value is None or isinstance(value, str | int | float):
        return value
    if (path := dotted_path(value)) is not None:
        return path
    raise CommandError(
        f"{place} is {value!r}, which a JSON file cannot hold: name a class or a "
        "function by its dotted path there, or build the object with a "
        '"()" factory.'
    )


def dotted_path(value: object) -> str | None:
    """The dotted path ``dictConfig`` finds ``value`` at, or None if none does."""
    module = getattr(value, "__module__", None)
    name = getattr(value, "__qualname__", None)
    if not isinstance(module, str) or not isinstance(name, str):
        return None
    path = f"{module}.{name}"
    try:
        found = _RESOLVER.resolve(path)
    except (ValueError, AttributeError):  # A name nothing imports, or <locals>.
        return None
    return path if found is value else None
