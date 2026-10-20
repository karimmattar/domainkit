"""``domainkit.clock``: the one clock the framework reads, and freezing it."""

import asyncio
import math
import time
from collections.abc import Callable
from contextvars import copy_context
from datetime import UTC, date, datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4

import jwt
import pytest

from domainkit import clock
from domainkit.architecture import check_domain
from domainkit.cache.memory import MemoryStore
from domainkit.clock import FrozenClock, freeze_time, frozen, moment
from domainkit.contrib.auth import middleware as auth_middleware
from domainkit.contrib.auth import models as auth_models
from domainkit.contrib.auth import tokens
from domainkit.contrib.auth.authentication import AuthenticationFailed
from domainkit.contrib.idempotency import middleware as idempotency
from domainkit.contrib.messaging import outbox, worker
from domainkit.ddd import DomainEvent
from domainkit.ddd import events as ddd_events
from domainkit.http.ratelimit import MemoryBackend
from domainkit.testing import TestClient
from domainkit_tests.conftest import Api, MakePackage, UseAuth
from domainkit_tests.test_auth_authentication import jwt_auth
from domainkit_tests.test_auth_http import JWT, audited, bearer, create_user, login

NEW_YEAR = datetime(2030, 1, 1, 9, tzinfo=UTC)


class Ticks:
    """A stand-in for the ``time`` module whose monotonic count tests move."""

    def __init__(self) -> None:
        """Start at 100 seconds."""
        self.now = 100.0

    def monotonic(self) -> float:
        """The count."""
        return self.now


@pytest.fixture
def ticks(monkeypatch: pytest.MonkeyPatch) -> Ticks:
    """Real time, as the clock module sees it, moved by the test."""
    fake = Ticks()
    monkeypatch.setattr(clock, "time", SimpleNamespace(monotonic=fake.monotonic))
    return fake


def test_the_real_clock() -> None:
    """Unfrozen, the clock is the host's: aware, in UTC."""
    before = datetime.now(UTC)
    now = clock.now()
    assert before <= now <= datetime.now(UTC)
    assert now.tzinfo is UTC
    assert clock.today() == now.date()
    before_count = time.monotonic()
    assert before_count <= clock.monotonic() <= time.monotonic()
    assert frozen() is None


@pytest.mark.parametrize(
    ("when", "expected"),
    [
        ("2030-01-01", datetime(2030, 1, 1, tzinfo=UTC)),
        ("2030-01-01T09:00:00Z", NEW_YEAR),
        ("2030-01-01T11:00:00+02:00", NEW_YEAR),
        ("2030-01-01T09:00:00", NEW_YEAR),
        (date(2030, 1, 1), datetime(2030, 1, 1, tzinfo=UTC)),
        (datetime(2030, 1, 1, 9), NEW_YEAR),  # noqa: DTZ001 - naive is read as UTC.
        (datetime(2030, 1, 1, 4, tzinfo=timezone(timedelta(hours=-5))), NEW_YEAR),
    ],
)
def test_moments_are_aware_and_in_utc(
    when: str | date | datetime, expected: datetime
) -> None:
    """Strings, dates, and naive datetimes are read as UTC; others converted."""
    assert moment(when) == expected
    assert moment(when).tzinfo is UTC


def test_a_frozen_clock_stands_still() -> None:
    """Inside the block, every reading is the frozen one; after it, real time."""
    with freeze_time("2030-01-01T09:00:00Z") as frozen_clock:
        count = clock.monotonic()
        assert clock.now() == NEW_YEAR
        assert clock.today() == date(2030, 1, 1)
        time.sleep(0.001)
        assert (clock.now(), clock.monotonic()) == (NEW_YEAR, count)
        assert frozen() is frozen_clock
        assert frozen_clock.active
    assert not frozen_clock.active
    assert frozen() is None
    assert abs(clock.now() - datetime.now(UTC)) < timedelta(seconds=5)


def test_without_a_moment_it_freezes_now() -> None:
    """``freeze_time()`` stops the clock where it is."""
    before = datetime.now(UTC)
    with freeze_time() as frozen_clock:
        assert before <= clock.now() <= datetime.now(UTC)
        assert frozen_clock.now() == clock.now()


def test_tick_moves_forward_only(ticks: Ticks) -> None:
    """``tick`` takes seconds or a timedelta; the count moves with it."""
    frozen_clock = FrozenClock(NEW_YEAR)
    assert frozen_clock.monotonic() == 100.0

    assert frozen_clock.tick() == NEW_YEAR + timedelta(seconds=1)
    assert frozen_clock.tick(0.5) == NEW_YEAR + timedelta(seconds=1.5)
    assert frozen_clock.tick(timedelta(hours=2)) == NEW_YEAR + timedelta(
        hours=2, seconds=1.5
    )
    assert frozen_clock.monotonic() == 100.0 + 7201.5
    assert frozen_clock.tick(0) == frozen_clock.now()
    with pytest.raises(
        ValueError, match=r"^tick\(\) moves forward; use move_to\(\) to go back\.$"
    ):
        frozen_clock.tick(-1)
    ticks.now += 60  # Real time passing does not move a clock that stands still.
    assert frozen_clock.now() == NEW_YEAR + timedelta(hours=2, seconds=1.5)


def test_move_to_jumps_but_the_count_never_goes_back(ticks: Ticks) -> None:
    """Going back moves the time only; going forward moves both."""
    frozen_clock = FrozenClock(NEW_YEAR)

    earlier = frozen_clock.move_to("2029-12-31T09:00:00Z")
    assert earlier == frozen_clock.now() == NEW_YEAR - timedelta(days=1)
    assert frozen_clock.monotonic() == 100.0
    assert frozen_clock.move_to(NEW_YEAR + timedelta(minutes=1)) == frozen_clock.now()
    assert frozen_clock.monotonic() == 100.0 + 86400 + 60


def test_a_ticking_clock_runs_on_from_its_moment(ticks: Ticks) -> None:
    """``tick=True`` starts at the moment and follows real time."""
    frozen_clock = freeze_time(NEW_YEAR, tick=True)
    ticks.now += 5
    assert frozen_clock.now() == NEW_YEAR + timedelta(seconds=5)
    assert frozen_clock.monotonic() == 105.0

    assert frozen_clock.tick(10) == NEW_YEAR + timedelta(seconds=15)
    assert frozen_clock.monotonic() == 115.0
    ticks.now += 1
    assert frozen_clock.now() == NEW_YEAR + timedelta(seconds=16)

    frozen_clock.move_to(NEW_YEAR)
    assert frozen_clock.monotonic() == 116.0
    ticks.now += 2
    assert (frozen_clock.now(), frozen_clock.monotonic()) == (
        NEW_YEAR + timedelta(seconds=2),
        118.0,
    )


def test_freezes_nest_and_unwind() -> None:
    """An inner freeze hides the outer one until it ends."""
    with freeze_time(NEW_YEAR) as outer:
        with freeze_time("2031-06-01") as inner:
            assert clock.now() == datetime(2031, 6, 1, tzinfo=UTC)
            assert inner.outer is outer
        assert clock.now() == NEW_YEAR
    assert frozen() is None


def test_start_is_idempotent_and_stop_restores() -> None:
    """Starting twice changes nothing; stopping twice does nothing more."""
    frozen_clock = FrozenClock(NEW_YEAR)
    try:
        assert frozen_clock.start() is frozen_clock.start()
        assert frozen_clock.outer is None
        assert clock.now() == NEW_YEAR
    finally:
        frozen_clock.stop()
    frozen_clock.stop()
    assert frozen() is None


def test_a_clock_started_elsewhere_lapses_when_stopped() -> None:
    """A clock stopped from another context stops for the contexts it was in.

    The ``freeze_time`` fixture stops clocks that async tests started in
    their own task, from pytest's thread.
    """
    outer, inner = FrozenClock(NEW_YEAR), FrozenClock(NEW_YEAR + timedelta(days=1))
    task = copy_context()
    task.run(outer.start)
    task.run(inner.start)
    copied = task.run(copy_context)  # A task started inside the freezes.
    assert task.run(frozen) is copied.run(frozen) is inner

    copied.run(inner.stop)  # Its value, but not its token: it lapses.
    assert task.run(frozen) is copied.run(frozen) is outer
    inner.stop()  # Stopping twice, from anywhere, changes nothing more.
    assert task.run(clock.now) == NEW_YEAR
    outer.stop()
    assert task.run(frozen) is copied.run(frozen) is None
    assert frozen() is None


def test_another_clock_stopped_first_unwinds_cleanly() -> None:
    """Stopping an outer clock while an inner one runs keeps the inner one."""
    outer = FrozenClock(NEW_YEAR).start()
    inner = FrozenClock(NEW_YEAR + timedelta(days=1)).start()
    outer.stop()
    try:
        assert frozen() is inner
    finally:
        inner.stop()
    assert frozen() is None
    with freeze_time(NEW_YEAR) as later:  # The stopped clock is not its outer.
        assert later.outer is None


@pytest.mark.anyio
async def test_tasks_see_their_own_clocks() -> None:
    """Tasks started in a freeze see it; concurrent work does not."""
    entered = asyncio.Event()
    leave = asyncio.Event()

    async def read() -> datetime:
        await leave.wait()
        return clock.now()

    async def frozen_task() -> list[datetime]:
        with freeze_time(NEW_YEAR):
            entered.set()
            child = asyncio.create_task(read())
            await leave.wait()
            return [clock.now(), await child]

    running = asyncio.create_task(frozen_task())
    await entered.wait()
    assert clock.now() != NEW_YEAR
    leave.set()
    assert await running == [NEW_YEAR, NEW_YEAR]


def test_reprs_show_the_time() -> None:
    """The repr names the moment, and whether the clock ticks."""
    assert repr(FrozenClock(NEW_YEAR)) == "<FrozenClock 2030-01-01T09:00:00+00:00>"
    ticking = repr(FrozenClock(NEW_YEAR, tick=True))
    assert ticking.startswith("<FrozenClock 2030-01-01T09:00:")
    assert ticking.endswith(", ticking>")


@pytest.mark.parametrize(
    "now",
    [
        tokens.utc_now,
        auth_models._now,
        idempotency._now,
        outbox._now,
        worker._now,
        ddd_events._now,
        lambda: DomainEvent().occurred_at,
    ],
)
def test_the_framework_reads_the_clock(now: Callable[[], datetime]) -> None:
    """Tokens, users, leases, messages, and events take their time from it."""
    with freeze_time(NEW_YEAR):
        assert now() == NEW_YEAR


def test_rate_windows_and_caches_count_on_the_clock() -> None:
    """Rate limits and memory caches in this process time with ``monotonic``."""
    assert MemoryBackend().clock is clock.monotonic
    assert MemoryStore(10).clock is clock.monotonic
    assert vars(auth_middleware)["monotonic"] is clock.monotonic


@pytest.mark.anyio
async def test_a_memory_rate_window_follows_a_frozen_clock() -> None:
    """A full quota refills only when the frozen clock moves."""
    backend = MemoryBackend()
    with freeze_time(NEW_YEAR) as frozen_clock:
        assert [(await backend.hit("k", 2, 60)).allowed for _ in range(3)] == [
            True,
            True,
            False,
        ]
        await asyncio.sleep(0.01)
        assert not (await backend.hit("k", 2, 60)).allowed
        frozen_clock.tick(30)
        assert (await backend.hit("k", 2, 60)).allowed


@pytest.mark.parametrize("reading", [4056.123456789, 4086.123456789, 8187.0000001])
@pytest.mark.anyio
async def test_a_frozen_rate_window_is_exact_wherever_the_real_count_stands(
    monkeypatch: pytest.MonkeyPatch, reading: float
) -> None:
    """The frozen count starts on a whole second; ticks of seconds add exactly.

    From these readings, adding 30 or 60 seconds crosses a power of two and
    rounds: the second request was refused, or the window stayed shut after
    the tick.
    """
    monkeypatch.setattr(clock, "time", SimpleNamespace(monotonic=lambda: reading))
    backend = MemoryBackend()
    with freeze_time(NEW_YEAR) as frozen_clock:
        start = clock.monotonic()
        assert [(await backend.hit("k", 2, 60)).allowed for _ in range(3)] == [
            True,
            True,
            False,
        ]
        frozen_clock.tick(30)
        assert (await backend.hit("k", 2, 60)).allowed
        frozen_clock.tick(30)
        assert clock.monotonic() - start == 60
    assert start == math.ceil(reading)


def test_tokens_and_the_audit_trail_follow_a_frozen_clock(api: Api) -> None:
    """Opaque tokens expire on the frozen clock, and audit rows carry its time.

    domainkit's ``TestClient`` carries the caller's frozen clock into the
    application's thread.
    """
    create_user("alice")
    with TestClient(api(TOKEN_TTL=timedelta(hours=1)).app) as client:
        with freeze_time(NEW_YEAR) as frozen_clock:
            token = login(client, "alice")["access_token"]
            frozen_clock.tick(timedelta(minutes=59))
            assert client.get("/whoami", headers=bearer(token)).status_code == 200
            frozen_clock.tick(timedelta(minutes=1))
            expired = client.get("/whoami", headers=bearer(token))
        assert expired.json()["detail"] == "Token expired."
        assert audited("auth.user_logged_in")[-1]["occurred_at"] == NEW_YEAR


def test_jwts_are_checked_on_the_frozen_clock(api: Api) -> None:
    """A JWT issued at a frozen moment is valid then, and expires on that clock."""
    create_user("alice")
    issuing = {**JWT, "JWT": {"SIGNING_KEY": "k" * 32, "LEEWAY": timedelta(seconds=10)}}
    app = api(**issuing).app
    with TestClient(app) as client, freeze_time(NEW_YEAR) as frozen_clock:
        token = bearer(login(client, "alice")["access_token"])

        def detail() -> object:
            body = client.get("/whoami", headers=token).json()
            return body.get("detail", body.get("user"))

        assert detail() == "alice"
        frozen_clock.move_to(NEW_YEAR - timedelta(seconds=10))
        assert detail() == "alice"  # Within the leeway.
        frozen_clock.move_to(NEW_YEAR - timedelta(seconds=11))
        assert detail() == "Invalid token."  # Issued in its future.
        frozen_clock.move_to(NEW_YEAR + timedelta(minutes=5, seconds=9))
        assert detail() == "alice"
        frozen_clock.move_to(NEW_YEAR + timedelta(minutes=5, seconds=10))
        assert detail() == "Token expired."


def test_memory_cache_entries_expire_on_the_clock() -> None:
    """A memory cache's lifetimes follow a frozen clock."""

    async def scenario() -> list[bytes | None]:
        store = MemoryStore(10)
        with freeze_time(NEW_YEAR) as frozen_clock:
            await store.set_many({"k": b"v"}, ttl=60_000)
            frozen_clock.tick(59.9)
            alive = await store.get_many(["k"])
            frozen_clock.tick(0.1)
            return [*alive, *await store.get_many(["k"])]

    assert asyncio.run(scenario()) == [b"v", None]


def test_jwt_times_are_checked_as_pyjwt_would(use_auth: UseAuth) -> None:
    """Malformed or future ``iat`` and ``nbf`` are invalid; ``exp`` expires.

    PyJWT's own checks read the host's clock, so domainkit makes them.
    """
    authenticator = jwt_auth(
        use_auth, SIGNING_KEY="k" * 32, LEEWAY=timedelta(seconds=5)
    )
    user = type("U", (), {"id": uuid4()})()
    now = int(NEW_YEAR.timestamp())

    def verdict(**changes: object) -> str:
        with freeze_time(NEW_YEAR):
            issued = authenticator.encode(user, uuid4())
            claims = jwt.decode(issued, options={"verify_signature": False})
            token = jwt.encode({**claims, **changes}, "k" * 32, algorithm="HS256")
            try:
                authenticator.decode(token)
            except AuthenticationFailed as failure:
                return str(failure.detail)
        return "valid"

    assert verdict() == "valid"
    assert verdict(iat=now + 5, nbf=now + 5, exp=now - 4) == "valid"
    assert verdict(iat=now + 6) == "Invalid token."
    assert verdict(nbf=now + 6) == "Invalid token."
    assert verdict(exp=now - 5) == "Token expired."
    for malformed in ({"iat": "soon"}, {"nbf": [1]}, {"exp": float("inf")}):
        assert verdict(**malformed) == "Invalid token."


DOMAIN_RULE = (
    "the domain layer may import only the standard library, domainkit.ddd, "
    "domainkit.clock, and its own domain layer"
)


def test_domain_layers_may_read_the_clock(make_package: MakePackage) -> None:
    """``domainkit.clock`` is as pure as the standard library: domains may use it."""
    make_package(
        "timely.orders",
        {
            "domain/model.py": """
                import domainkit.clock
                from domainkit.clock import now
            """,
            "domain/cached.py": "from domainkit.cache import caches\n",
        },
    )

    assert {
        (violation.module, violation.imported, violation.rule)
        for violation in check_domain("timely.orders")
    } == {
        ("timely.orders.domain.cached", imported, DOMAIN_RULE)
        for imported in ("domainkit.cache", "domainkit.cache.caches")
    }
