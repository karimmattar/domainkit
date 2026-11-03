"""``domainkit.contrib.keys``: KEYS, its checks, its commands, and crypto-shredding."""

import io
import json
import os
import secrets
import uuid
from collections.abc import Iterator
from contextlib import contextmanager, redirect_stdout
from datetime import date, datetime
from pathlib import Path
from typing import Any, Final, cast

import pytest
from pydantic import ValidationError
from sqlalchemy import func, select, text, update

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
    if keys.get("DATA_KEYS"):  # These tests have no dev values to keep.
        keys.setdefault("KEEP_DEVELOPMENT_KEY", False)
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
    assert (several.current, several.retired) == ("c", ("a", "b", "dev"))
    dropped = KeysSettings(DATA_KEYS={"a": FIRST}, KEEP_DEVELOPMENT_KEY=False)
    assert (dropped.retired, dropped.development_key) == ((), False)
    kms = {
        "PROVIDER": "vault",
        "VAULT": {"URL": "https://v.test", "KEY": "k", "TOKEN": "t"},
        "DATA_KEYS": {"a": "vault:v1:x"},
    }
    assert KeysSettings(**kms).retired == ()
    assert KeysSettings(**kms, KEEP_DEVELOPMENT_KEY=True).retired == ("dev",)


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
    assert (aws.region, aws.client.timeout) == ("eu-west-1", 5.0)
    vault = provider_for(
        KeysSettings(
            PROVIDER="vault",
            VAULT={
                "URL": "https://vault.test",
                "KEY": "k",
                "TOKEN": "t",
                "NAMESPACE": "n",
                "MOUNT": "keys/transit",
            },
            DATA_KEYS={"a": "vault:v1:x"},
            TIMEOUT=2.5,
        )
    )
    assert isinstance(vault, VaultTransitKeyProvider)
    assert (vault.key, vault.mount, vault.namespace) == ("k", "keys/transit", "n")
    assert (vault.url, vault.client.timeout) == ("https://vault.test", 2.5)
    assert isinstance(provider_for(KeysSettings()), LocalKeyProvider)


def test_without_data_keys_the_ring_has_a_development_key() -> None:
    """``dev``, derived from ``SECRET_KEY``: the same in every process."""
    first, second = ring_for(KeysSettings()), ring_for(KeysSettings())
    assert first.ids == ("dev",)
    assert first.data_key("dev") == second.data_key("dev")
    assert first.data_key("dev") == derive_key(
        "keys.development-data-key", key=SECRET_KEY
    )
    assert ring_for(KeysSettings(DATA_KEYS={"a": FIRST})).ids == ("a", "dev")
    kept = KeysSettings(DATA_KEYS={"a": FIRST}, KEEP_DEVELOPMENT_KEY=False)
    assert ring_for(kept).ids == ("a",)


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
        assert problems[0] == (
            f"KEYS.PROVIDER is 'local' in {environment}: the key that wraps the "
            "data keys derives from SECRET_KEY, so anyone who reads the settings "
            "can open every encrypted field."
        )
        assert problems[0].hint == (  # type: ignore[attr-defined]
            "Set KEYS.PROVIDER to a key service (aws-kms, vault, gcp-kms, "
            "azure-key-vault, huawei-kms, pkcs11, custom), or "
            "KEYS.ALLOW_LOCAL_KEK = True to accept it."
        )


def test_retired_keys_are_warned_about() -> None:
    """``keys.W001`` names them, and says how to move what they seal."""
    registry.clear()
    with override_settings(
        INSTALLED_DOMAINS=[KEYS, ALPHA],
        KEYS={"DATA_KEYS": {"a": FIRST}, "KEEP_DEVELOPMENT_KEY": False},
    ):
        setup()
        assert domain_warnings("keys") == []
    with override_settings(
        INSTALLED_DOMAINS=[KEYS, ALPHA],
        KEYS={
            "DATA_KEYS": {"a": FIRST, "b": SECOND},
            "CURRENT_KEY": "b",
            "KEEP_DEVELOPMENT_KEY": False,
        },
    ):
        setup()
        [warning] = domain_warnings("keys")
    with override_settings(
        INSTALLED_DOMAINS=[KEYS, ALPHA],
        KEYS={
            "DATA_KEYS": {"a": FIRST, "b": SECOND, "c": wrapped()},
            "CURRENT_KEY": "c",
            "KEEP_DEVELOPMENT_KEY": False,
        },
    ):
        setup()
        [two] = domain_warnings("keys")
    registry.clear()
    assert isinstance(warning, CheckMessage)
    assert warning.id == "keys.W001"
    assert warning == (
        "KEYS.DATA_KEYS keeps retired keys (a): values and subjects' keys may "
        "still be sealed under them."
    )
    assert str(two).startswith("KEYS.DATA_KEYS keeps retired keys (a, b): ")
    assert warning.hint == (
        "Run 'manage.py keys reencrypt', then 'manage.py keys status --check'; "
        "remove the keys once nothing uses them (for dev, set "
        "KEYS.KEEP_DEVELOPMENT_KEY = False)."
    )


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
        with pytest.raises(
            CommandError,
            match=r"^A key's id is 1 to 32 letters, digits, '_' and '-', such as "
            r"2026-10\.$",
        ):
            command("generate", "--id", "no spaces")


def test_rewrap_prints_the_same_keys_under_the_newest_kek(
    database_url: str, tmp_path: Path
) -> None:
    """Each data key unwraps to what it did."""
    with project(database_url, tmp_path, DATA_KEYS={"a": FIRST}):
        printed = command("rewrap")
    assert printed.startswith('{\n  "a": "local:')
    again = json.loads(printed.rsplit("}", 1)[0] + "}")
    provider = LocalKeyProvider.from_secret_key()
    assert provider.unwrap_now(again["a"]) == provider.unwrap_now(FIRST)
    assert printed.endswith(
        "\nReplace KEYS['DATA_KEYS'] with these: the data keys are the same.\n"
    )


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
    with project(database_url, tmp_path, AUDIT, DATA_KEYS={"a": FIRST}):
        run_async(
            save(
                Cardholder(id=ANN, name="Ann", card_number=PAN, api_secret=b"x"),
                Cardholder(id=BOB, name="Bob"),
                Payment(
                    id=uuid.UUID(int=1), holder_id=ANN, pan=CardNumber(PAN), note="n"
                ),
                Payment(
                    id=uuid.UUID(int=2),
                    holder_id=BOB,
                    pan=CardNumber("5500"),
                    note="m",
                ),
            )
        )
        assert "retired: none" in command("status", "--check")
    rotated = {"DATA_KEYS": {"a": FIRST, "b": SECOND}, "CURRENT_KEY": "b"}
    with project(database_url, tmp_path, AUDIT, **rotated):
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
        assert "default subjects' keys wrapped under a: 2" in status
        with pytest.raises(
            CommandError,
            match=r"^keys\.W001: 8 value\(s\), index\(es\), or subject key\(s\) are "
            r"still under a retired key; run 'manage\.py keys reencrypt'\.$",
        ):
            command("status", "--check")
        with pytest.raises(CommandError, match=r"^--batch-size must be at least 1\.$"):
            command("reencrypt", "--batch-size", "0")
        printed = command("reencrypt", "--batch-size", "1")
        assert printed.strip() == (
            "Sealed 3 value(s) again, remade 3 blind index(es), and wrapped 2 subject "
            "key(s) under b."
        )
        assert "retired: a." in command("status", "--check")
        assert command("reencrypt").strip() == (
            "Sealed 0 value(s) again, remade 0 blind index(es), and wrapped 0 subject "
            "key(s) under b."
        )
        recorded = run_async(
            rows(
                "SELECT details FROM domainkit_audit_log "
                "WHERE action = 'operator.keys_reencrypt'"
            )
        )
        first = recorded[0]["details"]
        first = json.loads(first) if isinstance(first, str) else first
        assert {
            key: first[key]
            for key in ("current", "values", "indexes", "subject_keys", "failed")
        } == {"current": "b", "values": 3, "indexes": 3, "subject_keys": 2, "failed": 0}
    with project(database_url, tmp_path, AUDIT, DATA_KEYS={"b": SECOND}):
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
                Payment(id=uuid.UUID(int=3), holder_id=BOB, pan=CardNumber(PAN)),
            )
        )
        run_async(tamper())
    with project(
        database_url, tmp_path, DATA_KEYS={"a": FIRST, "b": SECOND}, CURRENT_KEY="b"
    ):
        with pytest.raises(
            CommandError,
            match=r"^2 value\(s\) did not open: tampered, or sealed under a key the "
            r"ring no longer holds\. They were left as they are\.$",
        ):
            command("reencrypt")
        assert "default subjects' keys wrapped under a: 1, b: 1\n" in command("status")
        status = run_async(key_status(keyring()))
        # The tampered values and their indexes, and Carol's broken key; Bob's,
        # after hers, moved.
        assert status.on_retired == 5
        assert status.subject_keys == {"default": {"a": 1, "b": 1}}


def test_a_row_changed_while_it_is_sealed_again_is_left_for_the_next_run(
    database_url: str, tmp_path: Path
) -> None:
    """The update names the value it read: one changed meanwhile is not overwritten.

    ``reencrypt`` locks a batch's rows as it reads them, so this cannot
    happen there; called alone, the batch still writes nothing over the
    change (it counts the rows it sent, which a lock makes every row).
    """
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
        assert done.failed == 0
        after = run_async(rows("SELECT id, pan FROM cards_payments"))[0]
        assert after["pan"] != read["pan"]
        assert after["pan"].startswith("dk1:k.a:")  # Not sealed again.

        async def pan() -> CardNumber:
            async with CardsUnitOfWork() as uow:
                return (await uow.payments.get(uuid.UUID(int=1))).pan

        assert run_async(pan()) == CardNumber("5500")  # The change, kept.


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


def test_a_value_whose_subject_key_is_gone_loses_its_index_when_moved(
    database_url: str, tmp_path: Path
) -> None:
    """Reencrypt clears the stale index of a value nobody can open any more."""
    with project(database_url, tmp_path, DATA_KEYS={"a": FIRST}):
        run_async(save(Cardholder(id=ANN, name="Ann", card_number=PAN)))
        run_async(drop_subject_keys())
    with project(
        database_url, tmp_path, DATA_KEYS={"a": FIRST, "b": SECOND}, CURRENT_KEY="b"
    ):
        assert command("reencrypt").startswith("Sealed 0 value(s) again, remade 1")
        row = run_async(
            rows("SELECT card_number, card_number_index FROM cards_cardholders")
        )[0]
        assert row["card_number"].startswith("dk1:s.")
        assert row["card_number_index"] is None


async def drop_subject_keys() -> None:
    """Delete every subject's key, as a lost table would."""
    async with connections.engine("default").begin() as connection:
        await connection.execute(subject_keys.delete())


async def tamper() -> None:
    """Copy payment 1's number into payments 2 and 3; add a broken subject key."""
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
            .where(tables.payments.c.id != uuid.UUID(int=1))
            .values(pan=Ciphertext(first))
        )
        await connection.execute(
            subject_keys.insert().values(
                id="0" * 32,
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
        assert (
            "default cards_payments.pan: b: 0; subjects' keys: 0; under no key "
            "held: 1; indexes to remake: 1\n" in status
        )
        assert "default subjects' keys wrapped under none\n" in status
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


@pytest.mark.anyio
async def test_shredding_adds_up_the_keys_of_every_database(
    database_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A database after the one holding the key does not reset the count."""
    from domainkit.contrib.keys import operations

    with project(database_url, tmp_path):
        await save(Cardholder(id=ANN, name="Ann", card_number=PAN))
        # The same database again, as a second one holding none of Ann's keys.
        monkeypatch.setattr(operations, "hosting_aliases", lambda: ("default",) * 2)
        assert await shred_subject(str(ANN)) == 1


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
    with pytest.raises(
        KeyStoreConflict,
        match=r"^The key of a subject could not be stored in 3 attempts: each time, "
        r"another writer's key for them was being erased\.$",
    ):
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


def test_an_action_is_required(database_url: str, tmp_path: Path) -> None:
    """``keys`` alone does nothing, and says so."""
    with project(database_url, tmp_path), pytest.raises((CommandError, SystemExit)):
        command()


def test_key_ids_are_matched_as_written_not_as_patterns(
    database_url: str, tmp_path: Path
) -> None:
    """``_`` in an id is no wildcard: ``x21``'s values are not ``x_1``'s."""
    with project(database_url, tmp_path, DATA_KEYS={"x21": FIRST}):
        run_async(
            save(Cardholder(id=ANN, name="Ann", card_number=PAN, api_secret=b"s"))
        )
    keys = {"DATA_KEYS": {"x_1": SECOND, "x21": FIRST}, "CURRENT_KEY": "x_1"}
    with project(database_url, tmp_path, **keys):
        status = command("status")
        assert "cards_cardholders.api_secret: x_1: 0, x21: 1;" in status
        assert "cards_cardholders.card_number: x_1: 0, x21: 0; subjects' keys: 1; " in (
            status
        )
        assert "indexes to remake: 1" in status
    keys = {"DATA_KEYS": {"x_1": SECOND, "x21": FIRST}, "CURRENT_KEY": "x21"}
    with project(database_url, tmp_path, **keys):
        assert "cards_cardholders.card_number: x21: 0, x_1: 0; subjects' keys: 1\n" in (
            command("status")
        )


@pytest.mark.usefixtures("database_access")
def test_a_decrypted_dump_names_its_operator_where_deployed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """In staging, ``--decrypt`` is what the refusal says needs a named operator.

    On a SQLite database of its own, on every run: staging asks PostgreSQL
    for TLS, which the tests' server lacks.
    """
    monkeypatch.delenv("DOMAINKIT_OPERATOR", raising=False)
    with (
        project(f"sqlite+aiosqlite:///{tmp_path / 'staging.sqlite3'}", tmp_path, AUDIT),
        override_settings(ENVIRONMENT="staging"),
        pytest.raises(
            CommandError,
            match=r"^Name the operator who runs dumpdata --decrypt in staging: ",
        ),
    ):
        call_command("dumpdata", "cards", "--decrypt", stdout=io.StringIO())


def test_a_decrypted_dump_is_recorded_with_its_tables(
    database_url: str, tmp_path: Path
) -> None:
    """``dumpdata --decrypt`` leaves ``operator.dumpdata`` naming what it opened."""
    with project(database_url, tmp_path, AUDIT):
        run_async(save(Cardholder(id=ANN, name="Ann", card_number=PAN)))
        call_command("dumpdata", "cards", "--decrypt", stdout=io.StringIO())
        call_command("dumpdata", "cards", stdout=io.StringIO())
        recorded = run_async(
            rows(
                "SELECT details FROM domainkit_audit_log "
                "WHERE action = 'operator.dumpdata'"
            )
        )
    [details] = [
        json.loads(row["details"])
        if isinstance(row["details"], str)
        else row["details"]
        for row in recorded
    ]
    assert details["decrypted_tables"] == ["cards_cardholders", "cards_payments"]


def test_values_under_the_development_key_move_to_a_data_key(
    database_url: str, tmp_path: Path
) -> None:
    """``dev`` stays retired beside ``DATA_KEYS``, through a SECRET_KEY rotation too."""
    old = SECRET_KEY
    new = "rotated-" + "r" * 50
    with project(database_url, tmp_path):
        run_async(
            save(
                Cardholder(id=ANN, name="Ann", card_number=PAN, api_secret=b"s"),
            )
        )
    keys = {"DATA_KEYS": {"a": FIRST}, "KEEP_DEVELOPMENT_KEY": None}
    with override_settings(SECRET_KEY=new, SECRET_KEY_FALLBACKS=[old]):
        with project(database_url, tmp_path, **keys):
            assert "Current key: a; retired: dev." in command("status")
            [warning] = domain_warnings("keys")
            assert "retired keys (dev)" in warning
            found = run_async(read_ann())
            assert (found.card_number, found.api_secret) == (PAN, b"s")
            printed = command("reencrypt")
            assert printed.startswith("Sealed 1 value(s) again, remade 1 blind")
            assert printed.strip().endswith("wrapped 1 subject key(s) under a.")
        dropped = {"DATA_KEYS": {"a": FIRST}, "KEEP_DEVELOPMENT_KEY": False}
        with project(database_url, tmp_path, **dropped):
            assert "retired: none" in command("status", "--check")
            found = run_async(read_ann())
            assert (found.card_number, found.api_secret) == (PAN, b"s")


async def read_ann() -> Cardholder:
    """Ann, found by her card number."""
    async with CardsUnitOfWork() as uow:
        from domainkit.ddd import F

        [ann] = await uow.cardholders.filter(F("card_number") == PAN)
    return ann


@pytest.mark.parametrize(
    ("url", "environment", "found"),
    [
        ("http://vault.internal:8200", "production", ["keys.E002"]),
        ("http://vault.internal", "staging", ["keys.E002"]),
        ("https://vault.internal:8200", "production", []),
        ("http://127.0.0.1:8200", "local", []),
    ],
)
def test_a_deployment_refuses_vault_over_plain_http(
    url: str, environment: str, found: list[str]
) -> None:
    """``keys.E002``: the token and the data keys would cross in clear."""
    registry.clear()
    keys = {
        "PROVIDER": "vault",
        "VAULT": {"URL": url, "KEY": "k", "TOKEN": "t"},
        "DATA_KEYS": {"a": "vault:v1:x"},
    }
    with override_settings(INSTALLED_DOMAINS=[KEYS, ALPHA], KEYS=keys):
        setup()
        with override_settings(ENVIRONMENT=environment):
            problems = domain_problems("keys", None)
    registry.clear()
    assert [problem.id for problem in problems] == found  # type: ignore[attr-defined]
    if found:
        assert problems[0] == (
            f"KEYS.VAULT.URL is plain HTTP in {environment}: Vault's token, and "
            "every data key it unwraps, would cross the network in clear."
        )
        assert problems[0].hint == "Serve Vault over TLS and give an https:// URL."  # type: ignore[attr-defined]


async def database_now() -> datetime | None:
    """PostgreSQL's clock, which dates tombstones there; None on SQLite."""
    async with connections.engine("default").connect() as connection:
        if connection.dialect.name != "postgresql":
            return None
        return cast(datetime, await connection.scalar(select(func.clock_timestamp())))


@pytest.mark.anyio
async def test_tombstones_are_kept_a_day(database_url: str, tmp_path: Path) -> None:
    """An erasure leaves its own, and forgets those older than a day."""
    from datetime import UTC, timedelta

    from domainkit.contrib.keys.tables import erasures

    with project(database_url, tmp_path):
        on_postgres = await database_now()
        now = on_postgres or datetime(2026, 10, 31, 12, tzinfo=UTC)
        # PostgreSQL's clock moves on before the erasure reads it: a day to
        # the second is exact only under SQLite's frozen clock.
        margin = timedelta(minutes=1) if on_postgres else timedelta(0)
        async with connections.engine("default").begin() as connection:
            await connection.execute(
                erasures.insert(),
                [
                    {"subject": "old", "erased_at": now - timedelta(days=1, seconds=1)},
                    {"subject": "recent", "erased_at": now - timedelta(hours=23)},
                    {"subject": "a day", "erased_at": now - timedelta(days=1) + margin},
                ],
            )
        with freeze_time(now):
            await shred_subject("ann")
        left = await rows(
            "SELECT subject FROM domainkit_keys_erasures ORDER BY subject"
        )
    assert [row["subject"] for row in left] == ["a day", "ann", "recent"]


@pytest.mark.anyio
async def test_a_host_clock_ahead_keeps_the_tombstone_it_leaves(
    database_url: str, tmp_path: Path
) -> None:
    """Two days ahead of the database, the erasure still keeps its tombstone.

    Tombstones are dated by the database's clock on PostgreSQL; forgetting
    them by the host's deleted the one just left (DATA-22), which undid
    SEC-15a-2: a key made meanwhile then survived the erasure.
    """
    from datetime import UTC, timedelta

    with project(database_url, tmp_path):
        now = await database_now() or datetime.now(UTC)
        with freeze_time(now + timedelta(days=2)):
            await shred_subject("ann")
        left = await rows("SELECT subject FROM domainkit_keys_erasures")
    assert [row["subject"] for row in left] == ["ann"]


# What the command says, and how it moves keys in batches -----------------------------


def keys_help(*action: str) -> str:
    """``keys``' ``--help``, or an action's, on one line."""
    from domainkit.contrib.keys.management.commands.keys import Command

    parser = Command().create_parser("manage.py", "keys")
    out = io.StringIO()
    with redirect_stdout(out), pytest.raises(SystemExit):
        parser.parse_args([*action, "--help"])
    return " ".join(out.getvalue().split())


def test_the_command_says_what_each_action_does() -> None:
    """Each action's line of ``--help``, and each option's line of the action's."""
    assert (
        "{generate,rewrap,status,reencrypt} generate make a data key, wrapped rewrap "
        "print DATA_KEYS wrapped under the KEK's newest version status count what "
        "each key seals reencrypt seal everything still under a retired key again "
        "options:"
    ) in keys_help()
    assert (" --id KEY_ID its id in DATA_KEYS (default: today's date)") in keys_help(
        "generate"
    )
    assert (
        " --check fail while anything is sealed under a retired key (keys.W001)"
    ) in keys_help("status")
    assert (
        " --batch-size BATCH_SIZE rows per transaction (default: 500)"
    ) in keys_help("reencrypt")
    assert " --operator NAME who runs this" in keys_help("reencrypt")


def test_status_names_every_retired_key(database_url: str, tmp_path: Path) -> None:
    """Retired keys are listed in the order DATA_KEYS gives them."""
    keys = {"DATA_KEYS": {"a": FIRST, "b": SECOND, "c": wrapped()}, "CURRENT_KEY": "c"}
    with project(database_url, tmp_path, **keys):
        assert command("status").startswith("Current key: c; retired: a, b.\n")


def holders(count: int) -> list[Cardholder]:
    """``count`` card holders, each with a card number sealed under their own key."""
    return [
        Cardholder(id=uuid.UUID(int=n + 1), name=f"h{n}", card_number=PAN)
        for n in range(count)
    ]


def test_reencrypt_reads_at_most_batch_size_rows_at_a_time(
    database_url: str, tmp_path: Path
) -> None:
    """A query per batch of ``--batch-size``, and one more that finds none left."""
    from domainkit.testing.queries import CapturedQueries

    with project(database_url, tmp_path, DATA_KEYS={"a": FIRST}):
        run_async(save(*holders(2)))
    rotated = {"DATA_KEYS": {"a": FIRST, "b": SECOND}, "CURRENT_KEY": "b"}
    with project(database_url, tmp_path, **rotated):
        with CapturedQueries() as queries:
            command("reencrypt", "--batch-size", "1")
        batches = [each for each in queries.statements if " LIMIT " in each]
        on_keys = [each for each in batches if "domainkit_keys_subject_keys" in each]
        # Two subjects' keys, and two card numbers' indexes to remake: three
        # queries each; passports, API secrets and payments, none to move: one.
        assert (len(on_keys), len(batches)) == (3, 3 + 3 + 4)


def test_one_batch_wraps_every_subject_key_in_it(
    database_url: str, tmp_path: Path
) -> None:
    """Each key moved counts, not only the last of a batch."""
    with project(database_url, tmp_path, DATA_KEYS={"a": FIRST}):
        run_async(save(*holders(2)))
    rotated = {"DATA_KEYS": {"a": FIRST, "b": SECOND}, "CURRENT_KEY": "b"}
    with project(database_url, tmp_path, **rotated):
        assert (
            command("reencrypt")
            .strip()
            .endswith("and wrapped 2 subject key(s) under b.")
        )


def test_subjects_keys_are_wrapped_again_in_the_order_of_their_ids(
    database_url: str, tmp_path: Path
) -> None:
    """One at a time, none is skipped: each batch starts after the last id done."""
    with project(database_url, tmp_path, DATA_KEYS={"a": FIRST}):
        run_async(save(*holders(8)))
    rotated = {"DATA_KEYS": {"a": FIRST, "b": SECOND}, "CURRENT_KEY": "b"}
    with project(database_url, tmp_path, **rotated):
        command("reencrypt", "--batch-size", "1")
        status = run_async(key_status(keyring()))
    assert status.subject_keys == {"default": {"b": 8}}


def test_a_subject_key_wrapped_again_meanwhile_is_left_as_it_is(
    database_url: str, tmp_path: Path
) -> None:
    """The update names the wrapping key it read: another run's move is kept."""
    with project(database_url, tmp_path, DATA_KEYS={"a": FIRST}):
        run_async(save(*holders(1)))
    rotated = {"DATA_KEYS": {"a": FIRST, "b": SECOND}, "CURRENT_KEY": "b"}
    with project(database_url, tmp_path, **rotated):
        read = run_async(stored_subject_keys())
        command("reencrypt")
        assert run_async(rewrap_again(read)) == 0


async def stored_subject_keys() -> list[Any]:
    """The rows of the subjects' keys, as ``reencrypt`` reads them."""
    async with connections.engine("default").connect() as connection:
        return list((await connection.execute(select(subject_keys))).all())


async def rewrap_again(read: list[Any]) -> int:
    """Wrap the keys of rows read before again, as ``reencrypt`` would; how many."""
    from domainkit.contrib.keys.operations import _rewrap_rows

    async with connections.engine("default").begin() as connection:
        return await connection.run_sync(_rewrap_rows, keyring(), read)


def test_a_value_under_a_retired_key_without_its_index_is_sealed_again(
    database_url: str, tmp_path: Path
) -> None:
    """A row written without its blind index (a fixture's) still moves, and gets one."""
    with project(database_url, tmp_path, DATA_KEYS={"a": FIRST}):
        run_async(
            save(Payment(id=uuid.UUID(int=1), holder_id=ANN, pan=CardNumber(PAN)))
        )
        run_async(drop_indexes())
    rotated = {"DATA_KEYS": {"a": FIRST, "b": SECOND}, "CURRENT_KEY": "b"}
    with project(database_url, tmp_path, **rotated):
        assert command("reencrypt").startswith(
            "Sealed 1 value(s) again, remade 1 blind index(es)"
        )


async def drop_indexes() -> None:
    """Clear the payments' blind indexes, as a fixture without them leaves them."""
    async with connections.engine("default").begin() as connection:
        await connection.execute(update(tables.payments).values(pan_index=None))


def test_a_subjects_lock_is_a_signed_64_bit_hash_the_same_everywhere() -> None:
    """BLAKE2b of the subject in 8 bytes, big-endian and signed: a BIGINT."""
    from domainkit.contrib.keys.store import _lock_key

    assert [_lock_key(subject) for subject in ("ann", "bob", "\ud800")] == [
        2326584288098257840,
        -7046027984218438332,
        -5504369782091661012,
    ]


@pytest.mark.anyio
async def test_a_subjects_key_that_does_not_open_is_passed_over_once(
    database_url: str, tmp_path: Path
) -> None:
    """The next batch starts after it: one at a time, the run still ends."""
    import asyncio

    from domainkit.contrib.keys import reencrypt

    with project(database_url, tmp_path, DATA_KEYS={"a": FIRST}):
        await save(*holders(1))
        async with connections.engine("default").begin() as connection:
            await connection.execute(
                subject_keys.insert().values(
                    id="f" * 32,
                    subject="carol",
                    wrapping_key="a",
                    key=b"\x00" * 60,
                    created_at=clock.now(),
                )
            )
    rotated = {"DATA_KEYS": {"a": FIRST, "b": SECOND}, "CURRENT_KEY": "b"}
    with project(database_url, tmp_path, **rotated):
        done = await asyncio.wait_for(reencrypt(keyring(), batch_size=1), 30)
    assert done.subject_keys == 1


@pytest.mark.anyio
@pytest.mark.skipif(
    not os.environ.get("DOMAINKIT_TEST_POSTGRES_URL"),
    reason="SQLite's clock is the host's",
)
async def test_keys_and_tombstones_are_dated_by_the_database(
    database_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An hour off on the host, a key's making and its erasure are PostgreSQL's."""
    from datetime import timedelta

    from sqlalchemy import func

    from domainkit.contrib.keys.tables import erasures

    real = clock.now
    monkeypatch.setattr(clock, "now", lambda: real() + timedelta(hours=1))
    with project(database_url, tmp_path):
        await save(Cardholder(id=ANN, name="Ann", card_number=PAN))
        async with connections.engine("default").connect() as connection:
            made = await connection.scalar(select(subject_keys.c.created_at))
        await shred_subject(str(ANN))
        async with connections.engine("default").connect() as connection:
            erased = await connection.scalar(select(erasures.c.erased_at))
            now = await connection.scalar(select(func.statement_timestamp()))
    assert made is not None
    assert erased is not None
    assert now is not None
    assert abs(made - now) < timedelta(minutes=5)
    assert abs(erased - now) < timedelta(minutes=5)


@pytest.mark.anyio
@pytest.mark.skipif(
    not os.environ.get("DOMAINKIT_TEST_POSTGRES_URL"),
    reason="SQLite serializes the two writers; the race needs PostgreSQL",
)
async def test_a_key_made_during_an_erasure_with_no_index_is_destroyed(
    database_url: str, tmp_path: Path
) -> None:
    """A first key sealing only a passport, no blind index: destroyed as it commits."""
    with project(database_url, tmp_path):
        async with CardsUnitOfWork() as uow:
            await uow.cardholders.add(Cardholder(id=ANN, name="Ann", passport="P1"))
            await uow.session.flush()  # Ann's key is made, not yet committed.
            await shred_subject(str(ANN))
            await uow.commit()
        assert await rows("SELECT subject FROM domainkit_keys_subject_keys") == []
