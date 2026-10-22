"""The benchmark ladder's counters and budgets (``benchmarks/``, not shipped).

CI gates on these counts, so they must be exact: no timing is asserted.
"""

import asyncio
import gc
import json
import os
import secrets
import subprocess
import sys
import threading
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import text
from sqlalchemy.engine.default import DefaultDialect
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from benchmarks import budgets as budgeting
from benchmarks import database as bench_database
from benchmarks import run as ladder_run
from benchmarks.budgets import MEASURES, Budgets, Verdict
from benchmarks.counting import CallCounter, SqlCounter
from benchmarks.database import SCHEMA, TOKEN_TTL, drop_schema, make_schema
from benchmarks.ladder import (
    IDEMPOTENCY_MIDDLEWARE,
    LADDER,
    POSTGRES_VARIABLE,
    Rung,
    masked,
)
from benchmarks.run import BUDGETS, ROOT, Tree
from benchmarks.rung import middleware, template_middleware
from domainkit.conf.global_settings import DEFAULT_MIDDLEWARE
from domainkit_tests.conftest import POSTGRES_VARIABLE as TEST_POSTGRES_VARIABLE


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


URL = "postgresql+asyncpg://bench:p%40ss-W0rd@db.internal:5432/bench"
"""A database URL with a password, percent-encoded as URLs write ``@``."""


@pytest.mark.parametrize(
    "text",
    [
        f"sqlalchemy.exc.ArgumentError: Could not parse SQLAlchemy URL {URL!r}",
        "input_value='postgresql+asyncpg://bench:p%40ss-W0rd@db.internal:5432/bench'",
        "password p@ss-W0rd refused; percent-encoded, p%40ss-W0rd",
        "another: redis://cache:other-secret@redis:6379/0, and postgres://u:x@h/d",
    ],
)
def test_what_the_harness_prints_never_holds_a_password(text: str) -> None:
    """The URL whole, its password on its own (either way), any URL's password."""
    shown = masked(text, URL)

    assert "W0rd" not in shown
    assert "other-secret" not in shown
    assert ":x@" not in shown
    assert "***" in shown


def test_urls_that_do_not_split_are_masked_too() -> None:
    """A URL urllib refuses (a broken IPv6 host) is masked as it is written."""
    broken = "postgresql://bench:s3cret-pw@[::1/db"

    assert masked(f"bad {broken}; s3cret-pw", broken) == "bad ***; ***"
    assert masked("no URL here", "") == "no URL here"
    assert masked("postgresql://bench@db/x", "postgresql://bench@db/x") == "***"


@pytest.fixture
def leaky(tmp_path: Path) -> Tree:
    """A tree whose interpreter prints the database URL, then fails.

    As an error message could, on stderr and stdout; it also writes the
    fixture it is given when told to, as the schema's maker does.
    """
    interpreter = tmp_path / "python"
    interpreter.write_text(
        f"#!{sys.executable}\n"
        "import os, sys\n"
        f"url = os.environ[{POSTGRES_VARIABLE!r}]\n"
        "print('connecting to', url)\n"
        "sys.stderr.write(f'could not parse {url!r}\\n')\n"
        "if 'made' in os.environ.get('LEAKY', '') and len(sys.argv) > 4:\n"
        "    open(sys.argv[-1], 'w').write('{}')\n"
        "sys.exit(1)\n"
    )
    interpreter.chmod(0o755)
    return Tree("current", tmp_path, str(interpreter))


def test_a_childs_output_reaches_no_one_with_the_password(
    leaky: Tree, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``child`` masks what it captured; a failed rung is reported masked."""
    done = ladder_run.child(leaky, ["benchmarks.rung", "me"], tmp_path, URL)
    options = SimpleNamespace(
        **dict.fromkeys(ladder_run.IN_PROCESS),
        profile_dir=None,
        postgres=URL,
    )
    ladder_run.measure(leaky, LADDER[0], options, tmp_path)  # type: ignore[arg-type]

    assert done.returncode == 1
    assert "W0rd" not in done.stdout + done.stderr
    assert "could not parse '***'" in done.stderr
    assert "W0rd" not in leaky.failures[LADDER[0].name]


def test_each_run_makes_a_schema_of_its_own_and_drops_only_that(
    leaky: Tree, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A random suffix; a schema whose maker failed before making it is not ours.

    The name is never one another run has: the maker refuses a schema that
    exists, and ``prepare`` drops only what its maker says it made.
    """
    with pytest.raises(RuntimeError, match="no database") as refused:
        ladder_run.prepare(leaky, URL, tmp_path)
    assert leaky.schema == ""  # Not made: nothing of this run's to drop.
    assert "W0rd" not in str(refused.value)

    monkeypatch.setenv("LEAKY", "made")
    names = set()
    for _ in range(2):
        with pytest.raises(RuntimeError):
            ladder_run.prepare(leaky, URL, tmp_path)
        names.add(leaky.schema)
        assert leaky.fixture is not None
        leaky.fixture.unlink()

    assert len(names) == 2
    assert all(SCHEMA.fullmatch(name) for name in names)
    assert all(name.startswith("domainkit_bench_current_") for name in names)


def test_the_database_tool_prints_its_failures_masked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Run by hand, ``python -m benchmarks.database`` masks its own traceback."""

    def fails(url: str, schema: str, fixture: Path) -> None:
        raise ValueError(f"cannot use {url}")

    monkeypatch.setenv(POSTGRES_VARIABLE, URL)
    monkeypatch.setattr(bench_database, "create", fails)

    code = bench_database.main(["create", "domainkit_bench_x", str(tmp_path / "f")])

    error = capsys.readouterr().err
    assert code == 1
    assert "ValueError: cannot use ***" in error
    assert "W0rd" not in error


def test_schemas_not_named_for_the_ladder_are_refused() -> None:
    """Neither made nor dropped: the name is checked before any connection."""
    with pytest.raises(ValueError, match="not domainkit_bench_"):
        bench_database._checked("public")
    with pytest.raises(ValueError, match="not domainkit_bench_"):
        bench_database._checked('domainkit_bench_x"; DROP SCHEMA public; --')


postgres_only = pytest.mark.skipif(
    not os.environ.get(TEST_POSTGRES_VARIABLE), reason=f"set {TEST_POSTGRES_VARIABLE}"
)


@postgres_only
def test_the_database_rungs_schema_is_never_replaced_and_its_token_expires(
    database_url: str, tmp_path: Path
) -> None:
    """Made once, by ``create``; a second ``create`` is refused and leaves it.

    The bench user has no usable password, and its token expires within
    ``TOKEN_TTL``, so a killed run leaves nothing to log in with for long.
    """
    schema = f"domainkit_bench_test_{secrets.token_hex(4)}"
    tree = Tree("current", ROOT / "src", sys.executable)
    environment = tree.environment(tmp_path / "migrations", database_url)

    def tool(*arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(  # noqa: S603 - this interpreter, the harness's tool.
            [sys.executable, "-m", "benchmarks.database", *arguments],
            env=environment,
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )

    first = tool("create", schema, str(tmp_path / "first.json"))
    again = tool("create", schema, str(tmp_path / "again.json"))

    async def read() -> tuple[list[object], list[str]]:
        engine = create_async_engine(database_url)
        try:
            async with engine.connect() as connection:
                ttl = (
                    await connection.execute(
                        text(  # A schema of the test's own making.
                            f'SELECT expires_at - created_at FROM "{schema}".'  # noqa: S608
                            "domainkit_tokens"
                        )
                    )
                ).scalars()
                passwords = (
                    await connection.execute(
                        text(f'SELECT password FROM "{schema}".domainkit_users')  # noqa: S608 - the test's schema.
                    )
                ).scalars()
                return list(ttl), list(passwords)
        finally:
            await engine.dispose()

    ttls, passwords = asyncio.run(read())
    gone = tool("drop", schema)

    assert first.returncode == 0, first.stderr
    assert set(json.loads((tmp_path / "first.json").read_text())) == {"token", "item"}
    assert again.returncode == 1
    assert "already exists" in again.stderr
    assert not (tmp_path / "again.json").exists()
    assert ttls == [TOKEN_TTL] == [timedelta(hours=1)]
    assert [password[:1] for password in passwords] == ["!"]
    assert gone.returncode == 0, gone.stderr

    async def schemas() -> list[str]:
        engine = create_async_engine(database_url)
        try:
            async with engine.connect() as connection:
                found = await connection.execute(
                    text("SELECT schema_name FROM information_schema.schemata")
                )
                return list(found.scalars())
        finally:
            await engine.dispose()

    assert schema not in asyncio.run(schemas())


@postgres_only
@pytest.mark.anyio
async def test_making_a_schema_that_exists_leaves_it_as_it_was(
    database_url: str,
) -> None:
    """Another run's schema, with what it holds, is never dropped to make ours."""
    schema = f"domainkit_bench_other_{secrets.token_hex(4)}"
    engine = create_async_engine(database_url)
    try:
        await make_schema(database_url, schema)
        async with engine.begin() as connection:
            await connection.execute(text(f'CREATE TABLE "{schema}".kept (n int)'))
        with pytest.raises(Exception, match="already exists"):
            await make_schema(database_url, schema)
        async with engine.connect() as connection:
            count = f'SELECT count(*) FROM "{schema}".kept'  # noqa: S608 - the test's schema.
            kept = await connection.execute(text(count))
            assert kept.scalar_one() == 0
        await drop_schema(database_url, schema)
        await drop_schema(database_url, schema)  # Gone already: nothing to do.
    finally:
        await engine.dispose()
