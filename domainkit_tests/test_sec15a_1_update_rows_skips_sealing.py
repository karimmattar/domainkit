"""SEC-15a-1: ``update_rows`` changes a subject or clears a value without ``_seal``.

``update_rows`` (and any ORM-enabled ``update()``) writes in one statement:
the mapper's ``before_update`` event, which seals again when the subject
changes and clears the blind index when the value goes, never runs. So:

- a payment moved to Bob keeps its note sealed under Ann's key: erasing
  Bob leaves it readable, erasing Ann destroys Bob's note;
- a card number set to None keeps its blind index: the row still matches
  ``F("card_number") == PAN``, and the index outlives the value.

The ORM path misses it too when the value is not loaded (``load_only``,
``defer``): ``_seal`` reads ``state.dict.get(key)`` as None, so a change
of subject leaves the value under the old person's key (and would set a
blind index to None).
"""

import uuid

import pytest

from domainkit.ddd import F
from domainkit_tests.sample_domains.cards.models import Cardholder
from domainkit_tests.sample_domains.cards.uow import CardsUnitOfWork
from domainkit_tests.test_encrypted_fields import (  # noqa: F401 - the fixture.
    ANN,
    BOB,
    PAN,
    bank,
    pay,
    raw,
    save,
)


@pytest.mark.anyio
async def test_erasing_the_new_holder_shreds_a_value_moved_by_update_rows(
    bank: str,  # noqa: F811
) -> None:
    """After the payment moved to Bob, erasing Bob must make its note unreadable."""
    from domainkit.contrib.keys import shred_subject

    await save(pay(1, note="Bob's gift", holder=ANN))
    async with CardsUnitOfWork() as uow:
        moved = await uow.payments.update_rows(
            F("id") == uuid.UUID(int=1), holder_id=BOB
        )
        assert moved == 1
        await uow.commit()
    await shred_subject(str(BOB))  # Bob asks to be erased.
    async with CardsUnitOfWork() as uow:
        payment = await uow.payments.get(uuid.UUID(int=1))
    assert payment.holder_id == BOB
    assert payment.note is None, "Bob's note survives Bob's erasure"


@pytest.mark.anyio
async def test_a_value_cleared_by_update_rows_loses_its_blind_index(
    bank: str,  # noqa: F811
) -> None:
    """A number set to None no longer matches, and leaves no index behind."""
    await save(Cardholder(id=ANN, name="Ann", card_number=PAN))
    async with CardsUnitOfWork() as uow:
        await uow.cardholders.update_rows(F("id") == ANN, card_number=None)
        await uow.commit()
    rows = await raw("SELECT card_number, card_number_index FROM cards_cardholders")
    assert rows[0]["card_number"] is None
    async with CardsUnitOfWork() as uow:
        found = await uow.cardholders.filter(F("card_number") == PAN)
    assert found == [], "a row whose number is None still matches the old number"
    assert rows[0]["card_number_index"] is None


@pytest.mark.anyio
async def test_a_subject_change_reseals_a_value_that_was_not_loaded(
    bank: str,  # noqa: F811
) -> None:
    """``_seal`` reads an unloaded attribute as None, and so leaves it under Ann."""
    from sqlalchemy import select
    from sqlalchemy.orm import load_only

    from domainkit.contrib.keys import shred_subject
    from domainkit_tests.sample_domains.cards.models import Payment

    await save(pay(1, note="Bob's gift", holder=ANN))
    async with CardsUnitOfWork() as uow:
        query = select(Payment).options(load_only(Payment.holder_id))  # type: ignore[arg-type]
        payment = (await uow.session.scalars(query)).one()
        payment.holder_id = BOB
        await uow.commit()
    await shred_subject(str(BOB))
    async with CardsUnitOfWork() as uow:
        payment = await uow.payments.get(uuid.UUID(int=1))
    assert payment.holder_id == BOB
    assert payment.note is None, "Bob's note survives Bob's erasure"
