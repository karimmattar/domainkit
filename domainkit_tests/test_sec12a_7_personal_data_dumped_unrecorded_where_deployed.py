"""SEC-12a-7: ``dumpdata`` copies everyone's personal data out unrecorded, deployed.

0.12 records a dump of guarded tables (``operator.dumpdata``) and wants a
named operator for it in staging and production: "dumpdata copied guarded
tables out unrecorded" (ADR 0100, amended). A dump of tables whose fields
are marked ``Personal`` stays unrecorded, by anyone, "as reads are not".
But ``privacy export`` of one person, a read, is recorded
(``privacy.export``); ``dumpdata patients_patients`` in production writes
every patient's name and address to a terminal or a file, with no named
operator and no row in the audit trail to say who took them or when.
Where deployed, a dump of personal data must name its operator and be
recorded, as the export of one person's is.
"""

import io
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest

from domainkit.conf import override_settings
from domainkit.management import CommandError, call_command
from domainkit.management.base import run_async
from domainkit_tests.sample_domains.patients import tables
from domainkit_tests.test_privacy import audited, clinic, insert_rows  # noqa: F401


@pytest.fixture
def database_url(tmp_path: Path) -> str:
    """SQLite, always: staging refuses PostgreSQL without TLS (security.E007)."""
    return f"sqlite+aiosqlite:///{tmp_path / 'clinic.sqlite3'}"


def test_a_dump_of_personal_data_is_recorded_where_deployed(
    clinic: str,  # noqa: F811 - the fixture.
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Refused without a named operator, or recorded."""
    monkeypatch.delenv("DOMAINKIT_OPERATOR", raising=False)
    patient = {
        "id": uuid.uuid4(),
        "name": "Ann Example",
        "email": "ann@example.com",
        "registered_at": datetime(2026, 1, 1, tzinfo=UTC),
        "version": 1,
    }
    run_async(insert_rows(tables.patients, patient))
    output = io.StringIO()
    with override_settings(ENVIRONMENT="staging"):
        try:
            call_command("dumpdata", tables.patients.name, stdout=output)
        except CommandError:
            return  # Refused without a named operator: fixed.

    assert "ann@example.com" in output.getvalue()  # Every patient, copied out.
    assert run_async(audited("operator.dumpdata")), (
        "staging dumped every patient's personal data, and nothing recorded it"
    )
