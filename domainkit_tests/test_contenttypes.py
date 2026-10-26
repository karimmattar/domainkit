"""Content types: which classes count, their options, and their rows."""

import asyncio
import io
import secrets
import threading
from collections.abc import Callable, Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import Column, Integer, MetaData, Table, select
from sqlalchemy.ext.asyncio import create_async_engine

from domainkit import setup
from domainkit.apps import registry
from domainkit.conf import override_settings
from domainkit.contrib.auth.models import Group, User
from domainkit.contrib.contenttypes import (
    DEFAULT_PERMISSIONS,
    ContentType,
    InstalledModel,
    content_type_key,
    installed_models,
    model_options,
)
from domainkit.contrib.contenttypes.sync import has_tables
from domainkit.contrib.contenttypes.tables import content_types
from domainkit.db import connections, mapper_registry, metadata
from domainkit.db import models as content_registry
from domainkit.db.migrations import include_object
from domainkit.db.models import _owner
from domainkit.db.routing import router
from domainkit.exceptions import ImproperlyConfigured
from domainkit.management import call_command
from domainkit_tests.conftest import ALPHA, CONTENTTYPES, UseAuth
from domainkit_tests.sample_domains.shop.models import Product, StockLevel
from domainkit_tests.test_db import Note


class InvoiceLine:
    """No ``Meta``: every default applies."""


def test_model_options_follow_meta() -> None:
    """Defaults, then whatever ``Meta`` declares."""
    plain = model_options(InvoiceLine)
    assert (plain.model, plain.verbose_name) == ("invoiceline", "invoice line")
    assert plain.default_permissions == DEFAULT_PERMISSIONS
    assert plain.permissions == ()

    stock = model_options(StockLevel)
    assert (stock.model, stock.verbose_name) == ("stocklevel", "stock")
    assert stock.default_permissions == ("change", "view")
    assert model_options(Product).permissions == (
        ("publish_product", "Can publish product"),
    )


def test_installed_models_are_the_mapped_classes_of_installed_domains(
    auth_project: str,
) -> None:
    """Project tables of installed domains count; others do not."""
    keys = [model.key for model in installed_models()]

    assert keys == sorted(keys)
    assert {
        ("auth", "group"),
        ("auth", "permission"),
        ("auth", "user"),
        ("contenttypes", "contenttype"),
        ("shop", "product"),
        ("shop", "stocklevel"),
    } <= set(keys)
    assert all(domain != "framework" for domain, _ in keys)
    assert content_type_key(Product) == ("shop", "product")
    with pytest.raises(LookupError, match="Note"):
        content_type_key(Note)


def test_untagged_tables_belong_to_the_defining_domain(
    use_auth: UseAuth, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without ``info["domain"]``, the class's package decides."""
    use_auth()
    untagged = Table("untagged", MetaData())
    alpha = SimpleNamespace(__module__=f"{ALPHA}.models")
    nowhere = SimpleNamespace(__module__="elsewhere.models")
    tagged = Table("tagged", MetaData(), info={"domain": "ghost"})

    owner = _owner(untagged, alpha)  # type: ignore[arg-type]
    assert owner is not None
    assert owner.name == ALPHA
    assert _owner(untagged, nowhere) is None  # type: ignore[arg-type]
    assert _owner(tagged, alpha) is None  # type: ignore[arg-type]
    # The most specific package wins, whatever the installation order.
    outer = SimpleNamespace(name="pkg")
    inner: Any = SimpleNamespace(name="pkg.inner")
    nested = SimpleNamespace(__module__="pkg.inner.models")
    elsewhere = SimpleNamespace(__module__="pkgx.models")
    for installed in ([outer, inner], [inner, outer]):
        monkeypatch.setattr(content_registry, "registry", installed)
        assert _owner(untagged, nested) is inner  # type: ignore[arg-type]
        assert _owner(untagged, elsewhere) is None  # type: ignore[arg-type]
    assert router.holds_table(untagged, "default")
    assert not router.holds_table(tagged, "default")


def test_content_types_without_auth(database_url: str, tmp_path: Path) -> None:
    """The app works alone: ``migrate`` creates its own rows."""
    registry.clear()
    with override_settings(
        INSTALLED_DOMAINS=[CONTENTTYPES, ALPHA],
        DATABASES={"default": {"URL": database_url}},
        MIGRATIONS_DIR=tmp_path / "migrations",
    ):
        setup()
        call_command("makemigrations", "-m", "types", stdout=io.StringIO())
        call_command("migrate", stdout=io.StringIO())
        call_command("migrate", stdout=io.StringIO())
        assert [model.key for model in installed_models()] == [
            ("contenttypes", "contenttype")
        ]

        async def stored() -> list[tuple[str, str]]:
            try:
                async with connections.engine().connect() as connection:
                    rows = await connection.execute(
                        select(content_types.c.domain, content_types.c.model)
                    )
                    return [tuple(row) for row in rows]
            finally:
                await connections.close_all()

        assert asyncio.run(stored()) == [("contenttypes", "contenttype")]
    registry.clear()


def test_installed_models_skip_foreign_and_unmapped_tables(
    use_auth: UseAuth, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only project tables count; untagged ones go by the class's package."""
    use_auth()
    project = MetaData()
    tagged = Table("tagged", project, info={"domain": "alpha"})
    untagged = Table("untagged", project)
    foreign = Table("foreign", MetaData(), info={"domain": "alpha"})

    def mapped(name: str, table: object) -> SimpleNamespace:
        cls = type(name, (), {"__module__": f"{ALPHA}.models"})
        return SimpleNamespace(class_=cls, local_table=table)

    mappers = [
        mapped("Tagged", tagged),
        mapped("Untagged", untagged),
        mapped("Foreign", foreign),
        mapped("Joined", object()),  # Mapped onto a join, say.
    ]
    monkeypatch.setattr(content_registry, "metadata", project)
    monkeypatch.setattr(
        content_registry, "mapper_registry", SimpleNamespace(mappers=mappers)
    )

    assert [model.key for model in installed_models()] == [
        ("alpha", "tagged"),
        ("alpha", "untagged"),
    ]


def test_has_tables_looks_in_each_tables_schema() -> None:
    """A table in another schema is found there, not in the default one."""

    async def check() -> tuple[bool, bool]:
        engine = create_async_engine("sqlite+aiosqlite://")
        try:
            async with engine.connect() as connection:
                await connection.exec_driver_sql("ATTACH DATABASE ':memory:' AS extra")
                await connection.exec_driver_sql("CREATE TABLE extra.things (id int)")
                elsewhere = Table("things", MetaData(), schema="extra")
                here = Table("things", MetaData())
                return (
                    await has_tables(connection, elsewhere),
                    await has_tables(connection, here),
                )
        finally:
            await engine.dispose()

    assert asyncio.run(check()) == (True, False)


def test_content_types_need_one_database(use_auth: UseAuth, tmp_path: Path) -> None:
    """Content types cannot live in a shard group."""
    databases = {
        "default": {"URL": f"sqlite+aiosqlite:///{tmp_path / 'a.sqlite3'}"},
        "s1": {"URL": f"sqlite+aiosqlite:///{tmp_path / 'b.sqlite3'}"},
        "s2": {"URL": f"sqlite+aiosqlite:///{tmp_path / 'c.sqlite3'}"},
    }
    with pytest.raises(ImproperlyConfigured, match="contenttypes"):
        use_auth(
            settings={
                "DATABASES": databases,
                "SHARDS": {"tenants": {"DATABASES": ["s1", "s2"]}},
                "DATABASE_ROUTING": {"contenttypes": "tenants", "auth": "tenants"},
            }
        )


def test_migrations_ignore_unknown_reflected_tables(use_auth: UseAuth) -> None:
    """A table only the database knows belongs to ``default``."""
    use_auth()
    assert include_object(None, "stray", "table", True, None, target="default")
    assert not include_object(None, "stray", "table", True, None, target="other")
    reflected = "domainkit_content_types"
    assert include_object(None, reflected, "table", True, None, target="default")
    # Columns and indexes follow their table; partitions are left alone.
    assert include_object(None, "ix_stray", "index", True, None, target="other")
    partition = "audit_records_2026_10"
    assert not include_object(
        None, partition, "table", True, None, partitions=frozenset({partition})
    )


class Counted:
    """``installed_models``, counting its calls."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Wrap the registry module's ``installed_models``."""
        self.calls = 0
        monkeypatch.setattr(content_registry, "installed_models", self)

    def __call__(self) -> list[InstalledModel]:
        """Count, then list the installed models."""
        self.calls += 1
        return installed_models()


type MakeTable = Callable[[], Table]


@pytest.fixture
def alpha_table() -> Iterator[MakeTable]:
    """Make tables of the alpha domain in the project's metadata; removed after."""
    made: list[Table] = []

    def make() -> Table:
        name = f"late_{secrets.token_hex(4)}"
        made.append(
            Table(
                name,
                metadata,
                Column("id", Integer, primary_key=True),
                info={"domain": "alpha"},
            )
        )
        return made[-1]

    yield make
    for table in made:
        if metadata.tables.get(table.key) is table:
            metadata.remove(table)


def late_class(table: Table) -> type:
    """A new class, mapped onto ``table``."""
    name = f"Late{table.name.removeprefix('late_')}"
    cls = type(name, (), {"__module__": f"{ALPHA}.models"})
    mapper_registry.map_imperatively(cls, table)
    return cls


def test_content_type_keys_are_worked_out_once(
    use_auth: UseAuth, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every lookup after the first is one dictionary lookup."""
    use_auth()
    counted = Counted(monkeypatch)

    keys = [content_type_key(cls) for cls in (User, Group, ContentType) * 3]

    assert (
        keys
        == [("auth", "user"), ("auth", "group"), ("contenttypes", "contenttype")] * 3
    )
    assert counted.calls == 1
    monkeypatch.setattr(content_registry, "_lock", None)  # A lookup never takes it.
    with pytest.raises(LookupError, match="Note"):
        content_type_key(Note)
    assert content_type_key(User) == ("auth", "user")
    assert counted.calls == 1


def test_content_type_keys_follow_the_installed_domains(
    use_auth: UseAuth, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``registry.clear`` and ``populate`` make them work the keys out again."""
    use_auth()
    assert content_type_key(User) == ("auth", "user")
    counted = Counted(monkeypatch)

    registry.clear()
    with pytest.raises(LookupError, match="User"):
        content_type_key(User)
    use_auth()

    assert content_type_key(User) == ("auth", "user")
    assert counted.calls == 2


def test_a_class_mapped_after_a_lookup_has_its_key(
    use_auth: UseAuth, alpha_table: MakeTable
) -> None:
    """Mapping a class makes the keys be worked out again."""
    use_auth()
    table = alpha_table()
    assert content_type_key(User) == ("auth", "user")

    late = late_class(table)
    assert content_type_key(late) == ("alpha", late.__name__.lower())


def test_a_table_removed_or_replaced_takes_its_key_away(
    use_auth: UseAuth, alpha_table: MakeTable
) -> None:
    """A table gone from the project's metadata has no content type any more."""
    use_auth()
    removed, replaced = (late_class(alpha_table()) for _ in range(2))
    assert content_type_key(removed) == ("alpha", removed.__name__.lower())

    metadata.remove(removed.__table__)  # type: ignore[attr-defined]
    with pytest.raises(LookupError, match="Late"):
        content_type_key(removed)
    assert content_type_key(replaced) == ("alpha", replaced.__name__.lower())
    metadata.remove(replaced.__table__)  # type: ignore[attr-defined]
    alpha_table()  # As many tables as before.
    with pytest.raises(LookupError, match="Late"):
        content_type_key(replaced)


def test_keys_another_thread_worked_out_meanwhile_are_used(
    use_auth: UseAuth, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A lookup that waited for the lock finds the keys done, and keeps them."""
    use_auth()
    done = (content_registry._state(), {User: ("auth", "user")})
    lock = threading.Lock()

    class Waited:
        """The lock, held meanwhile by a thread that worked the keys out."""

        def __enter__(self) -> None:
            lock.acquire()
            monkeypatch.setattr(content_registry, "_known", done)

        def __exit__(self, *exc: object) -> None:
            lock.release()

    monkeypatch.setattr(content_registry, "_known", (None, {}))
    monkeypatch.setattr(content_registry, "_lock", Waited())
    counted = Counted(monkeypatch)

    assert content_type_key(User) == ("auth", "user")
    assert counted.calls == 0
    assert content_registry._known is done
