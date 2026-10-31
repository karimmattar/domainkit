"""Fields encrypted at rest: sealed by the ORM, bound to their row, unread in SQL."""

import io
import json
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Annotated, Any, Final, cast

import pytest
from sqlalchemy import Column, Integer, MetaData, Table, Uuid, select, text, update
from sqlalchemy import inspect as sa_inspect
from sqlalchemy.dialects import sqlite
from sqlalchemy.exc import StatementError

from domainkit import setup
from domainkit.apps import registry
from domainkit.conf import override_settings
from domainkit.db import EncryptedColumns, EncryptedType, connections, table_for
from domainkit.db import fixtures as fixture_io
from domainkit.db.encrypted import (
    Ciphertext,
    Plaintext,
    _fields,
    context,
    lookup,
    open_row,
    seal_row,
)
from domainkit.db.query import compile_criteria, compile_ordering
from domainkit.ddd import (
    AggregateRoot,
    DuplicateValue,
    Encrypted,
    F,
    InvalidQuery,
    Sensitive,
    ValueObject,
)
from domainkit.ddd.query import Condition, Lookup, Ordering, Selection
from domainkit.encryption import DecryptionFailed
from domainkit.exceptions import ImproperlyConfigured
from domainkit.management import call_command
from domainkit.management.base import run_async
from domainkit.resources import schema_for
from domainkit_tests.conftest import ALPHA
from domainkit_tests.sample_domains.cards import tables
from domainkit_tests.sample_domains.cards.models import Cardholder, CardNumber, Payment
from domainkit_tests.sample_domains.cards.tables import start_mappers
from domainkit_tests.sample_domains.cards.uow import CardsUnitOfWork

KEYS: Final = "domainkit.contrib.keys"
CARDS: Final = "domainkit_tests.sample_domains.cards"
PAN: Final = "4111111111111111"
OTHER_PAN: Final = "5500000000000004"
ANN: Final = uuid.UUID("00000000-0000-4000-8000-00000000000a")
BOB: Final = uuid.UUID("00000000-0000-4000-8000-00000000000b")


def pay(
    number: int, pan: str = PAN, *, holder: uuid.UUID = ANN, note: str | None = None
) -> Payment:
    """Payment ``number`` by ``holder``'s card ``pan``."""
    return Payment(
        id=uuid.UUID(int=number), holder_id=holder, pan=CardNumber(pan), note=note
    )


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


async def raw(query: str) -> list[dict[str, Any]]:
    """Rows of ``query``, as the database holds them."""
    async with connections.engine("default").connect() as connection:
        return [dict(row) for row in (await connection.execute(text(query))).mappings()]


async def save(*aggregates: Any) -> None:  # noqa: ANN401
    """Add ``aggregates`` in one unit of work."""
    async with CardsUnitOfWork() as uow:
        for aggregate in aggregates:
            repository = (
                uow.payments if isinstance(aggregate, Payment) else uow.cardholders
            )
            await repository.add(aggregate)
        await uow.commit()


# What the database holds ------------------------------------------------------------


@pytest.mark.anyio
async def test_a_card_number_is_unreadable_in_the_database(bank: str) -> None:
    """The done-when test: a PAN read straight from the database shows no plaintext."""
    await save(
        Cardholder(
            id=ANN, name="Ann", card_number=PAN, passport="P123", api_secret=b"s3"
        ),
        pay(1, note="gift"),
    )
    rows = await raw("SELECT * FROM cards_cardholders") + await raw(
        "SELECT * FROM cards_payments"
    )
    dump = json.dumps(rows, default=str)
    for secret in (PAN, "P123", "gift"):
        assert secret not in dump
    assert rows[0]["card_number"].startswith("dk1:s.")
    assert rows[0]["passport"].startswith("dk1:s.")  # Personal: sealed for Ann.
    assert rows[0]["api_secret"].startswith("dk1:k.dev:")
    assert rows[1]["pan"].startswith("dk1:k.dev:")
    assert rows[1]["pan_index"].startswith("dev:")
    async with CardsUnitOfWork() as uow:
        ann = await uow.cardholders.get(ANN)
        payment = await uow.payments.get(uuid.UUID(int=1))
    assert (ann.card_number, ann.passport, ann.api_secret) == (PAN, "P123", b"s3")
    assert (payment.pan, payment.note) == (CardNumber(PAN), "gift")


@pytest.mark.anyio
async def test_none_is_stored_as_null(bank: str) -> None:
    """A missing value is NULL, with no index, and reads back as None."""
    await save(Cardholder(id=ANN, name="Ann"))
    row = (await raw("SELECT * FROM cards_cardholders"))[0]
    assert (row["card_number"], row["card_number_index"], row["passport"]) == (
        None,
        None,
        None,
    )
    async with CardsUnitOfWork() as uow:
        assert (await uow.cardholders.get(ANN)).card_number is None


@pytest.mark.anyio
async def test_a_value_copied_to_another_row_or_column_does_not_open(bank: str) -> None:
    """Bound to its table, column and row: moved, it fails to load."""
    await save(pay(1), pay(2, OTHER_PAN, holder=BOB))
    first = (await raw("SELECT pan FROM cards_payments ORDER BY id"))[0]["pan"]
    async with connections.engine("default").begin() as connection:
        await connection.execute(
            update(tables.payments)
            .where(tables.payments.c.id == uuid.UUID(int=2))
            .values(pan=Ciphertext(first))
        )
    with pytest.raises(DecryptionFailed, match="another row"):
        async with CardsUnitOfWork() as uow:
            await uow.payments.get(uuid.UUID(int=2))
    await save(Cardholder(id=BOB, name="Bob", card_number=PAN))
    async with connections.engine("default").begin() as connection:
        await connection.execute(
            update(tables.cardholders).values(passport=tables.cardholders.c.card_number)
        )
    with pytest.raises(DecryptionFailed, match="another row or column"):
        async with CardsUnitOfWork() as uow:
            await uow.cardholders.get(BOB)


# Writing ----------------------------------------------------------------------------


@pytest.mark.anyio
async def test_a_changed_value_is_sealed_again_and_others_are_left_alone(
    bank: str,
) -> None:
    """Changing the name rewrites no ciphertext; changing the number does."""
    await save(Cardholder(id=ANN, name="Ann", card_number=PAN, api_secret=b"x"))
    before = (await raw("SELECT * FROM cards_cardholders"))[0]
    async with CardsUnitOfWork() as uow:
        ann = await uow.cardholders.get(ANN)
        ann.name = "Ann B."
        await uow.commit()
    renamed = (await raw("SELECT * FROM cards_cardholders"))[0]
    assert renamed["card_number"] == before["card_number"]
    assert renamed["api_secret"] == before["api_secret"]
    async with CardsUnitOfWork() as uow:
        ann = await uow.cardholders.get(ANN)
        ann.card_number = OTHER_PAN
        await uow.commit()
    assert ann.card_number == OTHER_PAN  # The aggregate holds its value again.
    changed = (await raw("SELECT * FROM cards_cardholders"))[0]
    assert changed["card_number"] != before["card_number"]
    assert changed["card_number_index"] != before["card_number_index"]
    assert changed["api_secret"] == before["api_secret"]
    async with CardsUnitOfWork() as uow:
        assert (await uow.cardholders.get(ANN)).card_number == OTHER_PAN


@pytest.mark.anyio
async def test_a_new_subject_seals_the_value_again_under_their_key(bank: str) -> None:
    """The note moves to Bob's key when the payment moves to Bob."""
    await save(pay(1, note="gift"))
    before = (await raw("SELECT note FROM cards_payments"))[0]["note"]
    async with CardsUnitOfWork() as uow:
        payment = await uow.payments.get(uuid.UUID(int=1))
        payment.holder_id = BOB
        await uow.commit()
    after = (await raw("SELECT note FROM cards_payments"))[0]["note"]
    assert after.split(":")[1] != before.split(":")[1]
    keys = await raw("SELECT subject FROM domainkit_keys_subject_keys ORDER BY subject")
    assert [row["subject"] for row in keys] == [str(ANN), str(BOB)]


@pytest.mark.anyio
async def test_a_sealed_value_set_on_purpose_is_written_as_it_is(bank: str) -> None:
    """An attribute given a ``Ciphertext`` (its own row's) is not sealed twice."""
    await save(pay(1, note="gift"))
    sealed = (await raw("SELECT note FROM cards_payments"))[0]["note"]
    async with CardsUnitOfWork() as uow:
        payment = await uow.payments.get(uuid.UUID(int=1))
        payment.note = Ciphertext(sealed)
        await uow.commit()
    assert (await raw("SELECT note FROM cards_payments"))[0]["note"] == sealed


@pytest.mark.anyio
async def test_a_failed_flush_gives_the_aggregate_its_value_back(bank: str) -> None:
    """The ciphertext never stays in the attribute: the duplicate keeps its card."""
    await save(pay(1))
    again = pay(1)
    with pytest.raises(DuplicateValue):
        await save(again)
    assert again.pan == CardNumber(PAN)


@pytest.mark.anyio
async def test_a_refreshed_row_is_opened_again(bank: str) -> None:
    """Expired and refreshed, the attribute holds the value, not the ciphertext."""
    await save(pay(1, note="gift"))
    async with connections.session("default") as session:
        payment = await session.get(Payment, uuid.UUID(int=1))
        assert payment is not None
        session.expire(payment)
        await session.refresh(payment)
        assert (payment.pan, payment.note) == (CardNumber(PAN), "gift")


@pytest.mark.anyio
async def test_a_row_needs_its_key_before_it_is_written(bank: str) -> None:
    """An encrypted value is bound to the key: none, and the write is refused."""
    with pytest.raises(ImproperlyConfigured, match="give it its id before saving"):
        await save(
            Payment(id=cast(uuid.UUID, None), holder_id=ANN, pan=CardNumber(PAN))
        )


# Querying ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_equality_goes_through_the_blind_index(bank: str) -> None:
    """``==`` and ``in_`` find rows by index; None and ``isnull`` by the column."""
    await save(
        pay(1),
        pay(2, OTHER_PAN, holder=BOB),
        Cardholder(id=ANN, name="Ann", card_number=PAN),
        Cardholder(id=BOB, name="Bob"),
    )

    async def ids(criteria: Any) -> list[uuid.UUID]:  # noqa: ANN401
        async with CardsUnitOfWork() as uow:
            repository = (
                uow.cardholders if "card_number" in repr(criteria) else uow.payments
            )
            return [found.id for found in await repository.filter(criteria)]

    assert await ids(F("pan") == PAN) == [uuid.UUID(int=1)]
    assert await ids(F("pan") == CardNumber(OTHER_PAN)) == [uuid.UUID(int=2)]
    assert await ids(F("pan").in_([PAN, OTHER_PAN])) == [
        uuid.UUID(int=1),
        uuid.UUID(int=2),
    ]
    assert await ids(F("card_number") == PAN) == [ANN]
    assert await ids(F("card_number").isnull()) == [BOB]
    assert await ids(F("card_number").isnull(False)) == [ANN]
    assert await ids(F("card_number") == None) == [BOB]  # noqa: E711


@pytest.mark.anyio
async def test_other_queries_on_an_encrypted_field_are_refused(bank: str) -> None:
    """No text lookups, no ranges, no ordering; no equality without an index."""
    async with CardsUnitOfWork() as uow:
        for criteria in (F("pan").contains("41"), F("pan") > PAN):
            with pytest.raises(InvalidQuery):
                await uow.payments.filter(criteria)
        with pytest.raises(InvalidQuery, match="without a blind index"):
            await uow.cardholders.filter(F("api_secret") == b"x")
        with pytest.raises(InvalidQuery):
            await uow.payments.filter(Selection(ordering=["pan"]))


def test_the_sql_compiler_refuses_what_the_domain_query_would() -> None:
    """Compiled directly, ordering and other lookups are refused too."""
    with pytest.raises(InvalidQuery, match="do not sort"):
        compile_ordering((Ordering("pan"),), Payment)
    with pytest.raises(InvalidQuery, match="ask only whether it is None"):
        compile_criteria(Condition("pan", Lookup.GT, PAN), Payment, "sqlite")


@pytest.mark.anyio
async def test_a_column_compared_with_a_value_refuses_it(bank: str) -> None:
    """Raw SQL that compares the column with a value fails, saying how to look up."""
    statement = select(tables.payments).where(tables.payments.c.pan == PAN)
    async with connections.engine("default").connect() as connection:
        with pytest.raises(StatementError, match="blind index"):
            await connection.execute(statement)


# Keys of people: crypto-shredding ---------------------------------------------------


@pytest.mark.anyio
async def test_a_shredded_subjects_values_read_as_none(bank: str) -> None:
    """Ann's keys destroyed: her number and passport are gone; the rest is not."""
    from domainkit.contrib.keys import shred_subject

    await save(
        Cardholder(id=ANN, name="Ann", card_number=PAN, passport="P1", api_secret=b"a"),
        Cardholder(id=BOB, name="Bob", card_number=OTHER_PAN),
        pay(1, note="gift"),
    )
    before = {row["id"]: row for row in await raw("SELECT * FROM cards_cardholders")}
    assert await shred_subject(str(ANN)) == 1
    assert await shred_subject(str(ANN)) == 0
    after = {row["id"]: row for row in await raw("SELECT * FROM cards_cardholders")}
    ann_row = next(
        row for key, row in after.items() if str(key).replace("-", "") == ANN.hex
    )
    ann_before = next(
        row for key, row in before.items() if str(key).replace("-", "") == ANN.hex
    )
    assert ann_row["card_number"] == ann_before["card_number"]  # Ciphertext stays.
    assert ann_row["card_number_index"] is None  # Its index does not.
    async with CardsUnitOfWork() as uow:
        ann = await uow.cardholders.get(ANN)
        bob = await uow.cardholders.get(BOB)
        payment = await uow.payments.get(uuid.UUID(int=1))
        assert await uow.cardholders.filter(F("card_number") == PAN) == []
    assert (ann.card_number, ann.passport, ann.api_secret) == (None, None, b"a")
    assert bob.card_number == OTHER_PAN
    assert (payment.pan, payment.note) == (CardNumber(PAN), None)
    async with CardsUnitOfWork() as uow:
        ann = await uow.cardholders.get(ANN)
        ann.card_number = PAN  # Ann comes back: a new key.
        await uow.commit()
    async with CardsUnitOfWork() as uow:
        assert (await uow.cardholders.get(ANN)).card_number == PAN


# Fixtures ---------------------------------------------------------------------------


def test_dumpdata_writes_ciphertext_unless_asked_to_decrypt(
    bank: str, tmp_path: Path
) -> None:
    """A dump holds no card number; ``--decrypt`` writes it, and loads back sealed."""
    run_async(
        save(
            Cardholder(id=ANN, name="Ann", card_number=PAN, api_secret=b"\x00\xff"),
            pay(1, note="gift"),
        )
    )
    sealed = io.StringIO()
    call_command("dumpdata", "cards", stdout=sealed)
    assert PAN not in sealed.getvalue()
    opened = io.StringIO()
    call_command("dumpdata", "cards", "--decrypt", stdout=opened)
    document = json.loads(opened.getvalue())
    holders = next(t for t in document["tables"] if t["table"] == "cards_cardholders")
    assert holders["rows"][0]["card_number"] == {"plaintext": PAN}
    assert holders["rows"][0]["api_secret"] == {"plaintext": "AP8="}
    assert holders["rows"][0]["passport"] is None
    for name, text_ in (
        ("sealed.json", sealed.getvalue()),
        ("opened.json", opened.getvalue()),
    ):
        run_async(empty())
        (tmp_path / name).write_text(text_)
        call_command("loaddata", str(tmp_path / name), stdout=io.StringIO())
        ann, payment, found = run_async(read_back())
        assert (ann.card_number, ann.api_secret) == (PAN, b"\x00\xff")
        assert (payment.pan, payment.note) == (CardNumber(PAN), "gift")
        assert [each.id for each in found] == [uuid.UUID(int=1)]


async def empty() -> None:
    """Delete every card holder and payment."""
    async with connections.engine("default").begin() as connection:
        await connection.execute(text("DELETE FROM cards_payments"))
        await connection.execute(text("DELETE FROM cards_cardholders"))


async def read_back() -> tuple[Cardholder, Payment, list[Payment]]:
    """Ann, her payment, and the payments her card number finds."""
    async with CardsUnitOfWork() as uow:
        ann = await uow.cardholders.get(ANN)
        payment = await uow.payments.get(uuid.UUID(int=1))
        found = await uow.payments.filter(F("pan") == PAN)
    return ann, payment, found


@pytest.mark.anyio
async def test_a_shredded_value_dumps_as_null(bank: str) -> None:
    """``--decrypt`` writes None where the subject's key is gone."""
    from domainkit.contrib.keys import shred_subject

    await save(Cardholder(id=ANN, name="Ann", card_number=PAN))
    await shred_subject(str(ANN))
    document = await fixture_io.dump("default", [tables.cardholders], decrypt=True)
    assert document["tables"][0]["rows"][0]["card_number"] is None


@pytest.mark.anyio
async def test_a_value_that_does_not_open_fails_the_decrypted_dump(bank: str) -> None:
    """Tampered: the dump says which table, never the value."""
    await save(pay(1), pay(2, holder=BOB))
    first = (await raw("SELECT pan FROM cards_payments ORDER BY id"))[0]["pan"]
    async with connections.engine("default").begin() as connection:
        await connection.execute(update(tables.payments).values(pan=Ciphertext(first)))
    with pytest.raises(fixture_io.FixtureError, match="'cards_payments' does not open"):
        await fixture_io.dump("default", [tables.payments], decrypt=True)


@pytest.mark.parametrize(
    ("value", "message"),
    [
        ("4111", "takes a sealed value"),
        ({"plaintext": 4111}, "plaintext that is not CardNumber"),
        ({"other": "x"}, "takes a sealed value"),
    ],
)
def test_a_fixture_holds_sealed_values_or_plaintexts_to_seal(
    value: object, message: str
) -> None:
    """Anything else in an encrypted column is refused, without the value."""
    with pytest.raises(fixture_io.FixtureError, match=message) as error:
        fixture_io._decoded_row(tables.payments, {"pan": value})
    assert "4111" not in str(error.value)


def test_a_bytes_plaintext_in_a_fixture_is_base64() -> None:
    """Bytes come as base64; anything else is refused."""
    decoded = fixture_io._decoded_row(
        tables.cardholders, {"api_secret": {"plaintext": "AP8="}}
    )
    assert decoded["api_secret"].value == b"\x00\xff"
    with pytest.raises(fixture_io.FixtureError, match="not bytes"):
        fixture_io._decoded_row(tables.cardholders, {"api_secret": {"plaintext": "!"}})
    decoded = fixture_io._decoded_row(
        tables.cardholders, {"api_secret": {"plaintext": None}}
    )
    assert decoded["api_secret"].value is None
    assert repr(decoded["api_secret"]) == "Plaintext(<hidden>)"


@pytest.mark.anyio
async def test_sealing_a_row_without_the_orm_needs_its_key(bank: str) -> None:
    """``seal_row`` binds to the primary key, and seals None as NULL."""
    async with connections.engine("default").begin() as connection:
        with pytest.raises(ImproperlyConfigured, match="this row lacks"):
            await connection.run_sync(
                seal_row, tables.payments, {"pan": Plaintext(PAN)}
            )
        row = await connection.run_sync(
            seal_row,
            tables.payments,
            {"id": uuid.UUID(int=1), "pan": Plaintext(None), "note": None},
        )
        assert row == {
            "id": uuid.UUID(int=1),
            "pan": None,
            "pan_index": None,
            "note": None,
        }
        opened = await connection.run_sync(open_row, tables.payments, row)
        assert opened == {"pan": None, "note": None}


@pytest.mark.anyio
async def test_migrations_store_sealed_text_and_the_subjects_keys(
    bank: str, tmp_path: Path
) -> None:
    """The revision has text columns, the index, and the keys table: no domain type."""
    revision = next((tmp_path / "migrations").glob("0001_*.py")).read_text()
    assert 'sa.Column("pan", sa.Text(), nullable=False)' in revision
    assert 'sa.Column("pan_index", sa.String(length=72), nullable=True)' in revision
    assert 'op.f("ix_cards_payments_pan_index")' in revision
    assert '"domainkit_keys_subject_keys"' in revision
    assert "EncryptedType" not in revision


# Deriving columns -------------------------------------------------------------------


class Secret(ValueObject, Sensitive):
    """A secret no schema shows."""

    value: str


def test_derived_columns_follow_the_marker() -> None:
    """Text, bytes, value objects; the subject from Personal; the index beside it."""
    holders = tables.cardholders
    assert isinstance(holders.c.card_number.type, EncryptedType)
    assert repr(holders.c.card_number.type) == (
        "EncryptedType(str, subject='id', blind_index='card_number_index')"
    )
    assert repr(holders.c.passport.type) == "EncryptedType(str, subject='id')"
    assert repr(holders.c.api_secret.type) == "EncryptedType(bytes)"
    assert holders.c.card_number_index.info == {"blind_index": "card_number"}
    assert not tables.payments.c.pan.nullable
    start_mappers()
    assert [field.key for field in _fields(sa_inspect(Payment))] == ["pan", "note"]
    assert [type(item).__name__ for item in EncryptedColumns("plain")] == ["Column"]


def test_deriving_refuses_what_cannot_be_sealed_or_shredded() -> None:
    """A subject that is no other field, or on a field that cannot be None; numbers."""
    metadata = MetaData()

    class Wrong(AggregateRoot[uuid.UUID]):
        a: Annotated[str | None, Encrypted(subject="nobody")] = None

    class Kept(AggregateRoot[uuid.UUID]):
        a: Annotated[str, Encrypted(subject="id")]

    class Numbered(AggregateRoot[uuid.UUID]):
        a: Annotated[int, Encrypted()]

    class Itself(AggregateRoot[uuid.UUID]):
        a: Annotated[str | None, Encrypted(subject="a")] = None

    class Unknown(AggregateRoot[uuid.UUID]):
        a: Annotated[Any, Encrypted()]

    for cls, message in (
        (Wrong, "must name another field"),
        (Itself, "must name another field"),
        (Kept, "hint it as allowing None"),
        (Numbered, "holds text or bytes"),
        (Unknown, "names no class"),
    ):
        with pytest.raises(TypeError, match=message):
            table_for(cls, f"t_{cls.__name__}", metadata=metadata)


def test_given_columns_replace_the_derived_ones() -> None:
    """A hand-written value or index column is used as it is."""

    class Holder(AggregateRoot[uuid.UUID]):
        number: Annotated[str | None, Encrypted(blind_index=True)] = None
        code: Annotated[str | None, Encrypted()] = None

    metadata = MetaData()
    columns = EncryptedColumns("number", blind_index=True)
    table = table_for(Holder, "holders", columns.value, metadata=metadata)
    assert table.c.number is columns.value
    assert "number_index" in table.c
    index = Column("number_index", Uuid)
    other = table_for(Holder, "holders2", index, metadata=metadata)
    assert other.c.number_index is index
    assert not other.indexes


def test_an_encrypted_value_object_may_be_sensitive() -> None:
    """Sealed at rest and kept out of schemas: both markers apply."""

    class Vault(AggregateRoot[uuid.UUID]):
        secret: Annotated[Secret | None, Encrypted()] = None

    table = table_for(Vault, "vaults", metadata=MetaData())
    type_ = cast(EncryptedType, table.c.secret.type)
    assert type_.from_bytes(type_.to_bytes(Secret("s"))) == Secret("s")
    with pytest.raises(TypeError, match="holds a secret"):
        schema_for(Vault)
    assert "secret" not in schema_for(Vault, exclude={"secret"}).model_fields


def test_an_encrypted_column_takes_its_kind_only() -> None:
    """``to_bytes`` refuses another kind; text keeps lone surrogates."""
    numbers = EncryptedType(CardNumber)
    assert numbers.to_bytes(PAN) == numbers.to_bytes(CardNumber(PAN)) == PAN.encode()
    type_ = EncryptedType(str)
    assert type_.from_bytes(type_.to_bytes("\ud800")) == "\ud800"
    with pytest.raises(TypeError, match="takes str, not bytes"):
        type_.to_bytes(b"x")
    with pytest.raises(TypeError, match="takes bytes, not str"):
        EncryptedType(bytes).to_bytes("x")
    with pytest.raises(ValueError, match="not text"):
        type_.from_json(3)
    assert type_.process_result_value(None, sqlite.dialect()) is None


def test_a_subject_or_index_column_must_be_mapped() -> None:
    """The mapping names the column it needs."""
    from domainkit.db import map_imperatively

    class Orphan(AggregateRoot[int]):
        value: str | None = None

    metadata = MetaData()
    table = Table(
        "orphans",
        metadata,
        Column("id", Integer, primary_key=True),
        Column("value", EncryptedType(str, subject="owner"), nullable=True),
        Column("owner", Integer),
        Column("version", Integer, nullable=False),
    )
    map_imperatively(Orphan, table, exclude_properties=["owner"])
    with pytest.raises(ImproperlyConfigured, match="which Orphan does not map"):
        _fields(sa_inspect(Orphan))

    class Lonely(AggregateRoot[int]):
        value: str | None = None

    other = Table(
        "lonely",
        metadata,
        Column("id", Integer, primary_key=True),
        Column("value", EncryptedType(str, blind_index="value_index"), nullable=True),
        Column("version", Integer, nullable=False),
    )
    map_imperatively(Lonely, other)
    with pytest.raises(ImproperlyConfigured, match="which lonely does not have"):
        _fields(sa_inspect(Lonely))


def test_lookup_needs_a_blind_index() -> None:
    """``lookup`` on a column without one says so."""
    with pytest.raises(InvalidQuery, match="without a blind index"):
        lookup(tables.cardholders.c.api_secret, [b"x"])


def test_the_context_binds_table_column_and_key() -> None:
    """An id object counts as the id it wraps."""

    class Id(ValueObject):
        value: uuid.UUID

    column = tables.payments.c.pan
    assert context(column, [Id(ANN)]) == context(column, [ANN])
    assert context(column, [ANN]) == f"cards_payments\x00pan\x00{ANN}".encode()


# The admin API ----------------------------------------------------------------------


@pytest.fixture
def staffed_bank(database_url: str, tmp_path: Path) -> Iterator[str]:
    """The bank, with auth for the admin, migrated."""
    from domainkit_tests.conftest import AUTH, CONTENTTYPES, FAST_HASHER

    registry.clear()
    with override_settings(
        INSTALLED_DOMAINS=[CONTENTTYPES, AUTH, KEYS, CARDS, ALPHA],
        AUTH={"PASSWORD_HASHERS": [FAST_HASHER]},
        DATABASES={"default": {"URL": database_url}},
        MIGRATIONS_DIR=tmp_path / "migrations",
    ):
        setup()
        call_command("makemigrations", "-m", "bank", stdout=io.StringIO())
        call_command("migrate", stdout=io.StringIO())
        yield database_url
    registry.clear()


def test_the_admin_api_reads_and_finds_encrypted_fields(staffed_bank: str) -> None:
    """Staff read the card number in clear, and look it up through its index."""
    from domainkit.contrib.admin import AdminSite
    from domainkit.http import FilterSet
    from domainkit.http.filters import Filter
    from domainkit_tests.test_admin import as_, client_of

    class PaymentFilters(FilterSet):
        """Payments by card number."""

        model = Payment
        pan = Filter("exact")

    run_async(save(pay(1), pay(2, OTHER_PAN, holder=BOB)))
    site = AdminSite()
    site.register(
        Payment,
        unit_of_work=CardsUnitOfWork,
        repository=lambda uow: uow.payments,
        label="cards.payment",
        filters=PaymentFilters,
    )
    client = client_of(site)
    listed = client.get("/admin/cards/payments", headers=as_("root"))
    found = client.get(f"/admin/cards/payments?pan={PAN}", headers=as_("root"))
    read = client.get(f"/admin/cards/payments/{uuid.UUID(int=1)}", headers=as_("root"))
    assert [item["pan"] for item in listed.json()["items"]] == [PAN, OTHER_PAN]
    assert [item["id"] for item in found.json()["items"]] == [str(uuid.UUID(int=1))]
    assert read.json()["pan"] == PAN
