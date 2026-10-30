"""Money over HTTP: schemas, filters, cursors, OpenAPI, and money errors (FIN-01)."""

from collections.abc import AsyncIterator
from typing import Any
from uuid import UUID

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import BaseModel

from domainkit.contrib.admin import AdminSite
from domainkit.db import connections
from domainkit.ddd import Command, EventBus, UnitOfWork
from domainkit.http.access import Public
from domainkit.http.errors import install_exception_handlers
from domainkit.http.filters import Filter, FilterSet
from domainkit.http.pagination import Pagination, cursor_length
from domainkit.money import Currency, Money
from domainkit.resources import ALL_FIELDS, ResourceRouter, schema_for
from domainkit.testing import AsyncTestClient
from domainkit_tests.conftest import UseAuth
from domainkit_tests.test_auth_access import user
from domainkit_tests.test_money_db import (
    InMemoryWallets,
    Wallet,
    WalletId,
    WalletsUnitOfWork,
    database,  # noqa: F401 - a fixture.
    dataset,
    metadata,
    store,
)
from domainkit_tests.test_resources import Users, as_

pytestmark = pytest.mark.anyio


class Pay(Command):
    """Pay an amount out of a wallet."""

    wallet_id: UUID
    amount: Money


class AmountIn(BaseModel):
    """An amount to pay."""

    amount: Money


async def pay(command: Pay, uow: WalletsUnitOfWork) -> Wallet:
    """Take the amount off the balance: of another currency, a CurrencyMismatch."""
    async with uow:
        wallet = await uow.wallets.get(WalletId(command.wallet_id))
        wallet.balance = wallet.balance - command.amount
        await uow.commit()
    return wallet


def wallet_filters() -> type[FilterSet]:
    """The wallet listing's filters, declared as the test runs."""

    class WalletFilters(FilterSet):
        """Amounts within a currency, the currency alone, a missing limit."""

        model = Wallet
        balance = Filter("exact", "gte", "lt", "in")
        limit = Filter("lte", "isnull")
        unit = Filter("exact", "in")
        currency = Filter(field="balance.currency")
        ordering_fields = ("balance", "limit", "name")

    return WalletFilters


@pytest.fixture
async def client(database: str) -> AsyncIterator[AsyncTestClient]:  # noqa: F811 - the fixture.
    """The wallet resource over the database, with the dataset stored."""
    async with connections.engine().begin() as connection:
        await connection.run_sync(metadata.create_all)
    await store(*dataset())
    resource = ResourceRouter(
        "/wallets",
        model=Wallet,
        unit_of_work=WalletsUnitOfWork,
        repository=lambda uow: uow.wallets,
        dependencies=[Public],
        crud=True,
        writable=ALL_FIELDS,
        filters=wallet_filters(),
        pagination=Pagination(default_limit=3),
        exports=("csv",),
    )
    resource.command_route(Pay, service=pay, body=AmountIn, path="/pay")
    app = FastAPI()
    install_exception_handlers(app)
    app.include_router(resource)
    async with AsyncTestClient(app) as served:
        yield served
    await connections.close_all()


async def listed(client: AsyncTestClient, **params: Any) -> list[str]:  # noqa: ANN401 - any value.
    """The names of every page of the listing, cursor after cursor."""
    found: list[str] = []
    cursor: str | None = None
    while True:
        page = await client.get(
            "/wallets", params={**params, **({"cursor": cursor} if cursor else {})}
        )
        assert page.status_code == 200, page.json()
        found += [item["name"] for item in page.json()["items"]]
        cursor = page.json().get("next_cursor")
        if not cursor:
            return found


async def test_money_is_written_and_read_as_amount_text_and_currency(
    client: AsyncTestClient,
) -> None:
    """The amount has its currency's places; a missing limit is null."""
    created = await client.post(
        "/wallets",
        json={
            "name": "new",
            "balance": {"amount": "10.5", "currency": "KWD"},
            "limit": {"amount": "7", "currency": "EUR"},
            "unit": "JPY",
        },
    )

    assert created.status_code == 201, created.json()
    body = (await client.get(created.headers["location"])).json()
    assert body["balance"] == {"amount": "10.500", "currency": "KWD"}
    assert body["limit"] == {"amount": "7.00", "currency": "EUR"}
    assert body["unit"] == "JPY"
    patched = await client.patch(
        created.headers["location"],
        json={"limit": None, "balance": {"amount": "-1", "currency": "USD"}},
    )
    assert patched.status_code == 200, patched.json()
    assert (patched.json()["limit"], patched.json()["balance"]["amount"]) == (
        None,
        "-1.00",
    )


@pytest.mark.parametrize(
    ("balance", "limit", "kind"),
    [
        ({"amount": "1.005", "currency": "USD"}, None, "inexact_amount"),
        ({"amount": "1", "currency": "ABC"}, None, "unknown_currency"),
        ({"amount": 1.5, "currency": "USD"}, None, "money_amount"),
        ({"amount": "1"}, None, "money_type"),
        (
            {"amount": "1", "currency": "USD"},
            {"amount": "1.234", "currency": "KWD"},
            "money_digits",
        ),
        (
            {"amount": "1", "currency": "USD"},
            {"amount": "1" * 11, "currency": "JPY"},
            "money_digits",
        ),
        ({"amount": "1" * 15, "currency": "JPY"}, None, "money_digits"),
    ],
)
async def test_money_a_column_cannot_hold_is_refused_before_the_database(
    client: AsyncTestClient,
    balance: dict[str, object],
    limit: dict[str, object] | None,
    kind: str,
) -> None:
    """422 validation errors, each with its type: nothing reaches a column."""
    response = await client.post(
        "/wallets", json={"name": "x", "balance": balance, "limit": limit}
    )

    assert response.status_code == 422, response.json()
    assert [error["type"] for error in response.json()["errors"]] == [kind]
    if kind == "money_digits":
        assert response.json()["errors"][0]["msg"] == (
            "The amount has more digits than this field holds."
        )


async def test_the_most_digits_a_column_holds_are_taken(
    client: AsyncTestClient,
) -> None:
    """14 digits before the point of the default column, 10 of Digits(12, 2)."""
    created = await client.post(
        "/wallets",
        json={
            "name": "full",
            "balance": {"amount": "9" * 14 + ".99", "currency": "USD"},
            "limit": {"amount": "9" * 10 + ".99", "currency": "USD"},
        },
    )

    assert created.status_code == 201, created.json()
    assert created.json()["balance"]["amount"] == "9" * 14 + ".99"


async def test_a_limit_of_more_places_than_its_column_fits_when_they_are_zeros(
    client: AsyncTestClient,
) -> None:
    """1.000 KWD fits two places exactly; it reads back as 1.000 KWD."""
    created = await client.post(
        "/wallets",
        json={
            "name": "kw",
            "balance": {"amount": "0", "currency": "KWD"},
            "limit": {"amount": "1", "currency": "KWD"},
        },
    )

    assert created.status_code == 201, created.json()
    body = (await client.get(created.headers["location"])).json()
    assert body["limit"] == {"amount": "1.000", "currency": "KWD"}


async def test_a_money_error_in_a_service_is_a_422_with_its_code(
    client: AsyncTestClient,
) -> None:
    """Two currencies: currency_mismatch, its summary, no amounts in it."""
    first = (await client.get("/wallets", params={"currency": "EUR"})).json()["items"][
        0
    ]

    paid = await client.post(
        f"/wallets/{first['id']}/pay",
        json={"amount": {"amount": "1", "currency": "USD"}},
    )
    same = await client.post(
        f"/wallets/{first['id']}/pay",
        json={"amount": {"amount": "1", "currency": "EUR"}},
    )

    assert paid.status_code == 422
    assert paid.json()["code"] == "currency_mismatch"
    assert paid.json()["detail"] == "The amounts are in different currencies."
    assert same.status_code == 200, same.json()


@pytest.mark.parametrize(
    ("params", "found"),
    [
        ({"balance__gte": "10", "balance_currency": "USD"}, ["a", "h"]),
        ({"balance__gte": "9.995", "balance_currency": "USD"}, ["a", "h"]),
        ({"balance__lt": "10", "balance_currency": "EUR"}, ["g"]),
        ({"balance": "10", "balance_currency": "EUR"}, ["c"]),
        ({"balance__in": ["10", "1000"], "balance_currency": "JPY"}, ["f"]),
        ({"balance_currency": "USD"}, ["a", "b", "d", "h"]),
        ({"currency": "KWD"}, ["e"]),
        ({"limit__lte": "100", "limit_currency": "EUR"}, ["c", "d"]),
        ({"limit__isnull": "true"}, ["b", "f", "h"]),
        ({"unit": "USD"}, ["a", "d"]),
        ({"unit__in": ["EUR", "KWD"]}, ["b", "g"]),
    ],
)
async def test_filters_compare_amounts_within_a_currency(
    client: AsyncTestClient, params: dict[str, object], found: list[str]
) -> None:
    """``total__gte=10&total_currency=USD``: USD totals of at least 10.00."""
    assert await listed(client, **params, ordering="name") == found


@pytest.mark.parametrize(
    ("params", "code", "detail"),
    [
        ({"balance__gte": "10"}, "invalid_filter", "Give balance_currency"),
        ({"limit__lte": "1"}, "invalid_filter", "Give limit_currency"),
        ({"balance_currency": "XYZ"}, "invalid_filter", "known currency"),
        ({"balance_currency": "TOOLONGCODE"}, "validation_error", ""),
        ({"balance__gte": "1e999", "balance_currency": "USD"}, "validation_error", ""),
    ],
)
async def test_filters_that_do_not_fit_are_refused(
    client: AsyncTestClient, params: dict[str, Any], code: str, detail: str
) -> None:
    """An amount without its currency, an unknown code, a code or amount too long."""
    response = await client.get("/wallets", params=params)

    assert response.status_code == 422, response.json()
    assert response.json()["code"] == code
    assert detail in response.json()["detail"]


@pytest.mark.parametrize("ordering", ["balance", "-balance", "limit", "-limit"])
async def test_pages_of_a_money_ordering_follow_their_cursors(
    client: AsyncTestClient, ordering: str
) -> None:
    """Three to a page: by currency, then amount, then id, across every page."""
    by_name = {each.name: each for each in dataset()}
    field = ordering.removeprefix("-")

    names = await listed(client, ordering=ordering)

    def key(name: str) -> tuple[object, ...]:
        value = getattr(by_name[name], field)
        return (1,) if value is None else (0, value.currency.code, value.amount)

    keys = [key(name) for name in names]
    assert sorted(names) == sorted(by_name)
    assert keys == sorted(keys, reverse=ordering.startswith("-"))


async def test_a_money_cursor_that_does_not_fit_is_refused(
    client: AsyncTestClient,
) -> None:
    """A cursor's money must be an amount of a known currency."""
    first = await client.get("/wallets", params={"ordering": "balance"})
    cursor = first.json()["next_cursor"]

    again = await client.get(
        "/wallets", params={"ordering": "balance", "cursor": cursor}
    )
    forged = await client.get(
        "/wallets", params={"ordering": "balance", "cursor": cursor[:-4] + "AAAA"}
    )

    assert again.status_code == 200
    assert forged.status_code == 422


async def test_money_exports_as_its_json_in_a_cell(client: AsyncTestClient) -> None:
    """A CSV cell of money holds its JSON, the amount as text."""
    exported = await client.get(
        "/wallets", params={"currency": "KWD"}, headers={"Accept": "text/csv"}
    )

    assert exported.status_code == 200
    assert '"{""amount"": ""1.234"", ""currency"": ""KWD""}"' in exported.text


async def test_openapi_names_one_money_schema_and_the_currency_parameters(
    client: AsyncTestClient,
) -> None:
    """Bodies refer to Money; filters take an amount and a code."""
    document = (await client.get("/openapi.json")).json()
    schemas = document["components"]["schemas"]

    assert schemas["Money"]["required"] == ["amount", "currency"]
    assert schemas["Money"]["properties"]["amount"]["type"] == "string"
    balance = schemas["WalletOut"]["properties"]["balance"]
    assert balance == {"$ref": "#/components/schemas/Money"}
    limit = schemas["WalletOut"]["properties"]["limit"]
    assert {"$ref": "#/components/schemas/Money"} in limit["anyOf"]
    parameters = {
        each["name"]: each
        for each in document["paths"]["/wallets"]["get"]["parameters"]
    }
    assert parameters["balance_currency"]["schema"]["anyOf"][0] == {
        "type": "string",
        "maxLength": 8,
    }
    assert "balance__gte" in parameters
    assert "limit_currency" in parameters
    assert "unit_currency" not in parameters


def test_money_filters_after_other_filters_have_their_currency() -> None:
    """Each money filter of a set brings its currency, wherever it is declared."""

    class Later(FilterSet):
        model = Wallet
        name = Filter()
        unit = Filter()
        limit = Filter("lte")
        balance = Filter("gte")

    assert Later.currencies == {
        "limit": "limit_currency",
        "balance": "balance_currency",
    }
    currency, lookup, part = Later.filters["balance_currency"]
    assert (currency.field, lookup, part.name, part.kind) == (
        "balance.currency",
        "exact",
        "currency",
        Currency,
    )


def test_a_currency_filter_name_cannot_be_taken() -> None:
    """``balance_currency`` belongs to the balance filter."""
    with pytest.raises(TypeError, match="balance_currency is the currency"):

        class Clashing(FilterSet):
            model = Wallet
            balance = Filter("gte")
            balance_currency = Filter(field="name")


def test_cursors_of_money_are_bounded_by_its_digits() -> None:
    """An amount of its column's digits and a code of eight."""
    from domainkit.ddd.query import Ordering

    by_balance = cursor_length((Ordering("balance"),), Wallet)
    by_limit = cursor_length((Ordering("limit"),), Wallet)

    assert by_balance > by_limit  # 18 digits against 12.


def test_read_schemas_take_money_as_it_is() -> None:
    """from_domain keeps the Money; JSON writes it as text."""
    schema = schema_for(Wallet)
    (wallet, *_) = dataset()

    shown = schema.from_domain(wallet)  # type: ignore[attr-defined]

    assert shown.balance is wallet.balance
    assert shown.model_dump(mode="json")["balance"] == {
        "amount": "10.00",
        "currency": "USD",
    }


class FakeWalletsUnitOfWork(UnitOfWork):
    """Wallets in memory."""

    def __init__(self, wallets: InMemoryWallets) -> None:
        """Work on ``wallets``."""
        super().__init__(events=EventBus())
        self.wallets = wallets

    async def _commit(self) -> None:
        """Nothing to write."""

    async def _rollback(self) -> None:
        """Nothing to undo."""


def test_the_admin_lists_filters_and_describes_money(use_auth: UseAuth) -> None:
    """Money in the item schema, the currency parameter among the filters."""
    use_auth()
    store = InMemoryWallets(dataset())
    site = AdminSite()
    site.register(
        Wallet,
        unit_of_work=lambda: FakeWalletsUnitOfWork(store),
        repository=lambda uow: uow.wallets,
        label="shop.wallet",
        list_display=["name", "balance"],
        filters=wallet_filters(),
    )
    app = FastAPI()
    install_exception_handlers(app)
    app.include_router(site.router, prefix="/admin")
    app.add_middleware(Users, users={"root": user(is_superuser=True)})
    client = TestClient(app)

    (described,) = client.get("/admin/meta", headers=as_("root")).json()["resources"]
    listed = client.get(
        "/admin/shop/wallets",
        params={"balance__gte": "10", "balance_currency": "USD", "ordering": "name"},
        headers=as_("root"),
    )

    schema = described["item_schema"]
    assert schema["properties"]["balance"] == {"$ref": "#/$defs/Money"}
    assert list(schema["$defs"]) == ["Money"]
    assert {
        "parameter": "balance_currency",
        "field": "balance.currency",
        "lookup": "exact",
    } in described["filters"]
    assert listed.status_code == 200, listed.json()
    assert [item["balance"] for item in listed.json()["items"]] == [
        {"amount": "10.00", "currency": "USD"},
        {"amount": "99999999999999.99", "currency": "USD"},
    ]
