"""Password hashing (Django's ``hashers``, with the same stored formats).

``AUTH.PASSWORD_HASHERS`` lists hasher classes by dotted path. The first
hashes new passwords; every one listed can check passwords it once hashed.
A password checked against an older or weaker hash is re-hashed with the
first hasher. Hashes from a Django project's database check here as they
are, so users can move over without resetting passwords.

Hashing is slow on purpose. The ``a``-prefixed functions run it on the
process's hashing threads (``AUTH.HASHING_THREADS``), so a login never blocks
the event loop. At most ``AUTH.HASHING_QUEUE`` calls wait for a thread; more
are refused at once with ``PasswordChecksBusy`` (503), so a flood of logins
cannot pile up work the process will never finish. Its ``Retry-After`` grows
with the queue, jittered, so refused clients do not all come back at once.
A call holds its place until its hash is done, even if its caller gave up,
and ``AUTH.HASHING_RESERVED`` places are kept for ``priority`` calls, such as
signed-in users changing their password during a flood of logins.
"""

import asyncio
import base64
import hashlib
import hmac
import math
import secrets
import string
import threading
from abc import ABC, abstractmethod
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from functools import cache, partial
from typing import ClassVar, Final

from domainkit import clock
from domainkit.exceptions import ImproperlyConfigured, ServiceBusy
from domainkit.utils.module_loading import import_string

from .models import UNUSABLE_PASSWORD_PREFIX

__all__ = [
    "Argon2PasswordHasher",
    "FastPBKDF2PasswordHasher",
    "PBKDF2PasswordHasher",
    "PBKDF2SHA1PasswordHasher",
    "PasswordHasher",
    "ScryptPasswordHasher",
]

SALT_CHARS: Final = string.ascii_letters + string.digits


class PasswordHasher(ABC):
    """One hashing algorithm; ``algorithm`` starts every hash it makes."""

    algorithm: ClassVar[str]
    insecure: ClassVar[bool] = False
    """Too fast for real passwords (tests only); staging and production refuse it."""
    salt_entropy: ClassVar[int] = 128
    """Bits of randomness in each salt."""

    @property
    def salt_length(self) -> int:
        """Characters a salt needs to hold ``salt_entropy`` bits."""
        return math.ceil(self.salt_entropy / math.log2(len(SALT_CHARS)))

    def salt(self) -> str:
        """A new random salt of ``salt_entropy`` bits."""
        return "".join(secrets.choice(SALT_CHARS) for _ in range(self.salt_length))

    def hash(self, password: str) -> str:
        """Hash ``password`` with a new salt."""
        return self.encode(password, self.salt())

    @abstractmethod
    def encode(self, password: str, salt: str) -> str:
        """Hash ``password`` with ``salt``; ``<algorithm>$...``."""

    @abstractmethod
    def verify(self, password: str, encoded: str) -> bool:
        """Whether ``password`` matches ``encoded``, compared in constant time."""

    def must_update(self, encoded: str) -> bool:
        """Whether ``encoded`` was made with weaker settings than these."""
        return False

    def harden_runtime(self, password: str, encoded: str) -> None:  # noqa: B027 - optional.
        """Spend the time an up-to-date hash would take (no timing hints)."""

    def _salt_too_short(self, salt: str) -> bool:
        """Whether ``salt`` holds fewer bits than ``salt_entropy``."""
        return len(salt) < self.salt_length


class PBKDF2PasswordHasher(PasswordHasher):
    """PBKDF2 with HMAC-SHA256 (the default; NIST SP 800-132), from the stdlib."""

    algorithm: ClassVar[str] = "pbkdf2_sha256"
    iterations: ClassVar[int] = 1_500_000
    digest: ClassVar[str] = "sha256"

    def encode(self, password: str, salt: str, iterations: int | None = None) -> str:
        """``<algorithm>$<iterations>$<salt>$<base64 hash>``."""
        rounds = iterations or self.iterations
        derived = hashlib.pbkdf2_hmac(
            self.digest, password.encode(), salt.encode(), rounds
        )
        return f"{self.algorithm}${rounds}${salt}${base64.b64encode(derived).decode()}"

    def decode(self, encoded: str) -> tuple[int, str]:
        """The iterations and salt of ``encoded``.

        Raises:
            ValueError: ``encoded`` is not a hash of this algorithm.
        """
        algorithm, iterations, salt, _ = encoded.split("$", 3)
        if algorithm != self.algorithm:
            raise ValueError(f"Not a {self.algorithm} hash.")
        return int(iterations), salt

    def verify(self, password: str, encoded: str) -> bool:
        """Hash again with the stored salt and iterations, and compare."""
        try:
            iterations, salt = self.decode(encoded)
        except ValueError:
            return False
        again = self.encode(password, salt, iterations)
        return hmac.compare_digest(encoded.encode(), again.encode())

    def must_update(self, encoded: str) -> bool:
        """Fewer iterations than now, or a short salt."""
        iterations, salt = self.decode(encoded)
        return iterations != self.iterations or self._salt_too_short(salt)

    def harden_runtime(self, password: str, encoded: str) -> None:
        """Run the iterations an older hash lacks."""
        iterations, salt = self.decode(encoded)
        if (extra := self.iterations - iterations) > 0:
            self.encode(password, salt, extra)


class PBKDF2SHA1PasswordHasher(PBKDF2PasswordHasher):
    """PBKDF2 with HMAC-SHA1, to check hashes moved from older systems."""

    algorithm: ClassVar[str] = "pbkdf2_sha1"
    digest: ClassVar[str] = "sha1"


class ScryptPasswordHasher(PasswordHasher):
    """scrypt (RFC 7914): memory-hard, from the stdlib."""

    algorithm: ClassVar[str] = "scrypt"
    work_factor: ClassVar[int] = 2**14
    block_size: ClassVar[int] = 8
    parallelism: ClassVar[int] = 5

    def encode(
        self,
        password: str,
        salt: str,
        n: int | None = None,
        r: int | None = None,
        p: int | None = None,
    ) -> str:
        """``scrypt$<n>$<salt>$<r>$<p>$<base64 hash>``."""
        n, r, p = n or self.work_factor, r or self.block_size, p or self.parallelism
        # hashlib's defaults, as Django: 64-byte keys, OpenSSL's memory limit.
        derived = hashlib.scrypt(password.encode(), salt=salt.encode(), n=n, r=r, p=p)
        hashed = base64.b64encode(derived).decode()
        return f"{self.algorithm}${n}${salt}${r}${p}${hashed}"

    def decode(self, encoded: str) -> tuple[int, str, int, int]:
        """The work factor, salt, block size, and parallelism of ``encoded``.

        Raises:
            ValueError: ``encoded`` is not a hash of this algorithm.
        """
        algorithm, n, salt, r, p, _ = encoded.split("$", 5)
        if algorithm != self.algorithm:
            raise ValueError(f"Not a {self.algorithm} hash.")
        return int(n), salt, int(r), int(p)

    def verify(self, password: str, encoded: str) -> bool:
        """Hash again with the stored parameters, and compare."""
        try:
            n, salt, r, p = self.decode(encoded)
        except ValueError:
            return False
        again = self.encode(password, salt, n, r, p)
        return hmac.compare_digest(encoded.encode(), again.encode())

    def must_update(self, encoded: str) -> bool:
        """Different cost parameters than now."""
        n, _, r, p = self.decode(encoded)
        return (n, r, p) != (self.work_factor, self.block_size, self.parallelism)


class Argon2PasswordHasher(PasswordHasher):
    """Argon2id (RFC 9106), the Password Hashing Competition's winner.

    Needs the ``argon2`` extra (``argon2-cffi``). Put it first in
    ``PASSWORD_HASHERS`` to hash new passwords with it.
    """

    algorithm: ClassVar[str] = "argon2"
    time_cost: ClassVar[int] = 2
    memory_cost: ClassVar[int] = 102_400
    parallelism: ClassVar[int] = 8

    def encode(self, password: str, salt: str) -> str:
        """``argon2$argon2id$v=19$m=...,t=...,p=...$<salt>$<hash>``."""
        from argon2 import low_level

        hashed = low_level.hash_secret(
            password.encode(),
            salt.encode(),
            time_cost=self.time_cost,
            memory_cost=self.memory_cost,
            parallelism=self.parallelism,
            hash_len=32,
            type=low_level.Type.ID,
        )
        return self.algorithm + hashed.decode()

    def verify(self, password: str, encoded: str) -> bool:
        """Let argon2-cffi check the hash."""
        from argon2 import PasswordHasher as Argon2
        from argon2.exceptions import InvalidHashError, VerificationError

        algorithm, _, rest = encoded.partition("$")
        if algorithm != self.algorithm:
            return False
        try:
            return Argon2().verify("$" + rest, password)
        except (VerificationError, InvalidHashError):
            return False

    def must_update(self, encoded: str) -> bool:
        """Different cost parameters than now."""
        from argon2 import extract_parameters

        parameters = extract_parameters("$" + encoded.partition("$")[2])
        return (
            parameters.time_cost,
            parameters.memory_cost,
            parameters.parallelism,
        ) != (self.time_cost, self.memory_cost, self.parallelism)


class FastPBKDF2PasswordHasher(PBKDF2PasswordHasher):
    """One PBKDF2 iteration: for tests only, where hashing time adds up.

    Staging and production refuse it.
    """

    iterations: ClassVar[int] = 1
    insecure: ClassVar[bool] = True


@dataclass(frozen=True, slots=True)
class PasswordCheck:
    """The outcome of checking a password."""

    correct: bool
    rehashed: str | None = field(default=None, repr=False)
    """A new hash of the correct password with current settings, if needed."""


@cache
def load_hashers(paths: tuple[str, ...]) -> tuple[PasswordHasher, ...]:
    """Instantiate the hasher classes named in ``paths``."""
    hashers = []
    for path in paths:
        try:
            hasher = import_string(path)
        except ImportError as exc:
            raise ImproperlyConfigured(f"AUTH.PASSWORD_HASHERS: {exc}") from exc
        if not (isinstance(hasher, type) and issubclass(hasher, PasswordHasher)):
            raise ImproperlyConfigured(
                f"AUTH.PASSWORD_HASHERS: {path!r} is not a PasswordHasher."
            )
        hashers.append(hasher())
    return tuple(hashers)


def get_hashers() -> tuple[PasswordHasher, ...]:
    """The configured hashers, the preferred one first."""
    from .settings import auth_settings

    return load_hashers(auth_settings().PASSWORD_HASHERS)


def identify_hasher(encoded: str) -> PasswordHasher:
    """The configured hasher that made ``encoded``.

    Raises:
        ValueError: No configured hasher made it.
    """
    algorithm = encoded.partition("$")[0]
    for hasher in get_hashers():
        if hasher.algorithm == algorithm:
            return hasher
    raise ValueError(f"No configured hasher for {algorithm!r} hashes.")


def is_password_usable(encoded: str | None) -> bool:
    """Whether ``encoded`` is a hash some password can match."""
    return encoded is not None and not encoded.startswith(UNUSABLE_PASSWORD_PREFIX)


def make_password(password: str, hasher: PasswordHasher | None = None) -> str:
    """Hash ``password`` with ``hasher`` (default: the preferred one)."""
    return (hasher or get_hashers()[0]).hash(password)


def verify_password(password: str, encoded: str | None) -> PasswordCheck:
    """Check ``password`` against ``encoded``; re-hash it if settings changed.

    A wrong password takes as long as a right one would with current
    settings, so response times do not tell how old a hash is. A hash no
    password matches (unusable, malformed, or of a hasher not configured)
    costs one hash too, so they do not tell which accounts have one.
    """
    preferred = get_hashers()[0]
    if encoded is None or encoded.startswith(UNUSABLE_PASSWORD_PREFIX):
        preferred.hash(password)
        return PasswordCheck(correct=False)
    try:
        hasher = identify_hasher(encoded)
        outdated = hasher is not preferred or preferred.must_update(encoded)
        correct = hasher.verify(password, encoded)
    except ValueError:  # A malformed hash never matches.
        preferred.hash(password)
        return PasswordCheck(correct=False)
    if not correct:
        if hasher is preferred and outdated:
            hasher.harden_runtime(password, encoded)
        return PasswordCheck(correct=False)
    return PasswordCheck(
        correct=True, rehashed=preferred.hash(password) if outdated else None
    )


def check_password(password: str, encoded: str | None) -> bool:
    """Whether ``password`` matches ``encoded``."""
    return verify_password(password, encoded).correct


class PasswordChecksBusy(ServiceBusy):
    """Every hashing thread is busy and the queue is full.

    The HTTP layer answers it with 503 and ``Retry-After: retry_after``.
    """

    code = "password_checks_busy"

    def __init__(self, retry_after: int = 1) -> None:
        """Ask the caller to try again in ``retry_after`` seconds."""
        super().__init__(
            "Too many password checks at once; try again in a moment.",
            retry_after=retry_after,
        )


FIRST_ESTIMATE: Final = 0.25
"""Seconds a hash is guessed to take until the pool has timed one."""
MAX_RETRY_AFTER: Final = 60
"""The longest ``Retry-After`` a refusal asks for, in seconds."""
_random = secrets.SystemRandom()


class HashingPool:
    """``threads`` threads that hash, and room for ``queue`` calls to wait.

    A call holds its place from when it is accepted until its hash is done:
    a caller cancelled meanwhile frees nothing while a thread still works
    for it, so the work in flight never exceeds ``threads + queue``. The
    last ``reserved`` places take ``priority`` calls only.
    """

    def __init__(self, threads: int, queue: int, reserved: int = 0) -> None:
        """Start the threads lazily, on the first call.

        Raises:
            ValueError: ``reserved`` is negative or more than ``queue``.
        """
        if not 0 <= reserved <= queue:
            raise ValueError(f"reserved must be from 0 to queue ({queue}).")
        self.threads = threads
        self.capacity = threads + queue
        self.reserved = reserved
        self.pending = 0
        """Calls accepted whose hash is not done, in this process."""
        self.seconds_per_hash = FIRST_ESTIMATE
        """A moving average of how long one hash takes."""
        # Every change of ``pending`` and the estimate holds it: event loops on
        # several threads (free-threaded Python) count in the same pool.
        self._lock = threading.Lock()
        self._executor = ThreadPoolExecutor(
            max_workers=threads, thread_name_prefix="domainkit-hashing"
        )

    async def run[T](
        self, function: Callable[..., T], *args: object, priority: bool = False
    ) -> T:
        """Call ``function(*args)`` on a hashing thread.

        Raises:
            PasswordChecksBusy: Every place it may take is pending already:
                ``threads + queue``, less ``reserved`` unless ``priority``.
        """
        limit = self.capacity if priority else self.capacity - self.reserved
        with self._lock:
            if self.pending >= limit:
                raise PasswordChecksBusy(self.retry_after(self.pending))
            self.pending += 1
        try:
            future = self._executor.submit(self._timed, partial(function, *args))
        except BaseException:
            self._release()
            raise
        future.add_done_callback(self._release)
        return await asyncio.wrap_future(future)

    def retry_after(self, pending: int) -> int:
        """Seconds to ask a refused caller to wait: until ``pending`` hashes drain.

        Jittered up to half again, so refused callers spread out; at least
        1 and at most ``MAX_RETRY_AFTER``.
        """
        drain = pending * self.seconds_per_hash / self.threads
        return min(MAX_RETRY_AFTER, max(1, math.ceil(drain * _random.uniform(1, 1.5))))

    def _timed[T](self, call: Callable[[], T]) -> T:
        """Run ``call`` on this thread, and fold its time into the average."""
        started = clock.monotonic()
        try:
            return call()
        finally:
            took = clock.monotonic() - started
            with self._lock:
                self.seconds_per_hash = 0.8 * self.seconds_per_hash + 0.2 * took

    def _release(self, future: object = None) -> None:
        """Give back a call's place, once its hash is done (or never started)."""
        with self._lock:
            self.pending -= 1


@cache
def hashing_pool(threads: int, queue: int, reserved: int = 0) -> HashingPool:
    """The process's pool for these settings."""
    return HashingPool(threads, queue, reserved)


def _pool() -> HashingPool:
    """The pool ``AUTH.HASHING_THREADS``, ``_QUEUE``, and ``_RESERVED`` describe."""
    from .settings import auth_settings

    config = auth_settings()
    reserved = min(config.HASHING_RESERVED, config.HASHING_QUEUE)
    return hashing_pool(config.HASHING_THREADS, config.HASHING_QUEUE, reserved)


async def amake_password(password: str, *, priority: bool = False) -> str:
    """``make_password`` on a hashing thread; raises ``PasswordChecksBusy``.

    ``priority`` may take the places ``AUTH.HASHING_RESERVED`` keeps.
    """
    return await _pool().run(make_password, password, priority=priority)


async def averify_password(
    password: str, encoded: str | None, *, priority: bool = False
) -> PasswordCheck:
    """``verify_password`` on a hashing thread; raises ``PasswordChecksBusy``."""
    return await _pool().run(verify_password, password, encoded, priority=priority)


async def acheck_password(
    password: str, encoded: str | None, *, priority: bool = False
) -> bool:
    """``check_password`` on a hashing thread; raises ``PasswordChecksBusy``."""
    return (await averify_password(password, encoded, priority=priority)).correct


def insecure_hashers(paths: Sequence[str]) -> list[str]:
    """The paths in ``paths`` that name hashers too fast for real passwords."""
    return [path for path in paths if load_hashers((path,))[0].insecure]
