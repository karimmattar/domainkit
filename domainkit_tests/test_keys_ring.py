"""The key ring: data keys by id, sealed values that name their key, subjects' keys."""

import gc
from collections.abc import Iterator
from typing import Any, Final, cast

import pytest
from sqlalchemy import Connection
from sqlalchemy.util import greenlet_spawn

from domainkit.encryption import DecryptionFailed
from domainkit.exceptions import ImproperlyConfigured
from domainkit.http.client import stub
from domainkit.keys import LocalKeyProvider
from domainkit.keys.ring import (
    Keyring,
    KeysNotLoaded,
    StoredSubjectKey,
    forget_held,
    keyring,
    sealed_key,
    use_keyring,
)
from domainkit.keys.vault import VaultTransitKeyProvider
from domainkit_tests.fake_kms import FakeVault

CONTEXT: Final = b"cards\x00number\x001"
KEK: Final = b"k" * 32


class Transaction:
    """Stands for a connection's open transaction."""


class FakeConnection:
    """What the ring asks of a connection: its transaction."""

    def __init__(self, transaction: Transaction | None = None) -> None:
        """Be in ``transaction``, or a new one."""
        self.transaction = transaction or Transaction()

    def get_transaction(self) -> Transaction | None:
        """The open transaction."""
        return self.transaction


class MemoryStore:
    """Subjects' keys in a dict; counts the reads that reach it."""

    def __init__(self) -> None:
        """Hold no key."""
        self.keys: dict[str, StoredSubjectKey] = {}
        self.reads = 0

    def find(self, connection: Connection, key_id: str) -> StoredSubjectKey | None:
        """The key ``key_id``."""
        self.reads += 1
        return self.keys.get(key_id)

    def of_subject(
        self, connection: Connection, subject: str
    ) -> StoredSubjectKey | None:
        """The key of ``subject``."""
        self.reads += 1
        return next((key for key in self.keys.values() if key.subject == subject), None)

    def add(self, connection: Connection, key: StoredSubjectKey) -> StoredSubjectKey:
        """Store ``key``."""
        self.keys[key.id] = key
        return key


def connection(transaction: Transaction | None = None) -> Connection:
    """A fake connection, typed as the ring takes it."""
    return cast(Connection, FakeConnection(transaction))


def ring(**options: Any) -> Keyring:  # noqa: ANN401
    """A ring of a local provider, ``k1`` current and ``k0`` retired."""
    provider = LocalKeyProvider([KEK])
    keys = {"k0": provider.wrap_now(b"0" * 32), "k1": provider.wrap_now(b"1" * 32)}
    options.setdefault("subject_keys", MemoryStore())
    return Keyring(provider, keys, "k1", **options)


@pytest.fixture
def no_ring() -> Iterator[None]:
    """No process ring, before and after."""
    use_keyring(None)
    yield
    use_keyring(None)


def test_a_sealed_value_names_its_key_and_opens_in_its_context() -> None:
    """``dk1:k.<id>:``; another context, or a change, does not open."""
    keys = ring()
    sealed = keys.seal(b"4111", CONTEXT)
    assert sealed.startswith("dk1:k.k1:")
    assert sealed_key(sealed) == ("k", "k1")
    assert keys.open(sealed, CONTEXT) == b"4111"
    assert keys.seal(b"4111", CONTEXT) != sealed  # A new nonce each time.
    with pytest.raises(DecryptionFailed, match="another row or column"):
        keys.open(sealed, b"cards\x00number\x002")
    claimed = sealed.replace("dk1:k.k1:", "dk1:k.k0:")
    with pytest.raises(DecryptionFailed, match="another row or column"):
        keys.open(claimed, CONTEXT)
    with pytest.raises(DecryptionFailed, match="another row or column"):
        keys.open(sealed[:-3] + "!!!", CONTEXT)


def test_a_retired_key_still_opens_and_a_lost_one_is_named() -> None:
    """Values under ``k0`` open; under a key the ring lacks, the error names it."""
    old = Keyring(ring().provider, ring().data_keys, "k0")
    sealed = old.seal(b"x", CONTEXT)
    assert ring().open(sealed, CONTEXT) == b"x"
    keys = ring()
    keys.data_keys.pop("k0")
    lost = Keyring(keys.provider, keys.data_keys, "k1")
    with pytest.raises(
        DecryptionFailed, match=r"'k0', which KEYS\.DATA_KEYS no longer"
    ):
        lost.open(sealed, CONTEXT)


@pytest.mark.parametrize(
    "text",
    ["plain", "dk2:k.k1:abc", "dk1:x.k1:abc", "dk1:k.:abc", "dk1:k.k1:", "dk1:k1:abc"],
)
def test_text_that_is_not_sealed_is_refused(text: str) -> None:
    """Only ``dk1:k.<id>:<payload>`` and ``dk1:s.<id>:<payload>`` are sealed values."""
    assert sealed_key(text) is None
    with pytest.raises(DecryptionFailed, match="not one the key ring sealed"):
        ring().open(text, CONTEXT)


def test_the_current_key_must_be_one_of_the_ring() -> None:
    """A ring cannot seal under a key it lacks."""
    with pytest.raises(ImproperlyConfigured, match="'k9' is not one"):
        Keyring(LocalKeyProvider([KEK]), {}, "k9")


def test_a_ring_lists_its_ids_current_first_and_never_shows_a_key() -> None:
    """``ids`` puts the current key first; ``repr`` names ids and provider."""
    keys = ring(known={"dev": b"d" * 32})
    assert keys.ids == ("k1", "dev", "k0")
    assert repr(keys) == (
        "Keyring(current='k1', ids=('k1', 'dev', 'k0'), provider=LocalKeyProvider())"
    )
    assert keys.data_key("dev") == b"d" * 32


def test_a_data_key_of_the_wrong_size_is_refused() -> None:
    """A provider that unwraps 16 bytes is misconfigured."""
    provider = LocalKeyProvider([KEK])
    keys = Keyring(provider, {"short": provider.wrap_now(b"s" * 16)}, "short")
    with pytest.raises(ImproperlyConfigured, match=r"\['short'\] unwraps to 16 bytes"):
        keys.data_key("short")


@pytest.mark.anyio
async def test_a_ring_loads_once_through_its_provider() -> None:
    """``load`` unwraps every key; a second call does nothing."""
    keys = ring()
    assert not keys.loaded
    await keys.load()
    assert keys.loaded
    keys.data_keys["k1"] = "broken"
    await keys.load()
    assert keys.data_key("k1") == b"1" * 32


@pytest.fixture
def vault() -> Iterator[FakeVault]:
    """A fake Vault with the transit key ``payments``."""
    fake = FakeVault()
    fake.create("payments")
    with stub(fake):
        yield fake


async def vault_ring() -> Keyring:
    """A ring of one Vault-wrapped key, not loaded."""
    provider = VaultTransitKeyProvider(
        "https://vault.test", "payments", token="s.test-token"
    )
    made = await provider.generate_data_key()
    return Keyring(provider, {"v1": made.wrapped}, "v1")


@pytest.mark.anyio
async def test_a_ring_loads_itself_inside_sqlalchemys_greenlet(
    vault: FakeVault,
) -> None:
    """Where the ORM's events run, the provider's call is awaited for them."""
    keys = await vault_ring()
    key = await greenlet_spawn(keys.data_key, "v1")
    assert len(key) == 32
    assert keys.loaded


@pytest.mark.anyio
async def test_a_ring_outside_the_greenlet_says_to_load_it(vault: FakeVault) -> None:
    """Nothing can await the provider here: the error says what to do."""
    keys = await vault_ring()
    with pytest.raises(KeysNotLoaded, match=r"await the ring's load\(\)"):
        keys.seal(b"x", CONTEXT)
    gc.collect()  # The coroutine it made was closed, never left unawaited.


def test_blind_indexes_are_keyed_per_data_key_and_leave_out_the_row() -> None:
    """The same value has one index per key; equal values, equal indexes."""
    keys = ring()
    current = keys.blind_index(b"4111", b"cards\x00number")
    assert current.startswith("k1:")
    assert len(current.split(":")[1]) == 32
    assert keys.blind_index(b"4111", b"cards\x00number") == current
    assert keys.blind_index(b"4111", b"cards\x00other") != current
    every = keys.blind_indexes(b"4111", b"cards\x00number")
    assert every[0] == current
    assert every[1].startswith("k0:")
    assert every[1].split(":")[1] != current.split(":")[1]


def test_a_subjects_key_is_made_once_and_seals_their_values() -> None:
    """``dk1:s.<id>:``; the key is stored wrapped under the current data key."""
    store = MemoryStore()
    keys = ring(subject_keys=store)
    here = connection()
    sealed = keys.seal(b"P123", CONTEXT, connection=here, subject="ann")
    kind, key_id = cast(tuple[str, str], sealed_key(sealed))
    assert kind == "s"
    stored = store.keys[key_id]
    assert (stored.subject, stored.wrapping_key, len(stored.wrapped)) == (
        "ann",
        "k1",
        60,
    )
    assert repr(stored) == f"StoredSubjectKey(id={key_id!r}, wrapping_key='k1')"
    assert keys.seal(b"P456", CONTEXT, connection=here, subject="ann").startswith(
        f"dk1:s.{key_id}:"
    )
    assert keys.open(sealed, CONTEXT, connection=here) == b"P123"
    assert store.reads == 1  # The transaction keeps what it read.
    assert keys.open(sealed, CONTEXT, connection=connection()) == b"P123"
    again = keys.seal(b"P789", CONTEXT, connection=connection(), subject="ann")
    assert again.startswith(f"dk1:s.{key_id}:")
    assert len(store.keys) == 1


def test_a_destroyed_subjects_key_opens_nothing() -> None:
    """Once the key is gone, the value reads as None."""
    store = MemoryStore()
    keys = ring(subject_keys=store)
    sealed = keys.seal(b"P123", CONTEXT, connection=connection(), subject="ann")
    store.keys.clear()
    assert keys.open(sealed, CONTEXT, connection=connection()) is None


def test_forgetting_a_subject_drops_what_open_transactions_hold() -> None:
    """A transaction that read the key reads again after ``forget_held``."""
    store = MemoryStore()
    keys = ring(subject_keys=store)
    transaction = Transaction()
    sealed = keys.seal(b"P", CONTEXT, connection=connection(transaction), subject="ann")
    keys.seal(b"Q", CONTEXT, connection=connection(transaction), subject="bob")
    forget_held({"ann"})
    store.keys = {
        key_id: key for key_id, key in store.keys.items() if key.subject != "ann"
    }
    assert keys.open(sealed, CONTEXT, connection=connection(transaction)) is None
    reads = store.reads
    keys.seal(b"R", CONTEXT, connection=connection(transaction), subject="bob")
    assert store.reads == reads  # Bob's key is still held.


def test_a_connection_without_a_transaction_keeps_nothing() -> None:
    """Each read goes to the store."""
    store = MemoryStore()
    keys = ring(subject_keys=store)

    class Bare:
        def get_transaction(self) -> None:
            return None

    bare = cast(Connection, Bare())
    sealed = keys.seal(b"P", CONTEXT, connection=bare, subject="ann")
    keys.open(sealed, CONTEXT, connection=bare)
    assert store.reads == 2


def test_a_changed_subject_key_is_refused() -> None:
    """A wrapped key moved to another subject does not unwrap."""
    store = MemoryStore()
    keys = ring(subject_keys=store)
    keys.seal(b"P", CONTEXT, connection=connection(), subject="ann")
    stored = next(iter(store.keys.values()))
    moved = StoredSubjectKey(stored.id, "bob", stored.wrapping_key, stored.wrapped)
    with pytest.raises(DecryptionFailed, match="was changed"):
        keys.unwrap_subject_key(moved)


def test_sealing_for_a_subject_needs_a_store_and_a_connection() -> None:
    """Without either, the error says which."""
    keys = ring(subject_keys=None)
    with pytest.raises(ImproperlyConfigured, match=r"install domainkit\.contrib\.keys"):
        keys.seal(b"P", CONTEXT, connection=connection(), subject="ann")
    with pytest.raises(ImproperlyConfigured, match="on a connection"):
        ring().seal(b"P", CONTEXT, subject="ann")


def test_the_process_ring_is_set_up_by_the_keys_app(no_ring: None) -> None:
    """Without it, the error names the app; ``use_keyring`` sets it."""
    with pytest.raises(ImproperlyConfigured, match=r"install domainkit\.contrib\.keys"):
        keyring()
    keys = ring()
    use_keyring(keys)
    assert keyring() is keys


def test_alternates_open_and_index_but_never_seal() -> None:
    """An older key of an id opens its values and subjects' keys, and is matched."""
    older = Keyring(
        LocalKeyProvider([KEK]),
        {},
        "dev",
        known={"dev": b"o" * 32},
        subject_keys=MemoryStore(),
    )
    store = cast(MemoryStore, older.subject_keys)
    sealed = older.seal(b"v", CONTEXT)
    by_subject = older.seal(b"w", CONTEXT, connection=connection(), subject="ann")
    index = older.blind_index(b"v", b"t\x00c")
    newer = Keyring(
        LocalKeyProvider([KEK]),
        {},
        "dev",
        known={"dev": b"n" * 32},
        alternates={"dev": [b"x" * 32, b"o" * 32]},
        subject_keys=store,
    )
    assert newer.open(sealed, CONTEXT) == b"v"
    assert newer.open(by_subject, CONTEXT, connection=connection()) == b"w"
    assert index in newer.blind_indexes(b"v", b"t\x00c")
    assert newer.blind_index(b"v", b"t\x00c") != index
    assert newer.seal(b"v", CONTEXT) != sealed
    with pytest.raises(DecryptionFailed):
        Keyring(LocalKeyProvider([KEK]), {}, "dev", known={"dev": b"n" * 32}).open(
            sealed, CONTEXT
        )
