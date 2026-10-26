"""Principal reads under load: one read per key, and kept ones read again ahead.

PERF-04: requests of one credential that miss at once read it once, and a
kept principal is read again by one request near the end of its ``TTL``
while the others are served it, never past it. A revocation is never
hidden by a read that began before it.
"""

import asyncio
from collections.abc import Callable, Coroutine, Iterator
from datetime import timedelta
from typing import Any

import pytest
from fakeredis import FakeServer

from domainkit.cache import CacheError
from domainkit.clock import FrozenClock, freeze_time
from domainkit.contrib.auth import AuthenticationFailed, AuthUnitOfWork, principals
from domainkit.contrib.auth.principals import (
    ALL,
    REFRESH_SHARE,
    Entry,
    Key,
    kept_or_read,
    principal_cache,
    read_once,
    remember,
    token_target,
)
from domainkit.contrib.auth.settings import PrincipalCacheSettings
from domainkit.contrib.auth.tokens import token_digest
from domainkit.management.base import run_async
from domainkit_tests.conftest import Api
from domainkit_tests.test_auth_http import bearer, create_user
from domainkit_tests.test_auth_principals import (
    STAMPED,
    credentials_of,
    issue,
    sample,
)
from domainkit_tests.test_auth_principals import shared as shared
from domainkit_tests.test_auth_principals import statements as statements

KEY: Key = ("token", "digest-of-a-token")
CONFIG = PrincipalCacheSettings(TTL=timedelta(seconds=10))
"""Read again ahead from 8 seconds on, kept until 10."""
TARGET = token_target("digest-of-a-token")


@pytest.fixture
def clock() -> Iterator[FrozenClock]:
    """Every principal dropped, on a frozen clock."""
    principal_cache.invalidate([ALL])
    with freeze_time("2026-05-01T10:00:00Z") as frozen:
        yield frozen
    principal_cache.invalidate([ALL])


def read_entry(**fields: Any) -> Entry:  # noqa: ANN401 - Entry's fields.
    """A read of ``KEY``, which rests on ``TARGET``, that may be kept."""
    return Entry(KEY, None, targets=(TARGET,), keep=True, **fields)


class Reads:
    """A fake read of ``KEY``: counts calls, and waits for ``release`` if held."""

    def __init__(
        self, *, held: bool = False, fail: BaseException | None = None
    ) -> None:
        """Answer at once, or once released; raise ``fail`` if given."""
        self.calls = 0
        self.started: list[int] = []
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        if not held:
            self.release.set()
        self.fail = fail
        self.answer: Callable[[], Entry | None] = read_entry

    async def __call__(self, started: int) -> Entry | None:
        """One read, from the mark ``started``."""
        self.calls += 1
        self.started.append(started)
        self.entered.set()
        await self.release.wait()
        if self.fail is not None:
            raise self.fail
        found = self.answer()
        if found is not None:
            found.started = started
        return found


async def kept(reads: Reads | None = None) -> Entry:
    """Read ``KEY`` once and keep it."""
    found = await kept_or_read(KEY, CONFIG, reads or Reads())
    assert found is not None
    remember(found, CONFIG)
    return found


async def soon[T](work: Coroutine[Any, Any, T]) -> asyncio.Task[T]:
    """Start ``work`` and let it run up to its first wait."""
    task = asyncio.ensure_future(work)
    for _ in range(5):
        await asyncio.sleep(0)
    return task


# One read per key.


@pytest.mark.anyio
async def test_requests_that_miss_at_once_read_once(clock: FrozenClock) -> None:
    """Ten requests of one credential, none kept: one read, the same entry."""
    reads = Reads(held=True)

    tasks = [await soon(kept_or_read(KEY, CONFIG, reads)) for _ in range(10)]
    reads.release.set()
    found = await asyncio.gather(*tasks)

    assert reads.calls == 1
    assert all(each is found[0] for each in found)
    assert sample("coalesced") >= 9


@pytest.mark.anyio
async def test_reads_of_other_keys_and_loops_are_their_own(clock: FrozenClock) -> None:
    """Another key reads for itself; so does the same key on another loop."""
    reads = Reads(held=True)
    first = await soon(read_once(KEY, reads))
    other = await soon(read_once(("token", "another"), reads))
    elsewhere = await asyncio.to_thread(asyncio.run, read_once(KEY, Reads()))
    reads.release.set()

    assert (await first) is not (await other)
    assert elsewhere is not None
    assert reads.calls == 2


@pytest.mark.anyio
async def test_a_request_never_joins_a_read_older_than_a_revocation(
    clock: FrozenClock,
) -> None:
    """Revoked while another request reads: a request arriving after reads anew."""
    reads = Reads(held=True)
    before = await soon(read_once(KEY, reads))
    await reads.entered.wait()
    principals.invalidate([TARGET])  # The revocation commits.
    revoked = Reads()
    revoked.answer = lambda: None  # The token is gone.

    after = await read_once(KEY, revoked)
    reads.release.set()

    assert after is None
    assert revoked.calls == 1
    assert (await before) is not None  # It arrived first: concurrent with it.


@pytest.mark.anyio
async def test_a_read_that_overlaps_a_revocation_is_not_shared(
    clock: FrozenClock,
) -> None:
    """Joined before the revocation, ended after it: the waiter reads again."""
    reads = Reads(held=True)
    leader = await soon(read_once(KEY, reads))
    waiter = await soon(read_once(KEY, reads))
    principals.invalidate([TARGET])
    reads.release.set()

    assert await leader is not None
    assert await waiter is not None
    assert reads.calls == 2  # The waiter's own read, begun after the change.
    assert reads.started[1] > reads.started[0]


@pytest.mark.anyio
async def test_a_change_to_something_else_still_shares_the_read(
    clock: FrozenClock,
) -> None:
    """Only what the entry rests on matters once joined."""
    reads = Reads(held=True)
    leader = await soon(read_once(KEY, reads))
    waiter = await soon(read_once(KEY, reads))
    principals.invalidate(["user:someone-else"])
    reads.release.set()

    assert await leader is await waiter
    assert reads.calls == 1


@pytest.mark.anyio
async def test_a_credential_that_is_not_there_is_shared_too(
    clock: FrozenClock,
) -> None:
    """Guesses of one value at once cost one read."""
    reads = Reads(held=True)
    reads.answer = lambda: None
    tasks = [await soon(read_once(KEY, reads)) for _ in range(3)]
    reads.release.set()

    assert await asyncio.gather(*tasks) == [None, None, None]
    assert reads.calls == 1


@pytest.mark.anyio
@pytest.mark.parametrize("fail", [RuntimeError("database down"), None])
async def test_waiters_read_for_themselves_when_the_read_fails(
    clock: FrozenClock, fail: BaseException | None
) -> None:
    """A failed or cancelled read leaves its waiters to read; they get theirs."""
    reads = Reads(held=True, fail=fail)
    leader = await soon(read_once(KEY, reads))
    waiter = await soon(read_once(KEY, Reads()))
    if fail is None:
        leader.cancel()
    else:
        reads.release.set()

    with pytest.raises(RuntimeError if fail else asyncio.CancelledError):
        await leader
    assert await waiter is not None
    assert principals._flights.get(dict) == {}


@pytest.mark.anyio
async def test_a_cancelled_waiter_leaves_the_read_to_the_others(
    clock: FrozenClock,
) -> None:
    """Cancelling one waiting request cancels neither the read nor the rest."""
    reads = Reads(held=True)
    leader = await soon(read_once(KEY, reads))
    quitter = await soon(read_once(KEY, reads))
    stayer = await soon(read_once(KEY, reads))
    quitter.cancel()
    reads.release.set()

    with pytest.raises(asyncio.CancelledError):
        await quitter
    assert await leader is await stayer
    assert reads.calls == 1


# Read again ahead of the deadline.


@pytest.mark.anyio
async def test_kept_entries_are_read_again_by_one_request_ahead_of_time(
    clock: FrozenClock,
) -> None:
    """In the last fifth of the TTL, one request reads; the others are served."""
    old = await kept()
    clock.tick(CONFIG.TTL.total_seconds() * (1 - REFRESH_SHARE) - 0.1)
    assert await kept_or_read(KEY, CONFIG, Reads()) is old  # Not yet.

    clock.tick(0.2)
    reads = Reads(held=True)
    refresher = await soon(kept_or_read(KEY, CONFIG, reads))
    others = [await kept_or_read(KEY, CONFIG, reads) for _ in range(3)]
    reads.release.set()
    fresh = await refresher

    assert reads.calls == 1
    assert others == [old, old, old]
    assert fresh is not None
    assert fresh is not old
    assert principal_cache.get(KEY, clock.monotonic()) is old  # Until replaced.
    remember(fresh, CONFIG)
    assert principal_cache.get(KEY, clock.monotonic()) is fresh
    clock.tick(CONFIG.TTL.total_seconds() * 0.75)
    assert await kept_or_read(KEY, CONFIG, Reads()) is fresh


@pytest.mark.anyio
async def test_nothing_is_served_past_its_deadline(clock: FrozenClock) -> None:
    """Read again or not, an entry is never used past its TTL."""
    old = await kept()
    clock.tick(CONFIG.TTL.total_seconds())
    reads = Reads()

    assert await kept_or_read(KEY, CONFIG, reads) is not old
    assert reads.calls == 1


@pytest.mark.anyio
async def test_what_a_new_read_finds_changed_drops_the_kept_entry(
    clock: FrozenClock,
) -> None:
    """Revoked elsewhere (no stamp): the read ahead finds it, and no one is served."""
    await kept()
    clock.tick(CONFIG.TTL.total_seconds() * 0.9)
    gone = Reads()
    gone.answer = lambda: None

    assert await kept_or_read(KEY, CONFIG, gone) is None
    assert principal_cache.get(KEY, clock.monotonic()) is None
    assert await kept_or_read(KEY, CONFIG, gone) is None
    assert gone.calls == 2


@pytest.mark.anyio
async def test_a_failed_read_ahead_serves_the_kept_entry_until_its_deadline(
    clock: FrozenClock,
) -> None:
    """The database fails: served as a hit, and no other request tries again."""
    old = await kept()
    clock.tick(CONFIG.TTL.total_seconds() * 0.9)
    failing = Reads(fail=RuntimeError("database down"))

    assert await kept_or_read(KEY, CONFIG, failing) is old
    assert await kept_or_read(KEY, CONFIG, failing) is old
    assert failing.calls == 1
    assert sample("refresh_failed") >= 1
    clock.tick(CONFIG.TTL.total_seconds() * 0.1)
    with pytest.raises(RuntimeError):
        await kept_or_read(KEY, CONFIG, failing)


@pytest.mark.anyio
async def test_a_read_ahead_that_outlives_the_deadline_fails(
    clock: FrozenClock,
) -> None:
    """Failing after the deadline passed meanwhile: nothing stale is served."""
    await kept()
    clock.tick(CONFIG.TTL.total_seconds() * 0.9)
    failing = Reads(held=True, fail=RuntimeError("database down"))
    refresher = await soon(kept_or_read(KEY, CONFIG, failing))
    clock.tick(CONFIG.TTL.total_seconds())
    failing.release.set()

    with pytest.raises(RuntimeError):
        await refresher


@pytest.mark.anyio
async def test_a_cancelled_read_ahead_lets_another_request_read(
    clock: FrozenClock,
) -> None:
    """The request went away, not the database: the next one reads again."""
    await kept()
    clock.tick(CONFIG.TTL.total_seconds() * 0.9)
    held = Reads(held=True)
    refresher = await soon(kept_or_read(KEY, CONFIG, held))
    refresher.cancel()
    with pytest.raises(asyncio.CancelledError):
        await refresher
    reads = Reads()

    assert await kept_or_read(KEY, CONFIG, reads) is not None
    assert reads.calls == 1


@pytest.mark.anyio
async def test_only_the_kept_entry_is_read_again() -> None:
    """An entry replaced or dropped is not claimed; the kept one once."""
    cache = principals.PrincipalCache()
    entry = read_entry()
    assert not cache.claim_refresh(entry)  # Not kept.
    cache.put(entry, limit=9)
    assert cache.claim_refresh(entry)
    assert not cache.claim_refresh(entry)
    cache.release_refresh(entry)
    assert cache.claim_refresh(entry)


@pytest.mark.anyio
async def test_logins_past_their_tokens_are_read_again(clock: FrozenClock) -> None:
    """``holds`` refusing a kept entry drops it, and reads."""
    old = await kept()
    reads = Reads()

    found = await kept_or_read(KEY, CONFIG, reads, lambda entry: False)

    assert found is not old
    assert reads.calls == 1
    assert principal_cache.get(KEY, clock.monotonic()) is None


# Through the database.


def test_tokens_that_miss_at_once_are_read_once(
    api: Api, statements: list[str]
) -> None:
    """Twenty requests of one token at once: two SELECTs, twenty users."""
    create_user("alice")
    api()
    raw = run_async(issue("alice"))

    async def burst() -> list[Any]:
        principal_cache.invalidate([ALL])
        statements.clear()
        return await asyncio.gather(
            *(credentials_of(headers=bearer(raw)) for _ in range(20))
        )

    found = run_async(burst())

    assert statements.count("SELECT") == 2
    assert len({id(each.user) for each in found}) == 20


def test_a_token_revoked_during_a_read_is_refused_to_later_requests(
    api: Api, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The race: a read in flight, the token revoked, then a request of it."""
    create_user("alice")
    api()
    raw = run_async(issue("alice"))
    entered, release = asyncio.Event(), asyncio.Event()
    ahead = principals.stamps_ahead

    async def held(*args: Any) -> Any:  # noqa: ANN401 - stamps_ahead's.
        if not release.is_set():
            entered.set()
            await release.wait()
        return await ahead(*args)

    monkeypatch.setattr("domainkit.contrib.auth.authentication.stamps_ahead", held)

    async def race() -> tuple[Any, BaseException | None]:
        principal_cache.invalidate([ALL])
        first = asyncio.ensure_future(credentials_of(headers=bearer(raw)))
        await entered.wait()  # Its read is under way, before any row.
        async with AuthUnitOfWork() as uow:
            await uow.tokens.revoke(token_digest(raw))
            await uow.commit()
        release.set()
        try:
            await credentials_of(headers=bearer(raw))
        except AuthenticationFailed as exc:
            refused: BaseException | None = exc
        else:
            refused = None
        return await asyncio.gather(first, return_exceptions=True), refused

    (first,), refused = run_async(race())

    assert isinstance(refused, AuthenticationFailed)
    assert refused.detail == "Invalid token."
    assert isinstance(first, AuthenticationFailed)  # It read after the revocation.
    assert len(principal_cache) == 0


def test_a_token_deleted_elsewhere_is_found_ahead_of_the_ttl(
    api: Api, statements: list[str]
) -> None:
    """No stamp reaches this process: the read ahead finds it gone, and refuses."""
    from sqlalchemy import delete

    from domainkit.contrib.auth.tables import tokens
    from domainkit.db import connections

    create_user("alice")
    api(PRINCIPAL_CACHE={"TTL": timedelta(seconds=10)})
    raw = run_async(issue("alice"))

    async def deleted() -> None:
        async with connections.engine().begin() as connection:  # Another process.
            await connection.execute(
                delete(tokens).where(tokens.c.id == token_digest(raw))
            )

    with freeze_time("2026-05-01T10:00:00Z") as frozen:
        assert run_async(credentials_of(headers=bearer(raw))) is not None
        run_async(deleted())
        frozen.tick(7.9)
        assert run_async(credentials_of(headers=bearer(raw))) is not None  # Kept.
        statements.clear()
        frozen.tick(0.2)
        for _ in range(2):
            with pytest.raises(AuthenticationFailed):
                run_async(credentials_of(headers=bearer(raw)))
        # Each reads the token, and finds none: nothing kept is served.
        assert statements.count("SELECT") == 2


# Stamps: a revocation made in another process during a read (SEC-10a-1).


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("kept_stamps", "now", "joins"),
    [
        (("s-all", "s-token"), ("s-all", "s-token"), True),
        (("s-all", "s-token"), ("s-all", "changed"), False),
        (None, ("s-all", "s-token"), False),
        (("s-all", "s-token"), CacheError("down"), False),
    ],
)
async def test_a_waiter_checks_the_shared_stamps_of_the_read_it_joins(
    clock: FrozenClock,
    monkeypatch: pytest.MonkeyPatch,
    kept_stamps: tuple[str, ...] | None,
    now: tuple[str, ...] | Exception,
    joins: bool,
) -> None:
    """Joined only while the stamps are the read's; unconfirmed: read again."""
    asked: list[tuple[str, tuple[str, ...]]] = []

    async def current(alias: str, targets: tuple[str, ...]) -> tuple[str, ...]:
        asked.append((alias, tuple(targets)))
        if isinstance(now, Exception):
            raise now
        return now

    monkeypatch.setattr(principals, "read_stamps", current)
    reads = Reads(held=True)
    reads.answer = lambda: read_entry(stamps=kept_stamps)
    leader = await soon(read_once(KEY, reads, "shared"))
    waiter = await soon(read_once(KEY, reads, "shared"))
    reads.release.set()

    assert (await leader is await waiter) is joins
    assert reads.calls == (1 if joins else 2)
    assert asked[:1] == ([] if kept_stamps is None else [("shared", (TARGET,))])


def test_a_request_after_a_stamped_revocation_never_joins_an_older_read(
    api: Api,
    shared: FakeServer,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Another process revokes and stamps a token while this one reads it.

    A request arriving after that must be refused, as a read of its own
    would refuse it, not served what the older read finds.
    """
    from domainkit.contrib.auth.repositories import PrincipalStore
    from domainkit.contrib.auth.tables import tokens
    from domainkit.db import connections

    create_user("alice")
    api(PRINCIPAL_CACHE=STAMPED)
    raw = run_async(issue("alice"))
    digest = token_digest(raw)
    original = PrincipalStore.principal
    read, release = asyncio.Event(), asyncio.Event()

    async def slow(self: PrincipalStore, *args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
        found = await original(self, *args, **kwargs)  # The token was stored.
        if not release.is_set():
            read.set()
            await release.wait()  # Its answer arrives late.
        return found

    monkeypatch.setattr(PrincipalStore, "principal", slow)

    async def scenario() -> tuple[Any, Any]:
        principal_cache.invalidate([ALL])
        first = asyncio.ensure_future(credentials_of(headers=bearer(raw)))
        await read.wait()
        async with connections.engine().begin() as connection:  # Elsewhere.
            await connection.execute(tokens.delete().where(tokens.c.id == digest))
        await principals.write_stamps("shared", [token_target(digest)])
        second = await soon(credentials_of(headers=bearer(raw)))
        release.set()
        done = await asyncio.gather(first, second, return_exceptions=True)
        return done[0], done[1]

    first, second = run_async(scenario())

    assert not isinstance(first, BaseException)  # It arrived before.
    assert isinstance(second, AuthenticationFailed)
    assert second.detail == "Invalid token."
