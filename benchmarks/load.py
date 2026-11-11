"""Rungs under load: requests a second and latency, with many connections open.

The ladder (``python -m benchmarks``) sends one request at a time and counts
what it costs: exact, and blind to what only shows with requests in flight
together (a lock every request waits on, a pool that runs dry, a call that
blocks the loop). This serves a rung with uvicorn, in a process of its own,
and sends it requests over ``--connections`` connections kept open::

    python -m benchmarks.load                      # fastapi, template
    python -m benchmarks.load --rungs fastapi,template,read --strict
    python -m benchmarks.load --rungs fastapi,read --pool-size 32 --max-overflow 0

Time on a shared machine is noise, so nothing here is gated on a time. Each
rung is compared with the baseline rung (plain FastAPI), measured in the
same run, in rounds that alternate the rungs: the ratio survives a slow
machine. ``load.toml`` bounds the ratios loosely: a gross regression fails,
a few percent do not. A response of another status than the rung's fails
whatever the ratios.

The client is this process: one event loop, a request at a time on each
connection, the next sent when the last is answered. It shares the machine
with the servers, so the figures are for comparing rungs and runs, not for
sizing a deployment (``benchmarks/compare`` does that, with ``oha``).

Database rungs need ``DOMAINKIT_BENCH_POSTGRES_URL``, as on the ladder.
"""

import argparse
import asyncio
import json
import logging
import os
import socket
import statistics
import subprocess
import sys
import tempfile
import time
import tomllib
import uuid
from collections.abc import Callable, Mapping, Sequence
from contextlib import ExitStack, suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

from benchmarks.ladder import POSTGRES_VARIABLE, RUNGS, Rung, masked
from benchmarks.run import ROOT, Tree, child, prepare

BUDGETS: Final = Path(__file__).parent / "load.toml"
DEFAULT_RUNGS: Final = ("fastapi", "template")
HOST: Final = "api.example.com"
"""The ``Host`` sent: the project's ``ALLOWED_HOSTS`` (as ``benchmarks.rung``)."""
BODY: Final = b'{"name": "widget", "price": 42}'
"""What a POST sends when the rung names no body (as ``benchmarks.rung``)."""
LIMITS: Final = ("p99_ratio", "throughput_ratio")
WARM_UP: Final = 1.0
"""Seconds of load before a round is measured."""
READY_TIMEOUT: Final = 60.0
PROBE: Final = (
    b"GET /hello HTTP/1.1\r\nHost: api.example.com\r\nConnection: close\r\n\r\n"
)
"""What a server is asked, to learn that it answers."""
BACKLOG: Final = 2048
"""Connections a server's socket may hold before it accepts them."""
STOP_TIMEOUT: Final = 30.0
SAMPLES: Final = 3
"""How many unexpected answers a round keeps, to say what went wrong."""


@dataclass(frozen=True, slots=True)
class Round:
    """One measured stretch of load on one rung."""

    requests: int
    seconds: float
    latencies: tuple[float, ...]
    """Seconds each request took, sorted."""
    unexpected: int
    """Responses of another status than the rung's, and connections lost."""
    samples: tuple[str, ...] = ()
    """A few of the unexpected, as text."""

    @property
    def per_second(self) -> float:
        """Requests answered a second."""
        return self.requests / self.seconds if self.seconds else 0.0

    def percentile(self, fraction: float) -> float:
        """The latency ``fraction`` of the requests were within, in milliseconds."""
        if not self.latencies:
            return 0.0
        index = min(len(self.latencies) - 1, int(fraction * len(self.latencies)))
        return self.latencies[index] * 1000


@dataclass(frozen=True, slots=True)
class Result:
    """A rung's rounds, summed up: the median round of each figure."""

    rung: str
    per_second: float
    p50_ms: float
    p99_ms: float
    unexpected: int
    samples: tuple[str, ...] = ()

    @classmethod
    def of(cls, rung: str, rounds: Sequence[Round]) -> "Result":
        """The median of ``rounds``, and every unexpected answer of any."""
        return cls(
            rung=rung,
            per_second=statistics.median(each.per_second for each in rounds),
            p50_ms=statistics.median(each.percentile(0.50) for each in rounds),
            p99_ms=statistics.median(each.percentile(0.99) for each in rounds),
            unexpected=sum(each.unexpected for each in rounds),
            samples=tuple(text for each in rounds for text in each.samples)[:SAMPLES],
        )

    def figures(self) -> dict[str, float | str]:
        """The figures, by name, rounded for a file."""
        return {
            "rung": self.rung,
            "per_second": round(self.per_second, 1),
            "p50_ms": round(self.p50_ms, 3),
            "p99_ms": round(self.p99_ms, 3),
            "unexpected": self.unexpected,
        }


@dataclass(frozen=True, slots=True)
class LoadBudgets:
    """How far a rung may fall behind the baseline, measured in the same run."""

    baseline: str = "fastapi"
    rungs: Mapping[str, Mapping[str, float]] = field(default_factory=dict)

    @classmethod
    def read(cls, path: Path = BUDGETS) -> "LoadBudgets":
        """The budgets in ``path``.

        Raises:
            ValueError: A rung is not on the ladder, or a limit is not one of
                ``LIMITS``, or is not above zero.
        """
        table = tomllib.loads(path.read_text(encoding="utf-8"))
        baseline = table.get("baseline", "fastapi")
        rungs: dict[str, dict[str, float]] = {}
        for rung, limits in table.get("rungs", {}).items():
            if rung not in RUNGS:
                raise ValueError(f"{path}: rungs.{rung} is not a rung")
            rungs[rung] = {}
            for name, value in limits.items():
                if name not in LIMITS or float(value) <= 0:
                    raise ValueError(f"{path}: rungs.{rung}.{name} = {value!r}")
                rungs[rung][name] = float(value)
        if baseline not in RUNGS:
            raise ValueError(f"{path}: baseline = {baseline!r}")
        return cls(baseline=baseline, rungs=rungs)


def ratios(result: Result, base: Result | None) -> tuple[float, float]:
    """``result``'s requests a second and 99th percentile, over ``base``'s."""
    if base is None:
        return 0.0, 0.0
    throughput = result.per_second / base.per_second if base.per_second else 0.0
    tail = result.p99_ms / base.p99_ms if base.p99_ms else 0.0
    return throughput, tail


def problems(results: Mapping[str, Result], budgets: LoadBudgets) -> list[str]:
    """What is over budget: one line each.

    Any unexpected answer, on any rung. For a budgeted rung that ran with
    the baseline, a 99th percentile more than ``p99_ratio`` times the
    baseline's, or fewer requests a second than ``throughput_ratio`` of the
    baseline's.
    """
    found = []
    for name, result in results.items():
        if result.unexpected:
            said = "; ".join(result.samples)
            found.append(
                f"{name}: {result.unexpected} answers were not the rung's: {said}"
            )
    base = results.get(budgets.baseline)
    for name, limits in budgets.rungs.items():
        measured = results.get(name)
        if measured is None or base is None:
            continue
        throughput, tail = ratios(measured, base)
        if tail > limits.get("p99_ratio", float("inf")):
            found.append(
                f"{name}: the 99th percentile is {tail:.1f} times "
                f"{budgets.baseline}'s; the budget is {limits['p99_ratio']:g}"
            )
        if throughput < limits.get("throughput_ratio", 0.0):
            found.append(
                f"{name}: {throughput:.2f} of {budgets.baseline}'s requests a "
                f"second; the budget is at least {limits['throughput_ratio']:g}"
            )
    return found


def requests_of(rung: Rung, fixture: Mapping[str, str]) -> Callable[[], bytes]:
    """What makes the rung's request, as it goes on the wire.

    The same bytes each time, but for a rung that sends an
    ``Idempotency-Key``: a new key each time.
    """
    path, body = rung.path, b""
    if rung.method == "POST":
        body = rung.body.encode() if rung.body else BODY
    for name, value in fixture.items():
        path = path.replace(f"{{{name}}}", value)
        body = body.replace(f"{{{name}}}".encode(), value.encode())
    lines = [
        f"{rung.method} {path} HTTP/1.1",
        f"Host: {HOST}",
        "Accept: application/json",
        "User-Agent: domainkit-load/1.0",
    ]
    if rung.token:
        lines.append(f"Authorization: Bearer {fixture['token']}")
    if body:
        lines += ["Content-Type: application/json", f"Content-Length: {len(body)}"]
    head = "\r\n".join(lines).encode()
    if not rung.idempotency_key:
        whole = head + b"\r\n\r\n" + body
        return lambda: whole
    run = uuid.uuid4().hex
    numbers = iter(range(10**12))

    def keyed() -> bytes:
        key = f"{run}-{next(numbers):012d}".encode()
        return head + b"\r\nIdempotency-Key: " + key + b"\r\n\r\n" + body

    return keyed


async def answer(reader: asyncio.StreamReader) -> tuple[int, bytes]:
    """Read one response: its status and its body.

    Raises:
        ValueError: The response is not one this client reads (chunked).
    """
    head = await reader.readuntil(b"\r\n\r\n")
    status = int(head[9:12])
    length = 0
    for line in head.split(b"\r\n")[1:]:
        name, _, value = line.partition(b":")
        if name.lower() == b"content-length":
            length = int(value)
        elif name.lower() == b"transfer-encoding":
            raise ValueError("A chunked response.")
    return status, await reader.readexactly(length) if length else b""


@dataclass(slots=True)
class Tally:
    """What the connections of one round saw."""

    latencies: list[float] = field(default_factory=list)
    unexpected: int = 0
    samples: list[str] = field(default_factory=list)

    def refuse(self, what: str) -> None:
        """Count an answer that was not the rung's, and keep a few."""
        self.unexpected += 1
        if len(self.samples) < SAMPLES:
            self.samples.append(what)


async def connection(
    port: int, request: Callable[[], bytes], expected: int, until: float, tally: Tally
) -> None:
    """Send requests over one connection, one after another, until ``until``."""
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
    except OSError as failure:
        tally.refuse(f"no connection: {failure}")
        return
    try:
        while (started := time.perf_counter()) < until:
            writer.write(request())
            await writer.drain()
            status, body = await answer(reader)
            tally.latencies.append(time.perf_counter() - started)
            if status != expected:
                tally.refuse(f"{status} {body[:200]!r}")
    except (OSError, asyncio.IncompleteReadError, asyncio.LimitOverrunError) as lost:
        tally.refuse(f"connection lost: {type(lost).__name__}")
    except ValueError as unread:
        tally.refuse(f"unreadable: {unread}")
    finally:
        writer.close()
        with suppress(OSError):  # Reset by the server: closed all the same.
            await writer.wait_closed()


async def load(
    port: int,
    rung: Rung,
    fixture: Mapping[str, str],
    connections: int,
    seconds: float,
    warm_up: float = WARM_UP,
) -> Round:
    """``seconds`` of load on the rung served at ``port``, after a warm-up."""
    request = requests_of(rung, fixture)

    async def stretch(length: float) -> tuple[Tally, float]:
        tally = Tally()
        started = time.perf_counter()
        await asyncio.gather(
            *(
                connection(port, request, rung.status, started + length, tally)
                for _ in range(connections)
            )
        )
        return tally, time.perf_counter() - started

    if warm_up:
        await stretch(warm_up)
    tally, took = await stretch(seconds)
    return Round(
        requests=len(tally.latencies),
        seconds=took,
        latencies=tuple(sorted(tally.latencies)),
        unexpected=tally.unexpected,
        samples=tuple(tally.samples),
    )


def serve(
    rung: Rung,
    listener: int,
    schema: str,
    pool_size: int | None = None,
    max_overflow: int | None = None,
) -> None:
    """Serve the rung's application until the process is stopped.

    ``listener`` is the file descriptor of a socket that already listens:
    the harness bound it and handed it down, so no other process can take
    its port between the choice and the server's start.

    ``pool_size`` and ``max_overflow`` replace the database's ``POOL_SIZE``
    and ``MAX_OVERFLOW``; unset, the project's (SQLAlchemy's 5 and 10).
    """
    import uvicorn

    from benchmarks.rung import domainkit_app, fastapi_app, raw_app

    # As the ladder: the project's known problems are not this run's news.
    logging.getLogger("domainkit.http.application").setLevel(logging.ERROR)
    database: dict[str, Any] | None = None
    if rung.database:
        database = {
            "URL": os.environ[POSTGRES_VARIABLE],
            "CONNECT_ARGS": {"server_settings": {"search_path": schema}},
        }
        if pool_size is not None:
            database["POOL_SIZE"] = pool_size
        if max_overflow is not None:
            database["MAX_OVERFLOW"] = max_overflow
    with ExitStack() as keep:
        if rung.app == "asgi":
            app = raw_app()
        elif rung.app == "fastapi":
            app = fastapi_app()
        else:
            app = domainkit_app(rung, database, keep)
        config = uvicorn.Config(app, log_level="error", access_log=False)
        uvicorn.Server(config).run(sockets=[socket.socket(fileno=listener)])


def wait_ready(
    port: int, server: "subprocess.Popen[bytes]", timeout: float = READY_TIMEOUT
) -> None:
    """Wait until the server answers a request.

    Its socket listens before it runs, so a connection proves nothing: an
    HTTP answer, of any status, does.

    Raises:
        RuntimeError: It stopped, or did not answer in time.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if server.poll() is not None:
            raise RuntimeError(f"The server stopped with status {server.returncode}.")
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5) as probe:
                probe.sendall(PROBE)
                if probe.recv(9).startswith(b"HTTP/1.1 "):
                    return
        except OSError:  # Refused, reset, or no answer yet: not up.
            pass
        time.sleep(0.05)
    raise RuntimeError("The server did not answer in time.")


def start(
    tree: Tree, rung: Rung, scratch: Path, postgres: str, pool: Sequence[str] = ()
) -> tuple["subprocess.Popen[bytes]", int]:
    """Start the rung's server, and wait until it answers; it, and its port.

    The socket is bound here and inherited by the server. This process
    then closes its own copy, so a server that dies refuses connections.
    What the server prints goes to a file in ``scratch``, not to the
    terminal: on a failure its last lines are reported, ``postgres``
    masked.

    Raises:
        RuntimeError: It stopped, or did not answer in time.
    """
    log = scratch / f"{rung.name}.log"
    command = [tree.python, "-m", "benchmarks.load", "--serve", rung.name]
    with socket.socket() as listener, log.open("wb") as output:
        listener.bind(("127.0.0.1", 0))
        listener.listen(BACKLOG)
        port: int = listener.getsockname()[1]
        command += ["--fd", str(listener.fileno()), "--schema", tree.schema, *pool]
        server = subprocess.Popen(  # noqa: S603 - our own interpreter and module.
            command,
            env=tree.environment(scratch / "migrations", postgres),
            cwd=ROOT,
            stdout=output,
            stderr=subprocess.STDOUT,
            pass_fds=[listener.fileno()],
        )
    try:
        wait_ready(port, server)
    except RuntimeError as failure:
        stop([server])
        said = masked(log.read_text(errors="replace")[-3000:], postgres)
        raise RuntimeError(f"{rung.name}: {failure}\n{said}") from None
    return server, port


def stop(servers: Sequence["subprocess.Popen[bytes]"]) -> None:
    """End the servers, and wait until each is gone."""
    for server in servers:
        server.terminate()
    for server in servers:
        try:
            server.wait(timeout=STOP_TIMEOUT)
        except subprocess.TimeoutExpired:
            server.kill()
            server.wait()


def measure(options: argparse.Namespace) -> dict[str, Result]:
    """Serve each chosen rung, and load them in alternating rounds.

    Raises:
        RuntimeError: A database rung was chosen and no database named, or
            the database or a server could not be made ready.
    """
    rungs = [RUNGS[name] for name in options.rungs.split(",")]
    postgres = os.environ.get(POSTGRES_VARIABLE, "")
    needs_database = any(rung.database for rung in rungs)
    if needs_database and not postgres:
        raise RuntimeError(f"Database rungs need {POSTGRES_VARIABLE}.")
    tree = Tree("load", ROOT / "src", sys.executable)
    pool: list[str] = []
    if options.pool_size is not None:
        pool += ["--pool-size", str(options.pool_size)]
    if options.max_overflow is not None:
        pool += ["--max-overflow", str(options.max_overflow)]
    rounds: dict[str, list[Round]] = {rung.name: [] for rung in rungs}
    servers: list[subprocess.Popen[bytes]] = []
    with tempfile.TemporaryDirectory() as directory:
        scratch = Path(directory)
        try:
            if needs_database:
                prepare(tree, postgres, scratch)
            fixture: dict[str, str] = (
                json.loads(tree.fixture.read_text())
                if tree.fixture is not None and tree.schema
                else {}
            )
            ports: dict[str, int] = {}
            for rung in rungs:
                # One at a time: those started are stopped if a later one fails.
                server, ports[rung.name] = start(tree, rung, scratch, postgres, pool)
                servers.append(server)
            for _ in range(options.rounds):
                for rung in rungs:
                    port = ports[rung.name]
                    rounds[rung.name].append(
                        asyncio.run(
                            load(
                                port,
                                rung,
                                fixture,
                                options.connections,
                                options.seconds,
                                options.warm_up,
                            )
                        )
                    )
        finally:
            stop(servers)
            if tree.schema:
                child(
                    tree,
                    ["benchmarks.database", "drop", tree.schema],
                    scratch / "migrations",
                    postgres,
                )
    return {name: Result.of(name, found) for name, found in rounds.items()}


def table(results: Mapping[str, Result], baseline: str) -> str:
    """The results, a rung a line, with each one's ratios to the baseline."""
    base = results.get(baseline)
    lines = [
        (
            f"{'rung':<18}{'req/s':>9}{'p50 ms':>9}{'p99 ms':>9}"
            f"{'req/s ratio':>13}{'p99 ratio':>11}{'unexpected':>12}"
        )
    ]
    for name, result in results.items():
        throughput, tail = ratios(result, base)
        lines.append(
            f"{name:<18}{result.per_second:>9.0f}{result.p50_ms:>9.2f}"
            f"{result.p99_ms:>9.2f}{throughput:>13.2f}{tail:>11.2f}"
            f"{result.unexpected:>12}"
        )
    return "\n".join(lines)


def markdown(results: Mapping[str, Result], baseline: str, found: Sequence[str]) -> str:
    """The results as a Markdown table, for the job's summary."""
    base = results.get(baseline)
    lines = [
        "### Under load",
        "",
        f"Ratios are to `{baseline}`, measured in the same run.",
        "",
        "| Rung | Requests/s | p50 ms | p99 ms | Requests/s ratio | p99 ratio |",
        "| --- | ---: | ---: | ---: | ---: | ---: |",
    ]
    for name, result in results.items():
        throughput, tail = ratios(result, base)
        lines.append(
            f"| `{name}` | {result.per_second:.0f} | {result.p50_ms:.2f} | "
            f"{result.p99_ms:.2f} | {throughput:.2f} | {tail:.2f} |"
        )
    lines += ["", *(f"- **Over budget:** {line}" for line in found)]
    return "\n".join(lines) + "\n"


def parse(argv: Sequence[str] | None) -> argparse.Namespace:
    """The command line."""
    parser = argparse.ArgumentParser(
        prog="python -m benchmarks.load",
        description="Measure rungs of the ladder under load.",
    )
    parser.add_argument("--rungs", default=",".join(DEFAULT_RUNGS))
    parser.add_argument("--connections", type=int, default=32)
    parser.add_argument("--seconds", type=float, default=4.0)
    parser.add_argument("--rounds", type=int, default=3)
    parser.add_argument(
        "--warm-up", type=float, default=WARM_UP, help="seconds, before each round"
    )
    parser.add_argument("--strict", action="store_true", help="fail over budget")
    parser.add_argument("--json", default="", help="write the results to this file")
    parser.add_argument("--summary", default="", help="append a Markdown table here")
    parser.add_argument(
        "--pool-size", type=int, help="POOL_SIZE of the database rungs' servers"
    )
    parser.add_argument(
        "--max-overflow", type=int, help="MAX_OVERFLOW of the database rungs' servers"
    )
    parser.add_argument("--serve", default="", help=argparse.SUPPRESS)
    parser.add_argument("--fd", type=int, default=0, help=argparse.SUPPRESS)
    parser.add_argument("--schema", default="", help=argparse.SUPPRESS)
    options = parser.parse_args(argv)
    unknown = [name for name in options.rungs.split(",") if name not in RUNGS]
    if unknown or options.serve not in ("", *RUNGS):
        parser.error(f"no rung named {', '.join(unknown) or options.serve}")
    return options


def main(argv: Sequence[str] | None = None) -> int:
    """Measure the rungs under load; 1 with ``--strict`` when over budget."""
    options = parse(argv)
    if options.serve:
        serve(
            RUNGS[options.serve],
            options.fd,
            options.schema,
            options.pool_size,
            options.max_overflow,
        )
        return 0
    budgets = LoadBudgets.read(BUDGETS)
    try:
        results = measure(options)
    except RuntimeError as failure:
        url = os.environ.get(POSTGRES_VARIABLE, "")
        sys.stderr.write(masked(str(failure), url) + "\n")
        return 1
    sys.stdout.write(table(results, budgets.baseline) + "\n")
    found = problems(results, budgets)
    for line in found:
        sys.stderr.write(f"OVER BUDGET: {line}\n")
    summary = options.summary or os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with Path(summary).open("a") as output:
            output.write(markdown(results, budgets.baseline, found))
    if options.json:
        figures = {name: result.figures() for name, result in results.items()}
        Path(options.json).write_text(json.dumps(figures, indent=2) + "\n")
    return 1 if found and options.strict else 0


if __name__ == "__main__":
    raise SystemExit(main())
