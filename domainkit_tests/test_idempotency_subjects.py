"""The kept responses a subject's export finds, and an erasure drops."""

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any, Final

import pytest
from sqlalchemy import insert, select

from domainkit.conf import override_settings
from domainkit.contrib.idempotency.operations import erase_subject, export_subject
from domainkit.contrib.idempotency.tables import idempotency_keys
from domainkit.db import connections

pytestmark = pytest.mark.anyio
US: Final = "\x1f"
"""The separator of a subject tag (``domainkit.ddd.personal.SEPARATOR``)."""
EARLY: Final = datetime(2026, 10, 1, 9, tzinfo=UTC)
LATE: Final = EARLY + timedelta(hours=1)


def kept(key: str, created_at: datetime, subjects: str, body: bytes) -> dict[str, Any]:
    """A key row holding a response about ``subjects``."""
    return {
        "key_hash": key,
        "fingerprint": "f" * 64,
        "status_code": 201,
        "headers": {"content-type": "application/json"},
        "body": body,
        "created_at": created_at,
        "expires_at": created_at + timedelta(days=1),
        "committed_at": created_at,
        "token": "t" * 32,
        "locked_until": created_at,
        "subjects": subjects,
    }


@pytest.fixture
async def responses(database_url: str) -> AsyncIterator[None]:
    """Ann's own response (not UTF-8), Bob's about Ann, older, and ANN's own."""
    with override_settings(DATABASES={"default": {"URL": database_url}}):
        async with connections.engine().begin() as connection:
            await connection.run_sync(idempotency_keys.create)
            await connection.execute(
                insert(idempotency_keys),
                [
                    kept("own", LATE, f"{US}ann{US}{US}", b'{"note": "\xff"}'),
                    kept("about", EARLY, f"{US}bob{US}{US}ann{US}", b"{}"),
                    kept("other", EARLY, f"{US}ANN{US}{US}", b"{}"),
                ],
            )
        yield
        await connections.close_all()


async def test_an_export_gives_the_subjects_own_responses_oldest_first(
    responses: None,
) -> None:
    """Others' responses about the subject are withheld; bytes not UTF-8 replaced."""
    exported = await export_subject("default", "ann")

    assert exported == [
        {
            "created_at": EARLY.isoformat(),
            "status_code": 201,
            "body": None,
            "withheld": True,
        },
        {
            "created_at": LATE.isoformat(),
            "status_code": 201,
            "body": '{"note": "�"}',
            "withheld": False,
        },
    ]


async def test_an_erasure_drops_only_the_responses_naming_the_subject(
    responses: None,
) -> None:
    """A subject differing only in case is another subject: its response stays."""
    dropped = await erase_subject("default", "ann")

    async with connections.engine().connect() as connection:
        rows = (
            await connection.execute(
                select(idempotency_keys.c.key_hash, idempotency_keys.c.body).order_by(
                    idempotency_keys.c.key_hash
                )
            )
        ).all()
    assert dropped == 2
    assert [tuple(row) for row in rows] == [
        ("about", None),
        ("other", b"{}"),
        ("own", None),
    ]
