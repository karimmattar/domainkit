"""Object permissions: grants on single objects, their ceilings, and ``VisibleTo``."""

import io
from collections.abc import Awaitable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final, NewType
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Column, Integer, MetaData, String, Table, select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from domainkit import setup
from domainkit.apps import registry
from domainkit.conf import override_settings
from domainkit.contrib.auth import AbstractBaseUser, AnonymousUser, AnyUser, Group
from domainkit.contrib.auth.backends import has_perm
from domainkit.contrib.auth.exceptions import UnknownPermission
from domainkit.contrib.auth.uow import AuthUnitOfWork
from domainkit.contrib.objectpermissions import (
    InMemoryObjectPermissions,
    InvalidGrant,
    ObjectPermission,
    ObjectPermissionBackend,
    ObjectPermissionGranted,
    ObjectPermissionRevoked,
    VisibleTo,
    grant_permission,
    grants_on,
    revoke_permission,
    visible_scope,
    visible_to,
)
from domainkit.contrib.objectpermissions.criteria import compile_visible
from domainkit.contrib.objectpermissions.models import object_column
from domainkit.contrib.objectpermissions.tables import object_permissions
from domainkit.db import SqlAlchemyRepository, SqlAlchemyUnitOfWork, map_imperatively
from domainkit.ddd import (
    AggregateRoot,
    DomainEvent,
    InMemoryRepository,
    PermissionDenied,
    event_bus,
)
from domainkit.ddd.fields import InvalidQuery
from domainkit.ddd.query import F
from domainkit.management import call_command
from domainkit.management.base import run_async
from domainkit_tests.conftest import ALPHA, AUTH, CONTENTTYPES, FAST_HASHER, SHOP
from domainkit_tests.sample_domains.shop.models import Product, ProductId
from domainkit_tests.test_auth_admin import join, make_group
from domainkit_tests.test_auth_http import create_user, grant

OBJECTPERMISSIONS: Final = "domainkit.contrib.objectpermissions"
BACKENDS: Final = [
    "domainkit.contrib.auth.backends.ModelBackend",
    "domainkit.contrib.objectpermissions.ObjectPermissionBackend",
]
MANAGE: Final = ("objectpermissions.add_objectpermission",)
CHANGE: Final = "shop.change_product"
VIEW: Final = "shop.view_product"


class Products(SqlAlchemyRepository[Product, ProductId]):
    """The shop's products."""

    model = Product


class ShopUnitOfWork(SqlAlchemyUnitOfWork):
    """One transaction on the products."""

    products: Products

    def create_repositories(self, session: AsyncSession) -> None:
        """Bind the repository."""
        self.products = Products(session)


@pytest.fixture
def project(database_url: str, tmp_path: Path) -> Iterator[str]:
    """Auth, object permissions, and the shop, migrated; the backends set."""
    registry.clear()
    with override_settings(
        INSTALLED_DOMAINS=[CONTENTTYPES, AUTH, OBJECTPERMISSIONS, SHOP, ALPHA],
        DATABASES={"default": {"URL": database_url}},
        MIGRATIONS_DIR=tmp_path / "migrations",
        AUTH={"PASSWORD_HASHERS": [FAST_HASHER], "BACKENDS": BACKENDS},
    ):
        setup()
        call_command("makemigrations", "-m", "init", stdout=io.StringIO())
        call_command("migrate", stdout=io.StringIO())
        yield database_url
    registry.clear()


def products(*names: str) -> list[Product]:
    """Store products named ``names``; return them."""

    async def store() -> list[Product]:
        made = [Product(id=ProductId(uuid4()), name=name) for name in names]
        async with ShopUnitOfWork() as uow:
            for product in made:
                await uow.products.add(product)
            await uow.commit()
        return made

    return run_async(store())


def person(username: str, *keys: str, **fields: Any) -> AbstractBaseUser:  # noqa: ANN401 - user fields.
    """Store a user granted ``keys``; return them as loaded for a request."""
    create_user(username, **fields)
    if keys:
        grant(username, *keys)
    return reload(username)


def reload(username: str) -> AbstractBaseUser:
    """The user as stored now, groups and permissions loaded."""

    async def load() -> AbstractBaseUser:
        async with AuthUnitOfWork() as uow:
            found = await uow.users.get_by_natural_key(username)
        assert found is not None
        return found

    return run_async(load())


def run[T](awaitable: Awaitable[T]) -> T:
    """Await in a loop of its own."""

    async def wait() -> T:
        return await awaitable

    return run_async(wait())


def test_grants_let_one_user_or_group_act_on_one_object(project: str) -> None:
    """A grant is about one object; groups pass it on; revoking takes it back."""
    actor = person(
        "actor", CHANGE, *MANAGE, "objectpermissions.delete_objectpermission"
    )
    ada = person("ada")
    make_group("editors")
    person("bob")
    join("bob", "editors")
    bob = reload("bob")
    editors = next(iter(bob.groups)).id
    first, second = products("first", "second")
    seen: list[DomainEvent] = []

    async def collect(event: DomainEvent) -> None:
        seen.append(event)

    event_bus.subscribe(ObjectPermissionGranted, collect)
    event_bus.subscribe(ObjectPermissionRevoked, collect)
    try:
        made = run(grant_permission(actor, CHANGE, first, user=ada.id))
        again = run(grant_permission(actor, CHANGE, first, user=ada.id))
        run(grant_permission(actor, CHANGE, first, group=editors))
        allowed = [
            run(has_perm(ada, CHANGE, first)),
            run(has_perm(ada, CHANGE, second)),
            run(has_perm(ada, CHANGE)),
            run(has_perm(bob, CHANGE, first)),
            run(has_perm(ada, VIEW, first)),
        ]
        listed = run(grants_on(first))
        removed = run(revoke_permission(actor, CHANGE, first, user=ada.id))
        after = run(has_perm(ada, CHANGE, first))
    finally:
        event_bus.unsubscribe(ObjectPermissionGranted, collect)
        event_bus.unsubscribe(ObjectPermissionRevoked, collect)

    assert again.id == made.id
    assert (made.content_type, made.object_id, made.granted_by) == (
        "shop.product",
        first.id,
        actor.id,
    )
    assert allowed == [True, False, False, True, False]
    assert sorted(str(each.user_id or each.group_id) for each in listed) == sorted(
        [str(ada.id), str(editors)]
    )
    assert (removed, after) == (1, False)
    assert [type(event) for event in seen] == [
        ObjectPermissionGranted,
        ObjectPermissionGranted,
        ObjectPermissionRevoked,
    ]
    granted = seen[0]
    assert isinstance(granted, ObjectPermissionGranted)
    assert (granted.object_id, granted.user_id, granted.by) == (
        str(first.id),
        ada.id,
        actor.id,
    )


def test_granters_hand_out_only_what_they_hold(project: str) -> None:
    """The ceilings: managing grants, holding the permission, and not oneself."""
    (product,) = products("one")
    manager = person("manager", *MANAGE)
    holder = person("holder", CHANGE)
    root = person("root", is_superuser=True)
    ada = person("ada")

    def refused(actor: AnyUser, **whom: Any) -> str:  # noqa: ANN401 - user or group.
        with pytest.raises(PermissionDenied) as caught:
            run(grant_permission(actor, CHANGE, product, **whom))
        return str(caught.value)

    assert refused(holder, user=ada.id) == "You may not manage permissions on objects."
    assert refused(manager, user=ada.id) == (
        "You may grant or revoke only permissions you hold on the object."
    )
    assert refused(AnonymousUser(), user=ada.id).startswith("You may not manage")
    run(grant_permission(root, CHANGE, product, user=manager.id))
    run(grant_permission(manager, CHANGE, product, user=ada.id))
    assert run(has_perm(ada, CHANGE, product))
    assert refused(manager, user=manager.id) == (
        "You cannot grant permissions to yourself."
    )
    remover = person("remover", "objectpermissions.delete_objectpermission")
    with pytest.raises(PermissionDenied, match="only permissions you hold"):
        run(revoke_permission(remover, CHANGE, product, user=ada.id))
    with pytest.raises(PermissionDenied, match="may not manage"):
        run(revoke_permission(holder, CHANGE, product, user=ada.id))
    with pytest.raises(InvalidGrant, match="not both or neither"):
        run(grant_permission(root, CHANGE, product))
    with pytest.raises(InvalidGrant, match="not both or neither"):
        run(grant_permission(root, CHANGE, product, user=ada.id, group=uuid4()))
    with pytest.raises(InvalidGrant, match="not one of the object's model"):
        run(grant_permission(root, "shop.change_stocklevel", product, user=ada.id))
    with pytest.raises(UnknownPermission):
        run(grant_permission(root, "shop.fly_product", product, user=ada.id))


def test_visible_to_filters_lists_in_the_database(project: str) -> None:
    """Granted objects only: in pages and counts; everything with the model's."""
    first, second, third = products("first", "second", "third")
    root = person("root", is_superuser=True)
    ada = person("ada")
    make_group("readers")
    person("bob")
    join("bob", "readers")
    bob = reload("bob")
    carol = person("carol", VIEW)
    readers = next(iter(bob.groups)).id
    run(grant_permission(root, VIEW, first, user=ada.id))
    run(grant_permission(root, VIEW, second, group=readers))
    run(grant_permission(root, CHANGE, third, user=ada.id))

    def names(user: AnyUser) -> tuple[list[str], int]:
        async def query() -> tuple[list[str], int]:
            criteria = await visible_to(user, "view", Product)
            async with ShopUnitOfWork() as uow:
                found = await uow.products.filter(criteria)
                page = await uow.products.paginate(criteria, limit=10, count=True)
            assert page.total == len(found)
            return sorted(each.name for each in found), len(page.items)

        return run_async(query())

    assert names(ada) == (["first"], 1)
    assert names(bob) == (["second"], 1)
    assert names(carol) == (["first", "second", "third"], 3)
    assert names(AnonymousUser()) == ([], 0)
    assert run(visible_to(carol, "view", Product)) is None


def test_the_backend_answers_only_about_objects_of_active_users(project: str) -> None:
    """No object, an anonymous or inactive user, or an odd object: nothing."""
    (product,) = products("one")
    root = person("root", is_superuser=True)
    ada = person("ada")
    run(grant_permission(root, CHANGE, product, user=ada.id))
    backend = ObjectPermissionBackend()
    ada.is_active = False

    assert run(backend.get_all_permissions(ada, product)) == frozenset()
    assert run(backend.get_all_permissions(reload("ada"), product)) == {CHANGE}
    assert run(backend.get_all_permissions(reload("ada"))) == frozenset()
    assert run(backend.get_all_permissions(AnonymousUser(), product)) == frozenset()
    assert run(backend.get_all_permissions(reload("ada"), object())) == frozenset()


# In memory -------------------------------------------------------------------------


def grant_of(permission: str, product: Product, **whom: Any) -> ObjectPermission:  # noqa: ANN401 - user or group.
    """A grant on ``product``, not stored."""
    return ObjectPermission.create(
        content_type="shop.product",
        permission=permission,
        object_id=product.id,
        user_id=whom.get("user"),
        group_id=whom.get("group"),
        by=None,
    )


def test_visible_to_reads_grants_in_memory(use_auth: Any) -> None:  # noqa: ANN401 - the fixture.
    """With ``grants=``, in-memory repositories filter as SQL does."""
    use_auth(
        settings={
            "INSTALLED_DOMAINS": [CONTENTTYPES, AUTH, OBJECTPERMISSIONS, SHOP, ALPHA]
        }
    )
    first, second, third = (
        Product(id=ProductId(uuid4()), name=name) for name in ("a", "b", "c")
    )
    store = InMemoryRepository[Product, ProductId]([first, second, third])
    ada, group = uuid4(), uuid4()
    grants = InMemoryObjectPermissions(
        [
            grant_of(VIEW, first, user=ada),
            grant_of(VIEW, second, group=group),
            grant_of(CHANGE, third, user=ada),
        ]
    )

    def names(criteria: Any) -> list[str]:  # noqa: ANN401 - criteria.
        return sorted(each.name for each in run(store.filter(criteria)))

    assert names(VisibleTo("view", ada, grants=grants)) == ["a"]
    assert names(VisibleTo(VIEW, uuid4(), frozenset({group}), grants=grants)) == ["b"]
    assert names(VisibleTo("view", None, grants=grants)) == []
    with pytest.raises(InvalidQuery, match="against the grants it is given"):
        names(VisibleTo("view", ada))
    with pytest.raises(InvalidQuery, match=r"not a permission of shop\.product"):
        names(VisibleTo("auth.view_user", ada, grants=grants))
    with pytest.raises(InvalidQuery, match="Bind the criteria"):
        VisibleTo("view", ada, grants=grants).evaluate(first)
    with pytest.raises(InvalidQuery, match="not mapped in an installed domain"):
        VisibleTo("view", ada).bind(dict)
    assert list(VisibleTo("view", ada).conditions()) == []
    assert names((F("name") == "a") & VisibleTo("view", ada, grants=grants)) == ["a"]


def test_scopes_narrow_resources_to_what_each_caller_may_see(use_auth: Any) -> None:  # noqa: ANN401 - the fixture.
    """``visible_scope`` reads the caller; holders of the model's see everything."""
    from starlette.requests import Request

    from domainkit_tests.test_auth_access import user

    use_auth(
        settings={
            "INSTALLED_DOMAINS": [CONTENTTYPES, AUTH, OBJECTPERMISSIONS, SHOP, ALPHA]
        }
    )
    product = Product(id=ProductId(uuid4()), name="a")
    grants = InMemoryObjectPermissions()
    scope = visible_scope("view", Product, grants=grants)
    viewer = user("shop.view_product")
    other = user()
    member = Group.create("members")
    other.groups.add(member)

    def scoped(who: AnyUser) -> Any:  # noqa: ANN401 - criteria.
        request = Request({"type": "http", "user": who, "headers": []})
        return run(scope(request))

    found = scoped(other)

    assert scoped(viewer) is None
    assert isinstance(found, VisibleTo)
    assert (found.user_id, found.group_ids, found.permission) == (
        other.id,
        frozenset({member.id}),
        VIEW,
    )
    assert scoped(AnonymousUser()).user_id is None
    assert not found.matches(product)


def test_grants_name_one_column_for_their_ids() -> None:
    """UUIDs, integers, and text each have a column; other ids are refused."""
    product = Product(id=ProductId(uuid4()), name="a")
    by_number = ObjectPermission.create(
        content_type="x.y",
        permission="x.v",
        object_id=7,
        user_id=uuid4(),
        group_id=None,
        by=None,
    )
    by_text = ObjectPermission.create(
        content_type="x.y",
        permission="x.v",
        object_id="k-1",
        user_id=None,
        group_id=uuid4(),
        by=None,
    )

    assert [object_column(kind) for kind in (UUID, int, str)] == [
        "object_uuid",
        "object_int",
        "object_text",
    ]
    assert (by_number.object_int, by_number.object_id) == (7, 7)
    assert (by_text.object_text, by_text.object_id) == ("k-1", "k-1")
    assert grant_of(VIEW, product, user=uuid4()).object_id == product.id
    for kind in (bool, float):
        with pytest.raises(TypeError, match="UUID, integer, or text ids"):
            object_column(kind)
    with pytest.raises(ValueError, match="for a user or for a group"):
        ObjectPermission.create(
            content_type="x.y",
            permission="x.v",
            object_id=1,
            user_id=None,
            group_id=None,
            by=None,
        )


Number = NewType("Number", int)


class Counter(AggregateRoot[Number]):
    """An aggregate with integer ids."""

    name: str


class Keyed(AggregateRoot[str]):
    """An aggregate with text ids."""

    name: str


ids_metadata = MetaData()
counters = Table(
    "framework_test_counters",
    ids_metadata,
    Column("id", Integer, primary_key=True, autoincrement=False),
    Column("name", String(10), nullable=False),
    Column("version", Integer, nullable=False),
)
keyed = Table(
    "framework_test_keyed",
    ids_metadata,
    Column("id", String(10), primary_key=True),
    Column("name", String(10), nullable=False),
    Column("version", Integer, nullable=False),
)
map_imperatively(Counter, counters)
map_imperatively(Keyed, keyed)


@pytest.mark.anyio
async def test_sql_compares_ids_in_their_own_column(tmp_path: Path) -> None:
    """Integer and text ids meet the grants' integer and text columns."""
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'ids.db'}")
    ada = uuid4()
    async with engine.begin() as connection:
        await connection.run_sync(ids_metadata.create_all)
        await connection.run_sync(object_permissions.create)
        await connection.execute(
            counters.insert(), [{"id": n, "name": str(n), "version": 1} for n in (1, 2)]
        )
        await connection.execute(
            keyed.insert(), [{"id": k, "name": k, "version": 1} for k in ("a", "b")]
        )
        for column, value in (("object_int", 2), ("object_text", "b")):
            await connection.execute(
                object_permissions.insert(),
                {
                    "id": uuid4(),
                    "version": 1,
                    "content_type": "x.y",
                    "permission": "x.view_y",
                    column: value,
                    "user_id": ada,
                    "granted_at": datetime.now(UTC),
                },
            )

        def criteria(user: UUID | None) -> VisibleTo:
            return VisibleTo("x.view_y", user, content_type="x.y")

        found = [
            (await connection.execute(select(table.c.id).where(condition)))
            .scalars()
            .all()
            for table, model, condition in (
                (counters, Counter, compile_visible(criteria(ada), Counter, "sqlite")),
                (keyed, Keyed, compile_visible(criteria(ada), Keyed, "sqlite")),
                (keyed, Keyed, compile_visible(criteria(None), Keyed, "sqlite")),
            )
        ]
    await engine.dispose()

    assert found == [[2], ["b"], []]
