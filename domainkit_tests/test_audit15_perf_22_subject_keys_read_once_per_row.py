"""PERF-22: reading a page of rows sealed for subjects reads each subject's key alone.

A field ``Encrypted(subject=...)`` (or ``Personal`` and ``Encrypted``) is
opened as its row loads (``domainkit.db.encrypted._open``), and the
subject's key is read from ``domainkit_keys_subject_keys`` by
``Keyring._subject_key_by_id``, one ``SELECT`` per key not yet held by
the transaction. A list of N people, each the subject of their own row,
so costs 1 + N statements: an N+1 that a ``ResourceRouter`` page of 50
(or 200) customers, an export, or a report pays on every request. On a
read without a transaction (``Reads.STATEMENTS``) nothing is held at all,
so each row (and each field) reads its key again.

The right behaviour: the keys a result's rows need are read together,
in one statement (or a bounded few), whatever the number of rows.
"""

import io
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Final

import pytest

from domainkit import setup
from domainkit.apps import registry
from domainkit.conf import override_settings
from domainkit.management import call_command
from domainkit.testing import assert_max_queries
from domainkit_tests.conftest import ALPHA
from domainkit_tests.sample_domains.cards.models import Cardholder
from domainkit_tests.sample_domains.cards.uow import CardsUnitOfWork

KEYS: Final = "domainkit.contrib.keys"
CARDS: Final = "domainkit_tests.sample_domains.cards"
PEOPLE: Final = 30


@pytest.fixture
def bank(database_url: str, tmp_path: Path) -> Iterator[str]:
    """The cards domain and the key ring, migrated."""
    registry.clear()
    with override_settings(
        INSTALLED_DOMAINS=[KEYS, CARDS, ALPHA],
        DATABASES={"default": {"URL": database_url}},
        MIGRATIONS_DIR=tmp_path / "migrations",
    ):
        setup()
        call_command("makemigrations", "-m", "bank", stdout=io.StringIO())
        call_command("migrate", stdout=io.StringIO())
        yield database_url
    registry.clear()


@pytest.mark.anyio
async def test_a_page_of_people_reads_their_keys_in_a_bounded_number_of_queries(
    bank: str,
) -> None:
    """Thirty card holders listed in one unit of work: not thirty-one statements."""
    async with CardsUnitOfWork() as uow:
        for number in range(PEOPLE):
            await uow.cardholders.add(
                Cardholder(
                    id=uuid.UUID(int=number + 1),
                    name=f"Person {number}",
                    card_number=f"41111111111{number:05d}",
                )
            )
        await uow.commit()
    with assert_max_queries(3) as captured:
        async with CardsUnitOfWork() as uow:
            people = await uow.cardholders.filter()
    assert len(people) == PEOPLE
    assert {person.card_number for person in people} == {
        f"41111111111{number:05d}" for number in range(PEOPLE)
    }
    assert len(captured) <= 3
