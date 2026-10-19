"""``openapi``: export the OpenAPI schema, or list breaking changes against one."""

import json
from argparse import ArgumentParser
from pathlib import Path
from typing import Any

from domainkit.http import get_asgi_application
from domainkit.http.openapi import breaking_changes
from domainkit.management.base import BaseCommand, CommandError

MAX_SCHEMA_BYTES = 64 * 1024 * 1024
"""The largest schema ``--diff`` reads."""


class Command(BaseCommand):
    """Print the schema as JSON, write it to a file, or compare it with one."""

    help = (
        "Export the OpenAPI schema as JSON; --diff OLD.json lists the changes "
        "that break clients of OLD.json and fails if there are any."
    )

    def add_arguments(self, parser: ArgumentParser) -> None:
        """Accept an output file or a schema to compare with."""
        mode = parser.add_mutually_exclusive_group()
        mode.add_argument(
            "-o", "--output", type=Path, help="write the schema here, not to stdout"
        )
        mode.add_argument(
            "--diff",
            type=Path,
            metavar="OLD",
            help="list breaking changes against this schema; exit 1 if any",
        )
        parser.add_argument(
            "--indent", type=int, default=2, help="spaces per level (default: 2)"
        )

    def handle(
        self,
        *,
        output: Path | None,
        diff: Path | None,
        indent: int,
        **options: object,
    ) -> None:
        """Build the application and export or compare its schema."""
        schema = get_asgi_application().openapi()
        if diff is not None:
            self._diff(_read(diff), schema, diff)
            return
        text = json.dumps(schema, indent=indent or None, ensure_ascii=False)
        if output is None:
            self.write(text)
            return
        output.write_text(f"{text}\n", encoding="utf-8")
        self.write(f"Wrote {output}.")

    def _diff(self, old: dict[str, Any], new: dict[str, Any], path: Path) -> None:
        """Print each breaking change; fail when there is one."""
        found = breaking_changes(old, new)
        if not found:
            self.write(f"No breaking changes since {path}.")
            return
        for change in found:
            self.write(f"Breaking: {change}")
        plural = "s" if len(found) > 1 else ""
        raise CommandError(f"{len(found)} breaking change{plural} since {path}.")


def _read(path: Path) -> dict[str, Any]:
    """Load an OpenAPI document, refusing what is not one."""
    try:
        if path.stat().st_size > MAX_SCHEMA_BYTES:
            raise CommandError(f"{path} is larger than {MAX_SCHEMA_BYTES} bytes.")
        document = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise CommandError(f"Cannot read {path}: {exc.strerror or exc}.") from None
    except (ValueError, RecursionError):
        raise CommandError(f"{path} is not a JSON document.") from None
    if not isinstance(document, dict) or not isinstance(document.get("paths"), dict):
        raise CommandError(f"{path} is not an OpenAPI schema (it has no paths).")
    return document
