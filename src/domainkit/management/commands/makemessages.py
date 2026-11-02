"""``makemessages``: bring each domain's catalogs up to date with its code.

``manage.py makemessages -l ar`` reads every module of each installed
domain (domainkit's own aside), finds the messages marked for translation
(``domainkit.i18n.extraction``), and writes
``<domain>/locale/ar/LC_MESSAGES/messages.po``: new messages empty,
translations kept, messages no code uses any more kept as obsolete
entries; a domain with nothing to translate gets no catalog.
``compilemessages`` then compiles them. ``--all`` updates every
locale a domain already has; ``--check`` writes nothing and fails if a
catalog is behind its code, for CI (alone, it checks every catalog).
"""

from argparse import ArgumentParser
from pathlib import Path

from domainkit.apps import registry
from domainkit.i18n.catalogs import CatalogError
from domainkit.i18n.extraction import extract, updated
from domainkit.i18n.translation import to_locale
from domainkit.management.base import BaseCommand, CommandError

FRAMEWORK = "domainkit."


class Command(BaseCommand):
    """Update ``<domain>/locale/<locale>/LC_MESSAGES/messages.po`` from the code."""

    help = (
        "Find the messages each domain marks for translation and update its catalogs."
    )

    def add_arguments(self, parser: ArgumentParser) -> None:
        """Accept domains, locales, --all, and --check."""
        parser.add_argument(
            "domains",
            nargs="*",
            metavar="DOMAIN",
            help="labels (default: every domain)",
        )
        parser.add_argument(
            "-l",
            "--locale",
            action="append",
            default=[],
            metavar="LOCALE",
            help="a locale to update or start, such as ar or pt_BR (repeatable)",
        )
        parser.add_argument(
            "--all",
            dest="every",
            action="store_true",
            help="update every locale a domain already has",
        )
        parser.add_argument(
            "--check",
            action="store_true",
            help="write nothing; fail if a catalog is behind its code",
        )

    def handle(
        self,
        *,
        domains: list[str],
        locale: list[str],
        every: bool,
        check: bool,
        **options: object,
    ) -> None:
        """Update, or check, each catalog of each domain.

        Raises:
            CommandError: No locale is named, a label names no domain, a
                module does not parse, or (``--check``) a catalog is behind.
        """
        every = every or (check and not locale)  # --check alone: every catalog.
        if not locale and not every:
            raise CommandError("Name a locale with -l (such as -l ar), or use --all.")
        configs = [
            config for config in registry if not config.name.startswith(FRAMEWORK)
        ]
        if domains:
            known = {config.label: config for config in configs}
            missing = [label for label in domains if label not in known]
            if missing:
                raise CommandError(f"No installed domain is labelled {missing}.")
            configs = [known[label] for label in domains]
        behind: list[str] = []
        for config in configs:
            root = Path(config.path)
            try:
                found = extract(root.rglob("*.py"), root)
            except SyntaxError as exc:
                raise CommandError(f"{exc.filename}:{exc.lineno}: {exc.msg}") from None
            locales = set(map(to_locale, locale))
            if every and (root / "locale").is_dir():
                locales |= {
                    path.name for path in (root / "locale").iterdir() if path.is_dir()
                }
            for code in sorted(locales):
                path = root / "locale" / code / "LC_MESSAGES" / "messages.po"
                if not found and not path.is_file():
                    continue  # Nothing to translate: no catalog to start.
                before = path.read_text(encoding="utf-8") if path.is_file() else None
                try:
                    text, untranslated = updated(
                        path, found, project=config.label, language=code
                    )
                except CatalogError as exc:
                    raise CommandError(f"{path}: {exc}") from None
                if check:
                    if text != before:
                        behind.append(str(path))
                    continue
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(text, encoding="utf-8")
                self.write(
                    f"{path}: {len(found)} message{'' if len(found) == 1 else 's'}, "
                    f"{untranslated} to translate."
                )
        if behind:
            raise CommandError(
                f"Behind their code: {', '.join(behind)}. Run makemessages."
            )
        if check:
            self.write("Every catalog is up to date with its code.")
