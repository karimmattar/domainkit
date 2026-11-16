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

The project's own modules outside every domain are read too: the package
of the settings module, and that of ``ROOT_ROUTER``'s module, as Django
reads the project's files. What they mark goes to the first directory of
``LOCALE_PATHS``, which is read first at run time; it is only added to
that catalog, which also holds the project's words for others' messages.
Without ``LOCALE_PATHS``, those no catalog translates already are named,
and ``--check`` fails (ADR 0166).
"""

import os
from argparse import ArgumentParser
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from gettext import NullTranslations
from importlib.util import find_spec
from pathlib import Path

from domainkit.apps import registry
from domainkit.conf import settings
from domainkit.i18n.catalogs import CatalogError
from domainkit.i18n.extraction import Found, added, extract, updated
from domainkit.i18n.translation import catalog, to_locale
from domainkit.management.base import BaseCommand, CommandError

FRAMEWORK = "domainkit."

type Update = Callable[..., tuple[str, int]]
"""``updated`` or ``added``: a catalog's new text, and how many lack a translation."""


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
            help="labels (default: every domain, and the project's own modules)",
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
        """Update, or check, each catalog of each domain, and the project's.

        Raises:
            CommandError: No locale is named, a label names no domain, a
                module does not parse, or (``--check``) a catalog is
                behind, or the project marks messages and ``LOCALE_PATHS``
                is empty.
        """
        every = every or (check and not locale)  # --check alone: every catalog.
        if not locale and not every:
            raise CommandError("Name a locale with -l (such as -l ar), or use --all.")
        installed = list(registry)
        configs = [
            config for config in installed if not config.name.startswith(FRAMEWORK)
        ]
        if domains:
            known = {config.label: config for config in configs}
            missing = [label for label in domains if label not in known]
            if missing:
                raise CommandError(f"No installed domain is labelled {missing}.")
            configs = [known[label] for label in domains]
        run = _Run(set(map(to_locale, locale)), every, check)
        for config in configs:
            root = Path(config.path)
            found = _extract(root.rglob("*.py"), root)
            self._update(run, root / "locale", found, config.label)
        problems = []
        if not domains:
            found = _extract(*project_modules(Path(c.path) for c in installed))
            if found and settings.LOCALE_PATHS:
                directory = Path(settings.LOCALE_PATHS[0])
                self._update(run, directory, found, settings.PROJECT_NAME, added)
            elif found and (unkept := untranslated(found)):
                if check:
                    problems.append(_unkept(unkept))
                else:
                    self.note(_unkept(unkept))
        if run.behind:
            problems.append(
                f"Behind their code: {', '.join(run.behind)}. Run makemessages."
            )
        if problems:
            raise CommandError(" ".join(problems))
        if check:
            self.write("Every catalog is up to date with its code.")

    def _update(
        self,
        run: "_Run",
        directory: Path,
        found: Sequence[Found],
        project: str,
        update: Update = updated,
    ) -> None:
        """Update, or check, the catalogs of ``directory`` with ``update``."""
        locales = run.locales
        if run.every and directory.is_dir():
            locales = locales | {
                path.name for path in directory.iterdir() if path.is_dir()
            }
        for code in sorted(locales):
            path = directory / code / "LC_MESSAGES" / "messages.po"
            if not found and not path.is_file():
                continue  # Nothing to translate: no catalog to start.
            before = path.read_text(encoding="utf-8") if path.is_file() else None
            try:
                text, untranslated = update(path, found, project=project, language=code)
            except CatalogError as exc:
                raise CommandError(f"{path}: {exc}") from None
            if run.check:
                if text != before:
                    run.behind.append(str(path))
                continue
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
            self.write(
                f"{path}: {len(found)} message{'' if len(found) == 1 else 's'}, "
                f"{untranslated} to translate."
            )


@dataclass(slots=True)
class _Run:
    """The locales one run updates, or checks, and the catalogs found behind."""

    locales: set[str]
    every: bool
    """Also every locale a catalog directory already has."""
    check: bool
    """Write nothing; name the catalogs behind their code."""
    behind: list[str] = field(default_factory=list)


def project_modules(domains: Iterable[Path]) -> tuple[list[Path], Path]:
    """The project's own modules outside ``domains``, and the root they are named from.

    The package that holds the settings module, and the one that holds
    ``ROOT_ROUTER``'s module (``config`` for ``config.urls.router``); a
    module outside any package stands alone. domainkit's own modules,
    and those of each installed domain, are left out.
    """
    router = (settings.ROOT_ROUTER or "").replace(":", ".")  # "config.urls:router"
    names = [settings.SETTINGS_MODULE, router.rpartition(".")[0]]
    places: list[Path] = []
    for name in dict.fromkeys(names):
        if not name or name == "domainkit" or name.startswith(FRAMEWORK):
            continue
        package = name.rpartition(".")[0] or name
        try:
            spec = find_spec(package)
        except ModuleNotFoundError:
            spec = None
        if spec is None:
            continue
        locations = spec.submodule_search_locations or [spec.origin or ""]
        places += [Path(where) for where in locations if where]
    outside = tuple(domains)
    files = {
        path
        for place in places
        for path in ([place] if place.is_file() else place.rglob("*.py"))
        if not any(path.is_relative_to(domain) for domain in outside)
    }
    parents = [place.parent for place in places] or [Path.cwd()]
    return sorted(files), Path(os.path.commonpath(parents))


def untranslated(found: Sequence[Found]) -> list[Found]:
    """The messages of ``found`` no catalog translates into a language of ``LANGUAGES``.

    The catalogs gettext reads: the domains' and domainkit's, compiled.
    A message one of them translates (``"Not Found"``) needs no catalog of
    the project's.
    """
    catalogs = [catalog(code) for code, _ in settings.LANGUAGES]
    return [
        message
        for message in found
        if not any(_translates(each, message) for each in catalogs)
    ]


def _translates(found: NullTranslations, message: Found) -> bool:
    """Whether ``found`` translates ``message`` (with its context and plural)."""
    key = (
        message.msgid
        if message.context is None
        else f"{message.context}\x04{message.msgid}"
    )
    text = (
        found.gettext(key)
        if message.plural is None
        else found.ngettext(key, message.plural, 1)
    )
    return text != key


def _extract(files: Iterable[Path], root: Path) -> list[Found]:
    """``extract``, a module that does not parse named as a command error."""
    try:
        return extract(files, root)
    except SyntaxError as exc:
        raise CommandError(f"{exc.filename}:{exc.lineno}: {exc.msg}") from None


def _unkept(found: Sequence[Found]) -> str:
    """Why the messages the project marks have no catalog to go to."""
    files = sorted(
        {where.rpartition(":")[0] for each in found for where in each.references}
    )
    count = f"{len(found)} message{'' if len(found) == 1 else 's'}"
    verb = "is" if len(found) == 1 else "are"
    return (
        f"{count} {verb} marked outside every domain ({', '.join(files)}), and "
        "LOCALE_PATHS names no directory to keep them: set it, then run "
        "makemessages."
    )
