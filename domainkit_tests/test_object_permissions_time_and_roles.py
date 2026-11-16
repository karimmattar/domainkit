"""Grants on objects that expire, roles, and permissions of other models (ADR 0151).

User report D2: ``contrib.objectpermissions`` had no expiry and no roles,
and refused a permission of another model on an object (``add_membership``
on a ``Company``). Here the shop's ``Product`` stands for the company and
``shop.change_stocklevel`` for the membership: a permission of the stock
levels, granted on one product.
"""

import io
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Final
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from domainkit import setup
from domainkit.apps import registry
from domainkit.clock import freeze_time
from domainkit.conf import override_settings
from domainkit.contrib.auth import AnyUser
from domainkit.contrib.auth.backends import ObjectGrant, has_perm, object_grants
from domainkit.contrib.auth.exceptions import UnknownPermission
from domainkit.contrib.objectpermissions import (
    InMemoryObjectPermissions,
    InvalidGrant,
    ObjectPermission,
    ObjectPermissionBackend,
    ObjectPermissionGranted,
    ObjectPermissionRevoked,
    ObjectRole,
    VisibleTo,
    clear_expired_grants,
    grant_permission,
    grant_role,
    grantable_on,
    grants_on,
    revoke_permission,
    revoke_role,
    visible_to,
)
from domainkit.contrib.objectpermissions.tables import object_permissions
from domainkit.db import connections
from domainkit.ddd import DomainEvent, InMemoryRepository, PermissionDenied, event_bus
from domainkit.ddd.fields import InvalidQuery
from domainkit.exceptions import ImproperlyConfigured
from domainkit.management import call_command
from domainkit.management.base import run_async
from domainkit_tests.conftest import (
    ALPHA,
    AUTH,
    CONTENTTYPES,
    FAST_HASHER,
    SHOP,
    UseAuth,
)
from domainkit_tests.sample_domains.shop.models import Product, ProductId, StockLevel
from domainkit_tests.test_auth_admin import join, make_group
from domainkit_tests.test_object_permissions import (
    BACKENDS,
    CHANGE,
    MANAGE,
    OBJECTPERMISSIONS,
    VIEW,
    ShopUnitOfWork,
    person,
    products,
    project,
    reload,
    run,
)

__all__ = ["project"]  # A fixture used here.

FRIDAY: Final = datetime(2026, 11, 20, 17, 0, tzinfo=UTC)
STOCK: Final = "shop.change_stocklevel"
EDITOR: Final = ObjectRole("editor", [VIEW, CHANGE])
VIEWER: Final = ObjectRole("viewer", [VIEW])


def names(user: AnyUser, permission: str = "view") -> tuple[list[str], int]:
    """The products ``visible_to`` lets ``user`` see, in SQL; and the count."""

    async def query() -> tuple[list[str], int]:
        criteria = await visible_to(user, permission, Product)
        async with ShopUnitOfWork() as uow:
            found = await uow.products.filter(criteria)
            page = await uow.products.paginate(criteria, limit=10, count=True)
        return sorted(each.name for each in found), page.total or 0

    return run_async(query())


def rows() -> int:
    """How many grants the table holds, expired or not."""

    async def count() -> int:
        async with connections.engine("default").connect() as connection:
            found = await connection.scalar(
                select(func.count()).select_from(object_permissions)
            )
        return int(found or 0)

    return run_async(count())


def recording() -> tuple[list[DomainEvent], Any]:
    """Collect grant events; return them and the function that stops it."""
    seen: list[DomainEvent] = []

    async def collect(event: DomainEvent) -> None:
        seen.append(event)

    event_bus.subscribe(ObjectPermissionGranted, collect)
    event_bus.subscribe(ObjectPermissionRevoked, collect)

    def stop() -> None:
        event_bus.unsubscribe(ObjectPermissionGranted, collect)
        event_bus.unsubscribe(ObjectPermissionRevoked, collect)

    return seen, stop


# Expiry ------------------------------------------------------------------------------


def test_an_expired_grant_never_answers_yes(project: str) -> None:
    """From its ``expires_at``, a grant counts nowhere; ``cleargrants`` deletes it.

    The backend, ``visible_to`` in SQL (pages and counts), the admin API's
    ``object_grants``, and ``grants_on`` all read the clock.
    """
    first, second = products("first", "second")
    root = person("root", is_superuser=True)
    make_group("readers")
    person("bob")
    join("bob", "readers")
    bob = reload("bob")
    readers = next(iter(bob.groups)).id
    ada = person("ada")

    def state() -> list[Any]:
        return [
            run(has_perm(ada, VIEW, first)),
            run(has_perm(bob, VIEW, second)),
            run(has_perm(ada, CHANGE, second)),
            names(ada),
            names(bob),
            run(object_grants(ada)),
            len(run(grants_on(first))),
        ]

    with freeze_time(FRIDAY - timedelta(hours=1)) as clock:
        run(grant_permission(root, VIEW, first, user=ada.id, expires_at=FRIDAY))
        run(grant_permission(root, VIEW, second, group=readers, expires_at=FRIDAY))
        run(grant_permission(root, CHANGE, second, user=ada.id))
        before = state()
        clock.move_to(FRIDAY - timedelta(microseconds=1))
        last_moment = state()
        clock.move_to(FRIDAY)
        after = state()
        kept = rows()
        output = io.StringIO()
        call_command("cleargrants", stdout=output)
        again = run(clear_expired_grants())

    assert before == last_moment
    assert before == [
        True,
        True,
        True,
        (["first"], 1),
        (["second"], 1),
        {
            ObjectGrant(VIEW, "shop.product", first.id),
            ObjectGrant(CHANGE, "shop.product", second.id),
        },
        1,
    ]
    assert after == [
        False,
        False,
        True,
        ([], 0),
        ([], 0),
        {ObjectGrant(CHANGE, "shop.product", second.id)},
        0,
    ]
    assert kept == 3
    assert output.getvalue() == "Deleted 2 expired grant(s).\n"
    assert (again, rows()) == (0, 1)


def test_granting_again_sets_the_expiry_asked_for(project: str) -> None:
    """The same expiry changes nothing; another renews the grant, as an event.

    A grant past its expiry but not yet cleared comes back, under its id.
    An expiry in the past, or without a time zone, is refused.
    """
    (product,) = products("one")
    root = person("root", is_superuser=True)
    ada = person("ada")
    seen, stop = recording()
    try:
        with freeze_time(FRIDAY - timedelta(days=1)) as clock:
            made = run(
                grant_permission(root, VIEW, product, user=ada.id, expires_at=FRIDAY)
            )
            same = run(
                grant_permission(root, VIEW, product, user=ada.id, expires_at=FRIDAY)
            )
            later = FRIDAY + timedelta(days=7)
            longer = run(
                grant_permission(root, VIEW, product, user=ada.id, expires_at=later)
            )
            clock.move_to(later)
            lapsed = run(has_perm(ada, VIEW, product))
            revived = run(grant_permission(root, VIEW, product, user=ada.id))
            back = run(has_perm(ada, VIEW, product))
            refusals = []
            for refused in (
                later,
                later - timedelta(days=1),
                later.replace(tzinfo=None) + timedelta(days=1),
            ):
                with pytest.raises(InvalidGrant) as caught:
                    run(
                        grant_permission(
                            root, CHANGE, product, user=ada.id, expires_at=refused
                        )
                    )
                refusals.append(str(caught.value))
            stored = run(grants_on(product))
    finally:
        stop()

    assert made.id == same.id == longer.id == revived.id
    assert (lapsed, back) == (False, True)
    assert refusals == [
        "A grant's expiry must be in the future.",
        "A grant's expiry must be in the future.",
        "A grant's expiry needs a time zone.",
    ]
    assert [(grant.id, grant.expires_at) for grant in stored] == [(made.id, None)]
    granted = [event for event in seen if isinstance(event, ObjectPermissionGranted)]
    assert [(event.grant_id, event.expires_at) for event in granted] == [
        (made.id, FRIDAY),
        (made.id, later),
        (made.id, None),
    ]
    assert {event.role for event in granted} == {None}


def test_grants_in_memory_expire_too(use_auth: UseAuth) -> None:
    """``VisibleTo`` in memory skips expired grants; ``clear_expired`` drops them."""
    use_auth(
        settings={
            "INSTALLED_DOMAINS": [CONTENTTYPES, AUTH, OBJECTPERMISSIONS, SHOP, ALPHA]
        }
    )
    lasting, lapsing = (
        Product(id=ProductId(uuid4()), name=name) for name in ("lasting", "lapsing")
    )
    store = InMemoryRepository[Product, ProductId]([lasting, lapsing])
    ada = uuid4()

    def grant_of(product: Product, expires_at: datetime | None) -> ObjectPermission:
        return ObjectPermission.create(
            content_type="shop.product",
            permission=VIEW,
            object_id=product.id,
            user_id=ada,
            group_id=None,
            by=None,
            expires_at=expires_at,
        )

    grants = InMemoryObjectPermissions(
        [grant_of(lasting, None), grant_of(lapsing, FRIDAY)]
    )
    criteria = VisibleTo("view", ada, grants=grants)

    def seen() -> list[str]:
        return sorted(each.name for each in run(store.filter(criteria)))

    with freeze_time(FRIDAY - timedelta(seconds=1)) as clock:
        before = seen()
        clock.move_to(FRIDAY)
        after = seen()
        cleared = run(grants.clear_expired())
    assert (before, after) == (["lapsing", "lasting"], ["lasting"])
    assert cleared == 1
    assert [grant.expires_at for grant in grants.items.values()] == [None]
    assert grant_of(lapsing, FRIDAY).expired(FRIDAY)
    assert not grant_of(lapsing, FRIDAY).expired(FRIDAY - timedelta(seconds=1))
    assert not grant_of(lasting, None).expired()


# Roles -------------------------------------------------------------------------------


def test_roles_grant_and_revoke_their_permissions_together(project: str) -> None:
    """One grant per permission, named after the role; revoking takes those only.

    Ada keeps ``view`` after the editor role goes: the viewer role and a
    grant of its own give it too.
    """
    first, second = products("first", "second")
    root = person("root", is_superuser=True)
    ada = person("ada")
    seen, stop = recording()
    try:
        editing = run(grant_role(root, EDITOR, first, user=ada.id))
        again = run(grant_role(root, EDITOR, first, user=ada.id))
        viewing = run(grant_role(root, VIEWER, first, user=ada.id))
        alone = run(grant_permission(root, VIEW, first, user=ada.id))
        held = [run(has_perm(ada, key, first)) for key in (VIEW, CHANGE)]
        listed = run(grants_on(first))
        removed = run(revoke_role(root, EDITOR, first, user=ada.id))
        none_left = run(revoke_role(root, EDITOR, first, user=ada.id))
        after = [run(has_perm(ada, key, first)) for key in (VIEW, CHANGE)]
        on_second = run(has_perm(ada, VIEW, second))
    finally:
        stop()

    assert [(grant.permission, grant.role) for grant in editing] == [
        (CHANGE, "editor"),
        (VIEW, "editor"),
    ]
    assert [grant.id for grant in again] == [grant.id for grant in editing]
    assert [(g.permission, g.role) for g in viewing] == [(VIEW, "viewer")]
    assert (alone.permission, alone.role) == (VIEW, None)
    assert held == [True, True]
    assert sorted((g.permission, g.role or "") for g in listed) == [
        (CHANGE, "editor"),
        (VIEW, ""),
        (VIEW, "editor"),
        (VIEW, "viewer"),
    ]
    assert (removed, none_left) == (2, 0)
    assert after == [True, False]
    assert on_second is False
    revoked = [e for e in seen if isinstance(e, ObjectPermissionRevoked)]
    assert sorted((e.permission, e.role) for e in revoked) == [
        (CHANGE, "editor"),
        (VIEW, "editor"),
    ]
    granted = [e for e in seen if isinstance(e, ObjectPermissionGranted)]
    assert [e.role for e in granted] == ["editor", "editor", "viewer", None]
    assert sorted((g.permission, g.role or "") for g in run(grants_on(first))) == [
        (VIEW, ""),
        (VIEW, "viewer"),
    ]


def test_a_role_expires_and_goes_with_a_permissions_revocation(project: str) -> None:
    """A role's grants share their expiry; ``revoke_permission`` takes any of them."""
    (product,) = products("one")
    root = person("root", is_superuser=True)
    group = make_group("helpers")
    ada = person("ada")

    with freeze_time(FRIDAY - timedelta(hours=1)) as clock:
        timed = run(grant_role(root, EDITOR, product, user=ada.id, expires_at=FRIDAY))
        run(grant_role(root, VIEWER, product, group=group))
        taken = run(revoke_permission(root, CHANGE, product, user=ada.id))
        changing = run(has_perm(ada, CHANGE, product))
        clock.move_to(FRIDAY)
        viewing = run(has_perm(ada, VIEW, product))
        left = run(grants_on(product))

    assert {grant.expires_at for grant in timed} == {FRIDAY}
    assert (taken, changing, viewing) == (1, False, False)
    assert [(g.permission, g.role, g.group_id) for g in left] == [
        (VIEW, "viewer", group)
    ]


def test_roles_stay_within_what_the_granter_holds(project: str) -> None:
    """The granter must hold every permission of the role; all or nothing is granted.

    Revoking a role checks the permissions its grants hold, also those the
    role no longer names.
    """
    (product,) = products("one")
    root = person("root", is_superuser=True)
    viewer = person(
        "viewer", VIEW, *MANAGE, "objectpermissions.delete_objectpermission"
    )
    ada = person("ada")
    unknown = ObjectRole("broken", [VIEW, "shop.fly_product"])

    with pytest.raises(PermissionDenied, match="only permissions you hold"):
        run(grant_role(viewer, EDITOR, product, user=ada.id))
    with pytest.raises(PermissionDenied, match="to yourself"):
        run(grant_role(root, EDITOR, product, user=root.id))
    with pytest.raises(UnknownPermission):
        run(grant_role(root, unknown, product, user=ada.id))
    with pytest.raises(InvalidGrant, match="not both or neither"):
        run(grant_role(root, EDITOR, product))
    nothing = run(grants_on(product))
    run(grant_role(viewer, VIEWER, product, user=ada.id))
    # The viewer role named ``change`` too, once: that grant stays until revoked.
    run(grant_role(root, ObjectRole("viewer", [VIEW, CHANGE]), product, user=ada.id))
    with pytest.raises(PermissionDenied, match="only permissions you hold"):
        run(revoke_role(viewer, VIEWER, product, user=ada.id))
    kept = sorted(g.permission for g in run(grants_on(product)))
    with pytest.raises(PermissionDenied, match="may not manage"):
        run(revoke_role(person("bystander", VIEW), VIEWER, product, user=ada.id))

    assert nothing == []
    assert kept == [CHANGE, VIEW]
    assert run(revoke_role(root, VIEWER, product, user=ada.id)) == 2


def test_roles_are_named_sets_of_permission_keys() -> None:
    """Sorted, each once; a name of 1 to 255 characters; keys ``domain.codename``."""
    role = ObjectRole("editor", [CHANGE, VIEW, CHANGE])

    assert role.permissions == (CHANGE, VIEW)
    assert role == ObjectRole("editor", (VIEW, CHANGE))
    assert hash(role) == hash(ObjectRole("editor", {VIEW, CHANGE}))
    for name, keys, message in (
        ("", [VIEW], "1 to 255 characters"),
        ("x" * 256, [VIEW], "1 to 255 characters"),
        ("empty", [], "has no permission"),
        ("odd", ["view_product"], "not a permission key"),
    ):
        with pytest.raises(ValueError, match=message):
            ObjectRole(name, keys)


# Permissions of other models -----------------------------------------------------


def test_another_models_permission_needs_the_projects_word(project: str) -> None:
    """``grantable_on(Product, ...)`` lets it be granted on products, and only there.

    Before, or on another model, it stays refused; ``has_perm`` on the
    product then answers, and ``visible_to`` lists the products it is on.
    """
    first, second = products("first", "second")
    root = person("root", is_superuser=True)
    stocker = person("stocker", STOCK, *MANAGE)
    ada = person("ada")
    stock = StockLevel(id=first.id)

    with pytest.raises(InvalidGrant, match="not one of the object's model"):
        run(grant_permission(root, STOCK, first, user=ada.id))
    grantable_on(Product, STOCK, domain="shop")
    made = run(grant_permission(stocker, STOCK, first, user=ada.id))
    role = run(
        grant_role(root, ObjectRole("stock", [STOCK, VIEW]), second, user=ada.id)
    )
    with pytest.raises(InvalidGrant, match="not one of the object's model"):
        run(grant_permission(root, "shop.view_stocklevel", first, user=ada.id))
    with pytest.raises(InvalidGrant, match="not one of the object's model"):
        run(grant_permission(root, VIEW, stock, user=ada.id))

    assert (made.content_type, made.permission) == ("shop.product", STOCK)
    assert [grant.permission for grant in role] == [STOCK, VIEW]
    assert run(has_perm(ada, STOCK, first)) is True
    assert run(has_perm(ada, STOCK)) is False
    assert names(ada, STOCK) == (["first", "second"], 2)
    assert run(visible_to(stocker, STOCK, Product)) is None
    assert run(ObjectPermissionBackend().get_all_permissions(ada, second)) == {
        STOCK,
        VIEW,
    }


def test_visible_to_takes_another_domains_permission_once_grantable(
    use_auth: UseAuth,
) -> None:
    """``bind`` refuses another domain's key unless it is ``grantable_on`` the model."""
    use_auth(
        settings={
            "INSTALLED_DOMAINS": [CONTENTTYPES, AUTH, OBJECTPERMISSIONS, SHOP, ALPHA]
        }
    )
    elsewhere = "auth.change_group"

    with pytest.raises(InvalidQuery, match=r"not a permission of shop\.product"):
        VisibleTo(elsewhere, None).bind(Product)
    grantable_on(Product, elsewhere, domain="shop")
    bound = VisibleTo(elsewhere, None).bind(Product)
    with pytest.raises(InvalidQuery, match=r"not a permission of shop\.stocklevel"):
        VisibleTo(elsewhere, None).bind(StockLevel)

    assert (bound.permission, bound.content_type) == (elsewhere, "shop.product")


def test_grantable_permissions_are_declared_in_ready(use_auth: UseAuth) -> None:
    """Outside a domain's ``ready()`` it needs ``domain=``; keys are checked."""
    use_auth(
        settings={
            "INSTALLED_DOMAINS": [CONTENTTYPES, AUTH, OBJECTPERMISSIONS, SHOP, ALPHA]
        }
    )

    with pytest.raises(ImproperlyConfigured, match=r"DomainConfig\.ready\(\)"):
        grantable_on(Product, STOCK)
    with pytest.raises(ValueError, match="Name the permissions"):
        grantable_on(Product, domain="shop")
    with pytest.raises(ValueError, match="not a permission key"):
        grantable_on(Product, "add_stocklevel", domain="shop")

    class Unmapped:
        """A class of no installed domain: it has no objects to grant on."""

    grantable_on(Unmapped, "auth.change_group", domain="shop")
    with pytest.raises(InvalidQuery, match=r"not a permission of shop\.product"):
        VisibleTo("auth.change_group", None).bind(Product)


# The table, from the release before ----------------------------------------------


@contextmanager
def at_2_3(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """The grants table as 2.3 had it: no ``expires_at``, no ``role``, no index."""
    added = [object_permissions.c.expires_at, object_permissions.c.role]
    expiring = next(
        index
        for index in object_permissions.indexes
        if index.name == "ix_domainkit_object_permissions_expires_at"
    )
    with monkeypatch.context() as patched:
        patched.setattr(
            object_permissions, "indexes", object_permissions.indexes - {expiring}
        )
        for column in added:
            object_permissions._columns.remove(column)
        try:
            yield
        finally:
            for column in added:
                object_permissions._columns.add(column)


def test_a_grants_table_of_2_3_gets_its_new_columns_from_the_next_revision(
    database_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``makemigrations`` adds the two nullable columns and the index, nothing else.

    Grants made before keep counting: they never expire, and no role names them.
    """
    registry.clear()
    with override_settings(
        INSTALLED_DOMAINS=[CONTENTTYPES, AUTH, OBJECTPERMISSIONS, SHOP, ALPHA],
        DATABASES={"default": {"URL": database_url}},
        MIGRATIONS_DIR=tmp_path / "migrations",
        AUTH={"PASSWORD_HASHERS": [FAST_HASHER], "BACKENDS": BACKENDS},
    ):
        setup()
        with at_2_3(monkeypatch):
            call_command("makemigrations", "-m", "at 2.3", stdout=io.StringIO())
            call_command("migrate", stdout=io.StringIO())
        output = io.StringIO()
        call_command("makemigrations", "-m", "grants in time", stdout=output)
        revision = Path(output.getvalue().split()[1].rstrip("."))
        call_command("migrate", stdout=io.StringIO())
        checked = io.StringIO()
        call_command("makemigrations", "--check", stdout=checked)
        (product,) = products("one")
        root = person("root", is_superuser=True)
        ada = person("ada")
        with freeze_time(FRIDAY - timedelta(days=1)):
            run(grant_role(root, EDITOR, product, user=ada.id, expires_at=FRIDAY))
            stored = run(grants_on(product))
    registry.clear()

    upgrade = revision.read_text(encoding="utf-8").split("def downgrade")[0]
    assert upgrade.count("add_column(") == 2
    assert upgrade.count("create_index(") == 1
    assert '"ix_domainkit_object_permissions_expires_at"' in upgrade
    assert "drop_" not in upgrade
    assert checked.getvalue() == "No changes detected.\n"
    assert {(grant.role, grant.expires_at) for grant in stored} == {("editor", FRIDAY)}
