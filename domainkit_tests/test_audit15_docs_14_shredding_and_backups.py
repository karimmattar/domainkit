"""DOCS-14: crypto-shredding does not reach a backup taken before the erasure.

``docs/contrib/keys.md`` promises that erasing a person destroys their key
so that "every copy of what it sealed, backups included, is then
unreadable", and that "every value sealed under those keys reads as None,
in the database and in every backup or dump of it"; ``docs/privacy.md``
repeats it ("in backups, replicas, and dumps too"). But a subject's key is
stored in ``domainkit_keys_subject_keys``, "in the same database and
transaction as the row", wrapped under a data key that stays in
``KEYS.DATA_KEYS`` (and in the KMS). A backup taken before the erasure
holds the ciphertext and the key that opens it, and the data keys still
unwrap that key: restoring it, or reading it with the service's keys,
gives the person's card number back.

The test takes such a backup the way point-in-time recovery would give it
back: the rows the erasure deleted (the subject's key) are put back as they
were, nothing else changes, and the card number is read through the
domain. What the docs promise is that it reads as None.
"""

from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import insert, select

from domainkit.contrib.keys import shred_subject
from domainkit.contrib.keys.tables import subject_keys
from domainkit.db import connections
from domainkit_tests.sample_domains.cards.models import Cardholder
from domainkit_tests.sample_domains.cards.uow import CardsUnitOfWork
from domainkit_tests.test_keys_app import ANN, PAN, project, save


async def backup_of_keys() -> list[dict[str, Any]]:
    """The subjects' keys as a backup taken now holds them."""
    async with connections.engine("default").connect() as connection:
        return [
            dict(row)
            for row in (await connection.execute(select(subject_keys))).mappings()
        ]


async def restore_keys(rows: list[dict[str, Any]]) -> None:
    """Put the backed-up key rows back, as restoring that backup does."""
    async with connections.engine("default").begin() as connection:
        await connection.execute(insert(subject_keys), rows)


@pytest.mark.anyio
async def test_a_backup_from_before_an_erasure_does_not_open_the_persons_values(
    database_url: str, tmp_path: Path
) -> None:
    """keys.md: once erased, a value "reads as None ... in every backup"."""
    with project(database_url, tmp_path):
        await save(Cardholder(id=ANN, name="Ann", card_number=PAN))
        backup = await backup_of_keys()
        assert [row["subject"] for row in backup] == [str(ANN)]

        assert await shred_subject(str(ANN)) == 1
        async with CardsUnitOfWork() as uow:
            assert (await uow.cardholders.get(ANN)).card_number is None

        await restore_keys(backup)  # The database as the backup holds it.
        async with CardsUnitOfWork() as uow:
            restored = await uow.cardholders.get(ANN)

        assert restored.card_number is None, (
            "a backup taken before the erasure still opens the erased person's "
            "card number: their key is in it, wrapped under a data key the "
            "service still holds"
        )
