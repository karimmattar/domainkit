"""The generic admin: registered aggregates over REST, and ``/admin/meta``."""

import json
import os
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import BaseModel
from starlette.requests import HTTPConnection

from domainkit.apps import registry
from domainkit.conf import override_settings
from domainkit.contrib import admin
from domainkit.contrib.admin import AdminSite
from domainkit.contrib.audit import api as audit_api
from domainkit.contrib.audit.api import HistoryPageOut
from domainkit.contrib.auth import AnyUser, BasePermission, Group, Permission, User
from domainkit.ddd import AggregateRoot
from domainkit.ddd.query import F
from domainkit.http import FilterSet, Pagination, get_asgi_application
from domainkit.http.errors import install_exception_handlers
from domainkit.resources import FieldPolicy, schema_for
from domainkit_tests.conftest import ALPHA, AUTH, CONTENTTYPES, SHOP, UseAuth
from domainkit_tests.gadgets import FakeGadgetsUnitOfWork, Gadget, InMemoryGadgets
from domainkit_tests.sample_domains.accounts.models import Account
from domainkit_tests.sample_domains.shop.models import Product, StockLevel
from domainkit_tests.test_audit_history import note, project
from domainkit_tests.test_auth_access import user
from domainkit_tests.test_auth_admin import staff
from domainkit_tests.test_resources import (
    DropGadget,
    GadgetAgain,
    GadgetFilters,
    GadgetsLabelled,
    RenameGadget,
    RenameIn,
    Users,
    again,
    as_,
    drop,
    labelled,
    rename,
)

__all__ = ["note", "project"]

GADGETS = "/admin/shop/gadgets"
ACCOUNTS = "domainkit_tests.sample_domains.accounts"


def staff_users() -> dict[str, AnyUser]:
    """Staff and others, by name, for the ``X-User`` header."""
    return {
        "outsider": user("shop.view_gadget"),
        "nobody": user(is_staff=True),
        "viewer": user("shop.view_gadget", is_staff=True),
        "changer": user("shop.view_gadget", "shop.change_gadget", is_staff=True),
        "costs": user("shop.view_gadget", "shop.view_cost", is_staff=True),
        "historian": user("shop.view_gadget", "audit.view_history", is_staff=True),
        "archivist": user("audit.view_history", is_staff=True),
        "root": user(is_superuser=True),
    }


@pytest.fixture
def store() -> InMemoryGadgets:
    """Gadgets a (3.00), b (2.00), and c (1.00)."""
    from domainkit_tests.gadgets import make_gadget

    return InMemoryGadgets(
        [
            make_gadget("b", price="2.00"),
            make_gadget("a", price="3.00"),
            make_gadget("c"),
        ]
    )


def site_of(store: InMemoryGadgets, **options: Any) -> AdminSite:  # noqa: ANN401 - register's options.
    """A site with the gadgets registered."""
    site = AdminSite()
    site.register(
        Gadget,
        unit_of_work=lambda: FakeGadgetsUnitOfWork(store),
        repository=lambda uow: uow.gadgets,
        label="shop.gadget",
        **options,
    )
    return site


def client_of(site: AdminSite) -> TestClient:
    """A client of an application with ``site`` at ``/admin``."""
    app = FastAPI()
    install_exception_handlers(app)
    app.include_router(site.router, prefix="/admin")
    app.add_middleware(Users, users=staff_users())
    return TestClient(app)


def test_staff_list_read_and_run_commands(
    store: InMemoryGadgets, use_auth: UseAuth
) -> None:
    """Lists show ``list_display``; reads all; actions are commands."""
    use_auth()
    site = site_of(
        store,
        list_display=["name", "price"],
        filters=GadgetFilters,
        commands=[
            admin.command(RenameGadget, service=rename, body=RenameIn, path="/rename")
        ],
    )
    client = client_of(site)
    gadget = next(item for item in store.items.values() if item.name == "b")
    item = f"{GADGETS}/{gadget.id}"

    listed = client.get(GADGETS, headers=as_("viewer")).json()["items"]
    read = client.get(item, headers=as_("viewer"))
    refused = client.post(f"{item}/rename", json={"name": "z"}, headers=as_("viewer"))
    renamed = client.post(
        f"{item}/rename",
        json={"name": "z"},
        headers=as_("changer", **{"If-Match": read.headers["etag"]}),
    )

    assert [sorted(each) for each in listed] == [["id", "links", "name", "price"]] * 3
    assert [each["name"] for each in listed] == ["a", "b", "c"]
    assert listed[1]["links"] == {"self": item}
    assert read.json()["label"] == "B"
    assert refused.status_code == 403
    assert (renamed.status_code, renamed.json()["name"]) == (200, "z")
    assert client.get(GADGETS).status_code == 401
    assert client.get(GADGETS, headers=as_("outsider")).status_code == 403
    assert client.get(GADGETS, headers=as_("nobody")).status_code == 403
    scoped = as_("root", **{"X-Scopes": "shop.view_gadget"})
    assert client.get(GADGETS, headers=scoped).status_code == 403
    assert client.get("/admin/meta", headers=scoped).status_code == 403


def test_search_and_ordering_make_filters(
    store: InMemoryGadgets, use_auth: UseAuth
) -> None:
    """Without a FilterSet, ``search`` and ``ordering`` make one."""
    use_auth()
    client = client_of(site_of(store, search=["name"], ordering=["price"]))

    found = client.get(GADGETS, params={"search": "b"}, headers=as_("viewer"))
    ordered = client.get(GADGETS, params={"ordering": "price"}, headers=as_("viewer"))

    assert [each["name"] for each in found.json()["items"]] == ["b"]
    assert [each["name"] for each in ordered.json()["items"]] == ["c", "b", "a"]


class Memo(AggregateRoot[uuid.UUID]):
    """An aggregate with text of any length."""

    text: str


def test_ordering_needs_bounded_fields() -> None:
    """A cursor holds the sort values: text without a ``MaxLength`` is refused."""
    with pytest.raises(TypeError, match=r"Memo.text .* MaxLength"):
        AdminSite().register(
            Memo,
            unit_of_work=FakeGadgetsUnitOfWork,
            repository=lambda uow: uow.gadgets,
            ordering=["text"],
        )


def test_queries_answer_through_their_handlers(
    store: InMemoryGadgets,
    use_auth: UseAuth,
) -> None:
    """``admin.query`` serves a query on the model's resource."""
    use_auth()
    client = client_of(
        site_of(
            store,
            queries=[admin.query(GadgetsLabelled, handler=labelled, path="/labelled")],
        )
    )

    found = client.get(
        f"{GADGETS}/labelled", params={"label": "C"}, headers=as_("viewer")
    )

    assert [each["name"] for each in found.json()["items"]] == ["c"]


def test_meta_describes_what_the_caller_may_use(
    store: InMemoryGadgets,
    use_auth: UseAuth,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Models the caller may view, fields they may see, commands they may run."""
    use_auth()
    monkeypatch.setattr(registry, "is_installed", lambda name: True)
    site = site_of(
        store,
        list_display=["name", "stock"],
        filters=GadgetFilters,
        policy=FieldPolicy(visible={"stock": "shop.view_cost"}),
        commands=[
            admin.command(RenameGadget, service=rename, body=RenameIn, path="/rename")
        ],
        queries=[admin.query(GadgetsLabelled, handler=labelled, path="/labelled")],
    )
    client = client_of(site)

    def meta(name: str) -> Any:  # noqa: ANN401 - JSON.
        answer = client.get("/admin/meta", headers=as_(name))
        assert answer.status_code == 200, answer.json()
        return answer.json()["resources"]

    (viewed,) = meta("viewer")
    (changed,) = meta("changer")
    (costed,) = meta("costs")
    (historied,) = meta("historian")

    assert viewed["label"] == "shop.gadget"
    assert viewed["path"] == GADGETS
    assert viewed["item_path"] == f"{GADGETS}/{{gadget_id}}"
    assert viewed["list_fields"] == ["id", "name"]
    assert costed["list_fields"] == ["id", "name", "stock"]
    assert "stock" not in viewed["item_schema"]["properties"]
    assert "stock" not in viewed["item_schema"]["required"]
    assert "stock" in costed["item_schema"]["properties"]
    assert {"parameter": "color__in", "field": "color", "lookup": "in"} in viewed[
        "filters"
    ]
    assert (viewed["ordering"], viewed["default_ordering"], viewed["search"]) == (
        ["name", "price"],
        ["name"],
        True,
    )
    assert [each["name"] for each in viewed["operations"]] == [
        "admin_shop_gadgets_labelled"
    ]
    assert viewed["operations"][0]["parameters"]["required"] == ["label"]
    rename_operation = changed["operations"][0]
    assert rename_operation["summary"] == "Rename a gadget."
    assert rename_operation["name"] == "admin_shop_rename_gadget"
    assert rename_operation["path"] == f"{GADGETS}/{{gadget_id}}/rename"
    assert rename_operation["body"]["properties"]["name"]["maxLength"] == 30
    assert (rename_operation["method"], rename_operation["detail"]) == ("POST", True)
    assert viewed["history_path"] is None
    assert historied["history_path"] == f"{GADGETS}/{{gadget_id}}/history"
    assert meta("nobody") == []
    assert meta("root")[0]["history_path"] == historied["history_path"]
    assert client.get("/admin/meta", headers=as_("outsider")).status_code == 403
    assert client.get("/admin/meta").status_code == 401


def test_meta_lists_every_field_without_list_display(
    store: InMemoryGadgets,
    use_auth: UseAuth,
) -> None:
    """Lists show the whole schema; no filters, ordering, or search."""
    use_auth()
    client = client_of(site_of(store))

    (described,) = client.get("/admin/meta", headers=as_("root")).json()["resources"]

    assert described["list_fields"][:4] == ["id", "version", "deleted_at", "name"]
    assert (described["filters"], described["ordering"], described["search"]) == (
        [],
        [],
        False,
    )
    assert described["operations"] == []
    assert described["history_path"] is None


def test_registrations_are_checked(
    store: InMemoryGadgets,
    use_auth: UseAuth,
) -> None:
    """Twice, after mounting, filters with search, and auth's own models."""
    use_auth()
    site = site_of(store)

    with pytest.raises(TypeError, match="registered in the admin already"):
        site.register(
            Gadget, unit_of_work=FakeGadgetsUnitOfWork, repository=lambda uow: uow
        )
    with pytest.raises(TypeError, match="in the FilterSet given"):
        AdminSite().register(
            Gadget,
            unit_of_work=FakeGadgetsUnitOfWork,
            repository=lambda uow: uow,
            filters=GadgetFilters,
            ordering=["name"],
        )
    assert site.router is site.router
    with pytest.raises(RuntimeError, match="before the admin's router is mounted"):
        site.register(dict, unit_of_work=FakeGadgetsUnitOfWork, repository=lambda u: u)
    users = AdminSite()
    users.register(
        Gadget,
        unit_of_work=FakeGadgetsUnitOfWork,
        repository=lambda uow: uow,
        label="auth.user",
    )
    with pytest.raises(TypeError, match=r"auth\.user has an admin of its own"):
        users.router  # noqa: B018 - built.
    audited = site_of(store, history=True)
    with pytest.raises(
        TypeError, match=r"history=True needs domainkit\.contrib\.audit"
    ):
        audited.router  # noqa: B018 - built.
    assert issubclass(
        site_of(store, search=["name"]).admins[Gadget].filters or object, FilterSet
    )


def test_the_default_site_registers() -> None:
    """``admin.register`` registers with ``admin.site``."""
    assert admin.register == admin.site.register


@pytest.fixture
def admin_client(note: uuid.UUID) -> Iterator[TestClient]:
    """The admin over the notes, audit installed, a note's history written."""
    with (
        override_settings(ROOT_ROUTER="domainkit_tests.admin_routes.router"),
        TestClient(get_asgi_application()) as client,
    ):
        yield client


def test_items_have_their_history(admin_client: TestClient, note: uuid.UUID) -> None:
    """``/history``: the audit trail about the item, for staff allowed to see it."""
    root = staff("root", is_superuser=True)
    url = f"/admin/alpha/notes/{note}/history"

    first = admin_client.get(url, params={"limit": 2}, headers=root).json()
    rest = admin_client.get(
        url, params={"limit": 2, "cursor": first["next_cursor"]}, headers=root
    ).json()
    missing = admin_client.get(
        f"/admin/alpha/notes/{uuid.uuid4()}/history", headers=root
    )
    meta = admin_client.get("/admin/meta", headers=root).json()["resources"]

    assert [item["details"]["title"] for item in first["items"]] == ["Final", "Draft"]
    assert [item["details"]["title"] for item in rest["items"]] == ["Draft"]
    assert missing.status_code == 404
    assert meta[0]["history_path"] == "/admin/alpha/notes/{note_id}/history"
    assert admin_client.get(url, headers=staff("plain")).status_code == 403


class Brief(BaseModel):
    """What the admin shows of a gadget."""

    id: uuid.UUID
    name: str


def test_registrations_pass_their_options_to_the_resource(
    store: InMemoryGadgets, use_auth: UseAuth, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Schema, presenter, pagination, scope, plural, filters, and history."""
    use_auth()
    monkeypatch.setattr(registry, "is_installed", lambda name: True)
    shown: list[str] = []

    def present(gadget: Gadget) -> Brief:
        shown.append(gadget.name)
        return Brief(id=gadget.id, name=gadget.name.upper())

    async def without_a(request: Any) -> Any:  # noqa: ANN401 - criteria.
        return F("name") != request.headers.get("x-hide", "a")

    asked: list[dict[str, Any]] = []

    async def read_history(actor: AnyUser, **query: Any) -> HistoryPageOut:  # noqa: ANN401 - its query.
        asked.append({"actor": actor.is_superuser, **query})
        return HistoryPageOut(items=[], next_cursor=None)

    monkeypatch.setattr(audit_api, "read_history", read_history)

    site = site_of(
        store,
        schema=Brief,
        present=present,
        pagination=Pagination(default_limit=1),
        scope=without_a,
        plural="things",
        filters=GadgetFilters,
        history_field="thing_id",
        commands=[admin.command(DropGadget, service=drop, method="DELETE")],
    )
    client = TestClient(client_of(site).app, root_path="/api")
    by_name = {gadget.name: gadget for gadget in store.items.values()}
    things = "/admin/shop/things"

    page = client.get(things, headers=as_("root")).json()
    blue = client.get(things, params={"color": "blue"}, headers=as_("root")).json()
    hidden = client.get(f"{things}/{by_name['a'].id}", headers=as_("root"))
    history = client.get(f"{things}/{by_name['a'].id}/history", headers=as_("root"))
    read = client.get(f"{things}/{by_name['b'].id}/history", headers=as_("root"))
    (meta,) = client.get("/admin/meta", headers=as_("root")).json()["resources"]
    dropped = client.delete(f"{things}/{by_name['c'].id}", headers=as_("root"))

    b = str(by_name["b"].id)
    assert page["items"] == [
        {"id": b, "name": "B", "links": {"self": f"/api{things}/{b}"}}
    ]
    assert blue["items"] == []
    assert (hidden.status_code, history.status_code) == (404, 404)
    assert meta["path"] == "/api/admin/shop/things"
    assert meta["history_path"] == "/api/admin/shop/things/{gadget_id}/history"
    assert meta["item_schema"]["title"] == "Brief"
    assert dropped.status_code == 204
    assert read.json() == {"items": [], "next_cursor": None}
    assert asked == [
        {
            "actor": True,
            "target": None,
            "field": "thing_id",
            "id": str(by_name["b"].id),
            "limit": 50,
            "cursor": None,
        }
    ]
    assert "b" in shown


def test_histories_take_bounded_pages(
    store: InMemoryGadgets, use_auth: UseAuth, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``limit`` is 1 to 200 and the cursor is checked, before any read."""
    use_auth()
    monkeypatch.setattr(registry, "is_installed", lambda name: True)
    site = site_of(store)
    client = client_of(site)
    gadget = next(iter(store.items.values()))
    url = f"{GADGETS}/{gadget.id}/history"

    for params in (
        {"limit": 0},
        {"limit": 201},
        {"cursor": "!"},
        {"cursor": "a" * 2049},
    ):
        answer = client.get(url, params=params, headers=as_("historian"))
        assert answer.status_code == 422, params
    # A history tells what the item held: its view permission comes first.
    assert client.get(url, headers=as_("viewer")).status_code == 403
    assert client.get(url, headers=as_("archivist")).status_code == 403
    (resource,) = [each.resource for each in site.admins.values()]
    (history,) = [each for each in resource.operations if each.name.endswith("history")]
    assert (history.method, history.action, history.detail) == ("GET", "view", True)
    assert [type(rule).__name__ for rule in history.rules] == [
        "IsUnscoped",
        "AnyOf",
        "HasModelPermission",
        "HasPermissions",
    ]


def test_the_admin_is_described_in_openapi_as_reviewed(
    store: InMemoryGadgets, use_auth: UseAuth, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Paths, answers, and errors of a site; UPDATE_SNAPSHOTS=1 rewrites."""
    use_auth()
    monkeypatch.setattr(registry, "is_installed", lambda name: True)
    site = site_of(
        store,
        list_display=["name"],
        commands=[
            admin.command(RenameGadget, service=rename, body=RenameIn, path="/rename")
        ],
        queries=[admin.query(GadgetsLabelled, handler=labelled, path="/labelled")],
    )
    document = client_of(site).get("/openapi.json").json()
    text = json.dumps(document, indent=1, sort_keys=True) + "\n"
    snapshot = Path(__file__).parent / "snapshots" / "openapi" / "admin.json"
    if os.environ.get("UPDATE_SNAPSHOTS"):
        snapshot.write_text(text, encoding="utf-8")

    assert text == snapshot.read_text(encoding="utf-8")


def test_users_groups_and_permissions_are_refused_whatever_their_label(
    use_auth: UseAuth,
) -> None:
    """A swapped user model, or a label that names another model, is refused."""
    use_auth(
        settings={
            "INSTALLED_DOMAINS": [CONTENTTYPES, AUTH, ACCOUNTS, SHOP, ALPHA],
        },
        USER_MODEL="domainkit_tests.sample_domains.accounts.models.Account",
        USER_TABLE="domainkit_tests.sample_domains.accounts.tables.accounts",
    )
    refused: tuple[tuple[type, str | None], ...] = (
        (Account, None),
        (Account, "alpha.note"),
        (User, None),
        (Group, "shop.group"),
        (Permission, None),
    )
    for model, label in refused:
        with pytest.raises(TypeError, match="admin of its own"):
            AdminSite().register(
                model,
                unit_of_work=FakeGadgetsUnitOfWork,
                repository=lambda uow: uow,
                label=label,
            )
    mapped = AdminSite()
    served: tuple[tuple[type, str | None], ...] = (
        (Product, None),
        (StockLevel, "shop.stocklevel"),
    )
    for model, label in served:
        mapped.register(
            model,
            unit_of_work=FakeGadgetsUnitOfWork,
            repository=lambda uow: uow,
            label=label,
        )
    assert mapped.router is mapped.router
    mislabelled = AdminSite()
    mislabelled.register(
        Product,
        unit_of_work=FakeGadgetsUnitOfWork,
        repository=lambda uow: uow,
        label="shop.gadget",
    )
    with pytest.raises(TypeError, match=r"shop\.product, not shop\.gadget"):
        mislabelled.router  # noqa: B018 - built.


def test_schemas_never_show_password_hashes() -> None:
    """A schema of a model with a password hash must leave the hash out."""
    with pytest.raises(TypeError, match="password"):
        schema_for(Account)

    shown = schema_for(Account, exclude=["password"])

    assert "password" not in shown.model_fields


class Ask(BasePermission):
    """Lets the request through; refuses every object."""

    async def has_object_permission(
        self, request: HTTPConnection, user: AnyUser, obj: object
    ) -> bool:
        """No object."""
        return False


def test_commands_and_queries_ask_their_rules_about_the_object(
    store: InMemoryGadgets, use_auth: UseAuth
) -> None:
    """``admin.command(rule=)`` and ``admin.query(rule=)`` look at the item."""
    use_auth()
    site = site_of(
        store,
        commands=[
            admin.command(
                RenameGadget, service=rename, body=RenameIn, path="/rename", rule=Ask()
            )
        ],
        queries=[admin.query(GadgetAgain, handler=again, path="/again", rule=Ask())],
    )
    client = client_of(site)
    gadget = next(iter(store.items.values()))
    item = f"{GADGETS}/{gadget.id}"

    renamed = client.post(f"{item}/rename", json={"name": "x"}, headers=as_("root"))
    asked = client.get(f"{item}/again", headers=as_("root"))

    assert (renamed.status_code, asked.status_code) == (403, 403)
    assert gadget.name != "x"


def test_meta_leaves_out_the_definitions_of_hidden_fields(
    store: InMemoryGadgets, use_auth: UseAuth
) -> None:
    """Nested schemas only hidden fields use go with them."""
    use_auth()
    policy = FieldPolicy(
        visible={"maker": "shop.view_maker", "parts": "shop.view_cost"}
    )
    client = client_of(site_of(store, policy=policy))

    (viewed,) = client.get("/admin/meta", headers=as_("viewer")).json()["resources"]
    (costed,) = client.get("/admin/meta", headers=as_("costs")).json()["resources"]

    assert sorted(viewed["item_schema"]["$defs"]) == ["Color", "Size"]
    assert sorted(costed["item_schema"]["$defs"]) == ["Color", "PartOut", "Size"]
