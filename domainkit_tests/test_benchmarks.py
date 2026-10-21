"""The benchmark ladder's counters and budgets (``benchmarks/``, not shipped).

CI gates on these counts, so they must be exact: no timing is asserted.
"""

import gc
import threading
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.engine.default import DefaultDialect
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from benchmarks import budgets as budgeting
from benchmarks.budgets import MEASURES, Budgets, Verdict
from benchmarks.counting import CallCounter, SqlCounter
from benchmarks.ladder import IDEMPOTENCY_MIDDLEWARE, LADDER, Rung
from benchmarks.run import BUDGETS
from benchmarks.rung import middleware, template_middleware
from domainkit.conf.global_settings import DEFAULT_MIDDLEWARE


def leaf() -> None:
    """Do nothing, countably."""


def calls_of(times: int) -> int:
    """Calls counted around ``times`` calls of ``leaf``."""
    with CallCounter() as counter:
        for _ in range(times):
            leaf()
    return counter.calls


def test_calls_are_counted_exactly() -> None:
    """Each call adds one, whatever the counter costs itself."""
    assert calls_of(30) - calls_of(10) == 20
    assert calls_of(7) == calls_of(7)


def test_calls_in_other_threads_count_too() -> None:
    """Work a thread does inside the block is counted: the logging thread's is."""

    def work() -> None:
        for _ in range(25):
            leaf()

    with CallCounter() as counter:
        thread = threading.Thread(target=work)
        thread.start()
        thread.join()

    stats = counter.stats().stats  # type: ignore[attr-defined]
    assert sum(entry[1] for key, entry in stats.items() if key[2] == "leaf") == 25
    assert counter.calls >= 25


def test_the_collector_waits_outside_the_block() -> None:
    """Collections are held off inside, and left as they were after."""
    assert gc.isenabled()
    with CallCounter():
        assert not gc.isenabled()
    assert gc.isenabled()
    gc.disable()
    try:
        with CallCounter():
            pass
        assert not gc.isenabled()
    finally:
        gc.enable()


async def query(engine: AsyncEngine, *statements: str, begin: bool = False) -> None:
    """Run ``statements`` on one connection from ``engine``'s pool."""
    if begin:
        async with engine.begin() as connection:
            for statement in statements:
                await connection.execute(text(statement))
        return
    async with engine.connect() as connection:
        for statement in statements:
            await connection.execute(text(statement))


@pytest.mark.anyio
async def test_round_trips_are_counted(database_url: str) -> None:
    """Statements, transactions, checkouts, and the pings before reuse."""
    engine = create_async_engine(database_url, pool_pre_ping=True)
    original = DefaultDialect._do_ping_w_event
    try:
        await query(engine, "SELECT 1")  # Opens the pooled connection: no ping.
        with SqlCounter() as sql:
            await query(engine, "SELECT 1", "select 2", begin=True)
            await query(engine, "SELECT 3")
        await query(engine, "SELECT 4")
    finally:
        await engine.dispose()

    counts = sql.counts
    assert (counts.statements, counts.transactions) == (3, 2)
    assert (counts.checkouts, counts.pings) == (2, 2)
    assert counts.verbs == {"SELECT": 3}
    assert DefaultDialect._do_ping_w_event is original


@pytest.mark.anyio
async def test_counters_nest(database_url: str) -> None:
    """Each counter counts what happens while it is entered."""
    engine = create_async_engine(database_url, pool_pre_ping=True)
    try:
        await query(engine, "SELECT 1")
        with SqlCounter() as outer:
            await query(engine, "SELECT 1")
            with SqlCounter() as inner:
                await query(engine, "SELECT 2", "SELECT 3")
            await query(engine, "SELECT 4")
    finally:
        await engine.dispose()

    assert (outer.counts.statements, outer.counts.pings) == (4, 3)
    assert (inner.counts.statements, inner.counts.checkouts) == (2, 1)
    assert inner.counts.pings == 1


PLANNED = Budgets(
    {
        "me": {
            "calls": 1000,
            "statements": 4,
            "transactions": 1,
            "checkouts": 1,
            "pings": 1,
        }
    },
    {"calls": 0.02},
)


def measured(**changes: float) -> dict[str, float]:
    """What ``me`` measured: its budget, but for ``changes``."""
    return {**PLANNED.rungs["me"], **changes}


def test_what_stays_within_its_tolerance_passes() -> None:
    """Calls may stray by the fraction allowed; round trips not at all."""
    assert budgeting.check(PLANNED, {"me": measured()}) == []
    assert budgeting.check(PLANNED, {"me": measured(calls=1020)}) == []
    assert budgeting.check(PLANNED, {"me": measured(calls=980)}) == []


def test_over_budget_fails_and_says_where() -> None:
    """The rung, the measure, the budget, and what was measured."""
    (calls,) = budgeting.check(PLANNED, {"me": measured(calls=1020.5)})
    (statements,) = budgeting.check(PLANNED, {"me": measured(statements=4.01)})

    assert calls.fails
    assert calls.message() == "OVER BUDGET: me calls: budget 1000, measured 1020.5."
    assert statements == Verdict("over", "me", "statements", 4, 4.01)
    assert statements.message() == (
        "OVER BUDGET: me statements: budget 4, measured 4.01."
    )


def test_gains_ask_for_a_lower_budget() -> None:
    """Under budget passes, with a hint to lock the gain in."""
    (calls,) = budgeting.check(PLANNED, {"me": measured(calls=979)})
    (pings,) = budgeting.check(PLANNED, {"me": measured(pings=0)})

    assert not calls.fails
    assert calls.message() == (
        "Under budget: me calls: budget 1000, measured 979; lower the budget to "
        "lock it in."
    )
    assert pings == Verdict("under", "me", "pings", 1, 0)


def test_missing_budgets_and_rungs_are_reported() -> None:
    """A measure without a budget, or a budgeted rung that did not run."""
    unbudgeted = budgeting.check(Budgets({}), {"new": measured()})
    (unmeasured,) = budgeting.check(PLANNED, {}, expected=["me", "other"])

    assert [verdict.measure for verdict in unbudgeted] == list(MEASURES)
    assert unbudgeted[0].message() == "new: no budget for calls; add one."
    assert not any(verdict.fails for verdict in unbudgeted)
    assert unmeasured.message() == "me: has budgets but was not measured."
    assert not unmeasured.fails


def test_budgets_are_written_from_a_run(tmp_path: Path) -> None:
    """Calls round up to a whole call; the tolerances are kept."""
    path = tmp_path / "budgets.toml"
    path.write_text(
        budgeting.render(PLANNED, {"me": measured(calls=1000.1, checkouts=1.004)})
    )

    loaded = budgeting.load(path)
    assert loaded.tolerance == {"calls": 0.02}
    assert loaded.rungs == {"me": measured(calls=1001, checkouts=1)}
    assert "rungs.me" in path.read_text()


@pytest.mark.parametrize(
    "content",
    [
        "[rungs.me]\nseconds = 1\n",
        "[rungs.me]\ncalls = -1\n",
        "[tolerance]\nseconds = 0.1\n",
        "[tolerance]\ncalls = -0.1\n",
    ],
)
def test_budgets_count_only_what_is_gated(tmp_path: Path, content: str) -> None:
    """Unknown measures and negative numbers are refused."""
    path = tmp_path / "budgets.toml"
    path.write_text(content)

    with pytest.raises(ValueError, match=r"budgets\.toml: (rungs|tolerance)\."):
        budgeting.load(path)


def test_every_rung_has_every_budget() -> None:
    """The committed budgets cover the whole ladder, and nothing else."""
    planned = budgeting.load(BUDGETS)

    assert set(planned.rungs) == {rung.name for rung in LADDER}
    assert all(set(measures) == set(MEASURES) for measures in planned.rungs.values())
    assert set(planned.tolerance) == set(MEASURES)


def test_stacks_come_from_the_settings_and_the_template() -> None:
    """``default`` is ``DEFAULT_MIDDLEWARE``; ``template`` what ``init`` writes."""
    template = template_middleware()

    assert middleware(Rung("x", "")) == []
    assert middleware(Rung("x", "", stack="default")) == list(DEFAULT_MIDDLEWARE)
    assert middleware(Rung("x", "", stack="template")) == template
    assert middleware(Rung("x", "", stack="template+idempotency")) == [
        *template,
        IDEMPOTENCY_MIDDLEWARE,
    ]
    assert "domainkit.contrib.auth.middleware.AuthenticationMiddleware" in template
