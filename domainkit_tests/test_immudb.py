"""immudb itself: verified ledger operations, TLS, signed states, evidence.

Set ``DOMAINKIT_TEST_IMMUDB_URL`` (``immudb://user:pw@host:3322/db``) to run
these, as CI does; ``DOMAINKIT_TEST_IMMUDB_CA`` and
``DOMAINKIT_TEST_IMMUDB_SIGNING_KEY`` name the server's CA certificate and
the public half of its ``--signingKey``. Keys are unique per test, so tests
can share one immudb database.
"""

import asyncio
import os
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Final

import pytest
from ecdsa import NIST256p, SigningKey

from domainkit.conf import LedgerConfig
from domainkit.contrib.evidence import Evidence, EvidenceTampered, LedgerEvidenceStore
from domainkit.ledger import (
    LedgerChanged,
    LedgerKeyExists,
    LedgerKeyNotFound,
    LedgerTampered,
    LedgerUnavailable,
)
from domainkit.ledger.immudb import ImmudbLedger

URL: Final = os.environ.get("DOMAINKIT_TEST_IMMUDB_URL", "")
CA: Final = os.environ.get("DOMAINKIT_TEST_IMMUDB_CA")
SIGNING_KEY: Final = os.environ.get("DOMAINKIT_TEST_IMMUDB_SIGNING_KEY")

pytestmark = [
    pytest.mark.anyio,
    pytest.mark.skipif(
        not URL, reason="set DOMAINKIT_TEST_IMMUDB_URL to an immudb server"
    ),
]
secured = pytest.mark.skipif(
    not (CA and SIGNING_KEY),
    reason="set DOMAINKIT_TEST_IMMUDB_CA and DOMAINKIT_TEST_IMMUDB_SIGNING_KEY",
)


def config(**options: object) -> LedgerConfig:
    """The test server, with TLS and signed states when they are configured."""
    values: dict[str, object] = {
        "URL": URL,
        "TLS_CA": CA,
        "SERVER_SIGNING_KEY": SIGNING_KEY,
    }
    return LedgerConfig.model_validate({**values, **options})


@pytest.fixture
async def ledger() -> AsyncIterator[ImmudbLedger]:
    """A ledger on the test server, closed afterwards."""
    opened = ImmudbLedger(config())
    yield opened
    await opened.close()


@pytest.fixture
def prefix() -> bytes:
    """A key prefix no other test uses."""
    return f"test/{uuid.uuid4().hex}/".encode()


async def test_verified_writes_reads_and_history(
    ledger: ImmudbLedger, prefix: bytes
) -> None:
    """Revisions accumulate; any revision reads back verified."""
    key = prefix + b"k"

    first = await ledger.append(key, b"one")
    second = await ledger.append(key, b"two")
    latest = await ledger.read(key)
    original = await ledger.read(key, revision=1)

    assert second.transaction > first.transaction
    assert (latest.value, latest.revision) == (b"two", 2)
    assert (original.value, original.revision, original.transaction) == (
        b"one",
        1,
        first.transaction,
    )
    assert await ledger.revision_count(key, limit=5) == 2
    assert await ledger.revision_count(prefix + b"none", limit=5) == 0
    assert await ledger.keys(prefix, limit=10) == [key]
    await ledger.check()


async def test_missing_keys_and_revisions(ledger: ImmudbLedger, prefix: bytes) -> None:
    """Unknown keys and revisions are LedgerKeyNotFound."""
    await ledger.append(prefix + b"k", b"v")

    with pytest.raises(LedgerKeyNotFound):
        await ledger.read(prefix + b"none")
    with pytest.raises(LedgerKeyNotFound):
        await ledger.read(prefix + b"k", revision=9)


async def test_create_writes_only_new_keys(ledger: ImmudbLedger, prefix: bytes) -> None:
    """The server refuses a second create; the first value stays alone."""
    key = prefix + b"k"

    created = await ledger.create(key, b"one")
    with pytest.raises(LedgerKeyExists, match="already has this key"):
        await ledger.create(key, b"two")
    entry = await ledger.read(key)

    assert (entry.value, entry.revision, entry.transaction) == (
        b"one",
        1,
        created.transaction,
    )
    assert await ledger.revision_count(key, limit=5) == 1
    # The refused write leaves the client's trusted state usable.
    assert (await ledger.create(prefix + b"next", b"v")).transaction > (
        created.transaction
    )


async def test_appends_after_a_revision_land_at_most_once(
    ledger: ImmudbLedger, prefix: bytes
) -> None:
    """Immudb refuses a write once a revision came after the one read."""
    key = prefix + b"k"

    first = await ledger.append(key, b"one", unchanged_since=0)
    read = await ledger.read(key)
    second = await ledger.append(key, b"two", unchanged_since=read.transaction)
    with pytest.raises(LedgerChanged):
        await ledger.append(key, b"late", unchanged_since=read.transaction)
    with pytest.raises(LedgerChanged):
        await ledger.append(key, b"again", unchanged_since=0)

    assert read.transaction == first.transaction
    assert second.transaction > first.transaction
    assert await ledger.revision_count(key, limit=5) == 2
    # The refused writes leave the client's trusted state usable.
    assert (await ledger.read(key)).value == b"two"
    assert (await ledger.create(prefix + b"next", b"v")).transaction > (
        second.transaction
    )


async def test_readiness_has_a_client_of_its_own(
    ledger: ImmudbLedger, prefix: bytes
) -> None:
    """The probe logs in on its own and checks the server's signed state."""
    await ledger.check()
    await ledger.append(prefix + b"k", b"v")
    await ledger.check()


async def test_racing_evidence_writers_leave_one_revision(
    ledger: ImmudbLedger,
) -> None:
    """Concurrent records of one evidence share one revision and its receipt."""
    store = LedgerEvidenceStore(
        ledger,
        namespace=f"t{uuid.uuid4().hex[:8]}",
        max_bytes=4096,
        subject_key=b"subject-key-for-the-immudb-tests",
    )
    evidence = Evidence(kind="kyc.checked", subject="customer:2", facts={"ok": True})

    receipts = await asyncio.gather(*(store.record(evidence) for _ in range(5)))

    assert len(set(receipts)) == 1
    assert (await store.verify(receipts[0])).intact
    assert await ledger.revision_count(receipts[0].key.encode(), limit=5) == 1


async def test_closed_ledgers_reconnect(ledger: ImmudbLedger, prefix: bytes) -> None:
    """After close(), the next call opens a new client."""
    await ledger.append(prefix + b"k", b"v")
    await ledger.close()

    assert (await ledger.read(prefix + b"k")).value == b"v"


@secured
async def test_a_wrong_signing_key_is_tampering(tmp_path: Path, prefix: bytes) -> None:
    """States signed by another key fail verification."""
    other = tmp_path / "other.pub"
    other.write_bytes(SigningKey.generate(curve=NIST256p).get_verifying_key().to_pem())
    impostor = ImmudbLedger(config(SERVER_SIGNING_KEY=other))
    try:
        with pytest.raises(LedgerTampered, match="did not verify"):
            await impostor.append(prefix + b"k", b"v")
    finally:
        await impostor.close()


@secured
async def test_creates_check_the_signing_key_too(tmp_path: Path, prefix: bytes) -> None:
    """A conditional write is verified like any other: a wrong key is tampering."""
    other = tmp_path / "other.pub"
    other.write_bytes(SigningKey.generate(curve=NIST256p).get_verifying_key().to_pem())
    impostor = ImmudbLedger(config(SERVER_SIGNING_KEY=other))
    try:
        with pytest.raises(LedgerTampered, match="did not verify"):
            await impostor.create(prefix + b"k", b"v")
    finally:
        await impostor.close()


@secured
async def test_plain_text_is_refused_by_a_tls_server(prefix: bytes) -> None:
    """Without TLS_CA the client speaks plain text, which the server refuses."""
    plain = ImmudbLedger(config(TLS_CA=None, TIMEOUT=5))
    try:
        with pytest.raises(LedgerUnavailable):
            await plain.check()
    finally:
        await plain.close()


async def test_evidence_on_immudb(ledger: ImmudbLedger) -> None:
    """Record, verify, list; a second write to the key is caught."""
    store = LedgerEvidenceStore(
        ledger,
        namespace=f"t{uuid.uuid4().hex[:8]}",
        max_bytes=4096,
        subject_key=b"subject-key-for-the-immudb-tests",
    )
    evidence = Evidence(
        kind="kyc.document_verified", subject="customer:1", facts={"ok": True}
    )

    receipt = await store.record(evidence)
    verified = await store.verify(receipt)
    listed = await store.for_subject("customer:1")
    await ledger.append(receipt.key.encode(), b"rewritten")

    assert verified.evidence == evidence
    assert [recorded.receipt for recorded in listed] == [receipt]
    with pytest.raises(EvidenceTampered, match="written again"):
        await store.verify(receipt)
