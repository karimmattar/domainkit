"""Password hashing: formats, upgrades, Django's hashes, and settings."""

import asyncio
import base64
import hashlib
import threading
from collections.abc import Callable
from typing import ClassVar

import pytest
from argon2 import low_level

from domainkit.clock import freeze_time
from domainkit.contrib.auth import hashers
from domainkit.contrib.auth.hashers import (
    SALT_CHARS,
    Argon2PasswordHasher,
    FastPBKDF2PasswordHasher,
    HashingPool,
    PasswordCheck,
    PasswordChecksBusy,
    PBKDF2PasswordHasher,
    PBKDF2SHA1PasswordHasher,
    ScryptPasswordHasher,
    acheck_password,
    amake_password,
    averify_password,
    check_password,
    identify_hasher,
    insecure_hashers,
    is_password_usable,
    load_hashers,
    make_password,
    verify_password,
)
from domainkit.contrib.auth.settings import AuthSettings
from domainkit.exceptions import ImproperlyConfigured
from domainkit_tests.conftest import UseAuth

HERE = "domainkit_tests.test_auth_hashers"
FAST = "domainkit.contrib.auth.hashers.FastPBKDF2PasswordHasher"
SCRYPT = "domainkit.contrib.auth.hashers.ScryptPasswordHasher"
SHA1 = "domainkit.contrib.auth.hashers.PBKDF2SHA1PasswordHasher"
PASSWORD = "correct horse"
DJANGO_HASHES = {
    # Made by Django 6.1.2 for "correct horse" with the salt "seasalt"
    # (Argon2: "seasaltseasalt", m=64, t=1, p=1).
    "pbkdf2_sha256": "pbkdf2_sha256$1000$seasalt$"
    "mQnueSakb748zqBAC1tmWVZsZbi2zPGZarEzTGdfmso=",
    "pbkdf2_sha1": "pbkdf2_sha1$1000$seasalt$iQvkNOF1wEL4Khh8eogJ8rUhipM=",
    "scrypt": "scrypt$16384$seasalt$8$5$nXachnDoZ26UFUF1o3PD3FjYfKq/mD4nZm22d5L0rp+"
    "Bi3DZTjkayk+fTW77FlT+2IelThLoihBYgWjZHfwh3Q==",
    "argon2": "argon2$argon2id$v=19$m=64,t=1,p=1$c2Vhc2FsdHNlYXNhbHQ$"
    "KFjpyjz2bIvzLO3nzmUHYyVdmFJAQzchDPVpwlCc0G0",
}


class TwoRoundsHasher(PBKDF2PasswordHasher):
    """PBKDF2 at two iterations: newer than the fast hasher's one."""

    iterations: ClassVar[int] = 2


class SmallArgon2Hasher(Argon2PasswordHasher):
    """Argon2 cheap enough for tests."""

    time_cost: ClassVar[int] = 1
    memory_cost: ClassVar[int] = 64
    parallelism: ClassVar[int] = 1


class SmallScryptHasher(ScryptPasswordHasher):
    """scrypt cheap enough for tests."""

    work_factor: ClassVar[int] = 2**4


class ReversedHasher(hashers.PasswordHasher):
    """A toy hasher that keeps every default."""

    algorithm: ClassVar[str] = "reversed"

    def encode(self, password: str, salt: str) -> str:
        """``reversed$<salt>$<password reversed>``."""
        return f"reversed${salt}${password[::-1]}"

    def verify(self, password: str, encoded: str) -> bool:
        """Compare the reversed text."""
        return encoded.rpartition("$")[2] == password[::-1]


NotAHasher = object()


def test_hashes_made_by_django_check_here() -> None:
    """Users can move from a Django database without new passwords."""
    for hasher in (
        PBKDF2PasswordHasher(),
        PBKDF2SHA1PasswordHasher(),
        ScryptPasswordHasher(),
        SmallArgon2Hasher(),
    ):
        encoded = DJANGO_HASHES[hasher.algorithm]
        assert hasher.verify(PASSWORD, encoded)
        assert not hasher.verify("wrong horse", encoded)


def test_pbkdf2_writes_djangos_format() -> None:
    """``pbkdf2_sha256$<iterations>$<salt>$<base64 of the derived key>``."""
    derived = hashlib.pbkdf2_hmac("sha256", b"pw", b"salt", 3)
    expected = f"pbkdf2_sha256$3$salt${base64.b64encode(derived).decode()}"

    assert PBKDF2PasswordHasher().encode("pw", "salt", 3) == expected
    assert PBKDF2PasswordHasher.iterations == 1_500_000


def test_hashes_split_as_djangos_do() -> None:
    """Parameters come from the first fields; the hash is all that follows."""
    pbkdf2, scrypt = PBKDF2PasswordHasher(), ScryptPasswordHasher()

    assert pbkdf2.decode("pbkdf2_sha256$7$salt$ab$cd") == (7, "salt")
    assert scrypt.decode("scrypt$16$salt$2$3$ab$cd") == (16, "salt", 2, 3)
    with pytest.raises(ValueError, match=r"^Not a pbkdf2_sha256 hash\.$"):
        pbkdf2.decode(DJANGO_HASHES["pbkdf2_sha1"])
    with pytest.raises(ValueError, match=r"^Not a scrypt hash\.$"):
        scrypt.decode("argon2$argon2id$v=19$m=64,t=1,p=1$c2FsdA$aGFzaA")
    assert not pbkdf2.verify(PASSWORD, "pbkdf2_sha256$1$salt")


def test_scrypt_checks_with_the_stored_costs() -> None:
    """A hash keeps its own block size and parallelism."""
    hasher = SmallScryptHasher()
    encoded = hasher.encode(PASSWORD, "seasalt", 16, 2, 3)

    assert encoded.startswith("scrypt$16$seasalt$2$3$")
    assert hasher.verify(PASSWORD, encoded)
    assert hasher.must_update(encoded)


def test_argon2_writes_djangos_hashes() -> None:
    """The same salt and costs give Django's hash, byte for byte."""
    encoded = SmallArgon2Hasher().encode(PASSWORD, "seasaltseasalt")

    assert encoded == DJANGO_HASHES["argon2"]


def test_salts_carry_128_bits() -> None:
    """22 characters of 62 kinds, new each time."""
    salt = PBKDF2PasswordHasher().salt()

    assert PBKDF2PasswordHasher().salt_length == 22
    assert len(salt) == 22
    assert set(salt) <= set(SALT_CHARS)
    assert salt != PBKDF2PasswordHasher().salt()


def test_hashers_refuse_other_algorithms_hashes() -> None:
    """A hash of another algorithm never matches."""
    assert not FastPBKDF2PasswordHasher().verify(PASSWORD, DJANGO_HASHES["scrypt"])
    assert not ScryptPasswordHasher().verify(PASSWORD, DJANGO_HASHES["pbkdf2_sha1"])
    assert not SmallArgon2Hasher().verify(PASSWORD, DJANGO_HASHES["scrypt"])
    assert not SmallArgon2Hasher().verify(PASSWORD, "argon2$garbage")


def test_must_update_follows_the_cost_settings() -> None:
    """Fewer iterations, a short salt, or other costs ask for a new hash."""
    fast = FastPBKDF2PasswordHasher()
    assert not fast.must_update(fast.hash(PASSWORD))
    assert fast.must_update(fast.encode(PASSWORD, "short"))
    assert TwoRoundsHasher().must_update(fast.hash(PASSWORD))

    scrypt = SmallScryptHasher()
    assert not scrypt.must_update(scrypt.hash(PASSWORD))
    assert ScryptPasswordHasher().must_update(scrypt.hash(PASSWORD))

    argon2 = SmallArgon2Hasher()
    assert not argon2.must_update(argon2.hash(PASSWORD))
    assert Argon2PasswordHasher().must_update(argon2.hash(PASSWORD))


@pytest.mark.parametrize(
    ("variant", "salt", "length"),
    [
        (low_level.Type.I, b"s" * 16, 32),
        (low_level.Type.D, b"s" * 16, 32),
        (low_level.Type.ID, b"s" * 8, 32),
        (low_level.Type.ID, b"s" * 15, 32),  # 120 bits: one byte short.
        (low_level.Type.ID, b"s" * 16, 16),
    ],
)
def test_argon2_upgrades_other_variants_short_salts_and_lengths(
    variant: low_level.Type, salt: bytes, length: int
) -> None:
    """Costs alone used to decide: ``argon2i`` at today's costs was kept (AUTH-07)."""
    argon2 = SmallArgon2Hasher()
    hashed = low_level.hash_secret(
        PASSWORD.encode(),
        salt,
        time_cost=argon2.time_cost,
        memory_cost=argon2.memory_cost,
        parallelism=argon2.parallelism,
        hash_len=length,
        type=variant,
    )
    encoded = argon2.algorithm + hashed.decode()

    assert argon2.verify(PASSWORD, encoded)
    assert argon2.must_update(encoded)
    current = low_level.hash_secret(
        PASSWORD.encode(),
        b"s" * 16,  # 128 bits: as many as a salt of ours holds.
        time_cost=argon2.time_cost,
        memory_cost=argon2.memory_cost,
        parallelism=argon2.parallelism,
        hash_len=32,
        type=low_level.Type.ID,
    )
    assert not argon2.must_update(argon2.algorithm + current.decode())


def test_scrypt_and_argon2_round_trip() -> None:
    """Each hasher checks what it made."""
    for hasher in (SmallScryptHasher(), SmallArgon2Hasher()):
        encoded = hasher.hash(PASSWORD)
        assert encoded.startswith(f"{hasher.algorithm}$")
        assert hasher.verify(PASSWORD, encoded)
        assert not hasher.verify("wrong", encoded)


def test_harden_runtime_runs_the_missing_iterations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only an older hash costs extra iterations; a current one costs none."""
    rounds: list[int | None] = []
    hasher = TwoRoundsHasher()
    original = hasher.encode

    def counting(password: str, salt: str, iterations: int | None = None) -> str:
        rounds.append(iterations)
        return original(password, salt, iterations)

    monkeypatch.setattr(hasher, "encode", counting)
    hasher.harden_runtime(PASSWORD, FastPBKDF2PasswordHasher().hash(PASSWORD))
    hasher.harden_runtime(PASSWORD, TwoRoundsHasher().hash(PASSWORD))

    assert rounds == [1]


def test_verify_password_upgrades_outdated_hashes(use_auth: UseAuth) -> None:
    """A correct password under an older hasher comes back re-hashed."""
    use_auth(PASSWORD_HASHERS=[f"{HERE}.TwoRoundsHasher", SCRYPT, FAST])
    current = make_password(PASSWORD)
    older = SmallScryptHasher().hash(PASSWORD)

    assert current.startswith("pbkdf2_sha256$2$")
    assert verify_password(PASSWORD, current) == PasswordCheck(correct=True)
    upgraded = verify_password(PASSWORD, older)
    assert upgraded.correct
    assert upgraded.rehashed is not None
    assert upgraded.rehashed.startswith("pbkdf2_sha256$2$")
    assert verify_password("wrong", older) == PasswordCheck(correct=False)


class SpyHashers:
    """What a check spends: ``harden_runtime`` calls, and hashes by hasher."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Count every hardening and every new hash."""
        self.hardened: list[str] = []
        self.hashed: list[str] = []
        harden_once = PBKDF2PasswordHasher.harden_runtime
        hash_once = hashers.PasswordHasher.hash

        def harden(self_: PBKDF2PasswordHasher, password: str, encoded: str) -> None:
            self.hardened.append(encoded)
            harden_once(self_, password, encoded)

        def hashing(self_: hashers.PasswordHasher, password: str) -> str:
            self.hashed.append(type(self_).__name__)
            return hash_once(self_, password)

        monkeypatch.setattr(PBKDF2PasswordHasher, "harden_runtime", harden)
        monkeypatch.setattr(hashers.PasswordHasher, "hash", hashing)

    def spent(self) -> tuple[list[str], list[str]]:
        """What was spent since the last call."""
        spent = (self.hardened, self.hashed)
        self.hardened, self.hashed = [], []
        return spent


def test_wrong_passwords_on_outdated_hashes_cost_full_time(
    use_auth: UseAuth, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A miss on any outdated hash costs what a current hash would (AUTH-07).

    The preferred hasher hardens a miss on its own older hash; a miss on
    another hasher's hash costs a preferred hash, as a right password does
    (it is hashed again). A legacy hash used to answer at once.
    """
    use_auth(PASSWORD_HASHERS=[f"{HERE}.TwoRoundsHasher", SHA1, SCRYPT])
    spy = SpyHashers(monkeypatch)
    older = FastPBKDF2PasswordHasher().hash(PASSWORD)
    current = TwoRoundsHasher().hash(PASSWORD)
    legacy = PBKDF2SHA1PasswordHasher().encode(PASSWORD, "seasalt", 1)
    other = SmallScryptHasher().hash(PASSWORD)
    spy.spent()

    assert not check_password("wrong", older)
    assert spy.spent() == ([older], [])
    assert not check_password("wrong", current)
    assert spy.spent() == ([], [])
    for encoded in (legacy, other):
        assert not check_password("wrong", encoded)
        assert spy.spent() == ([], ["TwoRoundsHasher"])
        assert check_password(PASSWORD, encoded)  # The same work for a right one.
        assert spy.spent() == ([], ["TwoRoundsHasher"])


def test_a_preferred_hasher_without_hardening_hashes_a_miss(
    use_auth: UseAuth, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An outdated Argon2 hash under Argon2 costs one current hash on a miss."""
    use_auth(PASSWORD_HASHERS=[f"{HERE}.SmallArgon2Hasher"])
    spy = SpyHashers(monkeypatch)
    hashed = low_level.hash_secret(
        PASSWORD.encode(),
        b"s" * 16,
        time_cost=1,
        memory_cost=64,
        parallelism=1,
        hash_len=32,
        type=low_level.Type.I,
    )
    variant = "argon2" + hashed.decode()
    current = SmallArgon2Hasher().hash(PASSWORD)
    spy.spent()

    assert not check_password("wrong", variant)
    assert spy.spent() == ([], ["SmallArgon2Hasher"])
    assert not check_password("wrong", current)
    assert spy.spent() == ([], [])
    upgraded = verify_password(PASSWORD, variant)
    assert upgraded.rehashed is not None
    assert "$argon2id$" in upgraded.rehashed


def test_unusable_unknown_and_malformed_hashes_never_match(
    use_auth: UseAuth, monkeypatch: pytest.MonkeyPatch
) -> None:
    """None, ``!...``, unknown algorithms, and broken hashes all fail.

    Each costs one hash with the preferred hasher, as a wrong password does,
    so response times do not tell which accounts have no usable password.
    """
    use_auth(PASSWORD_HASHERS=[f"{HERE}.TwoRoundsHasher"])
    hashed: list[str] = []
    hash_once = TwoRoundsHasher.hash

    def counted(self: TwoRoundsHasher, password: str) -> str:
        hashed.append(password)
        return hash_once(self, password)

    monkeypatch.setattr(TwoRoundsHasher, "hash", counted)
    for encoded in (None, "!abc", "md5$x$y", "pbkdf2_sha256$x"):
        assert verify_password(PASSWORD, encoded) == PasswordCheck(correct=False)
    assert hashed == [PASSWORD] * 4
    assert not is_password_usable(None)
    assert not is_password_usable("!abc")
    assert is_password_usable(make_password(PASSWORD))
    with pytest.raises(ValueError, match="No configured hasher"):
        identify_hasher("md5$x")


def test_async_helpers_run_in_threads(use_auth: UseAuth) -> None:
    """``amake_password`` and friends give the same answers."""
    use_auth()

    async def run() -> tuple[bool, bool, PasswordCheck]:
        encoded = await amake_password(PASSWORD)
        return (
            await acheck_password(PASSWORD, encoded),
            await acheck_password("wrong", encoded),
            await averify_password(PASSWORD, encoded),
        )

    right, wrong, check = asyncio.run(run())
    assert (right, wrong, check) == (True, False, PasswordCheck(correct=True))


def test_hasher_settings_are_checked(use_auth: UseAuth) -> None:
    """Unknown paths and other objects are configuration errors."""
    use_auth()
    with pytest.raises(ImproperlyConfigured, match="PASSWORD_HASHERS"):
        load_hashers(("nowhere.Hasher",))
    with pytest.raises(ImproperlyConfigured, match="is not a PasswordHasher"):
        load_hashers((f"{HERE}.NotAHasher",))
    assert insecure_hashers([FAST, SHA1]) == [FAST]
    assert hashers.get_hashers()[0].insecure


def test_hashers_only_need_encode_and_verify() -> None:
    """``must_update`` and ``harden_runtime`` have safe defaults."""
    hasher = ReversedHasher()
    encoded = hasher.hash(PASSWORD)

    assert hasher.verify(PASSWORD, encoded)
    assert not hasher.must_update(encoded)
    hasher.harden_runtime(PASSWORD, encoded)


def test_hashing_pools_refuse_calls_beyond_their_queue() -> None:
    """Threads plus queue calls may be pending; the next is refused at once."""
    pool = HashingPool(threads=1, queue=1)
    started, release = threading.Event(), threading.Event()

    def slow(value: str) -> str:
        started.set()
        release.wait(5)
        return value

    async def scenario() -> tuple[list[str], int]:
        running = asyncio.create_task(pool.run(slow, "first"))
        waiting = asyncio.create_task(pool.run(slow, "second"))
        await asyncio.to_thread(started.wait, 5)
        await asyncio.sleep(0)
        with pytest.raises(PasswordChecksBusy) as busy:
            await pool.run(slow, "third")
        assert pool.pending == 2
        assert busy.value.code == "password_checks_busy"
        assert busy.value.message == (
            "Too many password checks at once; try again in a moment."
        )
        assert busy.value.retry_after == 1
        release.set()
        return list(await asyncio.gather(running, waiting)), pool.pending

    assert asyncio.run(scenario()) == (["first", "second"], 0)


def test_pending_calls_free_their_place_when_cancelled_or_failing() -> None:
    """A cancelled or failed call no longer counts against the queue."""
    pool = HashingPool(threads=1, queue=0)

    def fail() -> None:
        raise ValueError("bad hash")

    async def scenario() -> int:
        with pytest.raises(ValueError, match="bad hash"):
            await pool.run(fail)
        return pool.pending

    assert asyncio.run(scenario()) == 0


def test_a_cancelled_call_keeps_its_place_until_its_hash_is_done() -> None:
    """Giving up frees nothing while a thread still hashes for the caller (N7)."""
    pool = HashingPool(threads=1, queue=0)
    started, release = threading.Event(), threading.Event()
    hashed: list[str] = []

    def slow(value: str) -> str:
        started.set()
        release.wait(5)
        hashed.append(value)
        return value

    async def scenario() -> int:
        caller = asyncio.create_task(pool.run(slow, "abandoned"))
        await asyncio.to_thread(started.wait, 5)
        caller.cancel()
        await asyncio.wait([caller], timeout=5)
        assert caller.cancelled()
        assert pool.pending == 1  # The thread still hashes for it.
        with pytest.raises(PasswordChecksBusy):
            await pool.run(slow, "refused")
        release.set()
        # The one thread takes this only after the first hash's place is given back.
        await asyncio.wrap_future(pool._executor.submit(lambda: None))
        return pool.pending

    assert asyncio.run(scenario()) == 0
    assert hashed == ["abandoned"]


def test_a_call_cancelled_before_its_turn_frees_its_place_at_once() -> None:
    """A queued call that never started gives its place back when cancelled."""
    pool = HashingPool(threads=1, queue=1)
    started, release = threading.Event(), threading.Event()

    def slow() -> None:
        started.set()
        release.wait(5)

    async def scenario() -> int:
        running = asyncio.create_task(pool.run(slow))
        await asyncio.to_thread(started.wait, 5)
        queued = asyncio.create_task(pool.run(slow))
        await asyncio.sleep(0)
        assert pool.pending == 2
        queued.cancel()
        await asyncio.wait([queued], timeout=5)
        pending = pool.pending
        release.set()
        await running
        return pending

    assert asyncio.run(scenario()) == 1


def test_retry_after_grows_with_the_queue_and_is_jittered() -> None:
    """Until the pending hashes drain, up to half again; from 1 to 60 seconds."""
    pool = HashingPool(threads=2, queue=8)
    pool.seconds_per_hash = 2.0

    waits = {pool.retry_after(10) for _ in range(200)}
    assert min(waits) >= 10  # 10 hashes of 2 s on 2 threads.
    assert max(waits) <= 15
    assert len(waits) > 1
    assert pool.retry_after(0) == 1
    assert pool.retry_after(1000) == hashers.MAX_RETRY_AFTER == 60


def test_the_pool_learns_how_long_a_hash_takes() -> None:
    """A moving average of the hashes timed, starting from a guess."""
    pool = HashingPool(threads=1, queue=0)
    assert pool.seconds_per_hash == hashers.FIRST_ESTIMATE == 0.25

    async def quick() -> str:
        return await pool.run(str, 1)

    assert asyncio.run(quick()) == "1"
    assert 0.2 <= pool.seconds_per_hash < 0.25  # Pulled toward a quick hash.


def test_each_hash_moves_the_average_a_fifth_of_the_way() -> None:
    """A moving average: 80% of what it was, 20% of the hash just timed."""
    pool = HashingPool(threads=1, queue=0)

    def one_second() -> str:
        frozen.tick(1.0)
        return "hashed"

    with freeze_time() as frozen:
        assert pool._timed(one_second) == "hashed"

    assert pool.seconds_per_hash == pytest.approx(0.8 * 0.25 + 0.2 * 1.0)


def test_a_refusal_says_retry_after_a_second_by_default() -> None:
    """``PasswordChecksBusy()`` without a wait asks for one second."""
    assert PasswordChecksBusy().retry_after == 1
    assert PasswordChecksBusy(7).retry_after == 7


def test_the_a_functions_hash_as_priority_only_when_asked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``priority`` reaches the pool; it is off unless given."""
    asked: list[bool] = []

    class Recording(HashingPool):
        async def run[T](
            self, function: Callable[..., T], *args: object, priority: bool = False
        ) -> T:
            asked.append(priority)
            return function(*args)

    monkeypatch.setattr(hashers, "_pool", lambda: Recording(threads=1, queue=0))
    monkeypatch.setattr(hashers, "make_password", lambda password: "made")
    monkeypatch.setattr(
        hashers, "verify_password", lambda password, encoded: PasswordCheck(True)
    )

    async def scenario() -> None:
        await amake_password(PASSWORD)
        await amake_password(PASSWORD, priority=True)
        await averify_password(PASSWORD, None)
        await averify_password(PASSWORD, None, priority=True)
        await acheck_password(PASSWORD, None)
        await acheck_password(PASSWORD, None, priority=True)

    asyncio.run(scenario())
    assert asked == [False, True, False, True, False, True]


def test_priority_calls_may_take_the_reserved_places() -> None:
    """Logins fill the pool less the reserved places; password changes, all."""
    pool = HashingPool(threads=1, queue=1, reserved=1)
    started, release = threading.Event(), threading.Event()

    def slow() -> None:
        started.set()
        release.wait(5)

    async def scenario() -> None:
        running = asyncio.create_task(pool.run(slow))
        await asyncio.to_thread(started.wait, 5)
        with pytest.raises(PasswordChecksBusy):
            await pool.run(slow)  # One place left, and it is reserved.
        change = asyncio.create_task(pool.run(slow, priority=True))
        await asyncio.sleep(0)
        assert pool.pending == 2
        with pytest.raises(PasswordChecksBusy):
            await pool.run(slow, priority=True)
        release.set()
        await asyncio.gather(running, change)

    asyncio.run(scenario())
    assert pool.pending == 0


@pytest.mark.parametrize("reserved", [-1, 2])
def test_the_reserved_places_are_part_of_the_queue(reserved: int) -> None:
    """None, or up to the whole queue."""
    with pytest.raises(ValueError, match="reserved must be from 0 to queue"):
        HashingPool(threads=1, queue=1, reserved=reserved)


def test_a_call_the_pool_cannot_take_gives_its_place_back() -> None:
    """If the threads are gone, the call fails and frees its place."""
    pool = HashingPool(threads=1, queue=0)
    pool._executor.shutdown()

    async def scenario() -> None:
        await pool.run(str, 1)

    with pytest.raises(RuntimeError, match="shutdown"):
        asyncio.run(scenario())
    assert pool.pending == 0


def test_reserved_places_come_from_settings_within_the_queue(use_auth: UseAuth) -> None:
    """``HASHING_RESERVED``, 1 by default, never more than ``HASHING_QUEUE``."""
    use_auth(HASHING_THREADS=2, HASHING_QUEUE=4)
    assert hashers._pool().reserved == 1
    use_auth(HASHING_THREADS=2, HASHING_QUEUE=0, HASHING_RESERVED=3)
    assert hashers._pool().reserved == 0
    use_auth(HASHING_THREADS=2, HASHING_QUEUE=4, HASHING_RESERVED=3)
    assert (hashers._pool().capacity, hashers._pool().reserved) == (6, 3)


def test_passwords_hash_on_the_configured_pool(
    use_auth: UseAuth, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The a-functions use the pool AUTH describes, on its own threads."""
    use_auth(HASHING_THREADS=3, HASHING_QUEUE=5)
    threads: list[str] = []

    def record(*args: object) -> str:
        threads.append(threading.current_thread().name)
        return "made"

    monkeypatch.setattr(hashers, "make_password", record)
    monkeypatch.setattr(hashers, "verify_password", record)

    async def scenario() -> list[object]:
        return [
            await amake_password(PASSWORD),
            await averify_password(PASSWORD, None),
        ]

    assert asyncio.run(scenario()) == ["made", "made"]
    assert all(name.startswith("domainkit-hashing") for name in threads)
    pool = hashers._pool()
    assert pool is hashers.hashing_pool(3, 5, 1)
    assert pool.capacity == 8


def test_hashing_defaults_to_one_thread_and_a_short_queue() -> None:
    """One hash at a time per process, like a Django worker; eight may wait."""
    defaults = AuthSettings()

    assert (defaults.HASHING_THREADS, defaults.HASHING_QUEUE) == (1, 8)
    assert defaults.HASHING_RESERVED == 1
