"""A process loads what it uses, and the public names still resolve (PERF-15).

``import domainkit.http`` loaded the whole application's imports, FastAPI
and SQLAlchemy included (679 modules at 0.11). A served application with
messaging and no broker or ledger loaded the ledgers (the immudb one
too), the brokers, and the messaging worker and dispatcher. They are
imported on first use now; each check runs in a process of its own.
"""

import importlib.metadata
import os
import subprocess
import sys
import textwrap
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

import pytest

import domainkit
import domainkit.http
from domainkit import lifecycle
from domainkit.conf import override_settings
from domainkit.contrib import messaging
from domainkit.contrib.messaging import worker
from domainkit.http import application, health
from domainkit.ledger import MemoryLedger
from domainkit.utils.module_loading import lazy_names

ROOT = Path(__file__).parents[1]
NEVER_LOADED = (
    "domainkit.ledger",
    "domainkit.broker",
    "domainkit.contrib.messaging.worker",
    "domainkit.contrib.messaging.dispatch",
)
"""What a web process with messaging, and no broker or ledger, never needs."""
SETTINGS = """\
INSTALLED_DOMAINS = [
    "domainkit.contrib.messaging",
    "domainkit.contrib.contenttypes",
    "domainkit.contrib.auth",
]
ROOT_ROUTER = "domainkit.http.health.router"
DATABASES = {"default": {"URL": "sqlite+aiosqlite:///:memory:"}}
"""
SERVE = """\
import asyncio, sys, uuid

import domainkit.http

print([name for name in {heavy!r} if name in sys.modules])
from domainkit.conf import configure

configure("lazy_settings")
app = domainkit.http.get_asgi_application()


async def main() -> None:
    sent = []

    async def receive():
        return {{"type": "http.request", "body": b"", "more_body": False}}

    async def send(message):
        sent.append(message)

    scope = {{
        "type": "http", "asgi": {{"version": "3.0"}}, "http_version": "1.1",
        "method": "GET", "scheme": "http", "path": "/ready",
        "raw_path": b"/ready", "root_path": "", "query_string": b"",
        "headers": [(b"host", b"localhost")], "client": ("127.0.0.1", 1),
        "server": ("127.0.0.1", 80), "state": {{}},
    }}
    async with app.router.lifespan_context(app):
        await app(scope, receive, send)  # Readiness checks the ledgers too.
        from domainkit.apps import registry
        from domainkit.contrib.messaging import MessagingSettings
        from domainkit.contrib.messaging.outbox import notify_committed

        config = registry.settings(MessagingSettings)
        await notify_committed(config, "default", [{{"id": uuid.uuid4()}}])
    print(sent[0]["status"])


asyncio.run(main())
print([name for name in sys.modules if name.startswith({never!r})])
"""


def run(script: str, path: Path | None = None) -> list[str]:
    """Run ``script`` in a fresh interpreter; the lines it printed."""
    environment = {
        name: value
        for name, value in os.environ.items()
        if not name.startswith("DOMAINKIT_")
    }
    paths = [str(ROOT), str(ROOT / "src"), *([str(path)] if path else [])]
    environment["PYTHONPATH"] = os.pathsep.join(paths)
    done = subprocess.run(  # noqa: S603 - the test interpreter and a fixed script.
        [sys.executable, "-c", script],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    assert done.returncode == 0, done.stderr
    return done.stdout.splitlines()


@pytest.mark.reads_own_source
def test_importing_domainkit_http_loads_neither_fastapi_nor_sqlalchemy() -> None:
    """The package's names are imported when first read, and are the same objects."""
    script = textwrap.dedent(
        """\
        import sys

        import domainkit.http

        heavy = ["fastapi", "sqlalchemy", "importlib.metadata"]
        print([name for name in heavy if name in sys.modules])
        print(len(sys.modules) < 150)
        from domainkit.http import get_asgi_application
        from domainkit.http.application import get_asgi_application as built

        print(get_asgi_application is built)
        print("get_asgi_application" in vars(domainkit.http))
        """
    )
    assert run(script) == ["[]", "True", "True", "True"]


def test_a_served_application_loads_no_ledger_broker_or_worker(
    tmp_path: Path,
) -> None:
    """Built, served, ready-checked, and after a commit's follow-up: none of them."""
    (tmp_path / "lazy_settings.py").write_text(SETTINGS)
    script = SERVE.format(
        heavy=["fastapi", "sqlalchemy", "domainkit.http.application"],
        never=NEVER_LOADED,
    )
    assert run(script, tmp_path) == ["[]", "200", "[]"]


def test_the_lazy_names_are_the_public_ones() -> None:
    """Every name ``__all__`` lists resolves, to its module's object."""
    names = {name: getattr(domainkit.http, name) for name in domainkit.http.__all__}

    assert names["get_asgi_application"] is application.get_asgi_application
    assert messaging.Worker is worker.Worker
    assert messaging.current_attempt is worker.current_attempt
    assert domainkit.__version__ == version("domainkit")


def test_the_version_is_read_on_first_use_then_kept(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The first read asks the distribution and keeps the answer on the module.

    The suite's first read keeps it for every test after: without clearing
    it first, no test of a mutation run (one process, forked) called the
    hook again, and its mutants lived.
    """
    monkeypatch.delattr(domainkit, "__version__", raising=False)
    assert "__version__" not in vars(domainkit)

    assert domainkit.__version__ == version("domainkit")
    assert vars(domainkit)["__version__"] == version("domainkit")


def test_a_source_tree_has_an_unknown_version(monkeypatch: pytest.MonkeyPatch) -> None:
    """Run from a source tree, not installed: ``0.0.0+unknown``, then kept."""

    def missing(name: str) -> str:
        raise PackageNotFoundError(name)

    monkeypatch.delattr(domainkit, "__version__", raising=False)
    monkeypatch.setattr(importlib.metadata, "version", missing)

    assert domainkit.__version__ == "0.0.0+unknown"
    assert vars(domainkit)["__version__"] == "0.0.0+unknown"


@pytest.mark.parametrize("module", [domainkit, domainkit.http, messaging])
def test_other_names_are_still_missing(module: object) -> None:
    """A name the module does not have raises ``AttributeError``, as before."""
    with pytest.raises(AttributeError, match="has no attribute 'missing'"):
        getattr(module, "missing")  # noqa: B009 - the module's hook, on purpose.


def test_a_lazy_name_is_imported_once_then_kept(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The first read sets the name on the module; later reads skip the hook."""
    module = type(sys)("domainkit_tests.lazy_example")
    monkeypatch.setitem(sys.modules, module.__name__, module)
    hook = lazy_names(module.__name__, {"PurePath": "pathlib"})

    found = hook("PurePath")

    from pathlib import PurePath

    assert found is PurePath
    assert vars(module)["PurePath"] is PurePath
    with pytest.raises(AttributeError, match="no attribute 'Path'"):
        hook("Path")  # Not one of its names.


@pytest.mark.anyio
async def test_ledgers_and_brokers_close_only_once_loaded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Shutdown closes what was opened, and imports neither to find out."""
    closed: list[str] = []

    async def ledgers_closed() -> None:
        closed.append("ledgers")

    async def brokers_closed() -> None:
        closed.append("brokers")

    import domainkit.broker
    import domainkit.ledger

    monkeypatch.setattr(domainkit.ledger.ledgers, "close_all", ledgers_closed)
    monkeypatch.setattr(domainkit.broker.brokers, "close_all", brokers_closed)
    await lifecycle.close_ledgers()
    await lifecycle.close_brokers()
    monkeypatch.delitem(sys.modules, "domainkit.ledger")
    monkeypatch.delitem(sys.modules, "domainkit.broker")
    await lifecycle.close_ledgers()
    await lifecycle.close_brokers()

    assert closed == ["ledgers", "brokers"]
    assert "domainkit.ledger" not in sys.modules


@pytest.mark.anyio
async def test_readiness_checks_ledgers_only_where_one_is_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With ``LEDGERS``, each is checked; without, ``domainkit.ledger`` stays out."""
    checked: list[str] = []

    async def check(self: MemoryLedger) -> None:
        checked.append("memory")

    monkeypatch.setattr(MemoryLedger, "check", check)
    # Imported again below: the package's attribute is put back too.
    monkeypatch.setattr(domainkit, "ledger", sys.modules["domainkit.ledger"])
    monkeypatch.delitem(sys.modules, "domainkit.ledger")
    with override_settings(LEDGERS={}):
        assert await health._check_ledgers(1.0) == []
        assert "domainkit.ledger" not in sys.modules
    with override_settings(LEDGERS={"audit": {"URL": "memory://"}}):
        assert await health._check_ledgers(1.0) == []

    assert checked == ["memory"]
