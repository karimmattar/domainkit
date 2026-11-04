"""Serve the same service on each framework and measure it the same way.

    python -m benchmarks.compare.run --check            # each app answers right
    python -m benchmarks.compare.run --duration 60s     # measure, with oha

Each app is served by uvicorn, one process, on PostgreSQL
(``COMPARE_DATABASE_URL``, an asyncpg URL), from the same table and rows
(``schema.sql``, made again before each app). ``oha`` sends the load; it
must be on the ``PATH``. See ``README.md`` for what is compared and how to
publish the numbers.
"""

import argparse
import asyncio
import json
import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import httpx
from sqlalchemy.ext.asyncio import create_async_engine

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
APPS: dict[str, list[str]] = {
    "ours": ["--factory", "domainkit.http:get_asgi_application"],
    "fastapi": ["benchmarks.compare.apps.fastapi_app:app"],
    "litestar": ["benchmarks.compare.apps.litestar_app:app"],
    "django": ["benchmarks.compare.apps.django_app.asgi:app"],
}
"""What uvicorn serves for each app."""
ENVIRONMENT: dict[str, dict[str, str]] = {
    "ours": {"DOMAINKIT_SETTINGS_MODULE": "benchmarks.compare.apps.ours.settings"},
}
KEY = "compare-key-0123456789"
NEW_ITEM = {"name": "a lamp", "price": "19.99"}


async def reset(url: str) -> None:
    """Make the table again, with its 1,000 rows."""
    engine = create_async_engine(url)
    async with engine.begin() as connection:
        for statement in (HERE / "schema.sql").read_text().split(";"):
            sql = "\n".join(
                line for line in statement.splitlines() if not line.startswith("--")
            ).strip()
            if sql:
                await connection.exec_driver_sql(sql)
    await engine.dispose()


def free_port() -> int:
    """A port nothing listens on."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port: int = sock.getsockname()[1]
        return port


def serve(app: str, port: int, url: str) -> subprocess.Popen[bytes]:
    """Start uvicorn serving ``app`` on ``port``."""
    env = {**os.environ, **ENVIRONMENT.get(app, {})}
    env |= {"COMPARE_DATABASE_URL": url, "COMPARE_API_KEY": KEY}
    env["PYTHONPATH"] = os.pathsep.join(
        filter(None, [str(ROOT), env.get("PYTHONPATH")])
    )
    command = [sys.executable, "-m", "uvicorn", *APPS[app], "--port", str(port)]
    command += ["--no-access-log", "--log-level", "warning", "--workers", "1"]
    return subprocess.Popen(command, cwd=ROOT, env=env)  # noqa: S603


def wait_ready(base: str, server: subprocess.Popen[bytes], seconds: float = 30) -> None:
    """Wait until the app answers.

    Raises:
        RuntimeError: The server exited.
        TimeoutError: It did not answer within ``seconds``.
    """
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if server.poll() is not None:
            raise RuntimeError(f"{base}: the server exited ({server.returncode})")
        try:
            httpx.get(f"{base}/items", headers={"X-API-Key": KEY}, timeout=1)
        except httpx.HTTPError:
            time.sleep(0.2)
        else:
            return
    raise TimeoutError(f"{base} did not answer within {seconds} seconds")


def check(base: str) -> list[str]:
    """What the app answers wrong, against the spec in README.md."""
    keyed = {"X-API-Key": KEY}
    problems: list[str] = []

    def expect(what: str, response: httpx.Response, status: int) -> None:
        if response.status_code != status:
            problems.append(f"{what}: {response.status_code}, not {status}")

    listed = httpx.get(f"{base}/items", headers=keyed)
    expect("GET /items", listed, 200)
    if listed.status_code == 200 and len(listed.json()["items"]) != 20:
        problems.append("GET /items: not 20 items")
    created = httpx.post(f"{base}/items", headers=keyed, json=NEW_ITEM)
    expect("POST /items", created, 201)
    if created.status_code == 201:
        body = created.json()
        if (body["name"], str(body["price"])) != (NEW_ITEM["name"], NEW_ITEM["price"]):
            problems.append(f"POST /items answered {body}")
        expect(
            "GET /items/{id}",
            httpx.get(f"{base}/items/{body['id']}", headers=keyed),
            200,
        )
    bad = httpx.post(f"{base}/items", headers=keyed, json={"name": "", "price": "-1"})
    if bad.status_code not in {400, 422}:
        problems.append(f"POST /items with a bad body: {bad.status_code}")
    expect("GET /items without the key", httpx.get(f"{base}/items"), 401)
    missing = f"{base}/items/00000000-0000-0000-0000-000000000000"
    expect("GET /items/{unknown}", httpx.get(missing, headers=keyed), 404)
    return problems


def load(
    base: str, item_id: str, args: argparse.Namespace
) -> dict[str, dict[str, float]]:
    """Requests a second and latencies of each route, from oha."""
    routes = {
        "read": ["GET", f"{base}/items/{item_id}", []],
        "page": ["GET", f"{base}/items", []],
        "create": [
            "POST",
            f"{base}/items",
            ["-d", json.dumps(NEW_ITEM), "-T", "application/json"],
        ],
    }
    results: dict[str, dict[str, float]] = {}
    for name, (method, url, extra) in routes.items():
        command = ["oha", "--no-tui", "--output-format", "json", "-m", method]
        command += ["-H", f"X-API-Key: {KEY}", "-c", str(args.connections), *extra]
        warmup = [*command, "-z", args.warmup, url]
        subprocess.run(warmup, check=True, capture_output=True)  # noqa: S603 - oha, ours.
        measured = [*command, "-z", args.duration, url]
        done = subprocess.run(measured, check=True, capture_output=True)  # noqa: S603
        report: dict[str, Any] = json.loads(done.stdout)
        codes = report.get("statusCodeDistribution", {})
        expected = "201" if method == "POST" else "200"
        results[name] = {
            "rps": report["summary"]["requestsPerSec"],
            "p50_ms": report["latencyPercentiles"]["p50"] * 1000,
            "p99_ms": report["latencyPercentiles"]["p99"] * 1000,
            "errors": sum(n for code, n in codes.items() if code != expected),
        }
    return results


def table(results: dict[str, dict[str, dict[str, float]]]) -> str:
    """The results as a Markdown table."""
    lines = [
        "| App | Route | Requests/s | p50 ms | p99 ms | Errors |",
        "| --- | --- | ---: | ---: | ---: | ---: |",
    ]
    for app, routes in results.items():
        for route, figures in routes.items():
            lines.append(
                f"| {app} | {route} | {figures['rps']:,.0f} | {figures['p50_ms']:.2f} "
                f"| {figures['p99_ms']:.2f} | {figures['errors']:.0f} |"
            )
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    """Check or measure each app; 1 if one answered wrong."""
    parser = argparse.ArgumentParser(prog="python -m benchmarks.compare.run")
    parser.add_argument("apps", nargs="*", choices=[[], *APPS], default=[])
    parser.add_argument("--check", action="store_true", help="only check answers")
    parser.add_argument("--duration", default="30s")
    parser.add_argument("--warmup", default="5s")
    parser.add_argument("--connections", type=int, default=64)
    parser.add_argument("--json", type=Path, help="keep every figure here")
    args = parser.parse_args(argv)
    url = os.environ["COMPARE_DATABASE_URL"]
    if not args.check and shutil.which("oha") is None:
        parser.error("measuring needs oha on the PATH (https://github.com/hatoo/oha)")
    failed = False
    results: dict[str, dict[str, dict[str, float]]] = {}
    for app in args.apps or list(APPS):
        asyncio.run(reset(url))
        port = free_port()
        base = f"http://127.0.0.1:{port}"
        server = serve(app, port, url)
        try:
            wait_ready(base, server)
            problems = check(base)
            verdict = "ok" if not problems else "; ".join(problems)
            sys.stderr.write(f"{app}: {verdict}\n")
            failed |= bool(problems)
            if not args.check and not problems:
                first = httpx.get(f"{base}/items", headers={"X-API-Key": KEY})
                results[app] = load(base, first.json()["items"][0]["id"], args)
        finally:
            server.terminate()
            server.wait(10)
    if results:
        sys.stdout.write(table(results))
        if args.json:
            args.json.write_text(json.dumps(results, indent=2))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
