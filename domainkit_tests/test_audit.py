"""The audit trail: committed events and explicit records."""

import asyncio
import io
import uuid
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import select

from domainkit import setup
from domainkit.apps import registry
from domainkit.conf import override_settings, settings
from domainkit.context import RequestContext, bind_request
from domainkit.contrib.audit import Outcome, action_for, record
from domainkit.contrib.audit.protection import PROTECT, UNPROTECT, protect, unprotect
from domainkit.contrib.audit.tables import audit_log
from domainkit.db import connections
from domainkit.management import call_command
from domainkit_tests.sample_domains.alpha.events import GreetingSent
from domainkit_tests.test_db import Note, NoteId, NotesUnitOfWork, NoteWritten, notes

pytestmark = pytest.mark.anyio

ALPHA = "domainkit_tests.sample_domains.alpha"
AUDIT = "domainkit.contrib.audit"


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
    with bind_request(RequestContext("req-9", "203.0.113.5", "POST", "/notes", actor)):
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
    assert (entry["request_id"], entry["client_ip"]) == ("req-9", "203.0.113.5")
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
    assert action_for(GreetingSent) == "alpha.greeting_sent"
    assert action_for(NoteWritten) == "domainkit_tests.note_written"


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
