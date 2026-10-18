"""immudb itself: verified ledger operations, TLS, signed states, evidence.

Set ``DOMAINKIT_TEST_IMMUDB_URL`` (``immudb://user:pw@host:3322/db``) to run
these, as CI does; ``DOMAINKIT_TEST_IMMUDB_CA`` and
``DOMAINKIT_TEST_IMMUDB_SIGNING_KEY`` name the server's CA certificate and
the public half of its ``--signingKey``. Keys are unique per test, so tests
can share one immudb database.
"""

import os
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Final

import pytest
from ecdsa import NIST256p, SigningKey

from domainkit.conf import LedgerConfig
from domainkit.contrib.evidence import Evidence, EvidenceTampered, LedgerEvidenceStore
from domainkit.ledger import LedgerKeyNotFound, LedgerTampered, LedgerUnavailable
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
