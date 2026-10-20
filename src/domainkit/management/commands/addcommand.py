"""``addcommand``: add a task command to an aggregate of an existing domain.

``manage.py addcommand catalog Product PublishProduct --field at:datetime``
writes, through every layer:

- ``Product.publish(*, at)``, which records ``ProductPublished``; the rule
  it enforces and the state it changes are yours to write;
- the command ``PublishProduct`` and its use case ``publish_product``;
- ``POST /products/{product_id}/publish`` on the product's resource, with
  a ``PublishProductIn`` body; ``If-Match`` makes it conditional;
- ``test_publish_product.py``, which starts from ``new_product()``.

The command's name is a verb and the aggregate's name; its event is the
aggregate's name and the verb in the past tense (``--event`` names
another).
"""

from argparse import ArgumentParser
from pathlib import Path

from domainkit.management.base import BaseCommand, CommandError
from domainkit.management.generators import (
    Domain,
    FieldSpec,
    Names,
    class_name,
    field_help,
    parse_fields,
    words,
)
from domainkit.utils.text import snake_case


class Command(BaseCommand):
    """Add a command, its use case, its route, and tests to an aggregate."""

    help = "Add a task command to an aggregate: the method, use case, route, tests."
    requires_setup = False

    def add_arguments(self, parser: ArgumentParser) -> None:
        """Accept the domain, the aggregate, the command, and its fields."""
        Domain.add_arguments(parser)
        parser.add_argument("command", help="the command's class, e.g. PublishProduct")
        parser.add_argument(
            "--field", action="append", default=[], dest="fields", help=field_help()
        )
        parser.add_argument(
            "--event", help="the event's class (default: e.g. ProductPublished)"
        )

    def handle(
        self,
        *,
        domain: str,
        aggregate: str,
        command: str,
        fields: list[str],
        event: str | None,
        plural: str | None,
        parent: str,
        tests_dir: Path | None,
        **options: object,
    ) -> None:
        """Plan every edit, then write them all."""
        target = Domain.named(domain, parent, tests_dir)
        names = Names.of(aggregate, plural)
        verb = verb_of(class_name(command, "command name"), names.cls)
        happened = class_name(event or f"{names.cls}{past_tense(verb)}", "event name")
        add_command(target, names, command, happened, parse_fields(fields))
        written = target.apply()
        self.write(f"Added {command} to {target.package}:")
        self.write("\n".join(f"  {path}" for path in written))


def verb_of(command: str, aggregate: str) -> str:
    """``Publish`` of ``PublishProduct``.

    Raises:
        CommandError: The command's name is not a verb and the aggregate's.
    """
    verb = command.removesuffix(aggregate)
    if verb in {command, ""}:
        raise CommandError(
            f"Name the command for what it does to the {aggregate}: "
            f"<Verb>{aggregate}, such as Publish{aggregate}."
        )
    return verb


def past_tense(verb: str) -> str:
    """``Published`` of ``Publish``; ``--event`` names those it gets wrong."""
    if verb.endswith("e"):
        return f"{verb}d"
    if verb.endswith("y") and verb[-2:-1] not in set("aeiou"):
        return f"{verb[:-1]}ied"
    return f"{verb}ed"


def add_command(
    domain: Domain, names: Names, command: str, event: str, specs: list[FieldSpec]
) -> None:
    """Plan the edits that add ``command`` (recording ``event``) to an aggregate.

    Raises:
        CommandError: A name it would define is taken, or a file is not as
            the generators made it.
    """
    A, a, many = names.cls, names.snake, names.plural
    method = snake_case(verb_of(command, A))
    service = snake_case(command)
    model = domain.edit("domain/model.py")
    events = domain.edit("domain/events.py")
    commands = domain.edit("application/commands.py")
    services = domain.edit("application/services.py")
    routes = domain.edit("api/routes.py")
    if not model.defines(A):
        raise CommandError(f"{domain.package} has no aggregate {A}.")
    if not routes.defines(many):
        raise CommandError(f"{routes.path} has no resource {many}; pass --plural.")
    for source, name in (
        (model, f"{A}.{method}"),
        (commands, command),
        (events, event),
        (services, service),
        (routes, f"{command}In"),
    ):
        if source.defines(name):
            raise CommandError(f"{source.path} already defines {name}.")
    for spec in specs:
        if spec.name == f"{a}_id":
            raise CommandError(f"{spec.name!r} comes from the path; leave it out.")
        for module, name in spec.plain_imports():
            for source in (model, events, commands):
                source.import_names(module, name)
        if spec.kind == "ref":
            target = spec.options[0]
            if not model.defines(f"{target}Id"):
                raise CommandError(
                    f"{target} is not an aggregate of {domain.package}; add it first."
                )
            services.import_names(domain.module("domain.model"), f"{target}Id")
    _domain_layer(domain, names, method, event, specs)
    _application_layer(domain, names, command, method, service, specs)
    _api_layer(domain, names, command, method, service, specs)
    _test(domain, names, command, method, service, event, specs)


def _domain_layer(
    domain: Domain, names: Names, method: str, event: str, specs: list[FieldSpec]
) -> None:
    """The aggregate's method, and the event it records."""
    A, a = names.cls, names.snake
    model = domain.edit("domain/model.py")
    events = domain.edit("domain/events.py")
    model.import_names(domain.module("domain.events"), event)
    parameters = "".join(
        f", {spec.name}: {spec.domain_type()}{spec.default()}" for spec in specs
    )
    values = "".join(f", {spec.name}={spec.name}" for spec in specs)
    model.add_to_class(
        A,
        [
            "",
            f"def {method}(self{', *' if specs else ''}{parameters}) -> None:",
            f'    """{words(method).capitalize()} the {names.title}."""',
            f"    self.record({event}({a}_id=self.id{values}))",
        ],
    )
    fields = "".join(f"\n    {spec.name}: {spec.plain()}" for spec in specs)
    events.import_names("uuid", "UUID")
    events.import_names("domainkit.ddd", "DomainEvent")
    rest = event.removeprefix(A)
    what = (
        f"A {names.title} was {words(rest)}" if rest and rest != event else words(event)
    )
    events.append(
        f'''
class {event}(DomainEvent):
    """{what[0].upper()}{what[1:]}."""

    {a}_id: UUID{fields}
'''
    )


def _application_layer(
    domain: Domain,
    names: Names,
    command: str,
    method: str,
    service: str,
    specs: list[FieldSpec],
) -> None:
    """The command, and its use case."""
    A, a, many = names.cls, names.snake, names.plural
    commands = domain.edit("application/commands.py")
    services = domain.edit("application/services.py")
    unit = f"{domain.camel}UnitOfWork"
    fields = "".join(
        f"\n    {spec.name}: {spec.plain()}{spec.default()}" for spec in specs
    )
    commands.import_names("uuid", "UUID")
    commands.import_names("domainkit.ddd", "Command")
    commands.append(
        f'''
class {command}(Command):
    """{words(method).capitalize()} a {names.title}."""

    {a}_id: UUID{fields}
'''
    )
    services.import_names(domain.module("application.commands"), command)
    services.import_names(domain.module("application.unit_of_work"), unit)
    services.import_names(domain.module("domain.model"), A, f"{A}Id")
    arguments = ", ".join(
        f"{spec.name}={spec.to_domain(f'command.{spec.name}')}" for spec in specs
    )
    services.append(
        f'''
async def {service}(command: {command}, uow: {unit}) -> {A}:
    """{words(method).capitalize()} a {names.title}."""
    async with uow:
        {a} = await uow.{many}.get({A}Id(command.{a}_id))
        {a}.{method}({arguments})
        await uow.commit()
    return {a}
'''
    )


def _api_layer(
    domain: Domain,
    names: Names,
    command: str,
    method: str,
    service: str,
    specs: list[FieldSpec],
) -> None:
    """The route on the aggregate's resource, and its body's schema."""
    many = names.plural
    routes = domain.edit("api/routes.py")
    routes.import_names(domain.module("application"), "services")
    routes.import_names(domain.module("application.commands"), command)
    body = ""
    if specs:
        routes.import_names("pydantic", "BaseModel")
        for spec in specs:
            if spec.type.limits:
                routes.import_names("pydantic", "Field")
            if spec.type.body:
                routes.import_names("pydantic", spec.type.body)
                continue
            for module, name in spec.plain_imports():
                routes.import_names(module, name)
        lines = "".join(f"\n    {spec.name}: {spec.body()}" for spec in specs)
        routes.insert_before(
            f"{many} = ResourceRouter(",
            [
                *f'''class {command}In(BaseModel):
    """What ``{command}`` takes."""
{lines}'''.splitlines(),
                "",
                "",
            ],
        )
        body = f", body={command}In"
    path = method.replace("_", "-")
    routes.insert_before(
        f"router.include_router({many})",
        [
            f"{many}.command_route(",
            f'    {command}, service=services.{service}{body}, path="/{path}"',
            ")",
        ],
    )


def _test(
    domain: Domain,
    names: Names,
    command: str,
    method: str,
    service: str,
    event: str,
    specs: list[FieldSpec],
) -> None:
    """The command's tests: its use case, and its route."""
    a, many = names.snake, names.plural
    fake = f"Fake{domain.camel}UnitOfWork"
    imports = sorted(
        {
            f"from {module} import {name}"
            for spec in specs
            for module, name in spec.type.example_imports
        }
    )
    examples = "".join(f", {spec.name}={spec.example()}" for spec in specs)
    body = (
        ", json={" + ", ".join(f'"{s.name}": {s.request()}' for s in specs) + "}"
        if specs
        else ""
    )
    path = method.replace("_", "-")
    domain.create(
        domain.tests / f"test_{service}.py",
        f'''"""``{command}``: its use case, and its route."""

{chr(10).join(imports)}

import pytest
from domainkit.ddd import DomainEvent, EventBus
from domainkit.testing import TestClient

from {domain.module("application")} import services
from {domain.module("application.commands")} import {command}
from {domain.module("domain.events")} import {event}

from .fakes import {fake}, new_{a}, new_{a}_request

URL = "/api/v1/{domain.name}/{many}"


@pytest.mark.anyio
async def test_{service}_publishes_{snake_case(event)}() -> None:
    """The use case changes the {names.title} and commits; {event} follows."""
    published: list[DomainEvent] = []
    bus = EventBus()

    async def collect(event: {event}) -> None:
        published.append(event)

    bus.subscribe({event}, collect)
    uow = {fake}(bus)
    {a} = new_{a}()
    async with uow:
        await uow.{many}.add({a})
        await uow.commit()

    await services.{service}({command}({a}_id={a}.id{examples}), uow)

    assert uow.commits == 2
    assert [type(each) for each in published] == [{event}]


def test_{service}_route(admin_client: TestClient) -> None:
    """``POST .../{path}`` runs the command on the {names.title} the path names."""
    created = admin_client.post(URL, json=new_{a}_request())
    assert created.status_code == 201, created.json()
    item = created.headers["location"]

    answer = admin_client.post(
        f"{{item}}/{path}"{body}, headers={{"If-Match": created.headers["etag"]}}
    )

    assert answer.status_code == 200, answer.json()
    assert answer.json()["id"] == created.json()["id"]
''',
    )
