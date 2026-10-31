"""SEC-15a-2: a first key, made by a write open during the erasure, survives it.

ADR 0120 says a write concurrent with an erasure "may leave the write's
value sealed under the key being destroyed: it reads as None". When the
write is the person's first sealed value, its transaction inserts their
key; ``shred_subject`` deletes in another transaction, which on
PostgreSQL does not see (nor wait for) the uncommitted row, so it deletes
nothing. The writer then commits the key: the erased person's card
number reads in clear, and its blind index stays, for good.
"""

import os

import pytest

from domainkit.ddd import F
from domainkit_tests.sample_domains.cards.models import Cardholder
from domainkit_tests.sample_domains.cards.uow import CardsUnitOfWork
from domainkit_tests.test_encrypted_fields import (  # noqa: F401 - the fixture.
    ANN,
    PAN,
    bank,
    raw,
)

pytestmark = pytest.mark.skipif(
    not os.environ.get("DOMAINKIT_TEST_POSTGRES_URL"),
    reason="SQLite serializes the two writers; the race needs PostgreSQL",
)


@pytest.mark.anyio
async def test_a_key_made_while_its_subject_is_erased_is_destroyed(
    bank: str,  # noqa: F811
) -> None:
    """Ann's card, written while she is erased, must not open afterwards."""
    from domainkit.contrib.keys import shred_subject

    async with CardsUnitOfWork() as uow:
        await uow.cardholders.add(Cardholder(id=ANN, name="Ann", card_number=PAN))
        await uow.session.flush()  # Ann's key is made, not yet committed.
        destroyed = await shred_subject(str(ANN))  # privacy erase, meanwhile.
        await uow.commit()
    keys = await raw("SELECT subject FROM domainkit_keys_subject_keys")
    async with CardsUnitOfWork() as uow:
        ann = await uow.cardholders.get(ANN)
        found = await uow.cardholders.filter(F("card_number") == PAN)
    assert (destroyed, keys) != (0, [{"subject": str(ANN)}]), (
        "the erasure destroyed nothing and Ann's key was committed after it"
    )
    assert ann.card_number is None, "Ann's card number reads after her erasure"
    assert found == []
