"""``init``: create a project from the project template (Django's ``startproject``).

It runs before any project exists, through the ``domainkit`` program the
package installs::

    domainkit init billingAPI            # creates ./billingAPI
    domainkit init billingAPI billing    # or names the directory
    domainkit init billingAPI .          # or the current one, a fresh clone say

The directory may exist and hold other files (``.git``, a ``README``):
``init`` refuses only when a file it would write is already there, and
names those files.

The project gets settings for local work, tests, and production; its routes
and ASGI application; ``manage.py``; a first test; a ``Dockerfile`` for its
image; and a CI workflow (``.github/workflows/ci.yml``) that runs its
checks and tests and builds the image. Its name sets the
environment variable prefix (``billingAPI``: ``BILLING_API_``).

``--profile fintech`` writes the same project with a bank's controls on and
wired (``docs/fintech.md``, ADR 0124): the PCI password preset, second
factors, step-up and transactions authorized per amount and payee, the
audit trail and evidence, required idempotency keys, messaging, encrypted
fields under a key service, the ledger, maker-checker approvals, and
limits, with a sample transfer that goes through all of them and its tests.
"""

import re
from argparse import ArgumentParser
from pathlib import Path
from typing import Final

from domainkit import __version__
from domainkit.conf.env import prefix_for
from domainkit.management.base import BaseCommand, CommandError
from domainkit.management.commands.startdomain import TEMPLATE_SUFFIX, fill, render

TEMPLATE_DIR: Final = Path(__file__).resolve().parents[1] / "templates" / "project"
PROFILES: Final = {"fintech": TEMPLATE_DIR.parent / "profiles" / "fintech"}
"""Each profile's files, written over the project template's (ADR 0124)."""
EXTRAS: Final = {
    None: ("postgres", "redis", "sqlite"),
    "fintech": ("audit", "immudb", "keys", "mfa", "postgres", "redis", "sqlite"),
}
"""The extras a project of each profile depends on: the fintech profile's
fields are sealed with AES-GCM under a key service reached over HTTP
(``keys``), its users have second factors (``mfa``), its trail is anchored
and its evidence recorded on immudb (``immudb``), and its checkpoints
signed (``audit``)."""
DOTFILE: Final = "dot-"
"""Template files and top directories named ``dot-<name>`` become ``.<name>``
(``.env``, ``.gitignore``, ``.github``)."""
_PROJECT_NAME: Final = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?")
_RELEASE: Final = re.compile(r"(\d+)\.(\d+)\.\d+")


def requirement(version: str, extras: tuple[str, ...] = EXTRAS[None]) -> str:
    """Depend on this release up to the next one that may break it.

    Before 1.0 a minor release may break (``>=0.1.1,<0.2``); after it, a
    major one. A version without release numbers (a source checkout) pins
    nothing. The production settings count in Redis: ``redis`` is an extra,
    as are ``extras``.
    """
    extras_named = f"domainkit[{','.join(extras)}]"
    match = _RELEASE.fullmatch(version)
    if match is None:
        return extras_named
    major, minor = int(match[1]), int(match[2])
    upper = f"0.{minor + 1}" if major == 0 else f"{major + 1}"
    return f"{extras_named}>={version},<{upper}"


def namespace_for(name: str) -> str:
    """The project's name as an audit and evidence namespace on a shared ledger.

    Lower-case letters, digits, ``_``, and ``-``, starting with a letter,
    at most 32 characters (``AUDIT.NAMESPACE``): ``billingAPI`` is
    ``billingapi``, ``ledger.payments`` is ``ledger-payments``.
    """
    return re.sub(r"[^a-z0-9_-]", "-", name.lower())[:32]


class Command(BaseCommand):
    """Generate a project directory that passes ``check`` and its tests."""

    help = (
        "Create a project: settings, routes, manage.py, a first test, a "
        "Dockerfile, and a CI workflow."
    )
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
            help="where to create it (default: ./<name>; '.' for the current "
            "one); it may hold other files, but none init writes",
        )
        parser.add_argument(
            "--profile",
            choices=sorted(PROFILES),
            help="a project with a sector's controls on and wired: fintech, "
            "a bank's first-review list (docs/fintech.md)",
        )

    def handle(
        self,
        *,
        name: str,
        directory: Path | None,
        profile: str | None = None,
        **options: object,
    ) -> None:
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
        if target.exists() and not target.is_dir():
            raise CommandError(f"{target} is a file, not a directory.")
        context = {
            "project": name,
            # Docker refuses capitals in an image's name.
            "image": name.lower(),
            "prefix": prefix,
            "namespace": namespace_for(name),
            "dependency": requirement(__version__, EXTRAS[profile]),
        }
        sources = [TEMPLATE_DIR, *([PROFILES[profile]] if profile else [])]
        planned = {
            path for source in sources for path in _planned(source, target, context)
        }
        if taken := sorted(path for path in planned if path.exists()):
            names = ", ".join(str(path.relative_to(target)) for path in taken[:5])
            more = f", and {len(taken) - 5} more" if len(taken) > 5 else ""
            raise CommandError(
                f"{target} already has files init would write: {names}{more}. "
                "Move them, or choose another directory."
            )
        written = {
            path for source in sources for path in render(source, target, context)
        }
        for path in sorted(written):
            dotted = _dotted(path, target)
            if dotted != path:  # Into .github even if the clone has one.
                dotted.parent.mkdir(parents=True, exist_ok=True)
                path.replace(dotted)
        _remove_emptied(written, target)
        # Not formatted: init runs before the project's environment (and its
        # ruff) exists, so the templates are written as its ruff would.
        self.write(_next_steps(name, target, profile))


def _planned(source: Path, target: Path, context: dict[str, str]) -> list[Path]:
    """Where ``render`` writes ``source`` in ``target``, with dot names as kept."""
    planned = []
    for path in sorted(source.rglob("*")):
        if path.is_dir() or "__pycache__" in path.parts:
            continue
        written = target / fill(str(path.relative_to(source)), context)
        if path.name.endswith(TEMPLATE_SUFFIX):
            written = written.with_name(written.name.removesuffix(TEMPLATE_SUFFIX))
        planned.append(_dotted(written, target))
    return planned


def _remove_emptied(written: set[Path], target: Path) -> None:
    """Remove the ``dot-`` directories ``render`` made, now that they are empty.

    Only those it wrote into, and only directories left empty, deepest
    first: a directory of the user's own is never touched.
    """
    made: set[Path] = set()
    for path in written:
        parts = path.relative_to(target).parts[:-1]
        for depth, part in enumerate(parts, 1):
            if part.startswith(DOTFILE):
                made.update(
                    target.joinpath(*parts[:below])
                    for below in range(depth, len(parts) + 1)
                )
    for directory in sorted(made, key=lambda each: len(each.parts), reverse=True):
        if directory.is_dir() and not any(directory.iterdir()):
            directory.rmdir()


def _dotted(path: Path, target: Path) -> Path:
    """``path`` with each ``dot-`` part below ``target`` named with a dot."""
    parts = [
        f".{part.removeprefix(DOTFILE)}" if part.startswith(DOTFILE) else part
        for part in path.relative_to(target).parts
    ]
    return target.joinpath(*parts)


def _next_steps(name: str, target: Path, profile: str | None) -> str:
    """Explain how to install and run the new project."""
    extras = ",".join([*EXTRAS[profile], "testing"])
    first = "fintech" if profile == "fintech" else "auth"
    steps = [f"Created {name} in {target}.", "Next steps:"]
    if target != Path.cwd().resolve():
        steps.append(f"  cd {target.relative_to(Path.cwd(), walk_up=True)}")
    steps += [
        f'  pip install "domainkit[{extras}]"   # or: uv sync, for both',
        "  pip install --group dev   # ruff formats what the generators write",
        "  python manage.py check",
        f'  python manage.py makemigrations -m "{first}"',
        "  python manage.py migrate",
        "  python manage.py createsuperuser",
    ]
    if profile == "fintech":
        steps.append("  # Deploying: the controls and their settings, docs/fintech.md")
    else:
        steps.append("  python manage.py startdomain <name>")
    steps.append("  pytest")
    return "\n".join(steps)
