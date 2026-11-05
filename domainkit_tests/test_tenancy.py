"""Multi-tenancy: each tenant's rows, caches, limits, logs, and database."""

import asyncio
import logging
from typing import Any, NewType
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

from domainkit.conf import TenancyConfig, override_settings
from domainkit.ddd import AggregateRoot, EventBus, InMemoryRepository, UnitOfWork
from domainkit.ddd.query import F
from domainkit.ddd.tenancy import (
    TenantMismatch,
    TenantOwned,
    TenantRequired,
    all_tenants,
    check_tenant_id,
    current_tenant,
    scoped_key,
    tenant,
)
from domainkit.exceptions import ImproperlyConfigured
from domainkit.http.access import AllowAny
from domainkit.http.errors import install_exception_handlers
from domainkit.resources import ALL_FIELDS, ResourceRouter
from domainkit.resources.schemas import schema_for
from domainkit.tenancy import TenantMiddleware, tenant_schema
from domainkit_tests.test_auth_access import user

pytestmark = pytest.mark.anyio

NoteId = NewType("NoteId", UUID)


class Note(TenantOwned, AggregateRoot[NoteId]):
    """One tenant's note."""

    text: str = ""


class Notes(InMemoryRepository[Note, NoteId]):
    """Notes, in memory."""


def note(text: str, owner: str | None = None) -> Note:
    """A new note."""
    return Note(id=NoteId(uuid4()), text=text, tenant_id=owner)


async def test_a_tenant_reads_and_changes_its_own_rows_only() -> None:
    """Added notes take the tenant; reads, finds, counts see only its own."""
    notes = Notes()
    with tenant("acme"):
        mine = note("a")
        await notes.add(mine)
        await notes.add(note("b"))
    with tenant("globex"):
        theirs = note("c")
        await notes.add(theirs)

        assert [n.text for n in await notes.filter()] == ["c"]
        assert await notes.find(mine.id) is None
        assert await notes.count() == 1
        assert await notes.exists(F("text") == "a") is False
    with tenant("acme"):
        assert {n.text for n in await notes.filter()} == {"a", "b"}
        assert (await notes.get(mine.id)).tenant_id == "acme"
    assert mine.tenant_id == "acme"
    assert theirs.tenant_id == "globex"


async def test_no_tenant_reads_nothing_unless_all_tenants_on_purpose() -> None:
    """Without a tenant: TenantRequired; all_tenants: every row."""
    notes = Notes()
    with tenant("acme"):
        await notes.add(note("a"))
    with tenant("globex"):
        await notes.add(note("b"))

    with pytest.raises(TenantRequired):
        await notes.filter()
    with pytest.raises(TenantRequired):
        await notes.add(note("orphan"))
    with all_tenants():
        assert len(await notes.filter()) == 2
        await notes.add(note("placed", owner="initech"))
        with pytest.raises(TenantRequired):
            await notes.add(note("nobody's"))


async def test_another_tenants_aggregate_is_refused() -> None:
    """Adding a note that names another tenant: TenantMismatch, 403."""
    notes = Notes()
    with tenant("acme"), pytest.raises(TenantMismatch) as refused:
        await notes.add(note("x", owner="globex"))
    assert refused.value.code == "tenant_mismatch"
    with tenant("acme"):
        made, created = await notes.get_or_create(F("text") == "y", lambda: note("y"))
    assert (made.tenant_id, created) == ("acme", True)


def test_tenant_ids_are_safe_to_put_anywhere() -> None:
    """Letters, digits, _ and -; 63 at most; no bools."""
    assert check_tenant_id(42) == "42"
    for bad in ("", "-x", "a b", "a" * 64, "a.b", True):
        with pytest.raises(ValueError, match="tenant id"):
            check_tenant_id(bad)
    with tenant("Acme-1"):
        assert current_tenant() == "Acme-1"
        assert scoped_key("k") == "t:Acme-1:k"
        with all_tenants():
            assert scoped_key("k") == "k"
    assert current_tenant() is None
    assert scoped_key("k") == "k"
    assert tenant_schema("Acme-1") == "tenant_acme_1"
    with override_settings(TENANCY={"SCHEMA": "t_{tenant}_data"}):
        assert tenant_schema("x") == "t_x_data"


def test_tenancy_settings_hold_together() -> None:
    """One way to name a tenant; schemas are plain names."""
    with pytest.raises(ValidationError, match="not both"):
        TenancyConfig(SUBDOMAIN_OF="example.com", HEADER="X-Tenant")
    with pytest.raises(ValidationError):
        TenancyConfig(SCHEMA='x"; drop')


async def member(user: Any, tenant_id: str) -> bool:  # noqa: ANN401
    """Only acme lets anyone in."""
    return tenant_id == "acme"


def resolve(connection: Any) -> str | None:  # noqa: ANN401
    """The tenant of a ?t= query parameter."""
    found: str | None = connection.query_params.get("t")
    return found


HERE = "domainkit_tests.test_tenancy"


def app_with(**tenancy: Any) -> TestClient:  # noqa: ANN401
    """An app behind the middleware, answering with the tenant it served."""
    inner = FastAPI()

    @inner.get("/whoami")
    async def whoami() -> dict[str, str | None]:
        return {"tenant": current_tenant()}

    @inner.get("/health")
    async def health() -> dict[str, str | None]:
        return {"tenant": current_tenant()}

    with override_settings(TENANCY=tenancy):
        wrapped = TenantMiddleware(inner)
    return TestClient(wrapped, base_url="http://acme.example.com")


def test_the_middleware_serves_the_subdomains_tenant() -> None:
    """acme.example.com is acme; example.com itself names none (400)."""
    client = app_with(SUBDOMAIN_OF="example.com")

    assert client.get("/whoami").json() == {"tenant": "acme"}
    bare = client.get("http://example.com/whoami")
    assert (bare.status_code, bare.json()["code"]) == (400, "tenant_not_named")
    deep = client.get("http://a.b.example.com/whoami")
    assert deep.json()["code"] == "tenant_not_named"
    assert client.get("http://example.com/health").json() == {"tenant": None}


def test_the_middleware_checks_membership_and_the_id() -> None:
    """A header names it; MEMBERSHIP refuses others (403); a bad id is 400."""
    client = app_with(HEADER="X-Tenant", MEMBERSHIP=f"{HERE}.member")

    assert client.get("/whoami", headers={"X-Tenant": "acme"}).json() == {
        "tenant": "acme"
    }
    other = client.get("/whoami", headers={"X-Tenant": "globex"})
    bad = client.get("/whoami", headers={"X-Tenant": "../etc"})
    assert (other.status_code, other.json()["code"]) == (403, "not_a_member")
    assert (bad.status_code, bad.json()["code"]) == (400, "invalid_tenant")


def test_a_resolver_of_the_projects_own() -> None:
    """RESOLVER replaces the others; REQUIRED off lets an unnamed one through."""
    client = app_with(RESOLVER=f"{HERE}.resolve", REQUIRED=False)

    assert client.get("/whoami?t=globex").json() == {"tenant": "globex"}
    assert client.get("/whoami").json() == {"tenant": None}
    nothing = app_with(REQUIRED=False)
    assert nothing.get("/whoami").json() == {"tenant": None}


def test_a_websocket_without_a_tenant_is_closed() -> None:
    """1008, before the app sees it."""
    from starlette.websockets import WebSocketDisconnect

    inner = FastAPI()

    @inner.websocket("/live")
    async def live(socket: Any) -> None:  # noqa: ANN401
        await socket.accept()

    with override_settings(TENANCY={"HEADER": "X-Tenant"}):
        client = TestClient(TenantMiddleware(inner))
    with (
        pytest.raises(WebSocketDisconnect) as closed,
        client.websocket_connect("/live"),
    ):
        pass
    assert closed.value.code == 1008


def test_the_middleware_needs_settings() -> None:
    """No TENANCY: refused at once."""
    with pytest.raises(ImproperlyConfigured, match="TENANCY"):
        TenantMiddleware(FastAPI())


async def test_lifespan_passes_through() -> None:
    """Scopes other than http and websocket are the app's."""
    seen: list[str] = []

    async def app(scope: Any, receive: Any, send: Any) -> None:  # noqa: ANN401
        seen.append(scope["type"])

    with override_settings(TENANCY={}):
        await TenantMiddleware(app)({"type": "lifespan"}, None, None)  # type: ignore[arg-type]
    assert seen == ["lifespan"]


async def test_caches_are_each_tenants_own() -> None:
    """The same key, two tenants, two entries."""
    from domainkit.cache import caches

    with override_settings(
        CACHES={"default": {"URL": "memory://"}},
    ):
        cache = caches["default"]
        with tenant("acme"):
            await cache.set("k", "acme's")
        with tenant("globex"):
            assert await cache.get("k") is None
            await cache.set("k", "globex's")
        with tenant("acme"):
            assert await cache.get("k") == "acme's"
        assert await cache.get("k") is None


async def test_rate_limits_count_each_tenant_apart() -> None:
    """A caller's count in one tenant leaves the other's whole."""
    from domainkit.http.ratelimit import MemoryBackend

    backend = MemoryBackend()
    with tenant("acme"):
        assert (await backend.hit("ip:1", 1, 60)).allowed
        assert not (await backend.hit("ip:1", 1, 60)).allowed
        await backend.refund("ip:1", 1, 60)
        assert (await backend.hit("ip:1", 1, 60)).allowed
    with tenant("globex"):
        assert (await backend.hit("ip:1", 1, 60)).allowed


def test_logs_name_the_tenant() -> None:
    """Every record carries tenant ("-" outside one)."""
    from domainkit.log import ContextFilter

    record = logging.LogRecord("x", logging.INFO, "f", 1, "m", (), None)
    ContextFilter().filter(record)
    assert record.__dict__["tenant"] == "-"
    with tenant("acme"):
        ContextFilter().filter(record)
    assert record.__dict__["tenant"] == "acme"


async def test_tasks_run_as_the_tenant_that_queued_them() -> None:
    """The outbox header carries it; the worker serves it."""
    from domainkit.contrib.messaging.outbox import context_headers
    from domainkit.contrib.messaging.worker import _as_tenant

    assert "tenant" not in context_headers()
    with tenant("acme"):
        headers = context_headers()
    assert headers["tenant"] == "acme"
    with _as_tenant(headers["tenant"]):
        assert current_tenant() == "acme"
    with _as_tenant(None):
        assert current_tenant() is None


def test_contexts_do_not_leak_between_tasks() -> None:
    """A tenant set in one task is not another's."""

    async def main() -> list[str | None]:
        async def serve(name: str) -> str | None:
            with tenant(name):
                await asyncio.sleep(0.01)
                return current_tenant()

        return list(await asyncio.gather(serve("a"), serve("b")))

    assert asyncio.run(main()) == ["a", "b"]


def sqlite() -> dict[str, str]:
    """A SQLite database's settings."""
    return {"URL": "sqlite+aiosqlite:///:memory:"}


def test_a_shard_group_takes_the_tenants_database() -> None:
    """No shard key: the current tenant's; PINNED gives one its own."""
    from domainkit.db import SqlAlchemyUnitOfWork

    class Work(SqlAlchemyUnitOfWork):
        database = "tenants"

    with override_settings(
        DATABASES={"default": sqlite(), "a": sqlite(), "b": sqlite(), "vip": sqlite()},
        SHARDS={
            "tenants": {"DATABASES": ["a", "b", "vip"], "PINNED": {"bigco": "vip"}}
        },
    ):
        with tenant("bigco"):
            assert Work().alias == "vip"
        with tenant("smallco"):
            from domainkit.db.routing import router

            assert Work().alias == router.shards("tenants").alias_for("smallco")
            assert Work(shard_key="bigco").alias == "vip"
        with pytest.raises(ImproperlyConfigured, match="shard_key"):
            Work()


def test_a_schema_database_needs_a_tenant() -> None:
    """A unit of work on SCHEMA_DATABASES names the tenant's schema, or refuses."""
    from domainkit.db import SqlAlchemyUnitOfWork

    class Work(SqlAlchemyUnitOfWork):
        database = "default"

    with override_settings(TENANCY={"SCHEMA_DATABASES": ["default"]}):
        with tenant("acme"):
            assert Work().schema == "tenant_acme"
        with pytest.raises(TenantRequired):
            Work()
    assert Work().schema is None


def test_migrating_a_tenant_needs_a_schema_database() -> None:
    """--tenant on a database without schemas, or on SQLite: refused."""
    from domainkit.management import call_command
    from domainkit.management.base import CommandError

    with (
        override_settings(TENANCY={"SCHEMA_DATABASES": []}),
        pytest.raises(CommandError, match="no schema per tenant"),
    ):
        call_command("migrate", "--tenant", "acme", "--database", "default")
    with override_settings(TENANCY={"SCHEMA_DATABASES": ["default"]}):
        with pytest.raises(CommandError, match="needs PostgreSQL"):
            call_command("migrate", "--tenant", "acme")
        with pytest.raises(CommandError, match="--plan"):
            call_command("migrate", "--tenant", "acme", "--plan")


async def test_a_schema_databases_transactions_work_in_the_tenants_schema(
    database_access: None,
) -> None:
    """On PostgreSQL: SET LOCAL search_path, so the pool is never left changed."""
    import os

    from sqlalchemy import text

    from domainkit.db import SqlAlchemyUnitOfWork, connections

    url = os.environ.get("DOMAINKIT_TEST_POSTGRES_URL")
    if not url:
        pytest.skip("set DOMAINKIT_TEST_POSTGRES_URL")

    class Work(SqlAlchemyUnitOfWork):
        database = "default"

    with override_settings(
        DATABASES={"default": {"URL": url}},
        TENANCY={"SCHEMA_DATABASES": ["default"]},
    ):
        try:
            async with connections.engine("default").begin() as connection:
                await connection.execute(
                    text('CREATE SCHEMA IF NOT EXISTS "tenant_acme"')
                )
            with tenant("acme"):
                async with Work() as uow:
                    inside = await uow.session.scalar(text("SELECT current_schema()"))
            async with connections.engine("default").connect() as connection:
                after = await connection.scalar(text("SELECT current_schema()"))
        finally:
            await connections.close_all()

    assert inside == "tenant_acme"
    assert after != "tenant_acme"


@pytest.mark.parametrize("reads", ["transaction", "statements", "snapshot"])
async def test_a_read_only_unit_of_work_reads_the_tenants_schema(
    database_access: None, reads: str
) -> None:
    """Each way of reading works in the tenant's schema, ``STATEMENTS`` too.

    ``STATEMENTS`` reads without a transaction, where ``SET LOCAL`` sets
    nothing: it reads in one, as through PgBouncer.
    """
    import os

    from sqlalchemy import text

    from domainkit.db import SqlAlchemyUnitOfWork, connections

    url = os.environ.get("DOMAINKIT_TEST_POSTGRES_URL")
    if not url:
        pytest.skip("set DOMAINKIT_TEST_POSTGRES_URL")

    class Work(SqlAlchemyUnitOfWork):
        database = "default"

    with override_settings(
        DATABASES={"default": {"URL": url}},
        TENANCY={"SCHEMA_DATABASES": ["default"]},
    ):
        try:
            async with connections.engine("default").begin() as connection:
                await connection.execute(
                    text('CREATE SCHEMA IF NOT EXISTS "tenant_acme"')
                )
            with tenant("acme"):
                async with Work(reads=reads) as uow:
                    first = await uow.session.scalar(text("SELECT current_schema()"))
                    second = await uow.session.scalar(text("SELECT current_schema()"))
        finally:
            await connections.close_all()

    assert (first, second) == ("tenant_acme", "tenant_acme")


class NotesWork(UnitOfWork):
    """A unit of work over ``Notes``, committing nothing anywhere."""

    def __init__(self, notes: Notes) -> None:
        """Hold ``notes``."""
        super().__init__(events=EventBus())
        self.notes = notes

    async def _commit(self) -> None:
        """Nothing to commit."""

    async def _rollback(self) -> None:
        """Nothing to roll back."""


def test_a_write_never_names_the_tenant() -> None:
    """Repositories set a row's tenant; no schema takes it, no router lets it.

    The audit found a generic PATCH moving a row into another tenant
    (A5-3).
    """
    notes = Notes()

    assert "tenant_id" not in schema_for(Note, write=True).model_fields
    assert "tenant_id" not in schema_for(Note, partial=True).model_fields
    assert "tenant_id" in schema_for(Note).model_fields
    with pytest.raises(TypeError, match="tenant_id"):
        ResourceRouter(
            "/notes",
            model=Note,
            unit_of_work=lambda: NotesWork(notes),
            repository=lambda uow: uow.notes,
            crud=True,
            writable=["text", "tenant_id"],
            rule=AllowAny(),
        )


def test_a_patch_cannot_move_a_row_to_another_tenant() -> None:
    """Asked over HTTP, the change is refused and the row stays where it was."""
    notes = Notes()
    mine = note("mine", "acme")
    with all_tenants():
        asyncio.run(notes.add(mine))
    router = ResourceRouter(
        "/notes",
        model=Note,
        unit_of_work=lambda: NotesWork(notes),
        repository=lambda uow: uow.notes,
        crud=True,
        writable=ALL_FIELDS,
        rule=AllowAny(),
    )
    app = FastAPI()
    install_exception_handlers(app)
    app.include_router(router)

    @app.middleware("http")
    async def anyone(request: Any, call_next: Any) -> Any:  # noqa: ANN401
        request.scope["user"] = user(is_superuser=True)
        return await call_next(request)

    with override_settings(TENANCY={"HEADER": "X-Tenant"}):
        app.add_middleware(TenantMiddleware)
        client = TestClient(app)
        moved = client.patch(
            f"/notes/{mine.id}",
            json={"tenant_id": "globex", "text": "planted"},
            headers={"X-Tenant": "acme"},
        )
        theirs = client.get(f"/notes/{mine.id}", headers={"X-Tenant": "globex"})
        still = client.get(f"/notes/{mine.id}", headers={"X-Tenant": "acme"})

    assert moved.status_code == 422
    assert theirs.status_code == 404
    assert still.json()["text"] == "mine"
