"""``ObjectRef``: a reference to an aggregate of any installed model."""

from uuid import uuid4

import pytest
from sqlalchemy.dialects import sqlite

from domainkit.contrib.auth import AuthUnitOfWork
from domainkit.db.derive import column_type
from domainkit.db.refs import ObjectRefType, load, model_of, ref_to
from domainkit.ddd.fields import FieldInfo
from domainkit.ddd.refs import ObjectRef
from domainkit.management.base import run_async
from domainkit_tests.sample_domains.shop.models import Product, ProductId
from domainkit_tests.test_db import Note, NoteId


def test_a_reference_names_loads_and_round_trips(auth_project: str) -> None:
    """To an installed aggregate, its class back, and the aggregate itself."""
    lamp = Product(id=ProductId(uuid4()), name="Lamp")
    ref = ref_to(lamp)

    async def store_and_load() -> tuple[Product, object]:
        async with AuthUnitOfWork() as uow:
            uow.session.add(lamp)
            await uow.commit()
        async with AuthUnitOfWork() as uow:
            loaded = await load(uow, ref)
            gone = ObjectRef(model="shop.product", id=str(uuid4()))
            return loaded, await load(uow, gone)

    loaded, missing = run_async(store_and_load())

    assert ref == ObjectRef(model="shop.product", id=str(lamp.id))
    assert model_of(ref) is Product
    assert (loaded.name, missing) == ("Lamp", None)
    assert ObjectRef.parse(str(ref)) == ref
    with pytest.raises(LookupError, match="Note"):
        ref_to(Note(id=NoteId(uuid4()), text="x"))
    with pytest.raises(LookupError, match="No installed model"):
        model_of(ObjectRef(model="shop.nothing", id="1"))


def test_references_are_checked_and_stored_as_text() -> None:
    """``domain.model:id`` in one column; anything else is refused."""
    stored = ObjectRefType()
    ref = ObjectRef(model="billing.invoice", id="42")
    dialect = sqlite.dialect()

    assert stored.process_bind_param(ref, dialect) == "billing.invoice:42"
    assert stored.process_result_value("billing.invoice:42", dialect) == ref
    assert stored.process_bind_param(None, dialect) is None
    assert stored.process_result_value(None, dialect) is None
    assert isinstance(
        column_type(FieldInfo("about", ObjectRef, ObjectRef)), ObjectRefType
    )
    for model, id_ in (
        ("Billing.Invoice", "1"),
        ("invoice", "1"),
        ("a.b", ""),
        ("a.b", "x:y"),
    ):
        with pytest.raises(ValueError, match="ObjectRef"):
            ObjectRef(model=model, id=id_)
    with pytest.raises(ValueError, match="written"):
        ObjectRef.parse("billing.invoice")
