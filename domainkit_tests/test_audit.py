"""The audit trail: committed events and explicit records."""

import asyncio
import hashlib
import hmac
import io
import os
import re
import uuid
from collections.abc import AsyncIterator, Callable, Collection, Iterator
from datetime import UTC, date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Annotated, Any, Optional

import pytest
from sqlalchemy import insert, select, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import create_async_engine

from domainkit import setup
from domainkit.apps import registry
from domainkit.conf import override_settings, settings
from domainkit.context import SYSTEM_ACTOR, RequestContext, bind_request
from domainkit.contrib.audit import (
    Outcome,
    action_for,
    forget,
    forget_anonymous,
    personal,
    pseudonym_of,
    record,
)
from domainkit.contrib.audit.management.commands.auditforget import Command
from domainkit.contrib.audit.personal import audited_databases, pseudonym, subject_keys
from domainkit.contrib.audit.protection import PROTECT, UNPROTECT, protect, unprotect
from domainkit.contrib.audit.recorder import event_rows
from domainkit.contrib.audit.settings import AuditSettings
from domainkit.contrib.audit.tables import audit_keys, audit_log
from domainkit.db import connections
from domainkit.ddd import Counted, DomainEvent, Personal, ValueObject
from domainkit.ddd.personal import counted_fields, personal_fields
from domainkit.management import CommandError, call_command
from domainkit.management.commands import partitions as partitions_command
from domainkit.testing import assert_num_queries
from domainkit.testing.queries import CapturedQueries
from domainkit_tests.conftest import POSTGRES_VARIABLE
from domainkit_tests.sample_domains.alpha.events import GreetingSent
from domainkit_tests.test_db import Note, NoteId, NotesUnitOfWork, NoteWritten, notes

pytestmark = pytest.mark.anyio

ALPHA = "domainkit_tests.sample_domains.alpha"
AUDIT = "domainkit.contrib.audit"
IP = "203.0.113.5"
ON_POSTGRES = bool(os.environ.get(POSTGRES_VARIABLE))


@pytest.fixture
def audited(database_url: str) -> Iterator[str]:
    """Install the audit app on an empty database."""
    url = database_url
    registry.clear()
    with override_settings(
        INSTALLED_DOMAINS=[AUDIT, ALPHA], DATABASES={"default": {"URL": url}}
    ):
        setup()
        yield url
    registry.clear()


@pytest.fixture
async def tables(audited: str) -> AsyncIterator[None]:
    """Create the audit and notes tables."""
    async with connections.engine().begin() as connection:
        await connection.run_sync(audit_log.create)
        await connection.run_sync(audit_keys.create)
        await connection.run_sync(notes.create)
    yield
    await connections.close_all()


async def entries() -> list[dict[str, Any]]:
    """Return the audit trail, oldest first."""
    async with connections.engine().connect() as connection:
        rows = await connection.execute(
            select(audit_log).order_by(audit_log.c.occurred_at)
        )
        return [dict(row._mapping) for row in rows]


async def write_note(*, actor: str | None = None, commit: bool = True) -> Note:
    """Store a note that records ``NoteWritten``, as ``actor``."""
    note = Note(id=NoteId(uuid.uuid4()), text="audited")
    note.record(NoteWritten(note_id=note.id))
    with bind_request(RequestContext("req-9", IP, "POST", "/notes", actor)):
        async with NotesUnitOfWork() as uow:
            await uow.notes.add(note)
            if commit:
                await uow.commit()
    return note


async def test_committed_events_are_audited(tables: None) -> None:
    """Each event becomes a row with the actor, request, and event fields."""
    note = await write_note(actor="alice")
    await write_note(commit=False)

    (entry,) = await entries()
    assert entry["action"] == "domainkit_tests.note_written"
    assert (entry["actor"], entry["outcome"]) == ("alice", "success")
    assert entry["request_id"] == "req-9"
    assert entry["client_ip"] == await pseudonym_of("alice", "client_ip", IP)
    assert entry["details"] == {"note_id": str(note.id)}
    assert entry["event_id"] is not None


async def test_anonymous_and_excluded(tables: None) -> None:
    """Unauthenticated requests are anonymous; excluded actions are skipped."""
    await write_note()
    with override_settings(AUDIT={"EXCLUDE": ["domainkit_tests.*"]}):
        await write_note(actor="bob")
    with override_settings(AUDIT={"EVENTS": False}):
        await write_note(actor="carol")

    assert [entry["actor"] for entry in await entries()] == ["anonymous"]


async def test_events_are_audited_where_they_commit(
    tables: None, tmp_path: Path
) -> None:
    """A unit of work on another alias writes its audit rows in that database."""

    class ElsewhereUnitOfWork(NotesUnitOfWork):
        database = "elsewhere"

    elsewhere = f"sqlite+aiosqlite:///{tmp_path / 'elsewhere.sqlite3'}"
    databases = {**settings.DATABASES, "elsewhere": {"URL": elsewhere}}
    with override_settings(DATABASES=databases):
        async with connections.engine("elsewhere").begin() as connection:
            await connection.run_sync(notes.create)
            await connection.run_sync(audit_log.create)
        note = Note(id=NoteId(uuid.uuid4()), text="x")
        note.record(NoteWritten(note_id=note.id))
        async with ElsewhereUnitOfWork() as uow:
            await uow.notes.add(note)
            await uow.commit()
        async with connections.engine("elsewhere").connect() as connection:
            audited = (await connection.execute(select(audit_log.c.action))).all()

    assert await entries() == []
    assert [row.action for row in audited] == ["domainkit_tests.note_written"]


async def test_explicit_records(tables: None) -> None:
    """Denials and other non-event actions are recorded on their own."""
    await record("auth.login", Outcome.DENIED, target="dave", reason="bad password")
    with bind_request(RequestContext("req-1", None, "GET", "/", actor=None)):
        await record("report.viewed", target="r-1")

    denied, viewed = await entries()
    assert (denied["actor"], denied["outcome"], denied["details"]) == (
        "system",
        "denied",
        {"reason": "bad password"},
    )
    assert (viewed["actor"], viewed["action"]) == ("anonymous", "report.viewed")


def test_action_names_use_domain_labels(audited: str) -> None:
    """Events of an installed domain are named after its label."""
    shipped = type("Shipped", (DomainEvent,), {"__module__": "outside.orders.events"})
    assert action_for(GreetingSent) == "alpha.greeting_sent"
    assert action_for(NoteWritten) == "domainkit_tests.note_written"
    assert action_for(shipped) == "outside.orders.shipped"


def test_auditlog_command(audited: str) -> None:
    """``auditlog`` lists entries newest first, with filters."""

    async def prepare() -> None:
        async with connections.engine().begin() as connection:
            await connection.run_sync(audit_log.create)
        await record("auth.login", Outcome.DENIED, target="dave")
        with bind_request(RequestContext("req-2", None, "GET", "/", actor="erin")):
            await record("report.viewed")
        await connections.close_all()

    asyncio.run(prepare())

    def run(*args: str) -> list[str]:
        output = io.StringIO()
        call_command("auditlog", *args, stdout=output)
        return output.getvalue().splitlines()

    newest, oldest = run()
    assert "erin  report.viewed  success  -  req-2" in newest
    assert "system  auth.login  denied  dave  -" in oldest
    assert len(run("--actor", "erin")) == 1
    assert len(run("--action", "auth.login")) == 1


class FakeOperations:
    """Records the statements a migration would run on one dialect."""

    def __init__(self, dialect: str) -> None:
        """Pretend to migrate a ``dialect`` database."""
        self.dialect = dialect
        self.statements: list[str] = []

    def get_bind(self) -> Any:  # noqa: ANN401 - only .dialect.name is read.
        """Return a stand-in connection."""
        return SimpleNamespace(dialect=SimpleNamespace(name=self.dialect))

    def execute(self, sqltext: str) -> None:
        """Record ``sqltext``."""
        self.statements.append(sqltext)


def test_protection_installs_triggers_on_postgresql_only() -> None:
    """protect/unprotect run their DDL on PostgreSQL and nothing elsewhere."""
    postgres, sqlite = FakeOperations("postgresql"), FakeOperations("sqlite")

    protect(postgres)
    unprotect(postgres)
    protect(sqlite)
    unprotect(sqlite)

    assert postgres.statements == [*PROTECT, *UNPROTECT]
    assert sqlite.statements == []
    assert all("domainkit_audit_log" in statement for statement in PROTECT[1:])


# Personal data ------------------------------------------------------------------


class MemberJoined(DomainEvent):
    """An event with personal data about a member."""

    member_id: uuid.UUID | None
    email: Annotated[str, Personal(of="member_id")]
    plan: Annotated[str, "not personal"] = "basic"


PSEUDONYM = re.compile(r"pseudonym:[0-9a-f]{32}")


async def commit_events(
    *events: DomainEvent,
    actor: str | None = None,
    uow_class: type[NotesUnitOfWork] = NotesUnitOfWork,
) -> None:
    """Commit ``events``, recorded by a new note, as ``actor`` from ``IP``."""
    note = Note(id=NoteId(uuid.uuid4()), text="audited")
    for event in events:
        note.record(event)
    with bind_request(RequestContext("req-7", IP, "POST", "/members", actor)):
        async with uow_class() as uow:
            await uow.notes.add(note)
            await uow.commit()


async def subjects(alias: str = "default") -> list[str]:
    """The subjects that have a key in ``alias``, sorted."""
    async with connections.engine(alias).connect() as connection:
        found = await connection.scalars(
            select(audit_keys.c.subject).order_by(audit_keys.c.subject)
        )
        return list(found)


def test_personal_fields_are_found_by_their_markers() -> None:
    """Only ``Personal`` metadata counts, and ``of`` must name another field."""

    class Unknown(DomainEvent):
        email: Annotated[str, Personal(of="member_id")]

    class Itself(DomainEvent):
        email: Annotated[str, Personal(of="email")]

    assert personal_fields(MemberJoined) == {"email": Personal(of="member_id")}
    for event, of in ((Unknown, "member_id"), (Itself, "email")):
        refused = (
            rf"\.{event.__name__}\.email: Personal\(of='{of}'\) must name another "
            r"field of the class\.$"
        )
        with pytest.raises(TypeError, match=refused):
            personal_fields(event)


class ListChanged(DomainEvent):
    """An event with lists the trail keeps only the size of."""

    members: Annotated[tuple[str, ...], Counted()]
    owners: Annotated[list[str] | None, Counted()] = None
    tags: tuple[str, ...] = ()


def test_counted_fields_are_audited_as_their_size() -> None:
    """``Counted`` keeps the number of items in the row; the event keeps them."""
    event = ListChanged(members=("u1", "u2", "u3"), tags=("a",))

    (draft,) = event_rows([event], AuditSettings())

    assert counted_fields(ListChanged) == {"members", "owners"}
    assert draft.row["details"] == {"members": 3, "owners": None, "tags": ["a"]}
    assert event.members == ("u1", "u2", "u3")
    assert counted_fields(MemberJoined) == frozenset()


class MemberMoved(DomainEvent):
    """An event whose personal fields may be missing."""

    member_id: uuid.UUID
    phone: Annotated[str, Personal(of="member_id")] | None = None
    street: Optional[Annotated[str, Personal(of="member_id")]] = None  # noqa: UP045


class Address(ValueObject):
    """A value object with a personal field of its own."""

    owner_id: uuid.UUID
    street: Annotated[str, Personal(of="owner_id")]


class Place(ValueObject):
    """A value object without personal data, which refers to itself."""

    name: str
    within: "Place | None" = None


class Elsewhere(ValueObject):
    """A value object whose annotation names a type only checkers import."""

    thing: "NotImportedHere"  # type: ignore[name-defined]  # noqa: F821


def test_personal_fields_may_be_optional() -> None:
    """A marker on one member of a union marks the field; each one is checked."""

    class Either(DomainEvent):
        member_id: uuid.UUID
        contact: (
            Annotated[str, Personal(of="nobody")]
            | Annotated[int, Personal(of="member_id")]
        )

    assert personal_fields(MemberMoved) == {
        "phone": Personal(of="member_id"),
        "street": Personal(of="member_id"),
    }
    with pytest.raises(TypeError, match=r"Personal\(of='nobody'\) must name"):
        personal_fields(Either)


def test_personal_markers_below_the_field_are_refused() -> None:
    """Only whole fields become pseudonyms: a marker deeper would be ignored."""
    marked = Annotated[str, Personal(of="member_id")]

    class Listed(DomainEvent):
        member_id: uuid.UUID
        emails: list[marked]

    class Mapped(DomainEvent):
        member_id: uuid.UUID
        emails: dict[str, marked] | None

    class Nested(DomainEvent):
        member_id: uuid.UUID
        address: Address

    class Described(DomainEvent):
        member_id: uuid.UUID
        emails: Annotated[list[marked], "not personal"]

    class Deeper(DomainEvent):
        member_id: uuid.UUID
        emails: dict[str, Annotated[list[marked], "not personal"]]

    whole = re.escape("Annotated[..., Personal(of=...)].")
    for event, field in (
        (Listed, "emails"),
        (Mapped, "emails"),
        (Nested, "address"),
        (Described, "emails"),
        (Deeper, "emails"),
    ):
        refused = (
            rf"\.{event.__name__}\.{field}: Personal is read only on the field "
            rf"itself; mark the whole field, as {whole}$"
        )
        with pytest.raises(TypeError, match=refused):
            personal_fields(event)


def test_other_fields_may_hold_any_type() -> None:
    """Unmarked classes, even recursive or unresolvable ones, and whole marks pass.

    A field marked as a whole is a pseudonym whole, markers inside it or not.
    """
    marked = Annotated[str, Personal(of="member_id")]

    class Visited(DomainEvent):
        member_id: uuid.UUID
        place: Place
        elsewhere: Elsewhere | None = None
        address: Annotated[Address, Personal(of="member_id")] | None = None
        emails: Annotated[tuple[marked, ...], Personal(of="member_id")] = ()

    assert personal_fields(Visited) == {
        "address": Personal(of="member_id"),
        "emails": Personal(of="member_id"),
    }


async def test_optional_personal_values_are_pseudonyms(tables: None) -> None:
    """An optional personal field is kept as a pseudonym, never as clear text."""
    member = uuid.uuid4()

    await commit_events(MemberMoved(member_id=member, phone="+20 100 000 0000"))

    (entry,) = await entries()
    phone = await pseudonym_of(str(member), "phone", "+20 100 000 0000")
    assert entry["details"] == {
        "member_id": str(member),
        "phone": phone,
        "street": None,  # Nothing personal to keep.
    }
    assert "+20 100" not in repr(entry)


async def test_personal_values_are_pseudonyms_of_their_subject(tables: None) -> None:
    """The member's email is theirs; the client's address is the actor's."""
    member = uuid.uuid4()

    await commit_events(
        MemberJoined(member_id=member, email="ann@example.com", plan="gold"),
        actor="alice",
    )

    (entry,) = await entries()
    email = await pseudonym_of(str(member), "email", "ann@example.com")
    assert entry["details"] == {
        "member_id": str(member),
        "plan": "gold",
        "email": email,
    }
    assert entry["client_ip"] == await pseudonym_of("alice", "client_ip", IP)
    assert PSEUDONYM.fullmatch(entry["details"]["email"])
    assert PSEUDONYM.fullmatch(entry["client_ip"])
    assert "ann@example.com" not in repr(entry)
    assert IP not in repr(entry)
    assert await subjects() == sorted(["alice", str(member)])


async def test_pseudonyms_link_one_subjects_equal_values_only(tables: None) -> None:
    """Equal values of one subject match; another subject's, or field's, do not."""
    ann, bob = uuid.uuid4(), uuid.uuid4()

    await commit_events(
        MemberJoined(member_id=ann, email="same@example.com"),
        MemberJoined(member_id=ann, email="same@example.com"),
        MemberJoined(member_id=bob, email="same@example.com"),
    )

    by_member: dict[str, set[str]] = {}
    for entry in await entries():
        details = entry["details"]
        by_member.setdefault(details["member_id"], set()).add(details["email"])
    assert len(by_member[str(ann)]) == len(by_member[str(bob)]) == 1
    assert by_member[str(ann)] != by_member[str(bob)]
    plan = await pseudonym_of(str(ann), "plan", "same@example.com")
    assert plan not in by_member[str(ann)]


async def test_anonymous_callers_are_one_subject_per_utc_day(tables: None) -> None:
    """Without an actor, the day the row occurred names the subject, in UTC."""
    late = datetime(2026, 1, 3, 1, 30, tzinfo=timezone(timedelta(hours=2)))

    await commit_events(
        MemberJoined(member_id=None, email="x@example.com", occurred_at=late)
    )

    (entry,) = await entries()
    subject = "anonymous:2026-01-02"
    assert entry["actor"] == "anonymous"
    assert entry["client_ip"] == await pseudonym_of(subject, "client_ip", IP)
    assert entry["details"]["email"] == await pseudonym_of(
        subject, "email", "x@example.com"
    )
    assert await subjects() == [subject]


async def test_forgetting_makes_pseudonyms_unlinkable(tables: None) -> None:
    """The rows stay as they are; a later event gets a new key."""
    ann = uuid.uuid4()
    await commit_events(
        MemberJoined(member_id=ann, email="ann@example.com"), actor="alice"
    )
    before = await entries()

    assert await forget(str(ann), "nobody") == 1
    assert await forget() == 0
    assert await pseudonym_of(str(ann), "email", "ann@example.com") is None
    assert await entries() == before
    assert await subjects() == ["alice"]
    await commit_events(
        MemberJoined(member_id=ann, email="ann@example.com"), actor="alice"
    )

    later = [entry for entry in await entries() if entry not in before]
    assert later[0]["details"]["email"] != before[0]["details"]["email"]
    assert later[0]["client_ip"] == before[0]["client_ip"]


async def test_anonymous_days_are_forgotten_before_a_date(tables: None) -> None:
    """Only anonymous days before the date go; other subjects stay."""
    for day in (1, 2):
        when = datetime(2026, 1, day, 12, tzinfo=UTC)
        await commit_events(MemberJoined(member_id=None, email="x", occurred_at=when))
    await commit_events(MemberJoined(member_id=None, email="x"), actor="alice")

    assert await forget_anonymous(date(2026, 1, 2)) == 1

    assert await subjects() == ["alice", "anonymous:2026-01-02"]


async def test_system_work_keeps_personal_values_as_the_days(tables: None) -> None:
    """Work no request queued is the system's; its values are the day's callers'."""
    job = RequestContext("job-1", None, "TASK", "tests.export", SYSTEM_ACTOR)
    with bind_request(job):
        await record("member.exported", personal={"email": "ann@example.com"})

    (entry,) = await entries()
    day = f"anonymous:{entry['occurred_at'].date().isoformat()}"
    assert entry["actor"] == SYSTEM_ACTOR
    assert entry["details"] == {
        "email": await pseudonym_of(day, "email", "ann@example.com")
    }
    assert await subjects() == [day]


async def test_explicit_records_keep_personal_details_apart(tables: None) -> None:
    """``personal`` details are the actor's, the day's callers', or ``subject``'s."""
    with bind_request(RequestContext("req-1", IP, "POST", "/auth/login", None)):
        await record(
            "auth.login", Outcome.DENIED, personal={"username": "dave"}, reason="bad"
        )
    with bind_request(RequestContext("req-2", None, "POST", "/reset", "erin")):
        await record("auth.reset", personal={"email": "e@example.com"})
    await record(
        "auth.export", personal={"email": "f@example.com", "phone": None}, subject="u-7"
    )

    denied, reset, export = await entries()
    day = f"anonymous:{denied['occurred_at'].date().isoformat()}"
    assert denied["details"] == {
        "reason": "bad",
        "username": await pseudonym_of(day, "username", "dave"),
    }
    assert denied["client_ip"] == await pseudonym_of(day, "client_ip", IP)
    assert reset["details"] == {
        "email": await pseudonym_of("erin", "email", "e@example.com")
    }
    assert reset["client_ip"] is None
    assert export["details"] == {
        "email": await pseudonym_of("u-7", "email", "f@example.com"),
        "phone": None,
    }
    with pytest.raises(
        ValueError,
        match=r"^Audit details cannot be both plain and personal: \['a', 'b'\]\.$",
    ):
        await record("x", personal={"b": 1, "a": 2, "c": 3}, a=1, b=2)


async def test_writers_racing_for_a_new_subject_share_its_key(tables: None) -> None:
    """Two transactions making the first key of a subject both succeed."""
    member = uuid.uuid4()

    await asyncio.gather(
        *(
            commit_events(MemberJoined(member_id=member, email="r@example.com"))
            for _ in range(2)
        )
    )

    emails = {entry["details"]["email"] for entry in await entries()}
    assert emails == {await pseudonym_of(str(member), "email", "r@example.com")}
    assert str(member) in await subjects()


async def test_forgetting_reaches_every_audited_database(
    tables: None, tmp_path: Path
) -> None:
    """Keys are deleted wherever a domain is routed, and in ``AUDIT.DATABASE``."""

    class ElsewhereUnitOfWork(NotesUnitOfWork):
        database = "elsewhere"

    elsewhere = f"sqlite+aiosqlite:///{tmp_path / 'elsewhere.sqlite3'}"
    databases = {**settings.DATABASES, "elsewhere": {"URL": elsewhere}}
    member = uuid.uuid4()
    with override_settings(
        DATABASES=databases, DATABASE_ROUTING={"alpha": "elsewhere"}
    ):
        async with connections.engine("elsewhere").begin() as connection:
            for table in (notes, audit_log, audit_keys):
                await connection.run_sync(table.create)
        for uow_class in (NotesUnitOfWork, ElsewhereUnitOfWork):
            await commit_events(
                MemberJoined(member_id=member, email="m@example.com"),
                uow_class=uow_class,
            )

        assert audited_databases() == ("default", "elsewhere")
        assert await forget(str(member)) == 2
        assert str(member) not in await subjects("elsewhere")
        assert (
            await pseudonym_of(str(member), "email", "m", database="elsewhere") is None
        )
    assert str(member) not in await subjects()


def test_auditforget_command(
    audited: str, local_zone: Callable[[str], None], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Subjects, anonymous days (counted in UTC), or both; one is needed.

    Each run is audited as its operator, with counts but not the subjects.
    """
    monkeypatch.setenv("DOMAINKIT_OPERATOR", "dpo")
    # A zone whose date differs from UTC's now, so a local "today" would show.
    local_zone("Etc/GMT-14" if datetime.now(UTC).hour >= 12 else "Etc/GMT+12")

    async def prepare() -> None:
        today = datetime.now(UTC).date()
        names = ["alice", "bob"]
        names += [f"anonymous:{today - timedelta(days=days)}" for days in (31, 30, 0)]
        async with connections.engine().begin() as connection:
            await connection.run_sync(audit_keys.create)
            await connection.run_sync(audit_log.create)
            await connection.execute(
                insert(audit_keys),
                [
                    {"subject": name, "key": bytes(32), "created_at": datetime.now(UTC)}
                    for name in names
                ],
            )
        await connections.close_all()

    asyncio.run(prepare())

    def run(*args: str) -> str:
        output = io.StringIO()
        call_command("auditforget", *args, stdout=output)
        return output.getvalue()

    assert run("alice", "nobody") == "Forgot 1 key.\n"
    assert run("--anonymous-days", "30") == "Forgot 1 key.\n"
    assert run("bob", "--anonymous-days", "0") == "Forgot 2 keys.\n"
    assert run("bob") == "Forgot 0 keys.\n"
    with pytest.raises(
        CommandError, match=r"^Name subjects to forget, or --anonymous-days\.$"
    ):
        run()
    with pytest.raises(CommandError, match=r"^--anonymous-days cannot be negative\.$"):
        run("--anonymous-days", "-1")

    async def recorded() -> list[tuple[str, str, dict[str, Any]]]:
        async with connections.engine().connect() as connection:
            rows = await connection.execute(
                select(audit_log.c.actor, audit_log.c.outcome, audit_log.c.details)
                .where(audit_log.c.action == "operator.auditforget")
                .order_by(audit_log.c.occurred_at)
            )
            found = [tuple(row) for row in rows]
        await connections.close_all()
        return found

    runs = asyncio.run(recorded())
    assert [(actor, outcome) for actor, outcome, _ in runs] == [
        ("operator:dpo", "success")
    ] * 4  # Refused runs did nothing, and are not recorded.
    assert runs[2][2] == {
        "subjects": 1,
        "anonymous_days": 0,
        "keys": 2,
        "operator_source": "environment",
    }
    assert "alice" not in repr(runs)


def test_pruning_partitions_is_audited(
    audited: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``partitions --prune`` records each database it pruned, and who ran it."""
    monkeypatch.delenv("DOMAINKIT_OPERATOR", raising=False)

    async def prepare() -> None:
        async with connections.engine().begin() as connection:
            await connection.run_sync(audit_log.create)
            await connection.run_sync(audit_keys.create)
        await connections.close_all()

    async def recorded() -> list[tuple[str, str, dict[str, Any]]]:
        async with connections.engine().connect() as connection:
            rows = await connection.execute(
                select(audit_log.c.action, audit_log.c.actor, audit_log.c.details)
            )
            found = [tuple(row) for row in rows]
        await connections.close_all()
        return found

    calls: list[tuple[str, dict[str, bool]]] = []

    async def maintain(alias: str, **options: bool) -> list[str]:
        calls.append((alias, options))
        return ["DROP TABLE events_p20200101"] if options.get("prune") else []

    asyncio.run(prepare())
    monkeypatch.setattr(partitions_command, "maintain", maintain)
    outputs = []
    for arguments in (("--prune", "--dry-run"), (), ("--prune", "--operator", "ops")):
        output = io.StringIO()
        call_command("partitions", *arguments, stdout=output)
        outputs.append(output.getvalue())

    assert calls == [
        ("default", {"prune": True, "dry_run": True}),
        ("default", {"prune": False, "dry_run": False}),
        ("default", {"prune": True}),
    ]
    assert outputs == [
        "DROP TABLE events_p20200101;\n",
        "Partitions of 'default' are up to date.\n",
        "Partitions of 'default': 1 change(s).\n",
    ]
    assert asyncio.run(recorded()) == [
        (
            "operator.partitions.prune",
            "operator:ops",
            {"database": "default", "changes": 1, "operator_source": "option"},
        )
    ]


def test_pseudonyms_are_a_keyed_hash_of_the_field_and_its_canonical_json() -> None:
    """The derivation is fixed, so rows stay findable across releases."""
    key = bytes(range(32))

    assert pseudonym(key, "nickname", {"b": 1, "a": "é"}) == (
        "pseudonym:fe0fdc5a5fe59c7adbdf51dc8b4b319b"
    )
    assert pseudonym(key, "nickname", "ann") != pseudonym(key, "nickname", "bob")
    assert pseudonym(key, "nickname", None) != pseudonym(key, "nickname", "null")


def test_values_with_lone_surrogates_have_pseudonyms() -> None:
    """JSON can escape half a surrogate pair, a lone surrogate: hashed as is."""
    key = bytes(range(32))
    message = b'nickname\x00"\xed\xa0\x80"'  # The surrogate, UTF-8 style.
    expected = hmac.new(key, message, hashlib.sha256).hexdigest()[:32]

    assert pseudonym(key, "nickname", "\ud800") == f"pseudonym:{expected}"
    assert pseudonym(key, "nickname", "\ud800") != pseudonym(key, "nickname", "\udc00")


async def test_a_lone_surrogate_in_a_personal_field_is_recorded(tables: None) -> None:
    """The event commits; its audit row holds the value's pseudonym."""
    member = uuid.uuid4()

    await commit_events(MemberJoined(member_id=member, email="\ud800", plan="gold"))

    (entry,) = await entries()
    assert entry["details"]["email"] == await pseudonym_of(
        str(member), "email", "\ud800"
    )


async def test_the_day_of_anonymous_callers_is_utcs_wherever_the_server_is(
    tables: None, local_zone: Callable[[str], None]
) -> None:
    """At UTC+14, 23:30 UTC on 2 January is still 2 January's subject."""
    local_zone("Etc/GMT-14")
    late = datetime(2026, 1, 2, 23, 30, tzinfo=UTC)

    await commit_events(MemberJoined(member_id=None, email="x", occurred_at=late))

    assert await subjects() == ["anonymous:2026-01-02"]


async def test_an_excluded_event_does_not_hide_the_next_one(tables: None) -> None:
    """Exclusions skip one event, not the rest of the commit."""
    with override_settings(AUDIT={"EXCLUDE": ["*.member_joined"]}):
        note = Note(id=NoteId(uuid.uuid4()), text="audited")
        note.record(MemberJoined(member_id=None, email="x"))
        note.record(NoteWritten(note_id=note.id))
        async with NotesUnitOfWork() as uow:
            await uow.notes.add(note)
            await uow.commit()

    assert [entry["action"] for entry in await entries()] == [
        "domainkit_tests.note_written"
    ]


def test_auditforget_explains_its_arguments() -> None:
    """``--help`` names the subjects and the retention period."""
    usage = Command().create_parser("manage.py", "auditforget").format_help()

    assert "usage: manage.py auditforget [-h] [--anonymous-days DAYS]" in usage
    assert "[subject ...]" in usage
    assert (
        "keep anonymous callers' addresses for DAYS days (UTC) and forget older ones"
        in " ".join(usage.split())
    )


# What subject keys cost, and how long they are kept --------------------------------


def on_keys(queries: CapturedQueries) -> list[str]:
    """The first word of each statement ``queries`` saw on the keys table."""
    return [
        statement.split(None, 1)[0]
        for statement in queries.statements
        if audit_keys.name in statement
    ]


async def test_known_subjects_cost_one_query_and_new_ones_two(tables: None) -> None:
    """A commit reads every subject's key at once; it inserts the missing ones.

    The insert returns the keys it made: they are not read again.
    """
    member = uuid.uuid4()
    joined = MemberJoined(member_id=member, email="ann@example.com")

    with CapturedQueries() as first:
        await commit_events(joined, actor="alice")
    with CapturedQueries() as known:
        await commit_events(joined, actor="alice")

    assert on_keys(first) == ["SELECT", "INSERT"]  # Alice and the member, new.
    assert on_keys(known) == ["SELECT"]
    entry = (await entries())[-1]
    assert entry["details"]["email"] == await pseudonym_of(
        str(member), "email", "ann@example.com"
    )


async def test_a_transaction_reads_each_subjects_key_once(tables: None) -> None:
    """Keys are kept for the rest of the transaction, and not past it."""
    async with NotesUnitOfWork() as uow:
        with CapturedQueries() as first:
            made = await subject_keys(uow.session, ["ann", "bob"])
        with assert_num_queries(0):
            again = await subject_keys(uow.session, ["bob", "ann"])
        with CapturedQueries() as more:
            more_keys = await subject_keys(uow.session, ["ann", "cid"])
        await uow.commit()
        with CapturedQueries() as next_transaction:
            after = await subject_keys(uow.session, ["ann"])

    assert on_keys(first) == ["SELECT", "INSERT"]
    assert again == made
    assert on_keys(more) == ["SELECT", "INSERT"]  # Only cid, which is new.
    assert more_keys["ann"] == made["ann"]
    assert on_keys(next_transaction) == ["SELECT"]
    assert after == {"ann": made["ann"]}


async def test_forgetting_drops_the_keys_open_transactions_keep(tables: None) -> None:
    """A transaction of this process never uses a key forgotten meanwhile.

    The keys of other subjects, and of later anonymous days, stay kept.
    """
    subjects = ["ann", "bob", "anonymous:2026-01-01", "anonymous:2026-01-02"]
    async with NotesUnitOfWork() as uow:  # Made and committed beforehand.
        await subject_keys(uow.session, subjects)
        await uow.commit()
    async with NotesUnitOfWork() as uow:
        kept = await subject_keys(uow.session, subjects)
        assert await forget("ann") == 1
        assert await forget_anonymous(date(2026, 1, 2)) == 1
        with assert_num_queries(0):
            still = await subject_keys(uow.session, ["bob", "anonymous:2026-01-02"])
        with CapturedQueries() as later:
            renewed = await subject_keys(uow.session, ["ann", "anonymous:2026-01-01"])
        await uow.commit()

    assert on_keys(later) == ["SELECT", "INSERT"]  # Both made anew.
    assert still == {name: kept[name] for name in ("bob", "anonymous:2026-01-02")}
    for subject in ("ann", "anonymous:2026-01-01"):
        assert renewed[subject] != kept[subject]
    assert await pseudonym_of("ann", "email", "a") == pseudonym(
        renewed["ann"], "email", "a"
    )


async def test_no_transaction_gets_a_key_forgotten_while_it_ran(
    tables: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Kept before forget began, or read while it deleted: both are dropped.

    One transaction kept the key before; it looks again once the delete
    committed, before forget returns. Another reads the key while forget
    deletes it; it looks again once forget returns.
    """
    async with NotesUnitOfWork() as uow:
        [old] = (await subject_keys(uow.session, ["ann"])).values()
        await uow.commit()
    delete = personal._delete
    seen: dict[str, bytes] = {}

    async with NotesUnitOfWork() as before, NotesUnitOfWork() as during:
        await subject_keys(before.session, ["ann"])

        async def deleting(condition: Any, *subjects: str) -> int:  # noqa: ANN401
            seen["during, read"] = (await subject_keys(during.session, ["ann"]))["ann"]
            deleted = await delete(condition, *subjects)
            seen["before, again"] = (await subject_keys(before.session, ["ann"]))["ann"]
            await before.session.rollback()  # Lets the other one write on SQLite.
            return deleted

        monkeypatch.setattr(personal, "_delete", deleting)
        assert await forget("ann") == 1
        seen["during, again"] = (await subject_keys(during.session, ["ann"]))["ann"]
        await during.commit()

    assert seen["during, read"] == old  # Read before the delete committed.
    assert seen["before, again"] != old
    assert seen["during, again"] not in {old, seen["before, again"]}
    assert await pseudonym_of("ann", "email", "a") == pseudonym(
        seen["during, again"], "email", "a"
    )


async def test_a_key_read_before_forget_is_not_kept_after_it(
    tables: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A lookup suspended while ``forget`` ran keeps nothing it read.

    It reads Ann's key, then waits to make Zed's; meanwhile ``forget("ann")``
    runs to the end. The lookup's own rows may use what it read, as if it
    had read just before ``forget``; the next lookup reads again.
    """
    async with NotesUnitOfWork() as uow:
        old = (await subject_keys(uow.session, ["ann"]))["ann"]
        await uow.commit()
    make_keys = personal._make_keys
    paused, resume = asyncio.Event(), asyncio.Event()

    async def held_back(
        executor: personal.Executor, subjects: list[str]
    ) -> dict[str, bytes]:
        paused.set()
        await resume.wait()
        return await make_keys(executor, subjects)

    monkeypatch.setattr(personal, "_make_keys", held_back)
    async with NotesUnitOfWork() as uow:
        lookup = asyncio.create_task(subject_keys(uow.session, ["ann", "zed"]))
        await paused.wait()
        assert await forget("ann") == 1
        resume.set()
        first = await lookup
        monkeypatch.setattr(personal, "_make_keys", make_keys)
        again = await subject_keys(uow.session, ["ann"])
        await uow.rollback()

    assert first["ann"] == old
    assert again["ann"] != old


async def lock_waiter_or(task: asyncio.Task[Any], url: str) -> None:
    """Return once ``task`` is done, or a session waits for an advisory lock.

    It asks the server until one is: there is nothing to wait on.
    """
    admin = create_async_engine(url, isolation_level="AUTOCOMMIT")
    waiting = text(
        "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND NOT granted"
    )
    try:
        async with admin.connect() as connection:
            while not task.done() and not await connection.scalar(waiting):
                pass
    finally:
        await admin.dispose()


async def test_forget_never_misses_a_first_key_being_made(
    tables: None, audited: str
) -> None:
    """A writer made Carol's first key, not committed yet: ``forget`` gets it too.

    On PostgreSQL, ``forget`` waits for the writer's commit, on the lock
    that making a key takes; it used to find nothing, and Carol kept a key
    after it returned. SQLite runs one writer at a time: told not to wait
    (``timeout`` 0), ``forget`` is refused until the key is committed.
    """
    await connections.close_all()
    impatient = {"URL": audited, "CONNECT_ARGS": {} if ON_POSTGRES else {"timeout": 0}}
    with override_settings(DATABASES={"default": impatient}):
        async with NotesUnitOfWork() as uow:
            await subject_keys(uow.session, ["carol"])
            forgetting = None
            if ON_POSTGRES:
                forgetting = asyncio.create_task(forget("carol"))
                await lock_waiter_or(forgetting, audited)
            else:
                with pytest.raises(OperationalError, match="database is locked"):
                    await forget("carol")
            await uow.commit()

        assert await (forgetting or forget("carol")) == 1
        assert await pseudonym_of("carol", "email", "c@example.com") is None
        await connections.close_all()


async def test_forget_anonymous_never_misses_a_first_day_key_being_made(
    tables: None, audited: str
) -> None:
    """Every anonymous day shares one lock, which ``forget_anonymous`` takes.

    It cannot know which days a writer is making keys for, so it waits for
    any writer making one, then deletes what is old enough.
    """
    await connections.close_all()
    impatient = {"URL": audited, "CONNECT_ARGS": {} if ON_POSTGRES else {"timeout": 0}}
    with override_settings(DATABASES={"default": impatient}):
        async with NotesUnitOfWork() as uow:
            await subject_keys(uow.session, ["anonymous:2026-01-01"])
            forgetting = None
            if ON_POSTGRES:
                forgetting = asyncio.create_task(forget_anonymous(date(2026, 1, 2)))
                await lock_waiter_or(forgetting, audited)
            else:
                with pytest.raises(OperationalError, match="database is locked"):
                    await forget_anonymous(date(2026, 1, 2))
            await uow.commit()

        assert await (forgetting or forget_anonymous(date(2026, 1, 2))) == 1
        assert await pseudonym_of("anonymous:2026-01-01", "client_ip", IP) is None
        await connections.close_all()


def test_subject_locks_are_stable_64_bit_hashes() -> None:
    """The same everywhere, for every subject, lone surrogates included.

    Anonymous days share one lock; other subjects have their own.
    """
    assert personal._lock_key("ann") == 7366055514945368831
    assert personal._lock_key("\udc80") == -2477758832629098479
    anonymous = {personal._lock_key(f"anonymous:2026-01-0{day}") for day in (1, 2)}
    assert anonymous == {personal._lock_key("anonymous:")}
    assert personal._lock_key("bob") not in {*anonymous, personal._lock_key("ann")}


async def test_only_a_new_subject_takes_its_lock(tables: None) -> None:
    """A known subject costs what it did: one query, and no lock.

    On PostgreSQL a new subject's key is made under its lock: one more
    statement, before the insert.
    """
    member = uuid.uuid4()
    joined = MemberJoined(member_id=member, email="ann@example.com")

    def locks(queries: CapturedQueries) -> int:
        return sum("pg_advisory_xact_lock" in s for s in queries.statements)

    with CapturedQueries() as first:
        await commit_events(joined, actor="alice")
    with CapturedQueries() as known:
        await commit_events(joined, actor="alice")

    assert (locks(first), locks(known)) == ((1 if ON_POSTGRES else 0), 0)
    assert on_keys(known) == ["SELECT"]


async def test_a_key_made_in_a_savepoint_goes_with_it(tables: None) -> None:
    """Rolled back, the key is gone, and made again; the stored one is used."""
    lost: dict[str, bytes] = {}

    async def undone(uow: NotesUnitOfWork) -> None:
        async with uow.savepoint():
            lost.update(await subject_keys(uow.session, ["zed"]))
            raise LookupError("undo")

    async with NotesUnitOfWork() as uow:
        await subject_keys(uow.session, ["ann"])
        with pytest.raises(LookupError, match="undo"):
            await undone(uow)
        with CapturedQueries() as after:
            kept = await subject_keys(uow.session, ["zed"])
        await uow.commit()

    assert on_keys(after) == ["SELECT", "INSERT"]
    assert kept["zed"] != lost["zed"]
    stored = await pseudonym_of("zed", "email", "z")
    assert stored == pseudonym(kept["zed"], "email", "z")


async def test_a_key_another_writer_made_first_is_read(
    tables: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Missed by the read, then refused by the insert: the stored key wins."""
    async with NotesUnitOfWork() as uow:
        stored = await subject_keys(uow.session, ["ann"])
        await uow.commit()
    reads = [personal._read_keys]

    async def missed_once(
        executor: personal.Executor, subjects: Collection[str]
    ) -> dict[str, bytes]:
        if len(reads) == 1:
            reads.append(personal._read_keys)
            return {}  # As if another writer had not committed it yet.
        return await reads[0](executor, subjects)

    monkeypatch.setattr(personal, "_read_keys", missed_once)
    async with NotesUnitOfWork() as uow:
        with CapturedQueries() as raced:
            keys = await subject_keys(uow.session, ["ann"])

    assert on_keys(raced) == ["INSERT", "SELECT"]  # Refused, then read.
    assert keys == stored


async def test_connections_keep_no_keys(tables: None) -> None:
    """Explicit records read their keys in their own short transaction."""
    async with connections.engine().begin() as connection:
        first = await subject_keys(connection, ["ann"])
        with CapturedQueries() as again:
            await subject_keys(connection, ["ann"])

    assert on_keys(again) == ["SELECT"]
    assert first["ann"]
