"""``addquery``: add a query on an aggregate of an existing domain.

``manage.py addquery catalog Product ProductsByOwner --by owner_id`` writes:

- the query ``ProductsByOwner`` and its handler ``products_by_owner``, which
  finds the first products whose ``owner_id`` is the query's, in
  ``application/queries.py``;
- ``GET /products/by-owner?owner_id=...`` on the product's resource
  (``query_route``);
- ``test_products_by_owner.py``, which starts from ``new_product()``.

Each ``--by`` names a field of the aggregate; the query's field has the
aggregate's type, so a value object stays one.
"""

import ast
import builtins
import re
from argparse import ArgumentParser
from pathlib import Path
from typing import Final

from domainkit.management.base import BaseCommand, CommandError
from domainkit.management.generators import (
    Domain,
    Names,
    Source,
    class_name,
    enumeration,
    snake_name,
    words,
)
from domainkit.utils.text import snake_case

LIMIT: Final = 100
"""How many aggregates a generated handler returns at most."""
PATH: Final = re.compile(r"(/[a-z0-9][a-z0-9-]*)+")
CONTAINERS: Final = frozenset({"list", "set", "frozenset", "dict", "tuple"})


class Command(BaseCommand):
    """Add a query, its handler, its route, and tests to an aggregate."""

    help = "Add a query on an aggregate: the message, handler, route, and tests."
    requires_setup = False

    def add_arguments(self, parser: ArgumentParser) -> None:
        """Accept the domain, the aggregate, the query, and what it filters by."""
        Domain.add_arguments(parser)
        parser.add_argument("query", help="the query's class, e.g. ProductsByOwner")
        parser.add_argument(
            "--by",
            action="append",
            default=[],
            dest="by",
            help="a field of the aggregate it must equal, repeated",
        )
        parser.add_argument("--path", help="the route's path (default: e.g. /by-owner)")

    def handle(
        self,
        *,
        domain: str,
        aggregate: str,
        query: str,
        by: list[str],
        path: str | None,
        plural: str | None,
        parent: str,
        tests_dir: Path | None,
        **options: object,
    ) -> None:
        """Plan every edit, then write them all."""
        target = Domain.named(domain, parent, tests_dir)
        names = Names.of(aggregate, plural)
        class_name(query, "query name")
        fields = [snake_name(name, "field name") for name in by]
        if len(set(fields)) != len(fields):
            raise CommandError("Name each field once.")
        route = path or default_path(query, names)
        if not PATH.fullmatch(route):
            raise CommandError(
                f"{route!r} is not a path of lowercase words: /by-owner."
            )
        add_query(target, names, query, fields, route)
        written = target.apply()
        self.write(f"Added {query} to {target.package}:")
        self.write("\n".join(f"  {path}" for path in written))


def default_path(query: str, names: Names) -> str:
    """``/by-owner`` for ``ProductsByOwner``: the query's name, the plural left out."""
    snake = snake_case(query)
    rest = snake.removeprefix(f"{names.plural}_")
    return "/" + (rest or snake).replace("_", "-")


def add_query(
    domain: Domain, names: Names, query: str, fields: list[str], path: str
) -> None:
    """Plan the edits that add ``query`` on an aggregate, filtering ``fields``.

    Raises:
        CommandError: A name it would define is taken, a field is not one
            the query can take, or a file is not as the generators made it.
    """
    A, many = names.cls, names.plural
    handler = snake_case(query)
    model = domain.edit("domain/model.py")
    routes = domain.edit("api/routes.py")
    if not model.defines(A):
        raise CommandError(f"{domain.package} has no aggregate {A}.")
    if not routes.defines(many):
        raise CommandError(f"{routes.path} has no resource {many}; pass --plural.")
    queries = _queries(domain, names)
    for name in (query, handler):
        if queries.defines(name):
            raise CommandError(f"{queries.path} already defines {name}.")
    annotations = {name: _annotation(model, A, name) for name in fields}
    for annotation in annotations.values():
        for name in sorted(_names(annotation)):
            _import(queries, model, domain, name)
    declared = "".join(
        f"\n    {name}: {ast.unparse(hint)}" for name, hint in annotations.items()
    )
    conditions = [f'F("{name}") == query.{name}' for name in fields]
    criteria = (
        " & ".join(f"({each})" for each in conditions)
        if len(conditions) > 1
        else (conditions or ["None"])[0]
    )
    which = f" with the query's {enumeration(fields)}" if fields else ""
    queries.append(
        f'''
class {query}(Query):
    """{words(query).capitalize()}."""
{declared}


async def {handler}(query: {query}, uow: {domain.camel}UnitOfWork) -> list[{A}]:
    """The first {LIMIT} {words(many)}{which}."""
    async with uow:
        page = await uow.{many}.paginate({criteria}, limit={LIMIT})
    return page.items
'''
    )
    routes.import_names(domain.module("application"), "queries")
    routes.import_names(domain.module("application.queries"), query)
    routes.insert_before(
        f"router.include_router({many})",
        [
            f"{many}.query_route(",
            f'    {query}, handler=queries.{handler}, path="{path}"',
            ")",
        ],
    )
    _test(domain, names, query, handler, fields, path)


def _queries(domain: Domain, names: Names) -> Source:
    """``application/queries.py``, made if it is missing."""
    path = domain.directory / "application" / "queries.py"
    if path.exists():
        queries = domain.edit("application/queries.py")
    else:
        queries = domain.start(
            path,
            f'''"""Queries of the {domain.name} domain: questions that change nothing.

Each query is a message, answered by the handler of its name in snake case;
``api/routes.py`` serves them (``query_route``).
"""
''',
        )
    queries.import_names("domainkit.ddd", "Query")
    queries.import_names("domainkit.ddd.query", "F")
    queries.import_names(
        domain.module("application.unit_of_work"), f"{domain.camel}UnitOfWork"
    )
    queries.import_names(domain.module("domain.model"), names.cls)
    return queries


def _annotation(model: Source, aggregate: str, name: str) -> ast.expr:
    """The aggregate's annotation of field ``name``, if a query can take it.

    Raises:
        CommandError: There is no such field, or it holds several values or
            may be None, which equality cannot ask about.
    """
    hint = model.annotation(aggregate, name)
    if hint is None:
        raise CommandError(f"{aggregate} declares no field {name!r}.")
    used = _names(hint)
    if used & CONTAINERS or "Optional" in used or _may_be_none(hint):
        raise CommandError(
            f"{aggregate}.{name} is {ast.unparse(hint)}: a query filters fields "
            "of one value; write the handler for others yourself."
        )
    return hint


def _may_be_none(hint: ast.expr) -> bool:
    """Whether an annotation lets the field be None."""
    return any(
        isinstance(node, ast.Constant) and node.value is None for node in ast.walk(hint)
    )


def _names(hint: ast.expr) -> set[str]:
    """Every name an annotation uses."""
    return {node.id for node in ast.walk(hint) if isinstance(node, ast.Name)}


def _import(queries: Source, model: Source, domain: Domain, name: str) -> None:
    """Import ``name`` as the domain's model has it.

    Raises:
        CommandError: The model neither defines nor imports it.
    """
    if hasattr(builtins, name):
        return
    if model.defines(name) and not model.origin(name):
        queries.import_names(domain.module("domain.model"), name)
        return
    origin = model.origin(name)
    if origin is None:
        raise CommandError(f"{model.path} does not say where {name} comes from.")
    queries.import_names(origin, name)


def _test(
    domain: Domain,
    names: Names,
    query: str,
    handler: str,
    fields: list[str],
    path: str,
) -> None:
    """The query's tests: its handler, and its route."""
    a, many = names.snake, names.plural
    fake = f"Fake{domain.camel}UnitOfWork"
    arguments = ", ".join(f"{name}={a}.{name}" for name in fields)
    params = (
        ", params={" + ", ".join(f'"{name}": body["{name}"]' for name in fields) + "}"
        if fields
        else ""
    )
    domain.create(
        domain.tests / f"test_{handler}.py",
        f'''"""``{query}``: its handler, and its route."""

import pytest
from domainkit.testing import TestClient

from {domain.module("application")} import queries
from {domain.module("application.queries")} import {query}

from .fakes import {fake}, new_{a}, new_{a}_request

URL = "/api/v1/{domain.name}/{many}"


@pytest.mark.anyio
async def test_{handler}_finds_what_it_asks_for() -> None:
    """The {names.title} that matches the query is found."""
    uow = {fake}()
    {a} = new_{a}()
    async with uow:
        await uow.{many}.add({a})
        await uow.commit()

    found = await queries.{handler}({query}({arguments}), uow)

    assert found == [{a}]


def test_{handler}_route(admin_client: TestClient) -> None:
    """``GET {path}`` answers the query's {words(many)}."""
    created = admin_client.post(URL, json=new_{a}_request())
    assert created.status_code == 201, created.json()
    body = created.json()

    answer = admin_client.get(f"{{URL}}{path}"{params})

    assert answer.status_code == 200, answer.json()
    assert [each["id"] for each in answer.json()["items"]] == [body["id"]]
''',
    )
