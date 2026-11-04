"""On a schema per tenant, erasing a person reaches the keys in each tenant's.

The audit found ``shred_subject``, ``keys status``, and ``keys reencrypt``
working in the default schema only: an erased person's key stayed in the
tenant's schema, and their values still opened (A4-1).
"""

import io
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import text

from domainkit import setup
from domainkit.apps import registry
from domainkit.conf import override_settings
from domainkit.db import connections
from domainkit.management import call_command
from domainkit.tenancy import tenant

pytestmark = pytest.mark.anyio

ANN = uuid.UUID("00000000-0000-4000-8000-00000000000a")
PAN = "4111111111111111"


@pytest.fixture
def bank(database_url: str, tmp_path: Path) -> Iterator[None]:
    """Cards with encrypted numbers, in tenant acme's schema of a new database."""
    if not database_url.startswith("postgresql"):
        pytest.skip(
            "schemas per tenant need PostgreSQL: set DOMAINKIT_TEST_POSTGRES_URL"
        )
    registry.clear()
    with override_settings(
        INSTALLED_DOMAINS=[
            "domainkit.contrib.keys",
            "domainkit_tests.sample_domains.cards",
            "domainkit_tests.sample_domains.alpha",
        ],
        DATABASES={"default": {"URL": database_url}},
        MIGRATIONS_DIR=tmp_path / "migrations",
        TENANCY={"SCHEMA_DATABASES": ["default"]},
    ):
        setup()
        call_command("makemigrations", "-m", "bank", stdout=io.StringIO())
        call_command("migrate", "--tenant", "acme", stdout=io.StringIO())
        yield
    registry.clear()


async def test_erasure_reaches_a_tenants_schema(bank: None) -> None:
    """The key in tenant acme's schema is destroyed; the card reads as None."""
    from domainkit.contrib.keys import shred_subject
    from domainkit_tests.sample_domains.cards.models import Cardholder
    from domainkit_tests.sample_domains.cards.uow import CardsUnitOfWork

    try:
        with tenant("acme"):
            async with CardsUnitOfWork() as uow:
                await uow.cardholders.add(
                    Cardholder(id=ANN, name="Ann", card_number=PAN)
                )
                await uow.commit()

        destroyed = await shred_subject(str(ANN))

        async with connections.engine("default").connect() as connection:
            left = (
                await connection.execute(
                    text("SELECT subject FROM tenant_acme.domainkit_keys_subject_keys")
                )
            ).all()
        with tenant("acme"):
            async with CardsUnitOfWork() as uow:
                ann = await uow.cardholders.get(ANN)
    finally:
        await connections.close_all()

    assert destroyed == 1
    assert left == []
    assert ann.card_number is None
