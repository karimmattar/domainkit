"""Encrypted fields written past the aggregate: bulk updates, unloaded values, reprs."""

import uuid
from typing import Annotated, Any, cast

import pytest
from sqlalchemy import select, update
from sqlalchemy.orm import load_only

from domainkit.ddd import AggregateRoot, Encrypted, F, InvalidQuery
from domainkit_tests.sample_domains.cards.models import Cardholder, Payment
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
async def test_an_orm_update_of_what_the_events_seal_is_refused(
    bank: str,  # noqa: F811
) -> None:
    """Encrypted values, their indexes, subjects, and keys; other columns go."""
    from domainkit.db.encrypted import guarded_attributes
    from domainkit_tests.test_db import Note

    await save(pay(1, note="gift"))
    async with CardsUnitOfWork() as uow:
        for values in (
            {"note": None},
            {Payment.holder_id: BOB},
            {"_pan_index": None},
            {"id": uuid.UUID(int=2)},
        ):
            statement = update(Payment).values(cast(Any, values))
            with pytest.raises(InvalidQuery, match="cannot set"):
                await uow.session.execute(statement)
        ordered = update(Payment).ordered_values(("note", None))
        with pytest.raises(InvalidQuery, match="note"):
            await uow.session.execute(ordered)
        await uow.session.execute(update(Payment).values(version=Payment.version))
        await uow.commit()
    assert guarded_attributes(Note.__mapper__) == frozenset()  # type: ignore[attr-defined]


@pytest.mark.anyio
async def test_update_rows_seals_through_the_orm(bank: str) -> None:  # noqa: F811
    """A new value is sealed for its row and subject, and found by its index."""
    await save(pay(1), Cardholder(id=ANN, name="Ann"))
    async with CardsUnitOfWork() as uow:
        changed = await uow.cardholders.update_rows(F("id") == ANN, card_number=PAN)
        await uow.commit()
    assert changed == 1
    async with CardsUnitOfWork() as uow:
        assert [
            found.id for found in await uow.cardholders.filter(F("card_number") == PAN)
        ] == [ANN]
        assert (await uow.cardholders.get(ANN)).version == 2
    async with CardsUnitOfWork() as uow:
        assert await uow.payments.update_rows(F("holder_id") == BOB, note="x") == 0


@pytest.mark.anyio
async def test_an_unloaded_value_moves_with_its_row_key(bank: str) -> None:  # noqa: F811
    """A new id seals the value again for it; an unloaded None stays None."""
    await save(pay(1, note="gift"), pay(2, holder=BOB))
    async with CardsUnitOfWork() as uow:
        model = cast(Any, Payment)
        query = select(Payment).options(load_only(model.id)).order_by(model.id)
        first, second = (await uow.session.scalars(query)).all()
        first.id = uuid.UUID(int=3)
        second.holder_id = ANN
        await uow.commit()
    async with CardsUnitOfWork() as uow:
        moved = await uow.payments.get(uuid.UUID(int=3))
        unset = await uow.payments.get(uuid.UUID(int=2))
    assert (moved.note, unset.note) == ("gift", None)


def test_an_entity_shows_encrypted_values_masked() -> None:
    """``<encrypted>`` for a value, None as it is, other fields as they are."""
    holder = Cardholder(id=ANN, name="Ann", card_number=PAN)
    shown = repr(holder)
    assert PAN not in shown
    assert "card_number=<encrypted>" in shown
    assert "api_secret=None" in shown
    assert "name='Ann'" in shown
    assert repr(pay(1)).startswith("Payment(id=")

    class Later(AggregateRoot[uuid.UUID]):
        secret: Annotated["Missing | None", Encrypted()] = None  # type: ignore[name-defined]  # noqa: F821

    assert (
        repr(Later(id=ANN))
        == f"{Later.__qualname__}(id={ANN!r}, version=0, secret=None)"
    )


@pytest.mark.anyio
async def test_an_unloaded_value_whose_key_is_gone_stays_gone(
    bank: str,  # noqa: F811
) -> None:
    """Shredded, then moved to another holder unloaded: still None, nothing raised."""
    from domainkit.contrib.keys import shred_subject

    await save(pay(1, note="gift"))
    await shred_subject(str(ANN))
    shredded = (await raw("SELECT note FROM cards_payments"))[0]["note"]
    async with CardsUnitOfWork() as uow:
        model = cast(Any, Payment)
        query = select(Payment).options(load_only(model.holder_id))
        payment = (await uow.session.scalars(query)).one()
        payment.holder_id = BOB
        await uow.commit()
    async with CardsUnitOfWork() as uow:
        assert (await uow.payments.get(uuid.UUID(int=1))).note is None
    # The ciphertext stays as shredded: nothing opens it.
    assert (await raw("SELECT note FROM cards_payments"))[0]["note"] == shredded
