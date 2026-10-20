"""``startaggregate``: add an aggregate to a domain ``startdomain`` made.

``manage.py startaggregate catalog Category --field title:str:100
--field parent:ref:Category?`` writes, into the existing domain, every
layer of a new aggregate and its tests:

- the aggregate, its id, its not-found error, and its repository port
  (``domain/model.py``), and its created and deleted events;
- the create and delete commands and use cases, and its repository in the
  unit of work;
- its table (``table_for``), mapping, and repository adapter;
- its resource (list, read, create, delete) in ``api/routes.py``;
- an in-memory repository, ``new_<name>()`` (a valid aggregate) and
  ``new_<name>_request()`` (a valid create body) in ``fakes.py``, for the
  tests of this and of later generators; and ``test_<plural>.py``.

Field kinds are those of ``domainkit.management.generators.FIELD_TYPES``
(``--help`` lists them); ``?`` lets a field be None. Every name is checked,
and every file edited in memory, before anything is written.
"""

from argparse import ArgumentParser
from pathlib import Path

from domainkit.management.base import BaseCommand, CommandError
from domainkit.management.generators import (
    Domain,
    FieldSpec,
    Names,
    field_help,
    parse_fields,
)


class Command(BaseCommand):
    """Add an aggregate, and its tests, to an existing domain."""

    help = "Add an aggregate to a domain made by startdomain: every layer, and tests."
    requires_setup = False

    def add_arguments(self, parser: ArgumentParser) -> None:
        """Accept the domain, the aggregate, and its fields."""
        Domain.add_arguments(parser)
        parser.add_argument(
            "--field", action="append", default=[], dest="fields", help=field_help()
        )

    def handle(
        self,
        *,
        domain: str,
        aggregate: str,
        fields: list[str],
        plural: str | None,
        parent: str,
        tests_dir: Path | None,
        **options: object,
    ) -> None:
        """Plan every file, then write them all."""
        target = Domain.named(domain, parent, tests_dir)
        names = Names.of(aggregate, plural)
        add_aggregate(target, names, parse_fields(fields))
        written = target.apply()
        self.write(f"Added {names.cls} to {target.package}:")
        self.write("\n".join(f"  {path}" for path in written))
        self.write(
            f'Next: python manage.py makemigrations -m "create {names.plural}", '
            "then migrate."
        )


def add_aggregate(domain: Domain, names: Names, specs: list[FieldSpec]) -> None:
    """Plan the edits and files that add the aggregate ``names`` to ``domain``.

    Raises:
        CommandError: A name it would define is taken, a reference names an
            aggregate the domain lacks, or a file is not as startdomain made it.
    """
    model = domain.edit("domain/model.py")
    if model.defines(names.cls) or model.defines(f"{names.cls}Id"):
        raise CommandError(f"{domain.package} already has {names.cls}.")
    for spec in specs:
        target = spec.options[0] if spec.kind == "ref" else None
        if target not in {None, names.cls} and not model.defines(f"{target}Id"):
            raise CommandError(
                f"{target} is not an aggregate of {domain.package}; add it first."
            )
    _domain_layer(domain, names, specs)
    _application_layer(domain, names, specs)
    _infrastructure_layer(domain, names)
    _api_layer(domain, names)
    _tests(domain, names, specs)


def _domain_layer(domain: Domain, names: Names, specs: list[FieldSpec]) -> None:
    """The aggregate, its id, error, port, and events."""
    A, a = names.cls, names.snake
    model = domain.edit("domain/model.py")
    events = domain.edit("domain/events.py")
    model.import_names("uuid", "UUID", "uuid4")
    model.import_names("typing", "NewType", "Self")
    model.import_names("domainkit.ddd", "AggregateRoot", "NotFound", "Repository")
    for spec in specs:
        for module, name in spec.domain_imports():
            model.import_names(module, name)
    model.import_names(domain.module("domain.events"), f"{A}Created", f"{A}Deleted")
    attributes = "\n".join(
        f"    {spec.name}: {spec.annotation()}{spec.default()}" for spec in specs
    )
    parameters = "".join(
        f", {spec.name}: {spec.domain_type()}{spec.default()}" for spec in specs
    )
    arguments = "".join(f", {spec.name}={spec.name}" for spec in specs)
    model.append(
        f'''
{A}Id = NewType("{A}Id", UUID)


class {A}NotFound(NotFound):
    """No {names.title} has the requested id."""


class {A}(AggregateRoot[{A}Id]):
    """Consistency boundary for one {names.title}; change it only through methods."""

{attributes}

    @classmethod
    def create(cls{", *" if specs else ""}{parameters}) -> Self:
        """Start a new {names.title} and record that it was created."""
        {a} = cls(id={A}Id(uuid4()){arguments})
        {a}.record({A}Created({a}_id={a}.id))
        return {a}

    def delete(self) -> None:
        """Record that the {names.title} goes; the repository removes it."""
        self.record({A}Deleted({a}_id=self.id))


class {A}Repository(Repository[{A}, {A}Id]):
    """Every {names.title}: ``get``, ``filter``, ``paginate``, ... (a port)."""

    def not_found(self, id_: {A}Id) -> {A}NotFound:
        """Report a missing {names.title}."""
        return {A}NotFound(f"{A} {{id_}} does not exist.")
'''.replace("\n\n\n\n", "\n\n\n")
    )
    events.import_names("uuid", "UUID")
    events.import_names("domainkit.ddd", "DomainEvent")
    events.append(
        f'''
class {A}Created(DomainEvent):
    """A {names.title} was created."""

    {a}_id: UUID


class {A}Deleted(DomainEvent):
    """A {names.title} was deleted."""

    {a}_id: UUID
'''
    )


def _application_layer(domain: Domain, names: Names, specs: list[FieldSpec]) -> None:
    """The commands, the use cases, and the repository in the unit of work."""
    A, a, many = names.cls, names.snake, names.plural
    unit = f"{domain.camel}UnitOfWork"
    commands = domain.edit("application/commands.py")
    services = domain.edit("application/services.py")
    port = domain.edit("application/unit_of_work.py")
    commands.import_names("uuid", "UUID")
    commands.import_names("domainkit.ddd", "Command")
    for spec in specs:
        for module, name in spec.plain_imports():
            commands.import_names(module, name)
    fields = "".join(
        f"\n    {spec.name}: {spec.plain()}{spec.default()}" for spec in specs
    )
    commands.append(
        f'''
class Create{A}(Command):
    """Create a {names.title}."""
{fields}


class Delete{A}(Command):
    """Delete a {names.title}."""

    {a}_id: UUID
'''
    )
    services.import_names(
        domain.module("application.commands"), f"Create{A}", f"Delete{A}"
    )
    services.import_names(domain.module("application.unit_of_work"), unit)
    services.import_names(domain.module("domain.model"), A, f"{A}Id")
    for spec in specs:
        if spec.kind == "ref":
            services.import_names(domain.module("domain.model"), f"{spec.options[0]}Id")
    arguments = ", ".join(
        f"{spec.name}={spec.to_domain(f'command.{spec.name}')}" for spec in specs
    )
    services.append(
        f'''
async def create_{a}(command: Create{A}, uow: {unit}) -> {A}:
    """Create and store a {names.title}."""
    {a} = {A}.create({arguments})
    async with uow:
        await uow.{many}.add({a})
        await uow.commit()
    return {a}


async def delete_{a}(command: Delete{A}, uow: {unit}) -> None:
    """Delete a {names.title}."""
    async with uow:
        {a} = await uow.{many}.get({A}Id(command.{a}_id))
        {a}.delete()
        await uow.{many}.remove({a})
        await uow.commit()
'''
    )
    port.import_names(domain.module("domain.model"), f"{A}Repository")
    port.add_attribute(unit, f"{many}: {A}Repository")


def _infrastructure_layer(domain: Domain, names: Names) -> None:
    """The table, its mapping, and the repository adapter."""
    A, many = names.cls, names.plural
    orm = domain.edit("infrastructure/orm.py")
    sql = domain.edit("infrastructure/unit_of_work.py")
    orm.import_names("domainkit.db", "map_imperatively", "table_for")
    orm.import_names(domain.module("domain.model"), A)
    orm.append(
        f'{many} = table_for({A}, "{domain.name}_{many}", domain="{domain.name}")'
    )
    orm.add_to_function("start_mappers", [f"map_imperatively({A}, {many})"])
    sql.import_names("domainkit.db", "SqlAlchemyRepository")
    sql.import_names(domain.module("domain.model"), A, f"{A}Id", f"{A}Repository")
    sql.append(
        f'''
class SqlAlchemy{A}Repository(SqlAlchemyRepository[{A}, {A}Id], {A}Repository):
    """{A} storage in the ``{domain.name}_{many}`` table."""

    model = {A}
'''
    )
    sql.add_to_function(
        f"SqlAlchemy{domain.camel}UnitOfWork.create_repositories",
        [f"self.{many} = SqlAlchemy{A}Repository(session)"],
    )


def _api_layer(domain: Domain, names: Names) -> None:
    """The resource: list, read, create, and delete."""
    A, a, many = names.cls, names.snake, names.plural
    routes = domain.edit("api/routes.py")
    for needed in ("router", "get_unit_of_work"):
        if not routes.defines(needed):
            raise CommandError(f"{routes.path} has no {needed}; was it renamed?")
    if routes.defines(many) or routes.defines(f"{A}In"):
        raise CommandError(f"{routes.path} already defines {many} or {A}In.")
    routes.import_names(
        "domainkit.resources", "ModelActions", "ResourceRouter", "schema_for"
    )
    routes.import_names(domain.module("application"), "services")
    routes.import_names(
        domain.module("application.commands"), f"Create{A}", f"Delete{A}"
    )
    routes.import_names(domain.module("domain.model"), A)
    routes.append(
        f"""
{A}In = schema_for({A}, write=True)
{many} = ResourceRouter(
    "/{many}",
    model={A},
    unit_of_work=get_unit_of_work,
    repository=lambda uow: uow.{many},
    rule=ModelActions({A}),
)
{many}.command_route(Create{A}, service=services.create_{a}, body={A}In)
{many}.command_route(Delete{A}, service=services.delete_{a}, method="DELETE")
router.include_router({many})
"""
    )


def _tests(domain: Domain, names: Names, specs: list[FieldSpec]) -> None:
    """The in-memory repository, the examples, and the aggregate's tests."""
    A, a, many = names.cls, names.snake, names.plural
    fakes = domain.edit("fakes.py", tests=True)
    fake = f"Fake{domain.camel}UnitOfWork"
    fakes.import_names("domainkit.ddd", "InMemoryRepository")
    fakes.import_names(domain.module("domain.model"), A, f"{A}Id", f"{A}Repository")
    fakes.insert_before_class(
        fake,
        f'''
class InMemory{A}Repository(InMemoryRepository[{A}, {A}Id], {A}Repository):
    """Dictionary-backed ``{A}Repository``; it answers queries as SQL does."""
''',
    )
    fakes.add_attribute(fake, f"{many}: InMemory{A}Repository")
    fakes.add_to_function(
        f"{fake}.__init__", [f"self.{many} = InMemory{A}Repository()"]
    )
    for spec in specs:
        for module, name in spec.type.example_imports:
            fakes.import_names(module, name)
        if spec.kind == "ref":
            fakes.import_names(domain.module("domain.model"), f"{spec.options[0]}Id")
    domain_values = ", ".join(f"{spec.name}={spec.domain_example()}" for spec in specs)
    body = ", ".join(f'"{spec.name}": {spec.request()}' for spec in specs)
    fakes.append(
        f'''
def new_{a}() -> {A}:
    """A valid {names.title}, its creation event taken: tests start from it."""
    {a} = {A}.create({domain_values})
    {a}.pull_events()
    return {a}


def new_{a}_request() -> dict[str, object]:
    """A valid body of ``POST /{many}``."""
    return {{{body}}}
'''
    )
    imports = sorted(
        {
            f"from {module} import {name}"
            for spec in specs
            for module, name in spec.type.example_imports
        }
        | {
            f"from {domain.module('domain.model')} import {spec.options[0]}Id"
            for spec in specs
            if spec.kind == "ref"
        }
    )
    command_values = ", ".join(f"{spec.name}={spec.example()}" for spec in specs)
    domain.create(
        domain.tests / f"test_{many}.py",
        f'''"""The {A} aggregate: its model, its use cases, and its resource."""

{chr(10).join(imports)}

import pytest

from domainkit.testing import TestClient
from {domain.module("application")} import services
from {domain.module("application.commands")} import Create{A}, Delete{A}
from {domain.module("domain.events")} import {A}Created, {A}Deleted
from {domain.module("domain.model")} import {A}, {A}NotFound

from .fakes import {fake}, new_{a}_request

URL = "/api/v1/{domain.name}/{many}"


def test_create_and_delete_record_events() -> None:
    """A new {names.title} records {A}Created; deleting it, {A}Deleted."""
    {a} = {A}.create({domain_values})
    {a}.delete()

    created, deleted = {a}.pull_events()
    assert isinstance(created, {A}Created)
    assert isinstance(deleted, {A}Deleted)
    assert created.{a}_id == deleted.{a}_id == {a}.id


@pytest.mark.anyio
async def test_use_cases_store_and_remove() -> None:
    """Creating stores and commits; deleting removes; a missing one is not found."""
    uow = {fake}()

    {a} = await services.create_{a}(Create{A}({command_values}), uow)
    assert uow.{many}.items == {{ {a}.id: {a} }}
    await services.delete_{a}(Delete{A}({a}_id={a}.id), uow)

    assert uow.{many}.items == {{}}
    with pytest.raises({A}NotFound):
        await services.delete_{a}(Delete{A}({a}_id={a}.id), uow)


def test_resource(admin_client: TestClient) -> None:
    """Create, read, list, and delete over HTTP."""
    created = admin_client.post(URL, json=new_{a}_request())
    assert created.status_code == 201, created.json()
    item = created.headers["location"]

    assert admin_client.get(item).json() == created.json()
    assert [each["id"] for each in admin_client.get(URL).json()["items"]] == [
        created.json()["id"]
    ]
    assert admin_client.delete(item).status_code == 204
    assert admin_client.get(item).status_code == 404
''',
    )
