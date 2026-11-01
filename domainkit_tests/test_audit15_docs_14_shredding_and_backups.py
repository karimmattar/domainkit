"""DOCS-14: crypto-shredding reaches a backup only once the erasure is applied to it.

A subject's key is stored in ``domainkit_keys_subject_keys``, "in the same
database and transaction as the row", wrapped under a data key that stays
in ``KEYS.DATA_KEYS``. A backup taken before the erasure holds the
ciphertext and the key that opens it. The 0.15 audit found the docs
promising that erasure reached "every backup"; the owner decided that the
docs state the limit instead, and that a restore applies the recorded
erasures again (``docs/contrib/keys.md``, "Backups taken before an
erasure"). This test pins both: restored, the backup opens the person's
card number; erased again, as the guide says to after a restore, it reads
as None.
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
async def test_a_backup_from_before_an_erasure_opens_until_the_erasure_is_applied(
    database_url: str, tmp_path: Path
) -> None:
    """keys.md: a backup from before an erasure opens; erasing again shreds it."""
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
        assert restored.card_number == PAN  # The documented limit.

        assert await shred_subject(str(ANN)) == 1  # The recorded erasure, again.
        async with CardsUnitOfWork() as uow:
            assert (await uow.cardholders.get(ANN)).card_number is None
