"""Content types: which classes count, their options, and their rows."""

import asyncio
import io
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import MetaData, Table, select
from sqlalchemy.ext.asyncio import create_async_engine

from domainkit import setup
from domainkit.apps import registry
from domainkit.conf import override_settings
from domainkit.contrib.contenttypes import (
    DEFAULT_PERMISSIONS,
    content_type_key,
    installed_models,
    model_options,
)
from domainkit.contrib.contenttypes import registry as content_registry
from domainkit.contrib.contenttypes.registry import _owner
from domainkit.contrib.contenttypes.sync import has_tables
from domainkit.contrib.contenttypes.tables import content_types
from domainkit.db import connections
from domainkit.db.migrations import include_object
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
