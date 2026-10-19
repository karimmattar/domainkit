"""Records' history from the audit trail: by target or aggregate, with changes."""

import asyncio
import base64
import io
import json
import re
import uuid
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated, Any

import pytest
from fastapi.testclient import TestClient

from domainkit import setup
from domainkit.apps import registry
from domainkit.conf import override_settings
from domainkit.context import RequestContext, bind_request
from domainkit.contrib.audit import forget, record
from domainkit.contrib.audit.history import (
    MAX_LIMIT,
    FieldChange,
    HistoryEntry,
    changes,
    history,
    history_page,
)
from domainkit.contrib.audit.tables import audit_log
from domainkit.db import connections
from domainkit.ddd import DomainEvent, Personal
from domainkit.http import get_asgi_application
from domainkit.management import CommandError, call_command
from domainkit.management.base import run_async
from domainkit_tests.conftest import ALPHA, AUTH, CONTENTTYPES, FAST_HASHER
from domainkit_tests.test_auth_admin import staff
from domainkit_tests.test_db import Note, NoteId, NotesUnitOfWork, notes

AUDIT = "domainkit.contrib.audit"
FLAGS = "domainkit.contrib.flags"
HISTORY = "/admin/audit/history"
MIDDLEWARE = [
    "domainkit.http.middleware.RequestContextMiddleware",
    "domainkit.contrib.auth.middleware.AuthenticationMiddleware",
]


class NoteTitled(DomainEvent):
    """A note got a title; the author's name is personal."""

    note_id: uuid.UUID
    title: str
    author: Annotated[str, Personal(of="note_id")]


def run(*args: str, **options: Any) -> str:  # noqa: ANN401 - command options.
    """Run a command and return what it printed."""
    output = io.StringIO()
    call_command(*args, stdout=output, **options)
    return output.getvalue()


@pytest.fixture
def project(database_url: str, tmp_path: Path) -> Iterator[None]:
    """Auth, audit, and flags migrated, plus the notes table."""
    registry.clear()
    with override_settings(
        INSTALLED_DOMAINS=[CONTENTTYPES, AUTH, AUDIT, FLAGS, ALPHA],
        DATABASES={"default": {"URL": database_url}},
        MIGRATIONS_DIR=tmp_path / "migrations",
        ROOT_ROUTER="domainkit_tests.staff_routes.router",
        MIDDLEWARE=MIDDLEWARE,
        AUTH={"PASSWORD_HASHERS": [FAST_HASHER]},
    ):
        setup()
        run("makemigrations", "-m", "init")
        run("migrate")

        async def create_notes() -> None:
            async with connections.engine().begin() as connection:
                await connection.run_sync(notes.create)
            await connections.close_all()

        asyncio.run(create_notes())
        yield
    registry.clear()


async def write_note(note_id: uuid.UUID, title: str, author: str) -> None:
    """Store or change a note, recording ``NoteTitled`` as user ``u1``."""
    with bind_request(RequestContext("req-1", "203.0.113.5", "PUT", "/notes", "u1")):
        async with NotesUnitOfWork() as uow:
            note = await uow.notes.find(NoteId(note_id))
            if note is None:
                note = Note(id=NoteId(note_id), text=title)
                await uow.notes.add(note)
            note.text = title
            note.record(NoteTitled(note_id=note_id, title=title, author=author))
            await uow.commit()
    await connections.close_all()


@pytest.fixture
def note(project: None) -> uuid.UUID:
    """A note titled three times."""
    note_id = uuid.uuid4()
    for title in ("Draft", "Draft", "Final"):
        run_async(write_note(note_id, title, "Ada Lovelace"))
    run_async(write_note(uuid.uuid4(), "Other", "Grace Hopper"))
    return note_id


def test_an_aggregates_history_lists_its_events_and_changes(note: uuid.UUID) -> None:
    """Newest first; each entry's changes against the one before it."""
    entries = run_async(history(aggregate=("note_id", str(note))))

    assert [entry.details["title"] for entry in entries] == ["Final", "Draft", "Draft"]
    assert {entry.action for entry in entries} == {"domainkit_tests.note_titled"}
    assert entries[0].changes == (FieldChange("title", "Draft", "Final"),)
    assert entries[1].changes == ()
    created = {change.field: change for change in entries[2].changes}
    assert set(created) == {"author", "note_id", "title"}
    assert created["title"] == FieldChange("title", None, "Draft")
    assert created["author"].after.startswith("pseudonym:")  # type: ignore[attr-defined]
    assert entries[0].actor == "u1"
    assert entries[0].database == "default"


def test_personal_values_stay_pseudonyms_after_forgetting(note: uuid.UUID) -> None:
    """Forgetting changes nothing in the history; values never come back."""
    before = run_async(history(aggregate=("note_id", str(note))))
    run_async(forget(str(note)))
    after = run_async(history(aggregate=("note_id", str(note))))

    assert [entry.details for entry in after] == [entry.details for entry in before]
    assert "Ada Lovelace" not in repr(after)


def test_histories_page_newest_first(note: uuid.UUID) -> None:
    """``before`` continues after an entry; ``more`` says whether there are any."""
    first = run_async(history_page(aggregate=("note_id", str(note)), limit=2))
    rest = run_async(
        history_page(
            aggregate=("note_id", str(note)), limit=2, before=first.entries[-1].position
        )
    )

    assert (len(first.entries), first.more) == (2, True)
    assert (len(rest.entries), rest.more) == (1, False)
    assert rest.entries[0].details["title"] == "Draft"
    assert rest.entries[0].changes != ()  # Its own changes; nothing older.


def test_targets_have_histories_too(project: None) -> None:
    """Explicit records name a target; ``before`` and ``after`` give changes."""

    async def records() -> None:
        await record("report.viewed", target="r-1")
        await record(
            "report.renamed",
            target="r-1",
            before={"name": "Q1", "owner": "x"},
            after={"name": "Q2", "owner": "x", "shared": True},
        )
        await record("report.viewed", target="r-2")

    asyncio.run(records())
    entries = run_async(history(target="r-1"))

    assert [entry.action for entry in entries] == ["report.renamed", "report.viewed"]
    assert entries[0].changes == (
        FieldChange("name", "Q1", "Q2"),
        FieldChange("shared", None, True),
    )
    assert run_async(history(target="nobody")) == []


def test_histories_merge_every_audited_database(
    note: uuid.UUID, tmp_path: Path
) -> None:
    """Rows from several databases come back as one history, by time."""
    elsewhere = f"sqlite+aiosqlite:///{tmp_path / 'elsewhere.sqlite3'}"

    async def copy() -> None:
        async with connections.engine("elsewhere").begin() as connection:
            await connection.run_sync(audit_log.create)
        moved = datetime.now(UTC) + timedelta(hours=1)
        async with connections.engine("elsewhere").begin() as connection:
            await connection.execute(
                audit_log.insert().values(
                    id=uuid.uuid4(),
                    occurred_at=moved,
                    actor="u2",
                    action="domainkit_tests.note_titled",
                    outcome="success",
                    details={"note_id": str(note), "title": "Moved"},
                )
            )
        await connections.close_all()

    with override_settings(
        DATABASES={
            "default": {"URL": connections.config().URL.get_secret_value()},
            "elsewhere": {"URL": elsewhere},
        }
    ):
        asyncio.run(copy())
        entries = run_async(
            history(
                aggregate=("note_id", str(note)), databases=["default", "elsewhere"]
            )
        )

    assert [(entry.database, entry.details["title"]) for entry in entries] == [
        ("elsewhere", "Moved"),
        ("default", "Final"),
        ("default", "Draft"),
        ("default", "Draft"),
    ]
    assert entries[0].changes == (FieldChange("title", "Final", "Moved"),)


@pytest.mark.parametrize(
    ("options", "message"),
    [
        ({}, "Name a target, or"),
        ({"target": "a", "aggregate": ("x", "1")}, "Name a target, or"),
        ({"target": "a", "limit": 0}, "1 to 200"),
        ({"target": "a", "limit": 201}, "1 to 200"),
        ({"aggregate": ("x'); --", "1")}, "is not a field name"),
    ],
)
def test_history_queries_are_checked(options: dict[str, Any], message: str) -> None:
    """One subject, a bounded page, and a plain field name."""
    with pytest.raises(ValueError, match=message):
        run_async(history(**options))


def test_changes_compare_with_the_newest_older_value() -> None:
    """Fields compare with the newest older entry that has them."""
    assert changes({"a": 1, "b": 2}, [{"a": 1}, {"b": 1, "a": 0}]) == (
        FieldChange("b", 1, 2),
    )
    assert changes({"before": [], "after": {}}, []) == (
        FieldChange("after", None, {}),
        FieldChange("before", None, []),
    )


# The command ----------------------------------------------------------------------


def test_auditlog_prints_a_history_with_changes(note: uuid.UUID) -> None:
    """``--aggregate FIELD=ID`` and ``--target``, each change under its entry."""
    lines = run("auditlog", "--aggregate", f"note_id={note}").splitlines()

    assert "u1  domainkit_tests.note_titled  success  req-1" in lines[0]
    assert lines[1] == "    title: 'Draft' -> 'Final'"
    assert len([line for line in lines if not line.startswith(" ")]) == 3
    assert run("auditlog", "--target", "nothing") == ""
    assert run("auditlog", "--aggregate", f"note_id={note}", "--database", "default")
    for value, message in [("note_id", "takes FIELD=ID"), ("x y=1", "is not a field")]:
        with pytest.raises(CommandError, match=message):
            run("auditlog", "--aggregate", value)


# The staff endpoint ---------------------------------------------------------------


@pytest.fixture
def client(note: uuid.UUID) -> Iterator[TestClient]:
    """A client of the staff routes, the note's history written."""
    with TestClient(get_asgi_application()) as test_client:
        yield test_client


def test_staff_read_histories_page_by_page(client: TestClient, note: uuid.UUID) -> None:
    """Pages of entries with their changes; the client's address left out."""
    headers = staff("auditor", "audit.view_history")
    query: dict[str, str | int] = {"field": "note_id", "id": str(note), "limit": 2}

    first = client.get(HISTORY, params=query, headers=headers).json()
    rest = client.get(
        HISTORY, params={**query, "cursor": first["next_cursor"]}, headers=headers
    ).json()

    assert [item["details"]["title"] for item in first["items"]] == ["Final", "Draft"]
    assert first["items"][0]["changes"] == [
        {"field": "title", "before": "Draft", "after": "Final"}
    ]
    assert "client_ip" not in first["items"][0]
    assert [item["details"]["title"] for item in rest["items"]] == ["Draft"]
    assert rest["next_cursor"] is None
    by_target = client.get(HISTORY, params={"target": "r-1"}, headers=headers)
    assert by_target.json() == {"items": [], "next_cursor": None}


def test_histories_need_the_audit_permission(client: TestClient) -> None:
    """Anonymous callers get 401; staff without ``audit.view_history`` 403."""
    query = {"target": "r-1"}

    assert client.get(HISTORY, params=query).status_code == 401
    plain = staff("plain")
    assert client.get(HISTORY, params=query, headers=plain).status_code == 403
    granted_but_not_staff = staff("outsider", "audit.view_history", is_staff=False)
    assert (
        client.get(HISTORY, params=query, headers=granted_but_not_staff).status_code
        == 403
    )


def cursor(*values: str) -> str:
    """A cursor holding ``values``, as ``encode_cursor`` writes them."""
    raw = json.dumps(list(values)).encode()
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


@pytest.mark.parametrize(
    ("params", "code"),
    [
        ({}, "invalid_history_query"),
        ({"target": "a", "field": "x", "id": "1"}, "invalid_history_query"),
        ({"field": "x"}, "invalid_history_query"),
        ({"target": "a", "id": "1"}, "invalid_history_query"),
        ({"field": "x y", "id": "1"}, "validation_error"),
        ({"target": "a", "limit": 500}, "validation_error"),
        ({"target": "a", "cursor": "%%%"}, "validation_error"),
        ({"target": "a", "cursor": cursor("x")}, "invalid_cursor"),
        (
            {"target": "a", "cursor": cursor("yesterday", str(uuid.uuid4()))},
            "invalid_cursor",
        ),
        (
            {"target": "a", "cursor": cursor("2026-10-19T10:00:00", str(uuid.uuid4()))},
            "invalid_cursor",
        ),
        (
            {"target": "a", "cursor": cursor("2026-10-19T10:00:00+00:00", "nope")},
            "invalid_cursor",
        ),
    ],
)
def test_history_queries_are_validated(
    client: TestClient, params: dict[str, Any], code: str
) -> None:
    """One subject, plain field names, bounded pages, and cursors we issued."""
    headers = staff("auditor", "audit.view_history")

    response = client.get(HISTORY, params=params, headers=headers)

    assert (response.status_code, response.json()["code"]) == (422, code)


EARLIER = datetime(2026, 10, 19, 10, 0, tzinfo=UTC)
LATER = EARLIER + timedelta(minutes=5)


def identifier(last: int) -> uuid.UUID:
    """A UUID ending in ``last``, to order rows by id."""
    return uuid.UUID(int=last)


async def write_rows(
    *rows: tuple[datetime, int, dict[str, Any]],
    target: str = "t-1",
    alias: str = "default",
) -> None:
    """Insert audit rows for ``target`` as given: time, id, details."""
    async with connections.engine(alias).begin() as connection:
        for at, last, details in rows:
            await connection.execute(
                audit_log.insert().values(
                    id=identifier(last),
                    occurred_at=at,
                    actor="u1",
                    action="test.changed",
                    outcome="success",
                    target=target,
                    details=details,
                )
            )
    await connections.close_all()


@contextmanager
def elsewhere(tmp_path: Path) -> Iterator[None]:
    """A second database, ``elsewhere``, with an audit trail of its own."""
    url = f"sqlite+aiosqlite:///{tmp_path / 'elsewhere.sqlite3'}"
    with override_settings(
        DATABASES={
            "default": {"URL": connections.config().URL.get_secret_value()},
            "elsewhere": {"URL": url},
        }
    ):

        async def create() -> None:
            async with connections.engine("elsewhere").begin() as connection:
                await connection.run_sync(audit_log.create)
            await connections.close_all()

        asyncio.run(create())
        yield


def ids(entries: Sequence[HistoryEntry]) -> list[int]:
    """The rows' ids, as the ints ``identifier`` took."""
    return [entry.id.int for entry in entries]


def test_pages_go_by_time_then_id(project: None) -> None:
    """Rows of one moment page by id; other targets never leak in."""
    asyncio.run(
        write_rows(
            (EARLIER, 0xFF, {}),
            (EARLIER, 0x80, {}),
            (EARLIER, 0x10, {}),
            (LATER, 1, {}),
        )
    )
    asyncio.run(write_rows((EARLIER, 5, {}), target="t-2"))

    first = run_async(history_page(target="t-1", limit=2))
    rest = run_async(
        history_page(target="t-1", limit=2, before=first.entries[-1].position)
    )

    assert (ids(first.entries), first.more) == ([1, 0xFF], True)
    assert (ids(rest.entries), rest.more) == ([0x80, 0x10], False)
    older = run_async(history(target="t-1", before=rest.entries[0].position))
    assert ids(older) == [0x10]


def test_pages_hold_one_to_two_hundred_entries(project: None) -> None:
    """Fifty by default; ``more`` only when more follow."""
    rows: list[tuple[datetime, int, dict[str, Any]]] = [
        (EARLIER + timedelta(seconds=n), n + 1, {}) for n in range(51)
    ]
    asyncio.run(write_rows(*rows))

    assert len(run_async(history(target="t-1"))) == 50
    assert ids(run_async(history(target="t-1", limit=1))) == [51]
    assert len(run_async(history(target="t-1", limit=MAX_LIMIT))) == 51
    exact = run_async(history_page(target="t-1", limit=51))
    assert (len(exact.entries), exact.more) == (51, False)


def test_changes_look_one_entry_past_the_page(project: None, tmp_path: Path) -> None:
    """The last entry of a page compares with the next older one only."""
    with elsewhere(tmp_path):
        asyncio.run(
            write_rows(
                (LATER, 3, {"x": 3}), (EARLIER + timedelta(minutes=1), 2, {"y": 1})
            )
        )
        asyncio.run(write_rows((EARLIER, 1, {"x": 1}), alias="elsewhere"))

        page = run_async(
            history_page(target="t-1", limit=1, databases=["default", "elsewhere"])
        )

    assert page.more
    assert page.entries[0].changes == (FieldChange("x", None, 3),)


def test_entries_say_what_they_are_about(note: uuid.UUID) -> None:
    """The target, and the event an entry records."""
    asyncio.run(record("report.viewed", target="r-1"))

    (viewed,) = run_async(history(target="r-1"))
    titled = run_async(history(aggregate=("note_id", str(note))))[0]

    assert (viewed.target, viewed.event_id) == ("r-1", None)
    assert isinstance(titled.event_id, uuid.UUID)
    assert titled.target is None


@pytest.mark.parametrize(
    ("options", "message"),
    [
        ({}, "Name a target, or an aggregate's field and id."),
        ({"target": "a", "limit": 0}, "A history page holds 1 to 200 entries."),
    ],
)
def test_history_query_errors_say_what_to_do(
    options: dict[str, Any], message: str
) -> None:
    """The whole message."""
    with pytest.raises(ValueError, match=f"^{re.escape(message)}$"):
        run_async(history(**options))


def test_auditlog_reads_one_database_and_filters(project: None, tmp_path: Path) -> None:
    """``--database`` reads one alias; ``--actor``, ``--action``, ``--limit``."""
    with elsewhere(tmp_path):
        asyncio.run(write_rows((EARLIER, 1, {}), (LATER, 2, {})))
        asyncio.run(write_rows((LATER, 3, {}), alias="elsewhere"))
        asyncio.run(record("report.viewed", target="r-1"))

        listed = run("auditlog", "--target", "t-1", "--database", "default")
        assert len(listed.splitlines()) == 2
        lines = run("auditlog", "--target", "t-1", "--database", "elsewhere")
        assert lines == "2026-10-19 10:05:00  u1  test.changed  success  -\n"
        assert len(run("auditlog", "--target", "t-1", "--limit", "1").splitlines()) == 1
        assert run("auditlog", "--action", "report.viewed").count("\n") == 1
        assert "report.viewed" not in run("auditlog", "--actor", "u1")
        assert run("auditlog", "--limit", "1").count("\n") == 1


def test_auditlog_reads_aggregate_ids_after_the_first_equals(project: None) -> None:
    """``FIELD=ID``: the id may hold ``=``; it may not be empty."""
    asyncio.run(write_rows((EARLIER, 1, {"code": "a=b"})))

    assert "test.changed" in run("auditlog", "--aggregate", "code=a=b")
    with pytest.raises(CommandError) as refused:
        run("auditlog", "--aggregate", "code=")
    assert str(refused.value) == "--aggregate takes FIELD=ID, as product_id=42."
