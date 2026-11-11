"""The load check: its client, its budgets, and what it reports.

``benchmarks/load.py`` serves rungs of the ladder with uvicorn and sends
them requests over many connections. Its client is tested against small
servers made here; one test runs the whole of it, on two rungs with no
database.
"""

import asyncio
import json
import os
import socket
import subprocess
import sys
import threading
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path

import pytest

from benchmarks import load as loading
from benchmarks.ladder import POSTGRES_VARIABLE, RUNGS
from benchmarks.load import LoadBudgets, Result, Round
from benchmarks.run import Tree

URL = "postgresql+asyncpg://bench:S3cretW0rd@db.example.com:5432/bench"
OK = b'HTTP/1.1 200 OK\r\ncontent-length: 15\r\n\r\n{"status":"ok"}'
REFUSED = b"HTTP/1.1 503 Service Unavailable\r\ncontent-length: 4\r\n\r\nbusy"
CHUNKED = b"HTTP/1.1 200 OK\r\ntransfer-encoding: chunked\r\n\r\n0\r\n\r\n"

type Handler = Callable[[asyncio.StreamReader, asyncio.StreamWriter], Awaitable[None]]


def answering(response: bytes, seen: list[bytes] | None = None) -> Handler:
    """A handler that answers every request on a connection with ``response``."""

    async def handle(
        reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            while True:
                head = await reader.readuntil(b"\r\n\r\n")
                if seen is not None:
                    seen.append(head)
                writer.write(response)
                await writer.drain()
        except asyncio.IncompleteReadError:
            pass
        finally:
            writer.close()

    return handle


async def hanging_up(
    reader: asyncio.StreamReader, writer: asyncio.StreamWriter
) -> None:
    """Read a request, and close the connection without an answer."""
    await reader.readuntil(b"\r\n\r\n")
    writer.close()


@asynccontextmanager
async def serving(handler: Handler) -> AsyncIterator[int]:
    """A server on a free port, for the block; the port."""
    server = await asyncio.start_server(handler, "127.0.0.1", 0)
    try:
        yield server.sockets[0].getsockname()[1]
    finally:
        server.close()
        await server.wait_closed()


@contextmanager
def nothing_listening() -> Iterator[int]:
    """A port this test holds, where nothing listens: connections are refused."""
    with socket.socket() as held:
        held.bind(("127.0.0.1", 0))
        yield held.getsockname()[1]


def result(name: str, per_second: float, p99: float, unexpected: int = 0) -> Result:
    """A rung's result with these figures."""
    return Result(name, per_second, p99 / 2, p99, unexpected, ("503 b'busy'",))


def test_a_round_counts_requests_a_second_and_percentiles() -> None:
    """Percentiles come from the sorted latencies, in milliseconds."""
    latencies = tuple(n / 1000 for n in range(1, 101))
    measured = Round(requests=100, seconds=2.0, latencies=latencies, unexpected=0)

    assert measured.per_second == 50.0
    assert measured.percentile(0.50) == pytest.approx(51.0)
    assert measured.percentile(0.99) == pytest.approx(100.0)
    assert measured.percentile(1.0) == pytest.approx(100.0)


def test_a_round_with_no_answers_has_no_figures() -> None:
    """Nothing answered: zeros, not a division by zero."""
    empty = Round(requests=0, seconds=0.0, latencies=(), unexpected=1)

    assert empty.per_second == 0.0
    assert empty.percentile(0.99) == 0.0


def test_a_result_is_the_median_round_and_every_unexpected_answer() -> None:
    """One slow round does not move the result; one refusal is kept."""
    rounds = [
        Round(100, 1.0, (0.001, 0.002), 0),
        Round(900, 1.0, (0.001, 0.900), 1, ("503 b'busy'",)),
        Round(110, 1.0, (0.001, 0.003), 0),
    ]

    summed = Result.of("template", rounds)

    assert summed.per_second == 110.0
    assert summed.p99_ms == pytest.approx(3.0)
    assert summed.unexpected == 1
    assert summed.samples == ("503 b'busy'",)
    assert summed.figures() == {
        "rung": "template",
        "per_second": 110.0,
        "p50_ms": 3.0,
        "p99_ms": 3.0,
        "unexpected": 1,
    }


def test_the_committed_budgets_name_rungs_and_bound_them_loosely() -> None:
    """``load.toml`` reads, and every rung it names is held by a ratio."""
    budgets = LoadBudgets.read()

    assert budgets.baseline == "fastapi"
    assert {"template", "read"} <= set(budgets.rungs)
    for name, limits in budgets.rungs.items():
        assert name in RUNGS
        assert limits
        assert limits.get("p99_ratio", 2) > 1
        assert 0 < limits["throughput_ratio"] < 1
        if RUNGS[name].database and name != "me":
            assert "p99_ratio" not in limits  # The pool's and the server's.


@pytest.mark.parametrize(
    ("content", "said"),
    [
        ("[rungs.nowhere]\np99_ratio = 2\n", "rungs.nowhere is not a rung"),
        ("[rungs.template]\nseconds = 2\n", "rungs.template.seconds = 2"),
        ("[rungs.template]\np99_ratio = 0\n", "rungs.template.p99_ratio = 0"),
        ('baseline = "nowhere"\n', "baseline = 'nowhere'"),
    ],
)
def test_budgets_for_no_rung_or_no_ratio_are_refused(
    tmp_path: Path, content: str, said: str
) -> None:
    """A misspelt rung or limit would bound nothing: it is an error."""
    path = tmp_path / "load.toml"
    path.write_text(content)

    with pytest.raises(ValueError, match=said):
        LoadBudgets.read(path)


def test_within_the_ratios_nothing_is_over_budget() -> None:
    """Slower than the baseline, inside both limits: no problem."""
    budgets = LoadBudgets(
        rungs={"template": {"p99_ratio": 4.0, "throughput_ratio": 0.2}}
    )
    results = {
        "fastapi": result("fastapi", 8000, 4.0),
        "template": result("template", 3000, 9.0),
    }

    assert loading.problems(results, budgets) == []


def test_a_tail_or_a_throughput_past_its_ratio_is_named() -> None:
    """Each limit broken is a line, with the ratio measured and the budget."""
    budgets = LoadBudgets(
        rungs={"template": {"p99_ratio": 4.0, "throughput_ratio": 0.2}}
    )
    results = {
        "fastapi": result("fastapi", 8000, 4.0),
        "template": result("template", 800, 40.0),
    }

    found = loading.problems(results, budgets)

    assert found == [
        "template: the 99th percentile is 10.0 times fastapi's; the budget is 4",
        "template: 0.10 of fastapi's requests a second; the budget is at least 0.2",
    ]


def test_an_unexpected_answer_is_over_budget_whatever_the_ratios() -> None:
    """A rung with no budget still fails on a refusal, and says what it got."""
    results = {"bare": result("bare", 5000, 5.0, unexpected=2)}

    found = loading.problems(results, LoadBudgets())

    assert found == ["bare: 2 answers were not the rung's: 503 b'busy'"]


def test_a_budgeted_rung_that_did_not_run_is_not_judged() -> None:
    """Budgets bound what was measured: a rung left out of the run is skipped."""
    budgets = LoadBudgets(rungs={"read": {"p99_ratio": 2.0}})

    assert loading.problems({"fastapi": result("fastapi", 8000, 4.0)}, budgets) == []
    assert loading.problems({"read": result("read", 1, 900.0)}, budgets) == []


def test_ratios_to_a_baseline_that_measured_nothing_are_zero() -> None:
    """No baseline figures: zeros, not a division by zero."""
    empty = result("fastapi", 0.0, 0.0)

    assert loading.ratios(result("template", 100, 5.0), empty) == (0.0, 0.0)
    assert loading.ratios(result("template", 100, 5.0), None) == (0.0, 0.0)


def test_a_get_goes_on_the_wire_with_the_projects_host() -> None:
    """The request line, the host the project allows, and no body."""
    request = loading.requests_of(RUNGS["template"], {})

    assert request() == (
        b"GET /hello HTTP/1.1\r\n"
        b"Host: api.example.com\r\n"
        b"Accept: application/json\r\n"
        b"User-Agent: domainkit-load/1.0\r\n\r\n"
    )
    assert request() is request()


def test_a_request_carries_the_fixtures_token_ids_and_body() -> None:
    """``{name}`` in the path and body is the fixture's; the token is sent."""
    fixture = {"token": "opaque", "cash": "c-1", "wallet": "w-2", "item": "i-3"}

    read = loading.requests_of(RUNGS["read"], fixture)()
    posting = loading.requests_of(RUNGS["posting"], fixture)()
    write = loading.requests_of(RUNGS["write"], fixture)()

    assert read.startswith(b"GET /items/i-3 HTTP/1.1\r\n")
    assert b"\r\nAuthorization: Bearer opaque\r\n" in read
    head, _, body = posting.partition(b"\r\n\r\n")
    assert json.loads(body)["debit"] == "c-1"
    assert json.loads(body)["credit"] == "w-2"
    assert f"Content-Length: {len(body)}".encode() in head
    assert write.endswith(b'\r\n\r\n{"name": "widget", "price": 42}')


def test_a_keyed_rung_sends_a_new_key_each_time() -> None:
    """Two requests of a keyed rung differ only in their ``Idempotency-Key``."""
    request = loading.requests_of(RUNGS["keyed-write"], {"token": "opaque"})

    first, second = request(), request()

    assert first != second
    assert first.count(b"\r\nIdempotency-Key: ") == 1
    assert len(first) == len(second)


@pytest.mark.anyio
async def test_load_counts_the_answers_of_every_connection() -> None:
    """Each connection sends until the time is up; all are answered."""
    seen: list[bytes] = []
    async with serving(answering(OK, seen)) as port:
        measured = await loading.load(port, RUNGS["template"], {}, 3, 0.2, 0.05)

    assert measured.unexpected == 0
    assert measured.requests == len(measured.latencies) > 3
    assert measured.requests <= len(seen)
    assert seen[0].startswith(b"GET /hello HTTP/1.1\r\n")
    assert list(measured.latencies) == sorted(measured.latencies)
    assert measured.seconds >= 0.2


@pytest.mark.anyio
async def test_another_status_is_counted_and_a_few_are_kept() -> None:
    """Every refusal is counted; the first few say what came back."""
    async with serving(answering(REFUSED)) as port:
        measured = await loading.load(port, RUNGS["template"], {}, 2, 0.1, 0.0)

    assert measured.unexpected == measured.requests > 0
    assert measured.samples[0] == "503 b'busy'"
    assert len(measured.samples) <= loading.SAMPLES


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("handler", "said"),
    [
        (hanging_up, "connection lost: IncompleteReadError"),
        (answering(CHUNKED), "unreadable: A chunked response."),
    ],
)
async def test_a_connection_that_cannot_be_read_is_unexpected(
    handler: Handler, said: str
) -> None:
    """A lost connection, or an answer this client does not read, is counted."""
    async with serving(handler) as port:
        measured = await loading.load(port, RUNGS["template"], {}, 1, 0.1, 0.0)

    assert measured.requests == 0
    assert measured.unexpected == 1
    assert measured.samples == (said,)


@pytest.mark.anyio
async def test_no_server_is_unexpected_too() -> None:
    """Nothing listening: each connection counts one, and says so."""
    with nothing_listening() as port:
        measured = await loading.load(port, RUNGS["template"], {}, 2, 0.1, 0.0)

    assert measured.requests == 0
    assert measured.unexpected == 2
    assert measured.samples[0].startswith("no connection: ")


def test_waiting_ends_when_the_server_answers() -> None:
    """A connection is not enough: the first HTTP answer is."""
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    asked: list[bytes] = []

    def answer_once(listener: socket.socket) -> None:
        connection, _ = listener.accept()
        with connection:
            asked.append(connection.recv(200))
            connection.sendall(b"HTTP/1.1 404 Not Found\r\ncontent-length: 0\r\n\r\n")

    try:
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen()
            thread = threading.Thread(target=answer_once, args=(listener,))
            thread.start()
            loading.wait_ready(listener.getsockname()[1], child, timeout=10)
            thread.join(timeout=10)
    finally:
        child.kill()
        child.wait()

    assert asked[0].startswith(b"GET /hello HTTP/1.1\r\nHost: api.example.com\r\n")


def test_waiting_ends_when_the_server_stops_or_never_answers() -> None:
    """A server that exits, or listens and stays silent, is an error, not a hang."""
    stopped = subprocess.Popen([sys.executable, "-c", "raise SystemExit(3)"])
    stopped.wait()
    silent = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        with nothing_listening() as port:
            with pytest.raises(RuntimeError, match="stopped with status 3"):
                loading.wait_ready(port, stopped, timeout=5)
            with pytest.raises(RuntimeError, match="did not answer in time"):
                loading.wait_ready(port, silent, timeout=0.2)
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen()
            with pytest.raises(RuntimeError, match="did not answer in time"):
                loading.wait_ready(listener.getsockname()[1], silent, timeout=0.7)
    finally:
        silent.kill()
        silent.wait()


def test_a_server_that_fails_is_reported_without_the_password(tmp_path: Path) -> None:
    """What a failed server printed is in the error, the database's URL masked."""
    interpreter = tmp_path / "python"
    interpreter.write_text(
        f"#!{sys.executable}\n"
        "import os, sys\n"
        f"print('could not connect to', os.environ[{POSTGRES_VARIABLE!r}])\n"
        "sys.exit(1)\n"
    )
    interpreter.chmod(0o755)
    tree = Tree("load", tmp_path, str(interpreter))

    with pytest.raises(RuntimeError) as raised:
        loading.start(tree, RUNGS["read"], tmp_path, URL, ["--pool-size", "8"])

    assert "read: The server stopped with status 1." in str(raised.value)
    assert "could not connect to ***" in str(raised.value)
    assert "W0rd" not in str(raised.value)


def test_stopping_kills_a_server_that_ignores_the_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A server that does not end when asked is killed, and waited for."""
    monkeypatch.setattr(loading, "STOP_TIMEOUT", 0.5)
    stubborn = subprocess.Popen(
        [
            sys.executable,
            "-c",
            (
                "import signal, time; "
                "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
                "print('ready', flush=True); time.sleep(60)"
            ),
        ],
        stdout=subprocess.PIPE,
    )
    assert stubborn.stdout is not None
    assert stubborn.stdout.readline() == b"ready\n"

    loading.stop([stubborn])

    assert stubborn.returncode is not None
    stubborn.stdout.close()


def test_database_rungs_without_a_database_are_refused(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A rung that needs PostgreSQL, and no URL: an error that names the variable."""
    monkeypatch.delenv(POSTGRES_VARIABLE, raising=False)

    assert loading.main(["--rungs", "fastapi,read"]) == 1
    assert f"Database rungs need {POSTGRES_VARIABLE}." in capsys.readouterr().err


def test_a_rung_that_is_not_on_the_ladder_is_refused(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A misspelt rung ends the run before anything is served."""
    with pytest.raises(SystemExit) as raised:
        loading.main(["--rungs", "fastapi,nowhere"])

    assert raised.value.code == 2
    assert "no rung named nowhere" in capsys.readouterr().err


def test_over_budget_fails_only_when_strict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Without ``--strict`` the problems are printed; with it they fail the run."""
    budgets = tmp_path / "load.toml"
    budgets.write_text("[rungs.template]\np99_ratio = 4\nthroughput_ratio = 0.2\n")
    summary = tmp_path / "summary.md"
    results = {
        "fastapi": result("fastapi", 8000, 4.0),
        "template": result("template", 3000, 40.0),
    }
    monkeypatch.setattr(loading, "BUDGETS", budgets)
    monkeypatch.setattr(loading, "measure", lambda options: results)
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)

    assert loading.main(["--summary", str(summary)]) == 0
    assert loading.main(["--strict", "--summary", str(summary)]) == 1

    printed = capsys.readouterr()
    assert "OVER BUDGET: template: the 99th percentile is 10.0 times" in printed.err
    assert "template" in printed.out
    written = summary.read_text()
    assert "| `template` | 3000 | 20.00 | 40.00 | 0.38 | 10.00 |" in written
    assert "- **Over budget:** template: the 99th percentile" in written


def test_the_whole_check_runs_on_rungs_with_no_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Two rungs are served, loaded and stopped; the figures are written."""
    figures = tmp_path / "load.json"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(tmp_path / "summary.md"))
    arguments = ["--rungs", "fastapi,bare", "--rounds", "1", "--seconds", "0.3"]
    arguments += ["--warm-up", "0.1", "--connections", "2", "--json", str(figures)]

    assert loading.main(arguments) == 0

    written = json.loads(figures.read_text())
    assert list(written) == ["fastapi", "bare"]
    for rung in written.values():
        assert rung["unexpected"] == 0
        assert rung["per_second"] > 0
        assert 0 < rung["p50_ms"] <= rung["p99_ms"]
    assert "req/s ratio" in capsys.readouterr().out
    assert "| `bare` |" in (tmp_path / "summary.md").read_text()


def test_the_pool_asked_for_is_the_servers(monkeypatch: pytest.MonkeyPatch) -> None:
    """``--pool-size`` and ``--max-overflow`` reach the database's settings."""
    import uvicorn

    from benchmarks import rung as rungs

    seen: dict[str, object] = {}

    def app(rung: object, database: dict[str, object] | None, keep: object) -> str:
        seen.update(database or {})
        return "the app"

    def run(server: uvicorn.Server, sockets: list[socket.socket]) -> None:
        seen["port"] = sockets[0].getsockname()[1]
        sockets[0].close()

    monkeypatch.setenv(POSTGRES_VARIABLE, URL)
    monkeypatch.setattr(rungs, "domainkit_app", app)
    monkeypatch.setattr(uvicorn.Server, "run", run)
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
        arguments = ["--serve", "read", "--schema", "domainkit_bench_x"]

        handed = ["--fd", str(os.dup(listener.fileno()))]
        pool = ["--pool-size", "32", "--max-overflow", "0"]
        assert loading.main([*arguments, *handed, *pool]) == 0

        assert seen == {
            "URL": URL,
            "CONNECT_ARGS": {"server_settings": {"search_path": "domainkit_bench_x"}},
            "POOL_SIZE": 32,
            "MAX_OVERFLOW": 0,
            "port": port,
        }
        seen.clear()
        handed = ["--fd", str(os.dup(listener.fileno()))]
        assert loading.main([*arguments, *handed]) == 0
        assert "POOL_SIZE" not in seen
        assert "MAX_OVERFLOW" not in seen
