"""``startdomain``: create a bounded context from the domain template.

Like Django's ``startapp``. By default the domain is four files, one per
layer: ``domain.py`` (the aggregate, its rules, and its ports),
``controllers.py`` (commands and use cases), ``models.py`` (the table and the
repository), and ``routes.py`` (the HTTP API), with a working aggregate and
its tests. ``--layered`` makes a package per layer instead (``domain``,
``application``, ``infrastructure``, ``api``), with a child entity, a
reference to another aggregate, and events, which ``addcommand``,
``addquery``, and ``addevent`` grow; ``--minimal`` makes a smaller layered
one. In a project laid out as ``init`` makes it, the domain is also
installed and its router mounted.
"""

import keyword
import re
from argparse import ArgumentParser
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from domainkit.conf.env import project_root
from domainkit.management.base import CommandError
from domainkit.management.generators import (
    Generator,
    article,
    install,
    install_user_model,
    package_directory,
)
from domainkit.utils.formatting import format_python
from domainkit.utils.text import camel_case, plural_of, singular_of, snake_case

TEMPLATE_DIR: Final = Path(__file__).resolve().parents[1] / "templates" / "domain"
MINIMAL_DIR: Final = TEMPLATE_DIR / "minimal"
"""The smaller domain of ``--layered --minimal``: its ``package`` and ``tests``."""
USER_DIR: Final = TEMPLATE_DIR / "user"
"""``--user``: the project's own user model and its table."""
SIMPLE_DIR: Final = TEMPLATE_DIR / "simple"
"""The default domain: four files, one per layer (``domain.py`` to ``routes.py``)."""
TEMPLATE_SUFFIX: Final = "-tpl"
_PLACEHOLDER: Final = re.compile(r"\{\{ (\w+) \}\}")
_SNAKE_NAME: Final = re.compile(r"[a-z][a-z0-9_]*")
_CLASS_NAME: Final = re.compile(r"[A-Z][A-Za-z0-9]*")
SERVER: Final = "http://127.0.0.1:8000"
"""Where ``runserver`` listens unless told otherwise."""
PROMPT_LINE: Final = 'read -rsp "Password: " PASSWORD && export PASSWORD'
"""Ask for the password, so it is in no command line and no shell history."""
LOGIN_CURL: Final = (
    "TOKEN=$(python -c \"import json, os; print(json.dumps({'username': "
    "'<username>', 'password': os.environ['PASSWORD']}))\" "
    f"| curl -s {SERVER}/auth/login "
    '-H "Content-Type: application/json" -d @- '
    "| python -c \"import json, sys; print(json.load(sys.stdin)['access_token'])\")"
)
"""Log in at the project template's ``/auth/login`` with the password typed at
``PROMPT_LINE`` (sent on stdin, never in an argument); keep the token in
``$TOKEN``."""
UNSET_LINE: Final = "unset PASSWORD"
"""Forget the password once logged in, so that no later process inherits it."""


def template_context(
    name: str, parent: str, aggregate: str | None, plural: str | None
) -> dict[str, str]:
    """Return the placeholder values for domain ``name``; validates the names."""
    if not _SNAKE_NAME.fullmatch(name) or keyword.iskeyword(name):
        raise CommandError(f"{name!r} is not a valid snake_case package name.")
    aggregate = aggregate or camel_case(singular_of(name))
    if not _CLASS_NAME.fullmatch(aggregate):
        raise CommandError(f"{aggregate!r} is not a valid CamelCase class name.")
    aggregate_snake = snake_case(aggregate)
    aggregates = plural or plural_of(aggregate_snake)
    if plural is None and aggregates == aggregate_snake:
        raise CommandError(
            f"{aggregate} reads as a plural: name the aggregate in the singular."
        )
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
        "a_aggregate": f"{article(aggregate_snake)} {aggregate_snake}",
        "A_aggregate": f"{article(aggregate_snake).title()} {aggregate_snake}",
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


@dataclass(frozen=True, slots=True)
class NewDomain:
    """What ``startdomain`` will write: the package, its tests, and where."""

    context: dict[str, str]
    package_dir: Path
    tests_root: Path
    root: Path
    install: bool
    minimal: bool = False
    layered: bool = False
    user: bool = False


class Command(Generator[NewDomain]):
    """Generate ``<parent>/<name>/`` and ``tests/<parent>/<name>/``."""

    help = "Create a domain package (and its tests) from the domain template."

    def add_arguments(self, parser: ArgumentParser) -> None:
        """Accept the domain name and naming overrides."""
        parser.add_argument("name", help="snake_case package name, e.g. billing")
        parser.add_argument(
            "--aggregate",
            help=(
                "aggregate root class name (default: the CamelCase domain name, "
                "in the singular)"
            ),
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
        parser.add_argument(
            "--manual",
            action="store_true",
            help="neither install the domain nor mount its router; print how",
        )
        parser.add_argument(
            "--layered",
            action="store_true",
            help="a package per layer (domain/, application/, infrastructure/, "
            "api/), with notes, an owner, and events; addcommand, addquery, and "
            "addevent grow it. Without it: four files, one per layer",
        )
        parser.add_argument(
            "--user",
            action="store_true",
            help="the project's own user model (default User) and its table, "
            "named in AUTH; before the first migrate",
        )
        parser.add_argument(
            "--minimal",
            action="store_true",
            help="with --layered: a smaller layered domain (an aggregate with a "
            "name, create and rename, a table, a resource, two tests)",
        )

    def plan(
        self,
        *,
        name: str,
        aggregate: str | None,
        plural: str | None,
        parent: str,
        tests_dir: Path | None,
        manual: bool = False,
        minimal: bool = False,
        layered: bool = False,
        user: bool = False,
        **options: object,
    ) -> NewDomain:
        """Check the names, and that neither directory exists yet.

        ``--minimal`` is a layered domain, so it implies ``--layered``.
        """
        if user and (layered or minimal):
            raise CommandError("--user makes a user domain; leave out --layered.")
        if user:
            aggregate = aggregate or "User"
        context = template_context(name, parent, aggregate, plural)
        package_dir = package_directory(parent) / name
        root = project_root()
        tests_root = tests_dir or root / "tests" / Path(*parent.split("."))
        for directory in (package_dir, tests_root / name):
            if directory.exists():
                raise CommandError(f"{directory} already exists.")
        return NewDomain(
            context,
            package_dir,
            tests_root,
            root,
            not manual,
            minimal,
            layered or minimal,
            user,
        )

    def apply(self, plan: NewDomain) -> str:
        """Render the package and its tests, install it if safe; what is left."""
        context, package_dir, tests_root, root = (
            plan.context,
            plan.package_dir,
            plan.tests_root,
            plan.root,
        )
        name = context["domain"]
        if plan.user:
            template = USER_DIR
        elif not plan.layered:
            template = SIMPLE_DIR
        else:
            template = MINIMAL_DIR if plan.minimal else TEMPLATE_DIR
        render(template / "package", package_dir, context)
        render(template / "tests", tests_root / name, context)
        directory = tests_root
        while directory != root and directory.is_relative_to(root):
            init = directory / "__init__.py"
            if not init.exists():
                init.write_text(f'"""Tests of {directory.name}."""\n', encoding="utf-8")
            directory = directory.parent
        format_python([package_dir, tests_root / name])
        if plan.user:
            return _user_steps(context, package_dir, tests_root / name, plan.install)
        routes = "api.routes" if plan.layered else "routes"
        done = (
            install(context["package"], name, root, routes=routes)
            if plan.install
            else []
        )
        return _next_steps(
            context,
            package_dir,
            tests_root / name,
            done,
            minimal=plan.minimal or not plan.layered,
            routes=routes,
        )


def _user_steps(
    context: dict[str, str], package_dir: Path, tests_dir: Path, install_it: bool
) -> str:
    """Install the user domain if safe; say what was done and what is left."""
    package, model, table = (
        context["package"],
        context["Aggregate"],
        context["aggregates"],
    )
    done = (
        install_user_model(package, model, table, project_root()) if install_it else []
    )
    steps = (
        []
        if done
        else [
            f"Add {package!r} to INSTALLED_DOMAINS in your settings.",
            (
                "Name the model and its table in your base settings:\n"
                f'       AUTH = {{"USER_MODEL": "{package}.models.{model}", '
                f'"USER_TABLE": "{package}.models.{table}"}}'
            ),
        ]
    )
    steps += [
        (
            "Before the first migrate (the users' table is this one now):\n"
            '       python manage.py makemigrations -m "users"\n'
            "       python manage.py migrate\n"
            "       python manage.py createsuperuser"
        ),
        (
            f"Add fields to {model} in {package_dir / 'models.py'}: on the class "
            "and as columns."
        ),
    ]
    return "\n".join(
        [
            f"Created {package_dir} and {tests_dir}.",
            *done,
            "Next steps:",
            *(f"  {number}. {step}" for number, step in enumerate(steps, 1)),
        ]
    )


def _next_steps(
    context: dict[str, str],
    package_dir: Path,
    tests_dir: Path,
    done: list[str],
    *,
    minimal: bool = False,
    routes: str = "api.routes",
) -> str:
    """Say what was done, and explain what is left to do."""
    package, domain = context["package"], context["domain"]
    steps = (
        []
        if done
        else [
            f"Add {package!r} to INSTALLED_DOMAINS in your settings.",
            (
                "Mount its router in your ROOT_ROUTER module:\n"
                f"       from {package}.{routes} import router as {domain}_router\n"
                f"       router.include_router({domain}_router, "
                f'prefix="/api/v1/{domain}")'
            ),
        ]
    )
    steps += [
        f'python manage.py makemigrations -m "create {domain}"',
        "python manage.py migrate",
        (
            f"Grant {domain}.view_{context['model']} (and add_, change_"
            f"{'' if minimal else ', delete_'}) to the users or groups that need "
            "them; superusers have them."
        ),
        (
            "python manage.py runserver; then, from another shell, log in as a "
            "superuser, typing its password at the prompt, and list the "
            f"{context['aggregates_title']}:\n"
            f"       {PROMPT_LINE}\n"
            f"       {LOGIN_CURL}\n"
            f"       {UNSET_LINE}\n"
            f"       curl {SERVER}/api/v1/{domain}/{context['aggregates']} "
            '-H "Authorization: Bearer $TOKEN"'
        ),
    ]
    return "\n".join(
        [
            f"Created {package_dir} and {tests_dir}.",
            *done,
            "Next steps:",
            *(f"  {number}. {step}" for number, step in enumerate(steps, 1)),
        ]
    )
