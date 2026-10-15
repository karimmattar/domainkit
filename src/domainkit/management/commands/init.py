"""``init``: create a project from the project template (Django's ``startproject``).

It runs before any project exists, through the ``domainkit`` program the
package installs::

    domainkit init billingAPI            # creates ./billingAPI
    domainkit init billingAPI billing    # or names the directory

The project gets settings for local work, tests, and production; its routes
and ASGI application; ``manage.py``; and a first test. Its name sets the
environment variable prefix (``billingAPI``: ``BILLING_API_``).
"""

import re
from argparse import ArgumentParser
from pathlib import Path
from typing import Final

from domainkit import __version__
from domainkit.conf.env import prefix_for
from domainkit.management.base import BaseCommand, CommandError
from domainkit.management.commands.startdomain import render
from domainkit.utils.formatting import format_python

TEMPLATE_DIR: Final = Path(__file__).resolve().parents[1] / "templates" / "project"
DOTFILE: Final = "dot-"
"""Template files named ``dot-<name>`` become ``.<name>`` (``.env``, ``.gitignore``)."""
_PROJECT_NAME: Final = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?")
_RELEASE: Final = re.compile(r"(\d+)\.(\d+)\.\d+")


def requirement(version: str) -> str:
    """Depend on this release up to the next one that may break it.

    Before 1.0 a minor release may break (``>=0.1.1,<0.2``); after it, a
    major one. A version without release numbers (a source checkout) pins
    nothing.
    """
    extras = "domainkit[postgres,sqlite]"
    match = _RELEASE.fullmatch(version)
    if match is None:
        return extras
    major, minor = int(match[1]), int(match[2])
    upper = f"0.{minor + 1}" if major == 0 else f"{major + 1}"
    return f"{extras}>={version},<{upper}"


class Command(BaseCommand):
    """Generate a project directory that passes ``check`` and its tests."""

    help = "Create a project: settings, routes, manage.py, and a first test."
    requires_setup = False

    def add_arguments(self, parser: ArgumentParser) -> None:
        """Accept the project name and an optional directory."""
        parser.add_argument(
            "name",
            help="project name, as in [project].name; it sets the environment "
            "variable prefix (billingAPI: BILLING_API_)",
        )
        parser.add_argument(
            "directory",
            nargs="?",
            type=Path,
            help="where to create it (default: ./<name>); empty or missing",
        )

    def handle(self, *, name: str, directory: Path | None, **options: object) -> None:
        """Render the template, then print the next steps."""
        if not _PROJECT_NAME.fullmatch(name):
            raise CommandError(
                f"{name!r} is not a valid project name: use letters, digits, '.', "
                "'-', and '_', starting and ending with a letter or digit."
            )
        prefix = prefix_for(name)
        if not prefix[0].isalpha():
            raise CommandError(
                f"{name!r} must start with a letter: it prefixes the project's "
                f"environment variables ({prefix}...)."
            )
        target = (directory or Path(name)).resolve()
        if target.exists() and (not target.is_dir() or any(target.iterdir())):
            raise CommandError(f"{target} already exists and is not empty.")
        context = {
            "project": name,
            "prefix": prefix,
            "dependency": requirement(__version__),
        }
        for path in render(TEMPLATE_DIR, target, context):
            if path.name.startswith(DOTFILE):
                path.rename(path.with_name(f".{path.name.removeprefix(DOTFILE)}"))
        format_python([target])
        self.write(_next_steps(name, target))


def _next_steps(name: str, target: Path) -> str:
    """Explain how to install and run the new project."""
    return "\n".join(
        [
            f"Created {name} in {target}.",
            "Next steps:",
            f"  cd {target.relative_to(Path.cwd(), walk_up=True)}",
            '  pip install "domainkit[postgres,sqlite,testing]"   # or: uv sync',
            "  python manage.py check",
            '  python manage.py makemigrations -m "auth"',
            "  python manage.py migrate",
            "  python manage.py createsuperuser",
            "  python manage.py startdomain <name>",
            "  pytest",
        ]
    )
