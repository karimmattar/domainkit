"""``domainkit.contrib.keys``: KEYS, its checks, its commands, and crypto-shredding."""

import io
import json
import secrets
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date
from pathlib import Path
from typing import Any, Final

import pytest
from pydantic import ValidationError
from sqlalchemy import select, text, update

from domainkit import clock, setup
from domainkit.apps import registry
from domainkit.checks import CheckMessage
from domainkit.clock import freeze_time
from domainkit.conf import override_settings
from domainkit.contrib.audit.tables import audit_log
from domainkit.contrib.keys import (
    KeysSettings,
    key_status,
    reencrypt,
    ring_for,
    shred_subject,
)
from domainkit.contrib.keys.operations import provider_for, subject_value
from domainkit.contrib.keys.tables import subject_keys
from domainkit.contrib.privacy import erase_subject, export_subject
from domainkit.db import connections
from domainkit.db.encrypted import Ciphertext
from domainkit.http.client import stub
from domainkit.keys import LocalKeyProvider
from domainkit.keys.aws import AwsKmsKeyProvider
from domainkit.keys.ring import keyring
from domainkit.keys.vault import VaultTransitKeyProvider
from domainkit.management import call_command
from domainkit.management.base import CommandError, run_async
from domainkit.signing import derive_key
from domainkit_tests.conftest import (
    ALPHA,
    domain_problems,
    domain_warnings,
    start_domain,
)
from domainkit_tests.fake_kms import FakeVault
from domainkit_tests.sample_domains.cards import tables
from domainkit_tests.sample_domains.cards.models import Cardholder, CardNumber, Payment
from domainkit_tests.sample_domains.cards.uow import CardsUnitOfWork
from domainkit_tests.settings import SECRET_KEY

KEYS: Final = "domainkit.contrib.keys"
CARDS: Final = "domainkit_tests.sample_domains.cards"
AUDIT: Final = "domainkit.contrib.audit"
PRIVACY: Final = "domainkit.contrib.privacy"
PAN: Final = "4111111111111111"
ANN: Final = uuid.UUID("00000000-0000-4000-8000-00000000000a")
BOB: Final = uuid.UUID("00000000-0000-4000-8000-00000000000b")


def wrapped() -> str:
    """A new data key, wrapped by the local provider of the tests' ``SECRET_KEY``."""
    kek = derive_key("keys.kek", key=SECRET_KEY)
    return LocalKeyProvider([kek]).wrap_now(secrets.token_bytes(32))


FIRST: Final = wrapped()
SECOND: Final = wrapped()


@contextmanager
def project(url: str, tmp_path: Path, *domains: str, **keys: Any) -> Iterator[None]:  # noqa: ANN401
    """The cards domain, the key app with ``KEYS = keys``, migrated once."""
    registry.clear()
    migrations = tmp_path / "migrations"
    with override_settings(
        INSTALLED_DOMAINS=[*domains, KEYS, CARDS, ALPHA],
        DATABASES={"default": {"URL": url}},
        MIGRATIONS_DIR=migrations,
        KEYS=keys,
    ):
        setup()
        if not migrations.exists():
            call_command("makemigrations", "-m", "bank", stdout=io.StringIO())
            call_command("migrate", stdout=io.StringIO())
        yield
    registry.clear()


async def save(*aggregates: Any) -> None:  # noqa: ANN401
    """Add ``aggregates`` in one unit of work."""
    async with CardsUnitOfWork() as uow:
        for aggregate in aggregates:
            repository = (
                uow.payments if isinstance(aggregate, Payment) else uow.cardholders
            )
            await repository.add(aggregate)
        await uow.commit()


async def rows(query: str) -> list[dict[str, Any]]:
    """Rows of ``query``."""
    async with connections.engine("default").connect() as connection:
        return [dict(row) for row in (await connection.execute(text(query))).mappings()]


def command(*arguments: str) -> str:
    """Run ``keys`` with ``arguments``; what it printed."""
    out = io.StringIO()
    call_command("keys", *arguments, stdout=out)
    return out.getvalue()


# Settings ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("keys", "message"),
    [
        ({"PROVIDER": "aws-kms"}, "needs AWS_KMS"),
        ({"PROVIDER": "vault"}, "needs VAULT"),
        (
            {"PROVIDER": "aws-kms", "AWS_KMS": {"KEY_ID": "k", "REGION": "eu-west-1"}},
            "needs DATA_KEYS",
        ),
        ({"DATA_KEYS": {"a": "vault:v1:x"}}, "a were not wrapped by 'local'"),
        ({"DATA_KEYS": {"a": FIRST}, "CURRENT_KEY": "b"}, "'b' is not in DATA_KEYS"),
        ({"DATA_KEYS": {"a": FIRST, "b": SECOND}}, "must name one of the DATA_KEYS"),
        ({"DATA_KEYS": {"bad id!": FIRST}}, "pattern"),
        ({"AWS_KMS": {"KEY_ID": "k", "REGION": "Europe"}}, "pattern"),
        ({"TIMEOUT": 0}, "greater than 0"),
    ],
)
def test_keys_settings_refuse_what_cannot_work(
    keys: dict[str, Any], message: str
) -> None:
    """A provider needs its settings and its own keys; the current key must exist."""
    with pytest.raises(ValidationError, match=message):
        KeysSettings(**keys)


def test_the_current_key_and_the_retired_ones() -> None:
    """One key is current alone; with several, ``CURRENT_KEY`` says; none: ``dev``."""
    assert KeysSettings().current == "dev"
    assert KeysSettings(DATA_KEYS={"a": FIRST}).current == "a"
    several = KeysSettings(
        DATA_KEYS={"a": FIRST, "c": SECOND, "b": FIRST}, CURRENT_KEY="c"
    )
    assert (several.current, several.retired) == ("c", ("a", "b"))


def test_each_provider_is_made_from_its_settings() -> None:
    """``PROVIDER`` picks the adapter, configured from its section."""
    aws = provider_for(
        KeysSettings(
            PROVIDER="aws-kms",
            AWS_KMS={
                "KEY_ID": "alias/k",
                "REGION": "eu-west-1",
                "ENDPOINT": "https://kms.test",
            },
            DATA_KEYS={"a": "aws-kms:AAAA"},
        )
    )
    assert isinstance(aws, AwsKmsKeyProvider)
    assert (aws.key_id, aws.endpoint) == ("alias/k", "https://kms.test")
    vault = provider_for(
        KeysSettings(
            PROVIDER="vault",
            VAULT={
                "URL": "https://vault.test",
                "KEY": "k",
                "TOKEN": "t",
                "NAMESPACE": "n",
            },
            DATA_KEYS={"a": "vault:v1:x"},
        )
    )
    assert isinstance(vault, VaultTransitKeyProvider)
    assert (vault.key, vault.mount, vault.namespace) == ("k", "transit", "n")
    assert isinstance(provider_for(KeysSettings()), LocalKeyProvider)


def test_without_data_keys_the_ring_has_a_development_key() -> None:
    """``dev``, derived from ``SECRET_KEY``: the same in every process."""
    first, second = ring_for(KeysSettings()), ring_for(KeysSettings())
    assert first.ids == ("dev",)
    assert first.data_key("dev") == second.data_key("dev")
    assert ring_for(KeysSettings(DATA_KEYS={"a": FIRST})).ids == ("a",)


@pytest.mark.parametrize(
    ("value", "column", "expected"),
    [
        (str(ANN), tables.cardholders.c.id, ANN),
        (ANN.hex, tables.cardholders.c.id, None),
        ("x", tables.cardholders.c.id, None),
        ("42", tables.payments.c.pan_index, "42"),
    ],
)
def test_a_subject_is_read_as_its_column_holds_it(
    value: str,
    column: Any,  # noqa: ANN401 - a Column.
    expected: object,
) -> None:
    """Only the form an id prints as matches; text columns take any subject."""
    assert subject_value(column, value) == expected


def test_an_integer_subject_must_be_written_as_it_prints() -> None:
    """``042`` is not 42's subject."""
    from sqlalchemy import Column, Integer, String
    from sqlalchemy.types import TypeDecorator

    class Opaque(TypeDecorator[str]):
        """A type written for SQLAlchemy 2.0: it cannot say what it holds."""

        impl = String
        cache_ok = True

        @property
        def python_type(self) -> type:
            raise NotImplementedError

    assert subject_value(Column("n", Integer), "42") == 42
    assert subject_value(Column("n", Integer), "042") is None
    assert subject_value(Column("p", Opaque()), "x") == "x"


# Checks -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("environment", "keys", "found"),
    [
        ("production", {}, ["keys.E001"]),
        ("staging", {}, ["keys.E001"]),
        ("production", {"ALLOW_LOCAL_KEK": True}, []),
        ("local", {}, []),
        (
            "production",
            {
                "PROVIDER": "vault",
                "VAULT": {"URL": "https://v.test", "KEY": "k", "TOKEN": "t"},
                "DATA_KEYS": {"a": "vault:v1:x"},
            },
            [],
        ),
    ],
)
def test_a_deployment_on_the_local_kek_must_opt_in(
    environment: str, keys: dict[str, Any], found: list[str]
) -> None:
    """``keys.E001``: the KEK would derive from a setting the application holds."""
    registry.clear()
    with override_settings(INSTALLED_DOMAINS=[KEYS, ALPHA], KEYS=keys):
        setup()
        with override_settings(ENVIRONMENT=environment):
            problems = domain_problems("keys", None)
    registry.clear()
    assert [
        problem.id for problem in problems if isinstance(problem, CheckMessage)
    ] == found
    if found:
        assert "ALLOW_LOCAL_KEK" in problems[0].hint  # type: ignore[attr-defined]


def test_retired_keys_are_warned_about() -> None:
    """``keys.W001`` names them, and says how to move what they seal."""
    registry.clear()
    with override_settings(
        INSTALLED_DOMAINS=[KEYS, ALPHA], KEYS={"DATA_KEYS": {"a": FIRST}}
    ):
        setup()
        assert domain_warnings("keys") == []
    with override_settings(
        INSTALLED_DOMAINS=[KEYS, ALPHA],
        KEYS={"DATA_KEYS": {"a": FIRST, "b": SECOND}, "CURRENT_KEY": "b"},
    ):
        setup()
        [warning] = domain_warnings("keys")
    registry.clear()
    assert isinstance(warning, CheckMessage)
    assert warning.id == "keys.W001"
    assert "retired keys (a)" in warning
    assert "keys reencrypt" in warning.hint


@pytest.mark.anyio
async def test_startup_unwraps_the_data_keys() -> None:
    """A provider that refuses stops startup; one that answers loads the ring."""
    fake = FakeVault()
    fake.create("payments")
    vault = {"URL": "https://vault.test", "KEY": "payments", "TOKEN": "s.test-token"}
    provider = VaultTransitKeyProvider(
        "https://vault.test", "payments", token="s.test-token"
    )
    registry.clear()
    with stub(fake):
        made = await provider.generate_data_key()
        with override_settings(
            INSTALLED_DOMAINS=[KEYS, ALPHA],
            KEYS={
                "PROVIDER": "vault",
                "VAULT": vault,
                "DATA_KEYS": {"v": made.wrapped},
            },
        ):
            setup()
            assert not keyring().loaded
            await start_domain("keys")
            assert keyring().data_key("v") == made.plaintext
    registry.clear()


# Commands ---------------------------------------------------------------------------


def test_generate_prints_a_wrapped_key_for_data_keys(
    database_url: str, tmp_path: Path
) -> None:
    """Today's date by default; an id in use or malformed is refused."""
    with project(database_url, tmp_path, DATA_KEYS={"a": FIRST}):
        with freeze_time(date(2026, 10, 31)):
            printed = command("generate")
        entry = json.loads(printed.splitlines()[0])
        [(key_id, key)] = entry.items()
        assert key_id == "2026-10-31"
        assert len(LocalKeyProvider.from_secret_key().unwrap_now(key)) == 32
        assert "CURRENT_KEY'] = '2026-10-31'" in printed
        assert '"b"' in command("generate", "--id", "b")
        with pytest.raises(CommandError, match="already holds 'a'"):
            command("generate", "--id", "a")
        with pytest.raises(CommandError, match="1 to 32 letters"):
            command("generate", "--id", "no spaces")


def test_rewrap_prints_the_same_keys_under_the_newest_kek(
    database_url: str, tmp_path: Path
) -> None:
    """Each data key unwraps to what it did."""
    with project(database_url, tmp_path, DATA_KEYS={"a": FIRST}):
        printed = command("rewrap")
    again = json.loads(printed.rsplit("}", 1)[0] + "}")
    provider = LocalKeyProvider.from_secret_key()
    assert provider.unwrap_now(again["a"]) == provider.unwrap_now(FIRST)
    assert "Replace KEYS['DATA_KEYS']" in printed


def test_a_key_service_that_fails_is_a_command_error(
    database_url: str, tmp_path: Path
) -> None:
    """The provider's error, on one line."""
    vault = {"URL": "https://vault.test", "KEY": "payments", "TOKEN": "bad"}
    fake = FakeVault()
    fake.create("payments")
    registry.clear()
    with (
        override_settings(
            INSTALLED_DOMAINS=[KEYS, ALPHA],
            KEYS={
                "PROVIDER": "vault",
                "VAULT": vault,
                "DATA_KEYS": {"v": "vault:v1:x"},
            },
        ),
        stub(fake),
    ):
        setup()
        with pytest.raises(
            CommandError, match=r"Vault refused datakey/plaintext \(403\)"
        ):
            command("generate", "--id", "w")
    registry.clear()


def test_rotating_the_data_key_and_reencrypting(
    database_url: str, tmp_path: Path
) -> None:
    """Status counts what each key seals; reencrypt moves it all; the old key goes."""
    with project(database_url, tmp_path, DATA_KEYS={"a": FIRST}):
        run_async(
            save(
                Cardholder(id=ANN, name="Ann", card_number=PAN, api_secret=b"x"),
                Cardholder(id=BOB, name="Bob"),
                Payment(
                    id=uuid.UUID(int=1), holder_id=ANN, pan=CardNumber(PAN), note="n"
                ),
                Payment(id=uuid.UUID(int=2), holder_id=BOB, pan=CardNumber("5500")),
            )
        )
        assert "retired: none" in command("status", "--check")
    rotated = {"DATA_KEYS": {"a": FIRST, "b": SECOND}, "CURRENT_KEY": "b"}
    with project(database_url, tmp_path, **rotated):
        status = command("status")
        assert "Current key: b; retired: a." in status
        assert (
            "default cards_payments.pan: b: 0, a: 2; subjects' keys: 0; "
            "indexes to remake: 2" in status
        )
        assert (
            "default cards_cardholders.card_number: b: 0, a: 0; subjects' keys: 1; "
            "indexes to remake: 1" in status
        )
        assert "default subjects' keys wrapped under a: 1" in status
        with pytest.raises(CommandError, match=r"keys\.W001: 7 value"):
            command("status", "--check")
        with pytest.raises(CommandError, match="at least 1"):
            command("reencrypt", "--batch-size", "0")
        printed = command("reencrypt", "--batch-size", "1")
        assert printed.strip() == (
            "Sealed 3 value(s) again, remade 3 blind index(es), and wrapped 1 subject "
            "key(s) under b."
        )
        assert "retired: a." in command("status", "--check")
        assert command("reencrypt").startswith("Sealed 0 value(s)")
    with project(database_url, tmp_path, DATA_KEYS={"b": SECOND}):
        holders, payments = run_async(read_all())
        assert [holder.card_number for holder in holders] == [PAN, None]
        assert [payment.pan for payment in payments] == [
            CardNumber(PAN),
            CardNumber("5500"),
        ]
        assert payments[0].note == "n"
        assert "retired: none" in command("status", "--check")


async def read_all() -> tuple[list[Cardholder], list[Payment]]:
    """Every card holder and payment, by id, and Ann's payment found by number."""
    async with CardsUnitOfWork() as uow:
        holders = await uow.cardholders.filter()
        payments = await uow.payments.filter()
        from domainkit.ddd import F

        assert [p.id for p in await uow.payments.filter(F("pan") == PAN)] == [
            uuid.UUID(int=1)
        ]
    return holders, payments


def test_reencrypt_leaves_and_counts_what_does_not_open(
    database_url: str, tmp_path: Path
) -> None:
    """A tampered value, or a subject's key, is never changed; the command fails."""
    with project(database_url, tmp_path, DATA_KEYS={"a": FIRST}):
        run_async(
            save(
                Payment(id=uuid.UUID(int=1), holder_id=ANN, pan=CardNumber(PAN)),
                Payment(
                    id=uuid.UUID(int=2), holder_id=BOB, pan=CardNumber(PAN), note="x"
                ),
            )
        )
        run_async(tamper())
    with project(
        database_url, tmp_path, DATA_KEYS={"a": FIRST, "b": SECOND}, CURRENT_KEY="b"
    ):
        with pytest.raises(CommandError, match="1 value"):
            command("reencrypt")
        status = run_async(key_status(keyring()))
        # The tampered value and its index, and the two broken subjects' keys.
        assert status.on_retired == 4
        assert status.subject_keys == {"default": {"a": 2}}


def test_a_row_changed_while_it_is_sealed_again_is_left_for_the_next_run(
    database_url: str, tmp_path: Path
) -> None:
    """The update names the value it read: one changed meanwhile is not overwritten."""
    from domainkit.contrib.keys.operations import Reencrypted

    with project(database_url, tmp_path, DATA_KEYS={"a": FIRST}):
        run_async(
            save(Payment(id=uuid.UUID(int=1), holder_id=ANN, pan=CardNumber(PAN)))
        )
        read = run_async(rows("SELECT id, pan FROM cards_payments"))[0]
        run_async(change_pan())
    with project(
        database_url, tmp_path, DATA_KEYS={"a": FIRST, "b": SECOND}, CURRENT_KEY="b"
    ):
        done = Reencrypted()
        stale = [(uuid.UUID(str(read["id"])), read["pan"])]
        run_async(rewrite(stale, done))
        assert (done.values, done.indexes, done.failed) == (0, 0, 0)


async def change_pan() -> None:
    """Give payment 1 another card number, through its aggregate."""
    async with CardsUnitOfWork() as uow:
        payment = await uow.payments.get(uuid.UUID(int=1))
        payment.pan = CardNumber("5500")
        await uow.commit()


async def rewrite(stale: list[tuple[uuid.UUID, str]], done: Any) -> None:  # noqa: ANN401
    """Seal ``stale`` rows of payments again, as ``reencrypt`` would."""
    from domainkit.contrib.keys.operations import _reencrypt_rows

    async with connections.engine("default").begin() as connection:
        await connection.run_sync(
            _reencrypt_rows,
            keyring(),
            tables.payments,
            tables.payments.c.pan,
            tables.payments.c.pan_index,
            stale,
            done,
        )


async def tamper() -> None:
    """Copy payment 1's number into payment 2, and break one subject's key."""
    async with connections.engine("default").begin() as connection:
        first = (
            await connection.execute(
                select(tables.payments.c.pan).where(
                    tables.payments.c.id == uuid.UUID(int=1)
                )
            )
        ).scalar_one()
        await connection.execute(
            update(tables.payments)
            .where(tables.payments.c.id == uuid.UUID(int=2))
            .values(pan=Ciphertext(first))
        )
        await connection.execute(update(subject_keys).values(key=b"\x00" * 60))
        await connection.execute(
            subject_keys.insert().values(
                id="f" * 32,
                subject="carol",
                wrapping_key="a",
                key=b"\x00" * 60,
                created_at=clock.now(),
            )
        )


def test_status_counts_values_under_no_key_held(
    database_url: str, tmp_path: Path
) -> None:
    """A value sealed under a key the ring lost is named as such."""
    with project(database_url, tmp_path, DATA_KEYS={"a": FIRST}):
        run_async(
            save(Payment(id=uuid.UUID(int=1), holder_id=ANN, pan=CardNumber(PAN)))
        )
    with project(database_url, tmp_path, DATA_KEYS={"b": SECOND}):
        status = command("status")
        assert "under no key held: 1" in status
        assert run_async(key_status(keyring())).unreadable == 1


# Crypto-shredding through privacy ---------------------------------------------------


@pytest.mark.anyio
async def test_erasing_a_person_destroys_their_keys(
    database_url: str, tmp_path: Path
) -> None:
    """Privacy erases the marked fields, then the key app destroys Ann's keys."""
    with project(database_url, tmp_path, AUDIT, PRIVACY):
        await save(
            Cardholder(id=ANN, name="Ann", card_number=PAN, passport="P1"),
            Cardholder(id=BOB, name="Bob", card_number="5500", passport="P2"),
        )
        exported = await export_subject(str(ANN))
        [record] = exported["domains"]["cards"]["cardholder"]
        assert record["fields"] == {"name": "Ann", "passport": "P1"}
        assert await erase_subject(str(ANN)) == 1
        async with CardsUnitOfWork() as uow:
            ann = await uow.cardholders.get(ANN)
            bob = await uow.cardholders.get(BOB)
        assert (ann.name, ann.card_number, ann.passport) == ("", None, None)
        assert (bob.card_number, bob.passport) == ("5500", "P2")
        keys = await rows("SELECT subject FROM domainkit_keys_subject_keys")
        assert [row["subject"] for row in keys] == [str(BOB)]
        async with connections.engine("default").connect() as connection:
            details = (
                await connection.execute(
                    select(audit_log.c.details).where(
                        audit_log.c.action == "privacy.copies_erased"
                    )
                )
            ).scalar_one()
        assert details["domain"] == "keys"
        assert details["copies"] == {"subject_keys": 1}


@pytest.mark.anyio
async def test_shredding_reaches_every_hosting_database(
    database_url: str, tmp_path: Path
) -> None:
    """``shred_subject`` counts the keys it destroyed; nothing for a stranger."""
    with project(database_url, tmp_path):
        await save(Cardholder(id=ANN, name="Ann", card_number=PAN))
        assert await shred_subject("stranger") == 0
        assert await shred_subject(str(ANN)) == 1
        assert await reencrypt(keyring()) is not None


def test_a_subjects_key_erased_while_it_is_stored_is_given_up_on() -> None:
    """The insert lost to a key that was gone when read: tried again, then refused."""
    from domainkit.contrib.keys.store import KeyStoreConflict, TableSubjectKeyStore
    from domainkit.keys.ring import StoredSubjectKey

    class Found:
        def first(self) -> None:
            return None

    class Losing:
        """A connection on which every insert conflicts, and nothing is found."""

        class dialect:
            name = "sqlite"

        def __init__(self) -> None:
            self.statements = 0

        def execute(self, statement: object) -> Found:
            self.statements += 1
            return Found()

    connection = Losing()
    key = StoredSubjectKey("a" * 32, "ann", "k", b"\x00" * 60)
    with pytest.raises(KeyStoreConflict, match="3 attempts"):
        TableSubjectKeyStore().add(connection, key)  # type: ignore[arg-type]
    assert connection.statements == 6


def test_two_writers_of_a_subjects_first_key_both_keep_the_first(
    database_url: str, tmp_path: Path
) -> None:
    """The second insert conflicts and returns the key stored first."""
    from domainkit.contrib.keys.store import TableSubjectKeyStore
    from domainkit.keys.ring import StoredSubjectKey

    first = StoredSubjectKey("a" * 32, "ann", "dev", b"\x01" * 60)
    second = StoredSubjectKey("b" * 32, "ann", "dev", b"\x02" * 60)

    async def store_both() -> tuple[StoredSubjectKey, StoredSubjectKey]:
        store = TableSubjectKeyStore()
        async with connections.engine("default").begin() as connection:
            made = await connection.run_sync(store.add, first)
            kept = await connection.run_sync(store.add, second)
        return made, kept

    with project(database_url, tmp_path):
        made, kept = run_async(store_both())
    assert made is first
    assert (kept.id, kept.wrapped) == (first.id, first.wrapped)
