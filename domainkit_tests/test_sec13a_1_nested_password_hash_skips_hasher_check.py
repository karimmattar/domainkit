"""SEC-13a-1: a ``PasswordHash`` inside another value object loads any "hash".

SEC-12a-3's fix lives in ``PasswordHashType.loaded``: a fixture's password
is refused unless ``AUTH.PASSWORD_HASHERS`` would make it, so neither
``plaintext$hunter2`` (a password kept in clear) nor a one-iteration hash
reaches a table. Only the users tables' hand-written ``password`` column
has that type. A ``PasswordHash`` a model holds in a field of its own
derives a plain ``ValueObjectType(PasswordHash, ...)`` (``column_type``
registers nothing for it), whose ``loaded`` checks only the text's shape.

0.13 makes a value object around another load through fixtures, from the
inside out (``Credential(hash: PasswordHash)``, an API client's or a
service account's secret); 0.12 refused such a column outright. Each level
is made with the derived type's ``loaded``, so ``loaddata`` stores
``plaintext$hunter2`` as the credential: the clear-text secret SEC-12a-3
kept out of the users table, through the nested form 0.13 opened. The
same column without the wrapper (a ``PasswordHash`` field derived by
``table_for``) loads it too.
"""

import asyncio
import io
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select

from domainkit import setup
from domainkit.apps import registry
from domainkit.conf import override_settings
from domainkit.contrib.auth.hashers import PBKDF2PasswordHasher
from domainkit.contrib.auth.models import PasswordHash
from domainkit.db import connections
from domainkit.db.derive import table_for
from domainkit.ddd import AggregateRoot, ValueObject
from domainkit.management import CommandError, call_command
from domainkit_tests.conftest import ALPHA, MakePackage

PRODUCTION = {
    "PASSWORD_HASHERS": ["domainkit.contrib.auth.hashers.PBKDF2PasswordHasher"]
}


class Credential(ValueObject):
    """An API client's secret, kept as a hash."""

    hash: PasswordHash


class Client(AggregateRoot[int]):
    """A machine that logs in with a secret."""

    credential: Credential
    secret: PasswordHash


clients = table_for(Client, "framework_test_sec13a_clients", domain="vault")


def run(*args: str) -> str:
    """Run a command and return what it printed."""
    output = io.StringIO()
    call_command(*args, stdout=output)
    return output.getvalue()


async def _rows() -> list[dict[str, Any]]:
    try:
        async with connections.engine().connect() as connection:
            result = await connection.execute(select(clients))
            return [dict(row._mapping) for row in result]
    finally:
        await connections.close_all()


@pytest.fixture
def project(
    database_url: str, tmp_path: Path, make_package: MakePackage
) -> Iterator[None]:
    """Auth and the ``vault`` domain, migrated, under production's hashers."""
    make_package("vault", {})
    registry.clear()
    with override_settings(
        INSTALLED_DOMAINS=[
            "domainkit.contrib.contenttypes",
            "domainkit.contrib.auth",
            ALPHA,
            "vault",
        ],
        DATABASES={"default": {"URL": database_url}},
        MIGRATIONS_DIR=tmp_path / "migrations",
        AUTH=PRODUCTION,
    ):
        setup()
        run("makemigrations", "-m", "init")
        run("migrate")
        yield
    registry.clear()


@pytest.mark.usefixtures("project")
@pytest.mark.parametrize(
    "column", ["credential", "secret"], ids=["nested", "derived-field"]
)
def test_a_fixture_cannot_store_a_clear_password_as_a_hash(
    tmp_path: Path, column: str
) -> None:
    """Refused, as it is in the users table's ``password``."""
    good = PBKDF2PasswordHasher().encode("a-real-secret", "s" * 22)
    row = {"id": 1, "version": 1, "credential": good, "secret": good}
    row[column] = "plaintext$hunter2"
    path = tmp_path / "clients.json"
    path.write_text(
        json.dumps(
            {
                "format": "domainkit.fixture",
                "version": 1,
                "tables": [{"table": clients.name, "rows": [row]}],
            }
        ),
        encoding="utf-8",
    )

    refused: CommandError | None = None
    try:
        run("loaddata", str(path))
    except CommandError as exc:
        refused = exc
    if refused is None:
        stored = asyncio.run(_rows())[0][column]
        hashed = stored.hash if isinstance(stored, Credential) else stored
        pytest.fail(f"loaddata stored a clear-text password: {hashed.value!r}")
    assert f"{clients.name}.{column}" in str(refused)
