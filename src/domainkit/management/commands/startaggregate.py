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

A money field (``--field balance:money``, or ``balance:money:12:2`` to size
its amount) is moved between aggregates as ``docs/databases.md`` says money
moves: ``Wallet.transfer_balance`` and ``receive_balance``, which refuse an
overdraft and another currency; ``TransferWalletBalance`` and its use case,
which lock both rows in one statement, by ascending id, and run the whole
work again when it loses a race (``uow.run(..., retry=Retry(...))``);
``POST /wallets/{wallet_id}/transfer-balance``; and their tests.
"""

from argparse import ArgumentParser
from decimal import ROUND_DOWN, Decimal

from domainkit.management.base import CommandError
from domainkit.management.generators import (
    AggregateGenerator,
    Domain,
    FieldSpec,
    Names,
    Planned,
    field_help,
    parse_fields,
)
from domainkit.utils.text import camel_case, snake_case


class Command(AggregateGenerator):
    """Add an aggregate, and its tests, to an existing domain."""

    help = "Add an aggregate to a domain made by startdomain: every layer, and tests."

    def add_options(self, parser: ArgumentParser) -> None:
        """Accept the aggregate's fields."""
        parser.add_argument(
            "--field", action="append", default=[], dest="fields", help=field_help()
        )

    def plan_for(
        self, domain: Domain, names: Names, *, fields: list[str], **options: object
    ) -> Planned:
        """Plan every layer of the aggregate, and its tests."""
        add_aggregate(domain, names, parse_fields(fields))
        return Planned(
            domain,
            names.cls,
            f'Next: python manage.py makemigrations -m "create {names.plural}", '
            "then migrate.",
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
    for spec in specs:
        if spec.kind == "money" and not spec.nullable:
            _transfers(domain, names, spec)


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


def _transfers(domain: Domain, names: Names, spec: FieldSpec) -> None:
    """Plan how the money field ``spec`` moves between two aggregates, and its tests.

    The use case follows ``docs/databases.md``: both rows locked in one
    statement, by ascending id, and the whole work run again when its
    transaction loses a race.

    Raises:
        CommandError: A name it would define is taken.
    """
    A, a, many, field = names.cls, names.snake, names.plural, spec.name
    F = camel_case(field)
    command, event = f"Transfer{A}{F}", f"{A}{F}Transferred"
    invalid, short = f"Invalid{A}Transfer", f"Insufficient{A}{F}"
    service = f"transfer_{a}_{field}"
    path = f"transfer-{field.replace('_', '-')}"
    what = field.replace("_", " ")
    model = domain.edit("domain/model.py")
    events = domain.edit("domain/events.py")
    commands = domain.edit("application/commands.py")
    services = domain.edit("application/services.py")
    routes = domain.edit("api/routes.py")
    for source, name in (
        (model, short),
        (events, event),
        (commands, command),
        (services, service),
        (routes, f"{command}In"),
    ):
        if source.defines(name):
            raise CommandError(f"{source.path} already defines {name}.")
    model.import_names("domainkit.ddd", "BusinessRuleViolation")
    model.import_names(domain.module("domain.events"), event)
    errors = f'''
class {short}(BusinessRuleViolation):
    """A {names.title} transfers at most the {what} it holds."""
'''
    if not model.defines(invalid):
        errors = f'''
class {invalid}(BusinessRuleViolation):
    """A transfer moves an amount above zero to another {names.title}."""

{errors}'''
    model.insert_before_class(A, errors)
    model.add_to_class(
        A,
        [
            "",
            f"def transfer_{field}(self, *, to: {A}Id, amount: Money) -> None:",
            f'    """Take ``amount`` off the {what}, for the {names.title} ``to``."""',
            "    if to == self.id or amount.amount <= 0:",
            f"        raise {invalid}(",
            (
                '            "A transfer moves an amount above zero to another '
                f'{names.title}."'
            ),
            "        )",
            f"    if amount > self.{field}:  # Of another currency, CurrencyMismatch.",
            f'        raise {short}(f"{A} {{self.id}} holds less than {{amount}}.")',
            f"    self.{field} -= amount",
            f"    self.record({event}({a}_id=self.id, to=to, amount=amount))",
            "",
            f"def receive_{field}(self, *, amount: Money) -> None:",
            f'    """Add a transfer\'s ``amount`` to the {what}."""',
            f"    self.{field} += amount",
        ],
    )
    for source in (events, commands):
        source.import_names("uuid", "UUID")
        source.import_names("domainkit.money", "Money")
    events.import_names("domainkit.ddd", "DomainEvent")
    events.append(
        f'''
class {event}(DomainEvent):
    """An amount of a {names.title}'s {what} went to another."""

    {a}_id: UUID
    to: UUID
    amount: Money
'''
    )
    commands.import_names("domainkit.ddd", "Command")
    commands.append(
        f'''
class {command}(Command):
    """Move an amount of {what} from one {names.title} to another."""

    {a}_id: UUID
    to: UUID
    amount: Money
'''
    )
    unit = f"{domain.camel}UnitOfWork"
    services.import_names("domainkit.ddd", "ConcurrentUpdate", "Retry")
    services.import_names(domain.module("application.commands"), command)
    if not services.defines("RACES"):
        services.insert_after_imports(
            [
                "",
                "",
                "RACES = Retry(attempts=5, retry_on=(ConcurrentUpdate,))",
                '"""Up to five runs of a transfer, each after a short random wait."""',
            ]
        )
    services.append(
        f'''
async def {service}(command: {command}, uow: {unit}) -> {A}:
    """Move the amount from one {names.title} to another, whole or not at all.

    Both rows are locked in one statement, by ascending id, so two transfers
    between the same {many} take the locks in the same order and cannot
    deadlock; the whole work runs again when its transaction loses a race.

    Add what a project decides: the idempotency middleware, so a request
    retried with its ``Idempotency-Key`` moves the amount once, and a rule
    that the caller may move money out of this {names.title}
    (``has_object_permission``).
    """
    source, target = {A}Id(command.{a}_id), {A}Id(command.to)

    async def work() -> {A}:
        async with uow:
            {many} = await uow.{many}.get_many_for_update([source, target])
            {many}[source].transfer_{field}(to=target, amount=command.amount)
            {many}[target].receive_{field}(amount=command.amount)
            await uow.commit()
        return {many}[source]

    return await uow.run(work, retry=RACES)
'''
    )
    routes.import_names("uuid", "UUID")
    routes.import_names("pydantic", "BaseModel")
    routes.import_names("domainkit.money", "Money")
    routes.import_names(domain.module("application.commands"), command)
    routes.insert_before(
        f"{many} = ResourceRouter(",
        [
            f"class {command}In(BaseModel):",
            f'    """What ``{command}`` takes."""',
            "",
            "    to: UUID",
            "    amount: Money",
            "",
            "",
        ],
    )
    routes.insert_before(
        f"router.include_router({many})",
        [
            f"{many}.command_route(",
            f"    {command},",
            f"    service=services.{service},",
            f"    body={command}In,",
            f'    path="/{path}",',
            ")",
        ],
    )
    _transfer_tests(domain, names, spec)


def _transfer_tests(domain: Domain, names: Names, spec: FieldSpec) -> None:
    """The tests of a money field's transfers: the rules, a lost race, the route.

    The route moves 1.00 USD out of an aggregate made from
    ``new_<name>_request()``, which holds 1.50 (1.00 in a column without
    cents), and refuses a second.
    """
    A, a, many, field = names.cls, names.snake, names.plural, spec.name
    held = Decimal("1.50") if int(spec.options[1]) >= 2 else Decimal("1.00")
    left, received = f"{held - 1:.2f}", f"{held + 1:.2f}"
    # Amounts the column holds: a column of fewer places refuses 0.01 when set.
    least = Decimal(1).scaleb(-min(int(spec.options[1]), 2))
    first, second = (
        Decimal(amount).quantize(least, ROUND_DOWN) for amount in ("2.50", "4.00")
    )
    small, moved, kept = f"{least:.2f}", f"{first:.2f}", f"{10 - first:.2f}"
    got, again, rest = f"{least + first:.2f}", f"{second:.2f}", f"{10 - second:.2f}"
    got_again = f"{least + second:.2f}"
    F = camel_case(field)
    command, event = f"Transfer{A}{F}", f"{A}{F}Transferred"
    invalid, short = f"Invalid{A}Transfer", f"Insufficient{A}{F}"
    service = f"transfer_{a}_{field}"
    path = f"transfer-{field.replace('_', '-')}"
    fake = f"Fake{domain.camel}UnitOfWork"
    domain.create(
        domain.tests / f"test_{service}.py",
        f'''"""``{command}``: the money moves whole, once, even when a race is lost."""

import pytest
from domainkit.ddd import ConcurrentUpdate, DomainEvent, EventBus
from domainkit.money import CurrencyMismatch, Money
from domainkit.testing import TestClient

from {domain.module("application")} import services
from {domain.module("application.commands")} import {command}
from {domain.module("domain.events")} import {event}
from {domain.module("domain.model")} import {A}, {short}, {invalid}

from .fakes import {fake}, new_{a}, new_{a}_request

URL = "/api/v1/{domain.name}/{many}"


def usd(amount: str) -> Money:
    """``amount`` US dollars."""
    return Money.of(amount, "USD")


async def funded(uow: {fake}, amount: str) -> {A}:
    """A stored {names.title} holding ``amount`` US dollars."""
    {a} = new_{a}()
    {a}.{field} = usd(amount)
    async with uow:
        await uow.{many}.add({a})
        await uow.commit()
    return {a}


def test_a_transfer_takes_at_most_the_{field}_in_its_currency() -> None:
    """Past the {field}, nothing, to itself, or of another currency: refused."""
    source, target = new_{a}(), new_{a}()
    source.{field} = usd("1.00")

    with pytest.raises({short}):
        source.transfer_{field}(to=target.id, amount=usd("1.01"))
    with pytest.raises({invalid}):
        source.transfer_{field}(to=target.id, amount=usd("0.00"))
    with pytest.raises({invalid}):
        source.transfer_{field}(to=source.id, amount=usd("1.00"))
    with pytest.raises(CurrencyMismatch):
        source.transfer_{field}(to=target.id, amount=Money.of("1", "EUR"))
    assert source.{field} == usd("1.00")
    assert source.pull_events() == []


@pytest.mark.anyio
async def test_{service}_publishes_{snake_case(event)}() -> None:
    """The money moves, in one commit; {event} follows."""
    published: list[DomainEvent] = []
    bus = EventBus()

    async def collect(event: {event}) -> None:
        published.append(event)

    bus.subscribe({event}, collect)
    uow = {fake}(bus)
    source, target = await funded(uow, "10.00"), await funded(uow, "{small}")

    command = {command}({a}_id=source.id, to=target.id, amount=usd("{moved}"))
    await services.{service}(command, uow)

    assert (source.{field}, target.{field}) == (usd("{kept}"), usd("{got}"))
    assert uow.commits == 3
    assert [type(each) for each in published] == [{event}]


@pytest.mark.anyio
async def test_a_transfer_that_loses_a_race_runs_again_whole() -> None:
    """The first commit fails; the second run moves the money, once."""

    class LosesOnce({fake}):
        lost = False

        async def _commit(self) -> None:
            if self.commits == 2 and not self.lost:  # The transfer's first commit.
                self.lost = True
                raise ConcurrentUpdate("Another transfer changed the {names.title}.")
            await super()._commit()

    uow = LosesOnce()
    source, target = await funded(uow, "10.00"), await funded(uow, "{small}")

    command = {command}({a}_id=source.id, to=target.id, amount=usd("{again}"))
    await services.{service}(command, uow)

    assert (uow.lost, uow.commits) == (True, 3)
    assert (source.{field}, target.{field}) == (usd("{rest}"), usd("{got_again}"))


def test_{service}_route(admin_client: TestClient) -> None:
    """A transfer past the {field} is refused, 422, and nothing moves."""
    ids = [admin_client.post(URL, json=new_{a}_request()).json()["id"] for _ in "ab"]
    transfer = {{"to": ids[1], "amount": {{"amount": "1.00", "currency": "USD"}}}}

    moved = admin_client.post(f"{{URL}}/{{ids[0]}}/{path}", json=transfer)
    refused = admin_client.post(f"{{URL}}/{{ids[0]}}/{path}", json=transfer)

    assert moved.status_code == 200, moved.json()
    assert moved.json()["{field}"] == {{"amount": "{left}", "currency": "USD"}}
    assert (refused.status_code, refused.json()["code"]) == (422, "{snake_case(short)}")
    target = admin_client.get(f"{{URL}}/{{ids[1]}}").json()
    assert target["{field}"] == {{"amount": "{received}", "currency": "USD"}}
''',
    )
