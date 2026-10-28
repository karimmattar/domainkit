"""Measure one rung in this process and print its costs as one JSON line.

``python -m benchmarks`` starts a process per rung, so no rung inherits
another's imports, caches, or settings. Run one by hand to look closer::

    python -m benchmarks.rung template --profile-out template.pstats

Requests go straight into the ASGI application, one at a time, with a scope
built here: no server, no sockets. After a warm-up, the process's modules
are counted (what starting and serving the rung imported), one pass counts
calls and round trips (exact), then several time the process's CPU (best of
N). The counters are imported only then, so their SQLAlchemy is not counted
in a rung that does not use it.
"""

import argparse
import ast
import asyncio
import json
import logging
import os
import platform
import re
import statistics
import sys
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, MutableMapping
from contextlib import ExitStack, asynccontextmanager
from importlib import import_module
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any, Final

from benchmarks import STARTUP_MODULES
from benchmarks.ladder import IDEMPOTENCY_MIDDLEWARE, POSTGRES_VARIABLE, RUNGS, Rung

type Scope = MutableMapping[str, Any]
type Message = MutableMapping[str, Any]
type Receive = Callable[[], Awaitable[Message]]
type Send = Callable[[Message], Awaitable[None]]
type ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]

HELLO: Final = b'{"status":"ok"}'
BODY: Final = json.dumps({"name": "widget", "price": 42}).encode()
HOST: Final = "api.example.com"
TEMPLATE: Final = Path("management/templates/project/src/config/settings/base.py-tpl")
"""Where ``init`` keeps the settings it writes, inside the domainkit package."""
PACKAGES: Final = (
    "domainkit",
    "fastapi",
    "starlette",
    "pydantic",
    "sqlalchemy",
    "asyncpg",
    "uvloop",
)
"""Whose versions each result records."""
_PLACEHOLDER: Final = re.compile(r"\{\{.*?\}\}")


def raw_app() -> ASGIApp:
    """The cheapest ASGI application: one constant response."""
    start = {
        "type": "http.response.start",
        "status": 200,
        "headers": [(b"content-type", b"application/json"), (b"content-length", b"15")],
    }
    body = {"type": "http.response.body", "body": HELLO}

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        await send(start)
        await send(body)

    return app


def fastapi_app() -> ASGIApp:
    """FastAPI as its tutorial writes it: a route returning a dict."""
    from fastapi import FastAPI

    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    @app.get("/hello")
    async def hello() -> dict[str, str]:
        return {"status": "ok"}

    return app


def template_middleware() -> list[str]:
    """``MIDDLEWARE`` as ``domainkit init`` writes it, read from the template."""
    import domainkit

    source = (Path(domainkit.__file__).parent / TEMPLATE).read_text()
    tree = ast.parse(_PLACEHOLDER.sub("placeholder", source))
    for node in tree.body:
        if (
            isinstance(node, ast.Assign)
            and isinstance(node.targets[0], ast.Name)
            and node.targets[0].id == "MIDDLEWARE"
        ):
            return list(ast.literal_eval(node.value))
    raise LookupError(f"No MIDDLEWARE in {TEMPLATE}.")


def middleware(rung: Rung) -> list[str]:
    """The middleware ``rung`` runs, outermost first."""
    if rung.stack == "none":
        return []
    if rung.stack == "default":
        from domainkit.conf.global_settings import DEFAULT_MIDDLEWARE

        return list(DEFAULT_MIDDLEWARE)
    stack = template_middleware()
    if rung.stack == "template+idempotency":
        stack.append(IDEMPOTENCY_MIDDLEWARE)  # Last, as docs/contrib.md says.
    return stack


def domainkit_app(
    rung: Rung, database: dict[str, Any] | None, keep: ExitStack
) -> ASGIApp:
    """The project's application, with the rung's settings; they stay in force."""
    from domainkit.conf import configure, override_settings

    configure("benchmarks.project.settings")
    overrides: dict[str, Any] = {
        "MIDDLEWARE": middleware(rung),
        "RATE_LIMIT": rung.rate_limit,
    }
    if database is not None:
        overrides["DATABASES"] = {"default": database}
    if rung.json_log:
        from domainkit.log import build_logging_config

        config = build_logging_config(log_format="json", level="INFO")
        sink = keep.enter_context(Path(os.devnull).open("w"))  # noqa: SIM115 - kept.
        config["handlers"]["console"]["stream"] = sink
        overrides["LOGGING"] = config
    keep.enter_context(override_settings(**overrides))
    from domainkit.http import get_asgi_application

    app: ASGIApp = get_asgi_application()
    return app


class Driver:
    """Send the rung's request to ``app``, again and again, and check the answers."""

    def __init__(self, app: ASGIApp, rung: Rung, fixture: dict[str, str]) -> None:
        """Build the request once; only the ``Idempotency-Key`` changes."""
        self.app = app
        self.rung = rung
        # A plain dict: a Counter's first miss would cost a call more.
        self.statuses: dict[int, int] = {}
        self.unexpected: list[str] = []
        path = rung.path.replace("{item}", fixture.get("item", ""))
        headers = [
            (b"host", HOST.encode()),
            (b"user-agent", b"Mozilla/5.0 (X11; Linux x86_64) domainkit-bench/1.0"),
            (b"accept", b"application/json"),
            (b"accept-encoding", b"gzip, deflate, br"),
            (b"connection", b"keep-alive"),
        ]
        if rung.token:
            headers.append((b"authorization", f"Bearer {fixture['token']}".encode()))
        self.body = BODY if rung.method == "POST" else b""
        if self.body:
            headers.append((b"content-type", b"application/json"))
            headers.append((b"content-length", str(len(self.body)).encode()))
        self.scope: Scope = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.4"},
            "http_version": "1.1",
            "method": rung.method,
            "scheme": "http",
            "path": path,
            "raw_path": path.encode(),
            "root_path": "",
            "query_string": b"",
            "headers": headers,
            "client": ("203.0.113.7", 50000),
            "server": ("10.0.0.5", 8000),
        }
        # Keys of one length, new in every request and every run.
        self._keys = (f"{uuid.uuid4().hex}-{n:09d}".encode() for n in range(10**9))

    async def run(self, requests: int) -> None:
        """Send ``requests`` requests, one after another."""
        for _ in range(requests):
            await self.request()

    async def request(self) -> None:
        """Send the request once and note the status."""
        scope = dict(self.scope)
        scope["state"] = {}
        if self.rung.idempotency_key:
            scope["headers"] = [
                *self.scope["headers"],
                (b"idempotency-key", next(self._keys)),
            ]
        received = False
        chunks: list[bytes] = []
        status = 0

        async def receive() -> Message:
            nonlocal received
            if received:
                return {"type": "http.disconnect"}
            received = True
            return {"type": "http.request", "body": self.body, "more_body": False}

        async def send(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
            elif status != self.rung.status:
                chunks.append(message.get("body", b""))

        await self.app(scope, receive, send)
        self.statuses[status] = self.statuses.get(status, 0) + 1
        if status != self.rung.status and len(self.unexpected) < 3:
            self.unexpected.append(f"{status} {b''.join(chunks)[:300]!r}")

    def check(self) -> None:
        """Every request since the last check got the rung's status, or raise."""
        wrong = {
            status: n
            for status, n in self.statuses.items()
            if status != self.rung.status
        }
        self.statuses.clear()
        if wrong:
            raise RuntimeError(
                f"{self.rung.name}: expected {self.rung.status}, got {wrong}: "
                f"{self.unexpected}"
            )


def flush_logs() -> None:
    """Wait until the logging thread has written what is queued, if there is one."""
    try:
        from domainkit.log import flush_logs as flush
    except ImportError:  # domainkit before 0.8 wrote inline.
        return
    flush()


@asynccontextmanager
async def served(app: ASGIApp) -> AsyncIterator[None]:
    """Run the application's lifespan around the block, as a server would."""
    router = getattr(app, "router", None)
    context = getattr(router, "lifespan_context", None)
    if context is None:
        yield
        return
    async with context(app):
        yield


async def measure(
    rung: Rung, options: argparse.Namespace, fixture: dict[str, str]
) -> dict[str, Any]:
    """Build the rung's application and measure it while it is served.

    ``fixture`` holds the user's token and an item's id, for database rungs.
    """
    database: dict[str, Any] | None = None
    if rung.database:
        database = {
            "URL": os.environ[POSTGRES_VARIABLE],
            "CONNECT_ARGS": {"server_settings": {"search_path": options.schema}},
        }
    with ExitStack() as keep:
        if rung.app == "asgi":
            app = raw_app()
        elif rung.app == "fastapi":
            app = fastapi_app()
        else:
            app = domainkit_app(rung, database, keep)
        async with served(app):
            figures = await passes(Driver(app, rung, fixture), options)
            await _close()
    return {"rung": rung.name, **figures}


async def passes(driver: Driver, options: argparse.Namespace) -> dict[str, Any]:
    """Warm up, count one pass (exact), then time several (best of N)."""
    await driver.run(options.warmup)
    driver.statuses.clear()  # The throttled rung's first request passes.
    driver.unexpected.clear()
    # What building and serving the rung imported, the harness's own included.
    modules = len(sys.modules.keys() - STARTUP_MODULES)
    from benchmarks.counting import CallCounter, SqlCounter

    # What the window costs by itself, to take off: whole calls per request.
    flush_logs()
    with SqlCounter(), CallCounter() as idle:
        await driver.run(0)
        flush_logs()
    with SqlCounter() as sql, CallCounter() as calls:
        await driver.run(options.count)
        flush_logs()
    driver.check()
    if options.profile_out:
        calls.stats().dump_stats(options.profile_out)

    runs = []
    for _ in range(options.repeats):
        started = time.process_time_ns()
        await driver.run(options.requests)
        runs.append((time.process_time_ns() - started) / options.requests / 1000)
        flush_logs()
    driver.check()

    counted = options.count
    return {
        "calls": round((calls.calls - idle.calls) / counted, 2),
        "statements": round(sql.counts.statements / counted, 3),
        "transactions": round(sql.counts.transactions / counted, 3),
        "checkouts": round(sql.counts.checkouts / counted, 3),
        "pings": round(sql.counts.pings / counted, 3),
        "modules": modules,
        "verbs": {
            verb: round(n / counted, 3) for verb, n in sorted(sql.counts.verbs.items())
        },
        "cpu_us": {
            "best": round(min(runs), 2),
            "median": round(statistics.median(runs), 2),
            "worst": round(max(runs), 2),
        },
        "counted": counted,
        "timed": options.requests * options.repeats,
        "loop": type(asyncio.get_running_loop()).__module__.split(".")[0],
        "python": platform.python_version(),
        "versions": _versions(),
    }


async def _close() -> None:
    """Close the database pools a domainkit rung opened."""
    if "domainkit.db.connections" in sys.modules:
        connections = import_module("domainkit.db.connections").connections
        await connections.close_all()


def _versions() -> dict[str, str]:
    """Versions of what is measured; domainkit's from the tree imported."""
    found = {}
    for name in PACKAGES:
        try:
            found[name] = version(name)
        except PackageNotFoundError:
            found[name] = "?"
    if "domainkit" in sys.modules:
        found["domainkit"] = str(Path(sys.modules["domainkit"].__file__ or "").parent)
    return found


def parse(argv: list[str]) -> argparse.Namespace:
    """Read the command line."""
    parser = argparse.ArgumentParser(prog="python -m benchmarks.rung")
    parser.add_argument("rung", choices=sorted(RUNGS))
    parser.add_argument("--warmup", type=int, default=300)
    parser.add_argument("--count", type=int, default=200, help="requests counted")
    parser.add_argument("--requests", type=int, default=2000, help="per timed run")
    parser.add_argument("--repeats", type=int, default=5, help="timed runs")
    parser.add_argument("--schema", default="")
    parser.add_argument("--fixture", default="")
    parser.add_argument("--profile-out", default="")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Measure the rung named on the command line and print one JSON line."""
    options = parse(sys.argv[1:] if argv is None else argv)
    rung = RUNGS[options.rung]
    if rung.database and not os.environ.get(POSTGRES_VARIABLE):
        raise SystemExit(
            f"{rung.name} needs {POSTGRES_VARIABLE}, --schema and --fixture."
        )
    logging.getLogger("domainkit.http.application").setLevel(logging.ERROR)
    try:
        import uvloop
    except ImportError:  # uvloop installs on Linux and macOS.
        factory = None
    else:
        factory = uvloop.new_event_loop
    fixture = json.loads(Path(options.fixture).read_text()) if rung.database else {}
    result = asyncio.run(measure(rung, options, fixture), loop_factory=factory)
    sys.stdout.write(json.dumps(result) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
