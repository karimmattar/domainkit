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

import re
from argparse import ArgumentParser
from typing import Final

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
    """Add a command, its use case, its route, and tests to an aggregate."""

    help = "Add a task command to an aggregate: the method, use case, route, tests."

    def add_options(self, parser: ArgumentParser) -> None:
        """Accept the command, its fields, and its event."""
        parser.add_argument("command", help="the command's class, e.g. PublishProduct")
        parser.add_argument(
            "--field", action="append", default=[], dest="fields", help=field_help()
        )
        parser.add_argument(
            "--event", help="the event's class (default: e.g. ProductPublished)"
        )

    def plan_for(
        self,
        domain: Domain,
        names: Names,
        *,
        command: str,
        fields: list[str],
        event: str | None,
        **options: object,
    ) -> Planned:
        """Plan the command, its event, use case, route, and tests."""
        verb = verb_of(class_name(command, "command name"), names.cls)
        happened = class_name(event or f"{names.cls}{past_tense(verb)}", "event name")
        add_command(domain, names, command, happened, parse_fields(fields))
        return Planned(domain, command)


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


_IRREGULAR: Final = {
    **dict.fromkeys(
        ["bet", "bid", "broadcast", "burst", "cast", "cost", "cut", "hit", "hurt"],
        None,
    ),
    **dict.fromkeys(["let", "put", "quit", "read", "set", "shed", "shut"], None),
    **dict.fromkeys(["split", "spread", "upset", "offset", "input"], None),
    "arise": "arisen",
    "become": "become",
    "begin": "begun",
    "bend": "bent",
    "bind": "bound",
    "bite": "bitten",
    "blow": "blown",
    "break": "broken",
    "bring": "brought",
    "build": "built",
    "buy": "bought",
    "catch": "caught",
    "choose": "chosen",
    "come": "come",
    "deal": "dealt",
    "dig": "dug",
    "do": "done",
    "draw": "drawn",
    "drive": "driven",
    "eat": "eaten",
    "fall": "fallen",
    "feed": "fed",
    "fight": "fought",
    "find": "found",
    "fly": "flown",
    "forbid": "forbidden",
    "forget": "forgotten",
    "forgive": "forgiven",
    "freeze": "frozen",
    "get": "got",
    "give": "given",
    "go": "gone",
    "grow": "grown",
    "hear": "heard",
    "hide": "hidden",
    "hold": "held",
    "keep": "kept",
    "know": "known",
    "lay": "laid",
    "lead": "led",
    "leave": "left",
    "lend": "lent",
    "lose": "lost",
    "make": "made",
    "mean": "meant",
    "meet": "met",
    "pay": "paid",
    "relay": "relayed",
    "ride": "ridden",
    "rise": "risen",
    "run": "run",
    "say": "said",
    "see": "seen",
    "seek": "sought",
    "sell": "sold",
    "send": "sent",
    "shake": "shaken",
    "shoot": "shot",
    "sit": "sat",
    "sleep": "slept",
    "slide": "slid",
    "speak": "spoken",
    "spend": "spent",
    "spin": "spun",
    "stand": "stood",
    "steal": "stolen",
    "stick": "stuck",
    "strike": "struck",
    "swear": "sworn",
    "sweep": "swept",
    "take": "taken",
    "teach": "taught",
    "tear": "torn",
    "tell": "told",
    "think": "thought",
    "throw": "thrown",
    "wake": "woken",
    "wear": "worn",
    "win": "won",
    "write": "written",
}
"""Past participles that are not the verb and ``ed``; None: the verb itself."""
_PREFIXES: Final = ("fore", "mis", "out", "over", "pre", "re", "un", "under", "up")
_PREFIXES_WITH: Final = (*_PREFIXES, "with")
"""Prefixes that keep a verb's participle: ``resend``, ``resent``."""
_STRESSED_LAST: Final = frozenset(
    {"abet", "acquit", "admit", "allot", "commit", "compel", "concur", "confer"}
    | {"control", "debug", "defer", "deter", "dispel", "embed", "emit", "equip"}
    | {"expel", "format", "incur", "infer", "kidnap", "occur", "omit", "patrol"}
    | {"permit", "program", "propel", "submit", "transfer", "transmit"}
)
"""Verbs of two syllables or more that double their last consonant."""
_ONE_SYLLABLE_CVC: Final = re.compile(r"[^aeiou]*[aeiou][b-df-hj-np-rtvz]")
"""One vowel, then one consonant that doubles: ``ship``, ``stop``, ``plan``."""
_ONSET: Final = re.compile(
    r"(?:[bcdfghjklmnpqrstvwz]|[bcfgp][lr]|[dt]r|s[chklmnpqtw]|[stw]h|tw|wr"
    r"|s[cp][lr]|str|[st]hr)"
    r"[aeiouy]"
)
"""How an English word may start: a consonant or two, then a vowel."""
_PARTICLES: Final = frozenset({"Back", "Down", "In", "Off", "On", "Out", "Over", "Up"})
"""A phrasal verb's last word, left as it is: ``CheckIn``, ``CheckedIn``."""
_WORDS: Final = re.compile(r"[A-Z]+(?![a-z])|[A-Z]?[a-z0-9]+")


def past_tense(verb: str) -> str:
    """``Published`` of ``Publish``: the participle an event is named with.

    English's common irregular verbs (``Paid`` of ``Pay``, ``Sent`` of
    ``Send``), also after a prefix (``Resent``); a last consonant doubled
    where English doubles it (``Shipped``, ``Submitted``); a phrasal verb's
    particle kept last (``CheckedIn``). ``--event`` names those it gets wrong.
    """
    parts = _WORDS.findall(verb)
    if "".join(parts) != verb:
        return _regular(verb)
    at = -2 if len(parts) > 1 and parts[-1] in _PARTICLES else -1
    word = parts[at]
    if word not in {word.lower(), word.capitalize()}:
        return _regular(verb)  # An acronym: ``SyncAPI``.
    done = _participle(word.lower())
    parts[at] = done.capitalize() if word[0].isupper() else done
    return "".join(parts)


def _participle(verb: str) -> str:
    """The past participle of the lower-case ``verb``."""
    for prefix in ("", *_PREFIXES_WITH):
        stem = verb.removeprefix(prefix)
        if verb.startswith(prefix) and stem in _IRREGULAR:
            done = _IRREGULAR[stem]
            return verb if done is None else f"{prefix}{done}"
    if _doubles(verb):
        return f"{verb}{verb[-1]}ed"
    return _regular(verb)


def _doubles(verb: str) -> bool:
    """Whether English doubles ``verb``'s last consonant: ``ship``, ``refer``.

    After a prefix, only a stem that starts as a word can (``re-fer``), not
    the rest of a word that only looks prefixed (``re-nder``, ``re-ckon``).
    """
    stems = [
        verb.removeprefix(prefix)
        for prefix in ("", *_PREFIXES)
        if verb.startswith(prefix)
        and (not prefix or _ONSET.match(verb.removeprefix(prefix)))
    ]
    return any(
        stem in _STRESSED_LAST
        or _ONE_SYLLABLE_CVC.fullmatch(stem.replace("qu", "q")) is not None
        for stem in stems
    )


def _regular(verb: str) -> str:
    """``ed`` added as English adds it to a regular verb."""
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
