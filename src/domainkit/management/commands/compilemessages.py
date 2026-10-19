"""``compilemessages``: compile ``.po`` catalogs to the ``.mo`` files gettext reads."""

from argparse import ArgumentParser
from pathlib import Path
from typing import Final

from domainkit import setup
from domainkit.apps import registry
from domainkit.conf import settings
from domainkit.i18n.catalogs import CatalogError, compile_po, parse_po, translated
from domainkit.management.base import BaseCommand, CommandError

FRAMEWORK: Final = "domainkit."


class Command(BaseCommand):
    """Compile each ``<locale>/LC_MESSAGES/*.po`` of the locale directories."""

    help = (
        "Compile translation catalogs (.po) to .mo files: in the directories "
        "named, or in LOCALE_PATHS and each installed domain's locale directory."
    )
    requires_setup = False

    def add_arguments(self, parser: ArgumentParser) -> None:
        """Accept directories, languages, and --check."""
        parser.add_argument(
            "directories",
            nargs="*",
            type=Path,
            metavar="DIRECTORY",
            help="locale directories (default: LOCALE_PATHS and the domains')",
        )
        parser.add_argument(
            "-l",
            "--locale",
            action="append",
            default=[],
            metavar="LOCALE",
            help="only this locale, such as ar or pt_BR (repeatable)",
        )
        parser.add_argument(
            "--check",
            action="store_true",
            help="write nothing; fail if a .mo file is missing or out of date",
        )

    def handle(
        self,
        *,
        directories: list[Path],
        locale: list[str],
        check: bool,
        **options: object,
    ) -> None:
        """Compile each catalog, or report the stale ones."""
        catalogs = sorted(
            path
            for directory in directories or _project_directories()
            for path in directory.glob("*/LC_MESSAGES/*.po")
            if not locale or path.parent.parent.name in locale
        )
        if not catalogs:
            raise CommandError("No .po catalogs found.")
        stale = []
        for po in catalogs:
            text = po.read_text(encoding="utf-8")
            try:
                compiled = compile_po(text)
            except CatalogError as exc:
                raise CommandError(f"{po}: {exc}") from None
            mo = po.with_suffix(".mo")
            if check:
                if not mo.is_file() or mo.read_bytes() != compiled:
                    stale.append(str(mo))
                continue
            _write(mo, compiled)
            count = sum(1 for _ in translated(parse_po(text)))
            self.write(f"Compiled {po} ({count} translated).")
        if stale:
            raise CommandError(f"Out of date: {', '.join(stale)}. Run compilemessages.")
        if check:
            self.write(f"{len(catalogs)} catalog(s) up to date.")


def _project_directories() -> list[Path]:
    """``LOCALE_PATHS`` and each installed domain's ``locale``, but the framework's."""
    setup()
    domains = [
        config.path / "locale"
        for config in registry
        if not config.name.startswith(FRAMEWORK)
    ]
    return [path for path in [*settings.LOCALE_PATHS, *domains] if path.is_dir()]


def _write(path: Path, data: bytes) -> None:
    """Replace ``path`` with ``data`` at once, so a running service never reads half."""
    partial = path.with_name(f".{path.name}.tmp")
    partial.write_bytes(data)
    partial.replace(path)
