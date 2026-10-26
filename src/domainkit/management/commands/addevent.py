"""``addevent``: add a domain event of an aggregate, and a reaction to it.

``manage.py addevent catalog Product ProductRestocked --field quantity:int``
writes:

- ``ProductRestocked`` (``product_id`` and ``quantity``) in
  ``domain/events.py``; a method of the aggregate records it (``addcommand``
  writes one that records an event of its own);
- ``on_product_restocked`` in ``application/handlers.py``, subscribed in
  ``apps.py`` as a durable handler: stored with the commit, run by the
  worker, at least once (``--no-handler`` leaves the reaction out);
- ``test_on_product_restocked.py``.
"""

from argparse import ArgumentParser

from domainkit.management.base import CommandError
from domainkit.management.generators import (
    AggregateGenerator,
    Domain,
    FieldSpec,
    Names,
    Planned,
    class_name,
    field_help,
    parse_fields,
    words,
)
from domainkit.utils.text import snake_case


class Command(AggregateGenerator):
    """Add a domain event, a handler, its subscription, and tests."""

    help = "Add a domain event of an aggregate, and a durable handler of it."

    def add_options(self, parser: ArgumentParser) -> None:
        """Accept the event and its fields."""
        parser.add_argument("event", help="the event's class, e.g. ProductRestocked")
        parser.add_argument(
            "--field", action="append", default=[], dest="fields", help=field_help()
        )
        parser.add_argument(
            "--no-handler",
            action="store_false",
            dest="handler",
            help="add the event only, without a handler",
        )

    def plan_for(
        self,
        domain: Domain,
        names: Names,
        *,
        event: str,
        fields: list[str],
        handler: bool,
        **options: object,
    ) -> Planned:
        """Plan the event, and its handler and test unless left out."""
        add_event(
            domain,
            names,
            class_name(event, "event name"),
            parse_fields(fields),
            handler=handler,
        )
        return Planned(domain, event)


def add_event(
    domain: Domain,
    names: Names,
    event: str,
    specs: list[FieldSpec],
    *,
    handler: bool = True,
) -> None:
    """Plan the edits that add ``event`` of an aggregate, and its handler.

    Raises:
        CommandError: A name it would define is taken, or a file is not as
            the generators made it.
    """
    A, a = names.cls, names.snake
    reaction = f"on_{snake_case(event)}"
    model = domain.edit("domain/model.py")
    events = domain.edit("domain/events.py")
    if not model.defines(A):
        raise CommandError(f"{domain.package} has no aggregate {A}.")
    if events.defines(event):
        raise CommandError(f"{events.path} already defines {event}.")
    if any(spec.name == f"{a}_id" for spec in specs):
        raise CommandError(f"{a}_id is the event's already; leave it out.")
    events.import_names("uuid", "UUID")
    events.import_names("domainkit.ddd", "DomainEvent")
    for spec in specs:
        for module, name in spec.plain_imports():
            events.import_names(module, name)
    fields = "".join(f"\n    {spec.name}: {spec.plain()}" for spec in specs)
    events.append(
        f'''
class {event}(DomainEvent):
    """{words(event).capitalize()}."""

    {a}_id: UUID{fields}
'''
    )
    if handler:
        _handler(domain, names, event, reaction)
        _test(domain, names, event, reaction, specs)


def _handler(domain: Domain, names: Names, event: str, reaction: str) -> None:
    """The handler, and its subscription in ``apps.py``."""
    a = names.snake
    handlers = domain.edit("application/handlers.py")
    apps = domain.edit("apps.py")
    if handlers.defines(reaction):
        raise CommandError(f"{handlers.path} already defines {reaction}.")
    config = next(
        (
            name
            for name in apps.classes()
            if name.endswith("Config") and apps.defines(f"{name}.ready")
        ),
        None,
    )
    if config is None:
        raise CommandError(f"{apps.path} has no DomainConfig with ready().")
    if not handlers.defines("logging"):
        handlers.add_imports(["import logging"])
    if not handlers.defines("logger"):
        handlers.insert_after_imports(["", "logger = logging.getLogger(__name__)"])
    handlers.import_names(domain.module("domain.events"), event)
    handlers.append(
        f'''
async def {reaction}(event: {event}) -> None:
    """React to {words(event)}: idempotently, as it may run more than once."""
    logger.info("{event} %s", event.{a}_id)
'''
    )
    apps.import_names("domainkit.ddd", "event_bus")
    apps.import_names(domain.module("application.handlers"), reaction)
    apps.import_names(domain.module("domain.events"), event)
    apps.add_to_function(
        f"{config}.ready",
        [f"event_bus.subscribe({event}, {reaction}, durable=True)"],
    )


def _test(
    domain: Domain, names: Names, event: str, reaction: str, specs: list[FieldSpec]
) -> None:
    """The handler's test."""
    a = names.snake
    imports = sorted(
        {
            f"from {module} import {name}"
            for spec in specs
            for module, name in spec.type.example_imports
        }
        | {"from uuid import UUID"}
    )
    values = "".join(f", {spec.name}={spec.example()}" for spec in specs)
    domain.create(
        domain.tests / f"test_{reaction}.py",
        f'''"""``{reaction}``: the reaction to ``{event}``."""

import logging
{chr(10).join(imports)}

import pytest

from {domain.module("application")} import handlers
from {domain.module("domain.events")} import {event}


@pytest.mark.anyio
async def test_{reaction}(caplog: pytest.LogCaptureFixture) -> None:
    """The handler reacts to the event."""
    event = {event}({a}_id=UUID(int=1){values})

    with caplog.at_level(logging.INFO, logger=handlers.__name__):
        await handlers.{reaction}(event)

    assert "{event} 00000000-0000-0000-0000-000000000001" in caplog.text
''',
    )
