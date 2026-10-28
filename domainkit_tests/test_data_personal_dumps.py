"""``dumpdata`` of personal data, where deployed (SEC-12a-7).

A dump of a table whose model has fields marked ``Personal`` copies people
out, as ``privacy export`` of one person does, and that export is
recorded. In staging and production such a dump needs a named operator,
and is recorded as ``operator.dumpdata`` with the personal tables it
held; elsewhere, and for tables of no one's data, nothing changes.
"""

import io
import json
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import Column, Integer, MetaData, Table

from domainkit.conf import override_settings
from domainkit.db import fixtures
from domainkit.management import CommandError, call_command
from domainkit.management.base import run_async
from domainkit_tests.sample_domains.patients import tables
from domainkit_tests.test_privacy import audited, clinic, insert_rows  # noqa: F401


@pytest.fixture
def database_url(tmp_path: Path) -> str:
    """SQLite, always: staging refuses PostgreSQL without TLS (security.E007)."""
    return f"sqlite+aiosqlite:///{tmp_path / 'clinic.sqlite3'}"


def dump(*args: str) -> str:
    """Run ``dumpdata``; what it printed."""
    output = io.StringIO()
    call_command("dumpdata", *args, stdout=output)
    return output.getvalue()


def test_personal_dumps_are_named_and_recorded_where_deployed(
    clinic: str,  # noqa: F811 - the fixture.
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Refused without a named operator; recorded with it; elsewhere, as before."""
    monkeypatch.delenv("DOMAINKIT_OPERATOR", raising=False)
    patient = {
        "id": uuid.uuid4(),
        "name": "Ann Example",
        "email": "ann@example.com",
        "registered_at": datetime(2026, 1, 1, tzinfo=UTC),
        "version": 1,
    }
    run_async(insert_rows(tables.patients, patient))
    locally = dump(tables.patients.name)
    with override_settings(ENVIRONMENT="staging"):
        rooms = dump(tables.rooms.name)  # No one's data: as before.
        with pytest.raises(CommandError) as refused:
            dump(tables.patients.name, tables.rooms.name)
        named = dump(tables.patients.name, tables.rooms.name, "--operator", "ops")

    assert "ann@example.com" in locally
    assert json.loads(rooms)["tables"] == [{"table": tables.rooms.name, "rows": []}]
    assert str(refused.value) == (
        "Name the operator who runs dumpdata of personal data in staging: give "
        "--operator NAME, or set DOMAINKIT_OPERATOR."
    )
    assert "ann@example.com" in named
    (row,) = run_async(audited("operator.dumpdata"))
    assert row["actor"] == "operator:ops"
    assert row["details"]["personal_tables"] == [tables.patients.name]
    assert row["details"]["include_guarded"] is False
    assert row["details"]["tables"] == {tables.patients.name: 1, tables.rooms.name: 0}
    assert "ann@example.com" not in json.dumps(row["details"])


def test_a_misplaced_marker_still_marks_personal_data(
    clinic: str,  # noqa: F811 - the fixture.
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``personal_fields`` refusing a model's markers counts as personal data.

    A table no installed model maps holds no one's, as far as a dump knows.
    """

    def misplaced(cls: type) -> dict[str, object]:
        raise TypeError(f"{cls.__qualname__}.notes: Personal is read only on ...")

    plain = Table("framework_test_unmapped", MetaData(), Column("id", Integer))
    assert not fixtures.personal(tables.rooms)
    assert not fixtures.personal(plain)
    monkeypatch.setattr(fixtures, "personal_fields", misplaced)
    assert fixtures.personal(tables.rooms)
