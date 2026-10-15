"""``startdomain``: create a bounded context from the domain template.

Like Django's ``startapp``, but the result is a layered package (``domain``,
``application``, ``infrastructure``, ``api``) with a working aggregate, its
persistence, its HTTP API, and tests, ready to rename and grow.
"""

import keyword
import re
from argparse import ArgumentParser
from importlib.util import find_spec
from pathlib import Path
from typing import Final

from domainkit.conf.env import project_root
from domainkit.management.base import BaseCommand, CommandError
from domainkit.utils.formatting import format_python
from domainkit.utils.text import camel_case, pluralize, snake_case

TEMPLATE_DIR: Final = Path(__file__).resolve().parents[1] / "templates" / "domain"
TEMPLATE_SUFFIX: Final = "-tpl"
_PLACEHOLDER: Final = re.compile(r"\{\{ (\w+) \}\}")
_SNAKE_NAME: Final = re.compile(r"[a-z][a-z0-9_]*")
_CLASS_NAME: Final = re.compile(r"[A-Z][A-Za-z0-9]*")


def template_context(
    name: str, parent: str, aggregate: str | None, plural: str | None
) -> dict[str, str]:
    """Return the placeholder values for domain ``name``; validates the names."""
    if not _SNAKE_NAME.fullmatch(name) or keyword.iskeyword(name):
        raise CommandError(f"{name!r} is not a valid snake_case package name.")
    aggregate = aggregate or camel_case(name)
    if not _CLASS_NAME.fullmatch(aggregate):
        raise CommandError(f"{aggregate!r} is not a valid CamelCase class name.")
    aggregate_snake = snake_case(aggregate)
    aggregates = plural or pluralize(aggregate_snake)
    if not _SNAKE_NAME.fullmatch(aggregates) or aggregates == aggregate_snake:
        raise CommandError(f"{aggregates!r} is not a valid snake_case plural.")
    return {
        "domain": name,
        "Domain": camel_case(name),
        "DOMAIN": name.upper(),
        "domain_title": name.replace("_", " ").title(),
        "package": f"{parent}.{name}",
        "Aggregate": aggregate,
        "aggregate": aggregate_snake,
        "model": aggregate.lower(),
        "aggregates": aggregates,
        "aggregates_title": aggregates.replace("_", " "),
        "table": f"{name}_{aggregates}",
    }


def fill(text: str, context: dict[str, str]) -> str:
    """Replace every ``{{ name }}`` in ``text`` with ``context[name]``."""
    return _PLACEHOLDER.sub(lambda match: context[match[1]], text)


def render(source: Path, target: Path, context: dict[str, str]) -> list[Path]:
    """Copy the ``source`` tree to ``target``, filling placeholders.

    Placeholders are filled in file names and in ``*-tpl`` files, which lose
    the suffix; other files are copied as they are.
    """
    created = []
    for path in sorted(source.rglob("*")):
        if path.is_dir() or "__pycache__" in path.parts:
            continue
        destination = target / fill(str(path.relative_to(source)), context)
        content = path.read_text(encoding="utf-8")
        if path.name.endswith(TEMPLATE_SUFFIX):
            destination = destination.with_name(
                destination.name.removesuffix(TEMPLATE_SUFFIX)
            )
            content = fill(content, context)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(content, encoding="utf-8")
        created.append(destination)
    return created


def package_directory(package: str) -> Path:
    """Return the directory of the importable package ``package``."""
    try:
        spec = find_spec(package)
    except ModuleNotFoundError:
        spec = None
    if spec is None or not spec.submodule_search_locations:
        raise CommandError(f"Parent package {package!r} cannot be imported.")
    return Path(next(iter(spec.submodule_search_locations)))


class Command(BaseCommand):
    """Generate ``<parent>/<name>/`` and ``tests/<parent>/<name>/``."""

    help = "Create a domain package (and its tests) from the domain template."
    requires_setup = False

    def add_arguments(self, parser: ArgumentParser) -> None:
        """Accept the domain name and naming overrides."""
        parser.add_argument("name", help="snake_case package name, e.g. billing")
        parser.add_argument(
            "--aggregate",
            help="aggregate root class name (default: the CamelCase domain name)",
        )
        parser.add_argument(
            "--plural", help="snake_case plural of the aggregate, for tables and URLs"
        )
        parser.add_argument(
            "--parent", default="domains", help="package that holds the domains"
        )
        parser.add_argument(
            "--tests-dir",
            type=Path,
            help="directory for the tests (default: tests/<parent path>)",
        )

    def handle(
        self,
        *,
        name: str,
        aggregate: str | None,
        plural: str | None,
        parent: str,
        tests_dir: Path | None,
        **options: object,
    ) -> None:
        """Render the package and its tests, then print the next steps."""
        context = template_context(name, parent, aggregate, plural)
        package_dir = package_directory(parent) / name
        root = project_root()
        tests_root = tests_dir or root / "tests" / Path(*parent.split("."))
        for directory in (package_dir, tests_root / name):
            if directory.exists():
                raise CommandError(f"{directory} already exists.")
        render(TEMPLATE_DIR / "package", package_dir, context)
        render(TEMPLATE_DIR / "tests", tests_root / name, context)
        directory = tests_root
        while directory != root and directory.is_relative_to(root):
            init = directory / "__init__.py"
            if not init.exists():
                init.write_text(f'"""Tests of {directory.name}."""\n', encoding="utf-8")
            directory = directory.parent
        format_python([package_dir, tests_root / name])
        self.write(_next_steps(context, package_dir, tests_root / name))


def _next_steps(context: dict[str, str], package_dir: Path, tests_dir: Path) -> str:
    """Explain how to install the generated domain."""
    package, domain = context["package"], context["domain"]
    return "\n".join(
        [
            f"Created {package_dir} and {tests_dir}.",
            "Next steps:",
            f"  1. Add {package!r} to INSTALLED_DOMAINS in your settings.",
            (
                "  2. Mount its router in your ROOT_ROUTER module:\n"
                f"       from {package}.api.routes import router as {domain}_router\n"
                f"       router.include_router({domain}_router, "
                f'prefix="/api/v1/{domain}")'
            ),
            f'  3. python manage.py makemigrations -m "create {domain}"',
            "  4. python manage.py migrate",
            (
                f"  5. Grant {domain}.view_{context['model']} (and add_, change_) "
                "to the users or groups that need them; superusers have them."
            ),
        ]
    )
