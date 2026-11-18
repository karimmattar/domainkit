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
from domainkit import i18n, lifecycle
from domainkit.conf import override_settings
from domainkit.contrib import messaging
from domainkit.contrib.messaging import worker
from domainkit.http import application, health
from domainkit.i18n import catalogs, formats, lazy, timezone, translation
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
    """Run ``script`` in a fresh interpreter; the lines it printed.

    No project's settings reach it, nor coverage's: a measured interpreter
    loads coverage's modules too (A28-TEST-05).
    """
    environment = {
        name: value
        for name, value in os.environ.items()
        if not name.startswith(("DOMAINKIT_", "COVERAGE_PROCESS_"))
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


@pytest.mark.reads_own_source
def test_importing_the_settings_loads_no_layer_checker() -> None:
    """``domainkit.architecture`` is the settings' only when they are validated.

    The settings check ``ARCHITECTURE.EXTRA_LAYERS`` against the checker's
    layers; 2.8 imported it with ``domainkit.conf`` (audit A28-ARCH-04).
    """
    script = textwrap.dedent(
        """\
        import sys

        import domainkit.conf

        print("domainkit.architecture" in sys.modules)
        """
    )
    assert run(script) == ["False"]


@pytest.mark.reads_own_source
def test_an_adapter_calls_out_without_the_serving_package() -> None:
    """``domainkit.outbound`` loads neither ``domainkit.http`` nor the web libraries.

    2.8's client lived in ``domainkit.http``, so every adapter that called a
    service loaded that package (audit A28-ARCH-04, ADR 0195).
    """
    script = textwrap.dedent(
        """\
        import sys

        import domainkit.outbound

        loaded = ["domainkit.outbound.client", "httpx2"]
        print([name for name in loaded if name in sys.modules])
        from domainkit.outbound.client import HttpClient
        from domainkit.storage.s3 import S3Storage

        web = ["domainkit.http", "fastapi", "starlette"]
        print([name for name in web if name in sys.modules])
        """
    )
    assert run(script) == ["[]", "[]"]


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


@pytest.mark.reads_own_source
def test_a_domain_layer_marks_texts_without_loading_the_settings() -> None:
    """``domainkit.i18n.lazy`` loads neither the settings nor the domains (I2).

    Its lazy strings look a text up when shown, and only then load the
    catalogs; ``domainkit.i18n``'s names are imported on first use.
    """
    script = textwrap.dedent(
        """\
        import sys

        before = set(sys.modules)
        from domainkit.i18n.lazy import gettext_lazy, gettext_noop

        added = sorted(set(sys.modules) - before)
        print([name for name in added if name.startswith("domainkit")])
        stdlib = sys.stdlib_module_names
        print([name for name in added if name.split(".")[0] not in stdlib])
        label = gettext_lazy("The request is invalid.")
        print(gettext_noop("Already paid."))
        from domainkit.conf import configure

        configure("domainkit_tests.settings")
        from domainkit.i18n import override

        with override("ar"):
            print(str(label))
        print("domainkit.i18n.translation" in sys.modules)
        """
    )
    loaded = [
        "domainkit",
        "domainkit.i18n",
        "domainkit.i18n.lazy",
        "domainkit.utils",
        "domainkit.utils.module_loading",
    ]
    assert run(script) == [
        str(loaded),
        str(loaded),  # Nothing but domainkit's, and the standard library.
        "Already paid.",
        "الطلب غير صالح.",
        "True",
    ]


def test_the_translation_names_are_the_public_ones() -> None:
    """Each name of ``domainkit.i18n`` resolves to its module's object, once."""
    names = {name: getattr(i18n, name) for name in i18n.__all__}

    assert names["gettext"] is translation.gettext
    assert names["gettext_lazy"] is lazy.gettext_lazy is translation.gettext_lazy
    assert names["LazyString"] is lazy.LazyString
    assert names["timezone"] is timezone
    assert i18n.__getattr__("timezone") is timezone  # As a fresh process calls it.
    assert names["date_format"] is formats.date_format
    assert vars(i18n)["gettext"] is translation.gettext


@pytest.mark.reads_own_source
def test_the_package_reads_its_modules_as_attributes() -> None:
    """``domainkit.i18n.translation`` works without importing it first; ``dir`` too."""
    script = textwrap.dedent(
        """\
        import domainkit.i18n

        print(domainkit.i18n.translation.__name__, domainkit.i18n.formats.__name__)
        print(domainkit.i18n.validation.validation_message.__module__)
        listed = dir(domainkit.i18n)
        print(all(name in listed for name in domainkit.i18n.__all__))
        print([name for name in ("lazy", "translation", "catalogs") if name in listed])
        """
    )
    assert run(script) == [
        "domainkit.i18n.translation domainkit.i18n.formats",
        "domainkit.i18n.validation",
        "True",
        "['lazy', 'translation', 'catalogs']",
    ]
    assert {"timezone", "translation", "gettext", "__doc__"} <= set(dir(i18n))
    assert i18n.__getattr__("catalogs") is catalogs


def test_the_lookups_of_lazy_strings_are_imported_once() -> None:
    """A lazy string shown calls ``translation``'s function, kept after the first."""
    str(lazy.gettext_lazy("Orders"))

    assert lazy._found == (
        translation.gettext,
        translation.ngettext,
        translation.pgettext,
    )


@pytest.mark.parametrize("module", [domainkit, domainkit.http, messaging, i18n])
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
