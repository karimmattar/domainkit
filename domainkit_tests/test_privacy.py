"""Privacy: exporting and erasing a person's data, and retention, across domains."""

import io
import json
import os
import stat
import uuid
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, Final

import pytest
from sqlalchemy import Column, Date, PickleType, String, insert, select, text, update
from sqlalchemy.exc import DBAPIError

from domainkit import setup
from domainkit.apps import registry
from domainkit.conf import override_settings
from domainkit.contrib.audit import record
from domainkit.contrib.audit.personal import pseudonym_of
from domainkit.contrib.audit.protection import statements
from domainkit.contrib.audit.tables import audit_keys, audit_log
from domainkit.contrib.privacy import (
    PersonalModel,
    Retention,
    check_subject,
    erase_subject,
    export_subject,
    operations,
    personal_models,
    problems,
    purge,
    retention_policies,
)
from domainkit.contrib.privacy.apps import PrivacyConfig
from domainkit.contrib.privacy.models import ERASED
from domainkit.db import connections
from domainkit.exceptions import ImproperlyConfigured
from domainkit.management import call_command
from domainkit.management.base import CommandError, run_async
from domainkit_tests.conftest import ALPHA
from domainkit_tests.sample_domains.patients import tables
from domainkit_tests.sample_domains.patients.apps import PatientsConfig
from domainkit_tests.sample_domains.patients.models import Visit

AUDIT: Final = "domainkit.contrib.audit"
PRIVACY: Final = "domainkit.contrib.privacy"
PATIENTS: Final = "domainkit_tests.sample_domains.patients"
ODDITIES: Final = "domainkit_tests.sample_domains.oddities"
NOW: Final = datetime(2026, 10, 19, 12, tzinfo=UTC)
ANN: Final = uuid.UUID("00000000-0000-4000-8000-00000000000a")
BOB: Final = uuid.UUID("00000000-0000-4000-8000-00000000000b")


@pytest.fixture
def clinic(database_url: str, tmp_path: Path) -> Iterator[str]:
    """The patients domain, privacy, and the audit trail, migrated."""
    registry.clear()
    with override_settings(
        INSTALLED_DOMAINS=[AUDIT, PRIVACY, PATIENTS, ALPHA],
        DATABASES={"default": {"URL": database_url}},
        MIGRATIONS_DIR=tmp_path / "migrations",
    ):
        setup()
        call_command("makemigrations", "-m", "clinic", stdout=io.StringIO())
        call_command("migrate", stdout=io.StringIO())
        yield database_url
    registry.clear()


async def insert_rows(table: Any, *rows: dict[str, Any]) -> None:  # noqa: ANN401 - a Table.
    """Insert ``rows`` into ``table``."""
    async with connections.engine("default").begin() as connection:
        await connection.execute(insert(table), list(rows))


async def rows_of(table: Any) -> list[dict[str, Any]]:  # noqa: ANN401 - a Table.
    """Every row of ``table``, in key order."""
    async with connections.engine("default").connect() as connection:
        query = select(table).order_by(*table.primary_key.columns)
        return [dict(row) for row in (await connection.execute(query)).mappings()]


async def audited(action: str) -> list[dict[str, Any]]:
    """The audit rows of ``action``, oldest first."""
    async with connections.engine("default").connect() as connection:
        query = (
            select(audit_log)
            .where(audit_log.c.action == action)
            .order_by(audit_log.c.occurred_at)
        )
        return [dict(row) for row in (await connection.execute(query)).mappings()]


def visit(number: int, *, patient: uuid.UUID = ANN, days: int = 0) -> dict[str, Any]:
    """A visit ``days`` before ``NOW``, with notes."""
    return {
        "id": uuid.UUID(int=number),
        "patient_id": patient,
        "visited_at": NOW - timedelta(days=days),
        "notes": f"note {number}",
        "symptoms": f"cough {number}",
        "version": 1,
    }


async def fill() -> None:
    """Two patients, their visits and appointments, cards, badges, and a room."""
    await insert_rows(
        tables.patients,
        {
            "id": ANN,
            "name": "Ann",
            "email": "ann@example.com",
            "registered_at": NOW,
            "version": 1,
        },
        {"id": BOB, "name": "Bob", "email": None, "registered_at": NOW, "version": 1},
    )
    await insert_rows(tables.visits, visit(1), visit(2), visit(3, patient=BOB))
    await insert_rows(
        tables.appointments,
        {
            "id": uuid.UUID(int=10),
            "patient_id": ANN,
            "doctor_id": "dr-who",
            "booked_at": NOW,
            "patient_name": "Ann",
            "doctor_phone": "555-0100",
            "version": 1,
        },
        {
            "id": uuid.UUID(int=11),
            "patient_id": BOB,
            "doctor_id": str(ANN),  # Ann is also a doctor.
            "booked_at": NOW,
            "patient_name": "Bob",
            "doctor_phone": "555-0199",
            "version": 1,
        },
    )
    await insert_rows(
        tables.cards,
        {"id": 1, "holder_id": str(ANN), "number": "4111-0001", "version": 1},
        {"id": 2, "holder_id": str(ANN), "number": "4111-0002", "version": 1},
    )
    await insert_rows(
        tables.badges,
        {
            "id": 7,
            "holder_id": str(ANN),
            "born_on": date(1990, 5, 17),
            "erased_count": 0,
            "version": 1,
        },
    )
    await insert_rows(
        tables.rooms,
        {"id": 1, "name": "Ann's room", "opened_on": date(2020, 1, 1), "version": 1},
    )


# Finding personal data -------------------------------------------------------------


def test_aggregates_with_personal_fields_are_found(clinic: str) -> None:
    """Each marked field, grouped by the field naming its subject."""
    found = {model.key: model for model in personal_models()}

    assert sorted(found) == [
        "patients.appointment",
        "patients.badge",
        "patients.card",
        "patients.patient",
        "patients.visit",
    ]
    appointment = found["patients.appointment"]
    assert dict(appointment.subjects) == {
        "patient_id": ("patient_name",),
        "doctor_id": ("doctor_phone",),
    }
    assert appointment.fields == ("patient_name", "doctor_phone")
    assert appointment.aliases() == ("default",)
    assert found["patients.badge"].erases_itself()
    assert found["patients.badge"].erasures == {}
    assert problems() == []


def test_markers_privacy_cannot_follow_are_problems(clinic: str) -> None:
    """Fields without columns, or without a way to erase them, stop privacy."""
    registry.clear()
    with override_settings(
        INSTALLED_DOMAINS=[AUDIT, PRIVACY, PATIENTS, ODDITIES, ALPHA]
    ):
        setup()
        found = problems()
        with pytest.raises(ImproperlyConfigured, match="Privacy cannot be applied"):
            personal_models()
        assert registry.get_domain_config("privacy").check_application(None) == found  # type: ignore[arg-type]

    assert found == [
        (
            "Basket.lines: Personal is read only on the field itself; mark the whole "
            "field, as Annotated[..., Personal(of=...)]."
        ),
        "Memo.nickname is not mapped to a column.",
        (
            "Note.scan cannot be erased by default (only optional fields and text "
            "can); define erase_personal_data() on Note."
        ),
        (
            "Secret.id cannot be erased by default (only optional fields and text "
            "can); define erase_personal_data() on Secret."
        ),
        (
            "Ticket.code cannot be erased by default (only optional fields and text "
            "can); define erase_personal_data() on Ticket."
        ),
    ]


def test_privacy_needs_the_audit_trail() -> None:
    """Every operation is audited, so the audit trail must be installed."""
    registry.clear()
    with (
        override_settings(INSTALLED_DOMAINS=[PRIVACY, ALPHA]),
        pytest.raises(ImproperlyConfigured, match=r"add 'domainkit\.contrib\.audit'"),
    ):
        setup()
    registry.clear()
    assert PrivacyConfig.databases(None, None) == frozenset()  # type: ignore[arg-type]


# Export ----------------------------------------------------------------------------


def test_a_subjects_data_is_exported_from_every_domain(clinic: str) -> None:
    """Every marked field about the person, with the id of its aggregate, audited."""

    async def export() -> dict[str, Any]:
        await fill()
        return await export_subject(str(ANN))

    exported = run_async(export())
    patients = exported["domains"]["patients"]

    assert exported["subject"] == str(ANN)
    assert datetime.fromisoformat(exported["exported_at"]).tzinfo is not None
    assert patients["patient"] == [
        {"id": str(ANN), "fields": {"name": "Ann", "email": "ann@example.com"}}
    ]
    assert patients["visit"] == [
        {
            "id": str(uuid.UUID(int=n)),
            "fields": {"notes": f"note {n}", "symptoms": f"cough {n}"},
        }
        for n in (1, 2)
    ]
    assert sorted(patients["appointment"], key=lambda r: r["id"]) == [
        {"id": str(uuid.UUID(int=10)), "fields": {"patient_name": "Ann"}},
        {"id": str(uuid.UUID(int=11)), "fields": {"doctor_phone": "555-0199"}},
    ]
    assert [record["fields"] for record in patients["card"]] == [
        {"number": "4111-0001"},
        {"number": "4111-0002"},
    ]
    assert patients["badge"] == [{"id": "7", "fields": {"born_on": "1990-05-17"}}]
    assert "room" not in patients
    rows = run_async(audited("privacy.export"))
    assert [(row["target"], row["details"]) for row in rows] == [
        (str(ANN), {"records": 8})
    ]
    assert rows[0]["actor"] == "system"


def test_subjects_of_another_type_match_nothing_of_that_type(clinic: str) -> None:
    """A subject that is no UUID or number finds only text columns."""

    async def export() -> dict[str, Any]:
        await fill()
        return await export_subject("dr-who")

    assert run_async(export())["domains"] == {
        "patients": {
            "appointment": [
                {"id": str(uuid.UUID(int=10)), "fields": {"doctor_phone": "555-0100"}}
            ]
        }
    }


def test_subjects_are_checked() -> None:
    """One to 255 characters, without control codes."""
    assert check_subject("x" * 255) == "x" * 255
    for bad in ("", "x" * 256, "a\nb", "a\x00b", "a\x7fb"):
        with pytest.raises(ValueError, match="A subject is 1 to 255"):
            check_subject(bad)


def test_subject_values_follow_their_column() -> None:
    """UUIDs and integers are parsed; other types are compared as text."""
    model = PersonalModel(
        "d",
        "m",
        object,
        tables.cards,
        {},
        {
            "number": Column("number", String(10)),
            "id": tables.cards.c.id,
            "blob": Column("blob", PickleType),
        },
    )
    assert operations._subject_value(model, "id", "12") == 12
    assert operations._subject_value(model, "id", "x") is operations._NO_MATCH
    assert operations._subject_value(model, "number", "12") == "12"
    assert operations._subject_value(model, "blob", "12") == "12"


# Erasure ---------------------------------------------------------------------------


def test_a_subjects_data_is_erased_through_units_of_work(clinic: str) -> None:
    """Fields become None, empty, or unique placeholders; others stay; all audited."""

    async def erase() -> tuple[int, int]:
        await fill()
        await record("clinic.viewed", personal={"name": "Ann"}, subject=str(ANN))
        first = await erase_subject(str(ANN), reason="ticket 42")
        again = await erase_subject(str(ANN))
        return first, again

    first, again = run_async(erase())
    patients = run_async(rows_of(tables.patients))
    visits = run_async(rows_of(tables.visits))
    appointments = run_async(rows_of(tables.appointments))
    cards = run_async(rows_of(tables.cards))
    badges = run_async(rows_of(tables.badges))

    assert (first, again) == (8, 1)  # The badge erases itself every time.
    assert [(p["name"], p["email"], p["version"]) for p in patients] == [
        ("", None, 2),
        ("Bob", None, 1),
    ]
    assert [(v["notes"], v["symptoms"]) for v in visits] == [
        (None, None),
        (None, None),
        ("note 3", "cough 3"),
    ]
    assert [(a["patient_name"], a["doctor_phone"]) for a in appointments] == [
        ("", "555-0100"),
        ("Bob", None),
    ]
    numbers = [card["number"] for card in cards]
    assert all(number.startswith(ERASED) for number in numbers)
    assert len(set(numbers)) == 2
    assert all(len(number) == 31 for number in numbers)
    assert [(b["born_on"], b["erased_count"]) for b in badges] == [
        (date(1990, 1, 1), 2)
    ]
    events = run_async(audited("privacy.personal_data_erased"))
    assert len(events) == 9
    assert {
        (event["details"]["model"], tuple(event["details"]["fields"]))
        for event in events
    } >= {
        ("patients.patient", ("name", "email")),
        ("patients.visit", ("notes", "symptoms")),
        ("patients.appointment", ("doctor_phone",)),
        ("patients.card", ("number",)),
        ("patients.badge", ("born_on",)),
    }
    assert {event["details"]["reason"] for event in events} == {"ticket 42", "request"}
    erasures = run_async(audited("privacy.erase"))
    assert [(row["target"], row["details"]) for row in erasures] == [
        (str(ANN), {"aggregates": 8, "reason": "ticket 42"}),
        (str(ANN), {"aggregates": 1, "reason": "request"}),
    ]
    assert run_async(pseudonym_of(str(ANN), "name", "Ann")) is None


def test_erasing_someone_unknown_changes_nothing(clinic: str) -> None:
    """No rows about them: nothing changes, and the request is still recorded."""

    async def erase() -> int:
        await fill()
        return await erase_subject("nobody")

    assert run_async(erase()) == 0
    assert [row["target"] for row in run_async(audited("privacy.erase"))] == ["nobody"]
    assert [p["name"] for p in run_async(rows_of(tables.patients))] == ["Ann", "Bob"]


# Retention -------------------------------------------------------------------------


async def fill_old() -> None:
    """Access logs, daily counts, and visits, old and new."""
    await insert_rows(
        tables.access_log,
        *(
            {"id": n, "patient_id": ANN, "accessed_at": NOW - timedelta(days=days)}
            for n, days in enumerate((1, 29, 31, 40, 400), start=1)
        ),
    )
    await insert_rows(
        tables.daily_counts,
        *(
            {"day": (NOW - timedelta(days=days)).date(), "ward": ward, "count": 3}
            for days in (1, 7, 8, 9)
            for ward in ("a", "b")
        ),
    )
    await insert_rows(
        tables.visits,
        visit(1, days=10),
        visit(2, days=366),
        visit(3, days=400),
        visit(4, days=500) | {"notes": None, "symptoms": None},
        visit(5, days=700) | {"notes": None},
    )


def test_retention_deletes_old_rows_and_erases_old_aggregates(
    clinic: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Rows past ``keep`` go; old visits keep their rows, not their notes."""
    monkeypatch.setattr(operations, "BATCH_SIZE", 2)

    async def apply() -> tuple[list[Any], list[Any]]:
        await fill_old()
        preview = await purge(dry_run=True, now=NOW)
        done = await purge(now=NOW)
        return preview, done

    preview, done = run_async(apply())

    assert [(r.policy, r.action, r.count) for r in preview] == [
        ("patients.patients_access_log", "would delete", 3),
        ("patients.patients_daily_counts", "would delete", 4),
        ("patients.patients_visits", "would erase", 3),
    ]
    assert [(r.policy, r.database, r.action, r.count, r.refused) for r in done] == [
        ("patients.patients_access_log", "default", "deleted", 3, None),
        ("patients.patients_daily_counts", "default", "deleted", 4, None),
        ("patients.patients_visits", "default", "erased", 3, None),
    ]
    assert done[0].before == NOW - timedelta(days=30)
    assert done[1].before == date(2026, 10, 12)
    assert [row["id"] for row in run_async(rows_of(tables.access_log))] == [1, 2]
    days = {row["day"] for row in run_async(rows_of(tables.daily_counts))}
    assert days == {date(2026, 10, 18), date(2026, 10, 12)}
    visits = {
        row["id"].int: (row["notes"], row["symptoms"])
        for row in run_async(rows_of(tables.visits))
    }
    assert visits == {
        1: ("note 1", "cough 1"),
        2: (None, None),
        3: (None, None),
        4: (None, None),
        5: (None, None),
    }
    purges = run_async(audited("privacy.purge"))
    assert [
        (row["target"], row["outcome"], row["details"]["rows"]) for row in purges
    ] == [
        ("patients.patients_access_log", "success", 3),
        ("patients.patients_daily_counts", "success", 4),
        ("patients.patients_visits", "success", 3),
    ]
    assert purges[0]["details"]["operation"] == "deleted"
    erased = run_async(audited("privacy.personal_data_erased"))
    assert {row["details"]["reason"] for row in erased} == {"retention"}
    assert run_async(purge(now=NOW))[2].count == 0  # Nothing left to erase.


def test_refusals_stop_one_table_and_are_recorded(
    clinic: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A table the database refuses to purge is reported; the rest go on."""

    class Refusal(Exception):
        sqlstate = "42501"

    async def refuse(*args: object) -> None:
        raise DBAPIError("DELETE", None, Refusal("append-only"))

    monkeypatch.setattr(operations, "_delete_old", refuse)

    async def apply() -> list[Any]:
        await fill_old()
        return await purge(now=NOW)

    done = run_async(apply())
    assert [(r.policy, r.count, r.refused) for r in done] == [
        (
            "patients.patients_access_log",
            0,
            "append-only: the database refuses to delete its rows",
        ),
        (
            "patients.patients_daily_counts",
            0,
            "append-only: the database refuses to delete its rows",
        ),
        ("patients.patients_visits", 3, None),
    ]
    failures = run_async(audited("privacy.purge"))
    assert [row["outcome"] for row in failures] == ["failure", "failure", "success"]
    assert failures[0]["details"]["reason"].startswith("append-only")

    class Other(Exception):
        sqlstate = "XX000"

    async def fail(*args: object) -> None:
        raise DBAPIError("DELETE", None, Other("broken"))

    monkeypatch.setattr(operations, "_delete_old", fail)
    with pytest.raises(DBAPIError):
        run_async(purge(now=NOW))


@pytest.mark.skipif(
    not os.environ.get("DOMAINKIT_TEST_POSTGRES_URL"),
    reason="set DOMAINKIT_TEST_POSTGRES_URL: the triggers are PostgreSQL's",
)
def test_an_append_only_table_is_never_purged(clinic: str) -> None:
    """PostgreSQL refuses the delete; the purge reports it and deletes nothing."""

    async def apply() -> list[Any]:
        await fill_old()
        async with connections.engine("default").begin() as connection:
            for statement in statements(tables.access_log)[0]:
                await connection.execute(text(statement))
        return await purge(now=NOW)

    done = run_async(apply())

    assert done[0].refused == "append-only: the database refuses to delete its rows"
    assert len(run_async(rows_of(tables.access_log))) == 5
    assert done[1].count == 4


@pytest.mark.parametrize(
    ("retention", "problem"),
    [
        (
            Retention(aggregate=PickleType, field="x", keep=timedelta(days=1)),
            "patients: PickleType is not mapped.",
        ),
        (
            Retention(table="nowhere", field="x", keep=timedelta(days=1)),
            "patients: no table is called 'nowhere'.",
        ),
        (
            Retention(
                table="domainkit_audit_keys", field="created_at", keep=timedelta(days=1)
            ),
            "patients: domainkit_audit_keys is not a table of 'patients'.",
        ),
        (
            Retention(
                table="domainkit_audit_log", field="occurred_at", keep=timedelta(days=1)
            ),
            (
                "patients: domainkit_audit_log is append-only: its rows are never "
                "deleted. Forget people's keys instead (auditforget)."
            ),
        ),
        (
            Retention(
                table="patients_ledger", field="written_at", keep=timedelta(days=1)
            ),
            (
                "patients: patients_ledger is append-only: its rows are never deleted. "
                "Forget people's keys instead (auditforget)."
            ),
        ),
        (
            Retention(
                table="patients_access_log", field="seen_at", keep=timedelta(days=1)
            ),
            "patients: patients_access_log has no column 'seen_at'.",
        ),
        (
            Retention(
                table="patients_access_log", field="patient_id", keep=timedelta(days=1)
            ),
            "patients: patients_access_log.patient_id is not a UTCDateTime or a Date.",
        ),
        (
            Retention(
                table="patients_daily_counts", field="ward", keep=timedelta(days=1)
            ),
            "patients: patients_daily_counts.ward is not a UTCDateTime or a Date.",
        ),
    ],
)
def test_retention_policies_are_checked(
    clinic: str,
    monkeypatch: pytest.MonkeyPatch,
    retention: Retention,
    problem: str,
) -> None:
    """Every policy names a table of its own domain, dated, and not append-only."""
    monkeypatch.setattr(PatientsConfig, "RETENTION", (retention,))

    assert problems() == [problem]
    with pytest.raises(ImproperlyConfigured, match="Privacy cannot be applied"):
        retention_policies()


@pytest.mark.parametrize(
    ("aggregate", "field"),
    [("Appointment", "booked_at"), ("Badge", "born_on"), ("Room", "opened_on")],
)
def test_erasing_retention_needs_fields_that_become_none(
    clinic: str, monkeypatch: pytest.MonkeyPatch, aggregate: str, field: str
) -> None:
    """Text, fields an aggregate erases itself, and no personal fields at all."""
    from domainkit_tests.sample_domains.patients import models

    retention = Retention(
        aggregate=getattr(models, aggregate),
        field=field,
        keep=timedelta(days=1),
        erase=True,
    )
    monkeypatch.setattr(PatientsConfig, "RETENTION", (retention,))

    assert problems() == [
        (
            f"patients: patients_{aggregate.lower()}s: erase=True needs an "
            "aggregate whose Personal fields are all optional, erased to None "
            "without erase_personal_data()."
        )
    ]


def test_columns_without_a_python_type_are_no_dates() -> None:
    """SQLAlchemy cannot tell some columns' types: they cannot date rows."""
    from domainkit.contrib.privacy import models

    assert models._python_type(Column("blob", PickleType)) is None
    assert models._python_type(Column("day", Date)) is date


def test_retention_needs_one_table_or_aggregate() -> None:
    """Exactly one of them, a positive time, and ``erase`` only for aggregates."""
    with pytest.raises(ValueError, match="a table or an aggregate, not both"):
        Retention(field="x", keep=timedelta(days=1))
    with pytest.raises(ValueError, match="a table or an aggregate, not both"):
        Retention(table="t", aggregate=Visit, field="x", keep=timedelta(days=1))
    with pytest.raises(ValueError, match="positive time"):
        Retention(table="t", field="x", keep=timedelta(0))
    with pytest.raises(ValueError, match="name it"):
        Retention(table="t", field="x", keep=timedelta(days=1), erase=True)


# The command -----------------------------------------------------------------------


def run(*args: str) -> str:
    """Run ``privacy`` and return what it printed."""
    output = io.StringIO()
    call_command("privacy", *args, stdout=output)
    return output.getvalue()


def test_the_command_exports(clinic: str, tmp_path: Path) -> None:
    """To standard output, or to a new file only its owner can read."""
    run_async(fill())
    printed = json.loads(run("export", "--subject", str(BOB)))
    target = tmp_path / "bob.json"
    written = run("export", "--subject", str(BOB), "--output", str(target))

    assert printed["domains"]["patients"]["patient"][0]["fields"]["name"] == "Bob"
    assert written == f"Wrote the personal data of {BOB} to {target}.\n"
    assert json.loads(target.read_text("utf-8"))["subject"] == str(BOB)
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    with pytest.raises(CommandError, match="exists; name a new file"):
        run("export", "--subject", str(BOB), "--output", str(target))
    with pytest.raises(CommandError, match="A subject is 1 to 255"):
        run("export", "--subject", "a\nb")


def test_the_command_erases_after_confirmation(
    clinic: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Typing the subject again goes on; anything else cancels."""
    run_async(fill())
    answers = iter(["someone else", str(BOB)])
    monkeypatch.setattr("builtins.input", lambda prompt: next(answers))

    with pytest.raises(CommandError, match="Erasure cancelled"):
        run("erase", "--subject", str(BOB))
    confirmed = run("erase", "--subject", str(BOB), "--reason", "ticket 7")
    unattended = run("erase", "--subject", str(BOB), "--noinput")

    assert confirmed == (
        f"Erased the personal data of {BOB} in 3 aggregates, and forgot their "
        "audit keys.\n"
    )
    assert unattended.startswith(f"Erased the personal data of {BOB} in 0 aggregates")
    assert [p["name"] for p in run_async(rows_of(tables.patients))] == ["Ann", ""]


def test_the_command_purges(clinic: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """One line per table and database; refusals fail the command."""
    run_async(fill_old())
    monkeypatch.setattr(operations, "datetime", FrozenTime)

    preview = run("purge", "--dry-run")
    done = run("purge")

    assert preview.splitlines()[0] == (
        "patients.patients_access_log on default: would delete 3 row(s) from "
        f"before {(NOW - timedelta(days=30)).isoformat()}"
    )
    assert done.splitlines()[2] == (
        "patients.patients_visits on default: erased 3 row(s) from before "
        f"{(NOW - timedelta(days=365)).isoformat()}"
    )

    class Refusal(Exception):
        sqlstate = "23503"

    async def refuse(*args: object) -> None:
        raise DBAPIError("DELETE", None, Refusal("referenced"))

    monkeypatch.setattr(operations, "_delete_old", refuse)
    output = io.StringIO()
    with pytest.raises(
        CommandError, match=r"Not purged: patients\.patients_access_log"
    ):
        call_command("privacy", "purge", stdout=output)
    assert "refused, other rows still refer to them" in output.getvalue()
    monkeypatch.setattr(PatientsConfig, "RETENTION", ())
    assert run("purge") == "No domain declares a RETENTION.\n"


class FrozenTime(datetime):
    """``datetime`` whose ``now`` is ``NOW``."""

    @classmethod
    def now(cls, tz: Any = None) -> "FrozenTime":  # noqa: ANN401 - as datetime's.
        """Always ``NOW``."""
        return cls.fromtimestamp(NOW.timestamp(), tz)


def test_keys_not_about_anyone_stay(clinic: str) -> None:
    """Erasing one person forgets their audit keys only."""

    async def erase() -> list[str]:
        await record("clinic.viewed", personal={"name": "Bob"}, subject=str(BOB))
        await erase_subject(str(ANN))
        async with connections.engine("default").connect() as connection:
            return list(await connection.scalars(select(audit_keys.c.subject)))

    assert run_async(erase()) == [str(BOB)]


def test_versions_guard_against_concurrent_changes(clinic: str) -> None:
    """Erasure writes through optimistic locking, as any change does."""

    async def erase() -> int:
        await fill()
        async with connections.engine("default").begin() as connection:
            await connection.execute(
                update(tables.patients)
                .where(tables.patients.c.id == ANN)
                .values(version=5)
            )
        return await erase_subject(str(ANN))

    assert run_async(erase()) == 8
    assert run_async(rows_of(tables.patients))[0]["version"] == 6
