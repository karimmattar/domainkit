"""A resource's act, authorized from start to end by the real core (user report A6).

A ``ResourceRouter`` command route declared with
``rule=StepUp(for_="shop.rename_product")``, in a migrated identity
project, with no stand-in rule: its twin asks for an authorization, built
and shown by the server; a code of the user's authenticator confirms it;
the act passes with it once, and refuses it again, and refuses it for
other arguments (ADR 0144). A ``@use_case`` is authorized through the
copy ``requires_authorization`` makes, and the test helpers do the round
trip (ADR 0161, user reports A13 and A17).
"""

from collections.abc import Iterator
from typing import Any, Final
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from domainkit.apps import registry
from domainkit.clock import FrozenClock, freeze_time
from domainkit.conf import override_settings
from domainkit.contrib.auth.actions import AuthorizationNotFound, register_action
from domainkit.contrib.auth.tables import authorizations
from domainkit.db import connections
from domainkit.management.base import run_async
from domainkit.testing import (
    Authenticator,
    UnexpectedResponse,
    authorize_act,
    confirm_authorization,
)
from domainkit.testing.authenticators import AuthorizationConfirmer
from domainkit_tests import sample_action_resources as shop
from domainkit_tests.conftest import Api
from domainkit_tests.sample_action_resources import RENAME_PRODUCT, ShopUnitOfWork
from domainkit_tests.sample_domains.shop.models import Product, ProductId
from domainkit_tests.support.auth import audited, bearer, create_user, grant, login
from domainkit_tests.test_auth_stepup import enrol, now_code

ROUTES: Final = "domainkit_tests.sample_action_resources.router"
START: Final = "2027-01-04T10:00:00Z"


class Shopper:
    """Alice, logged in, with an authenticator app, who may rename products."""

    def __init__(self, client: TestClient, clock: FrozenClock) -> None:
        """Log alice in and turn her second factor on."""
        self.client = client
        self.clock = clock
        self.headers = bearer(login(client, "alice")["access_token"])
        self.secret = enrol(client, self.headers, clock)

    def rename(self, product: UUID, name: str, authorization: str | None = None) -> Any:  # noqa: ANN401 - a response.
        """The act: rename ``product``, sending ``authorization`` if given."""
        headers = dict(self.headers)
        if authorization is not None:
            headers["Action-Authorization"] = authorization
        return self.client.post(
            f"/products/{product}/rename", json={"name": name}, headers=headers
        )

    def ask(self, product: UUID, name: str) -> Any:  # noqa: ANN401 - a response.
        """The twin: ask for an authorization of renaming ``product``."""
        return self.client.post(
            f"/products/{product}/rename/authorization",
            json={"name": name},
            headers=self.headers,
        )

    def confirm(self, authorization_id: str) -> Any:  # noqa: ANN401 - a response.
        """Confirm the authorization with now's code."""
        return self.client.post(
            f"/auth/authorizations/{authorization_id}/confirm",
            json={"code": now_code(self.secret, self.clock)},
            headers=self.headers,
        )

    def authorized(self, product: UUID, name: str) -> str:
        """``<id>.<code>``: a confirmed authorization of renaming ``product``."""
        asked = self.ask(product, name)
        assert asked.status_code == 201, asked.json()
        confirmed = self.confirm(asked.json()["id"])
        assert confirmed.status_code == 200, confirmed.json()
        authorization: str = confirmed.json()["authorization"]
        return authorization


@pytest.fixture
def frozen() -> Iterator[FrozenClock]:
    """One clock for codes and expiries."""
    with freeze_time(START) as clock:
        yield clock


@pytest.fixture
def alice(identity: Api, frozen: FrozenClock) -> Iterator[Shopper]:
    """Alice's session at the products, the action registered."""
    shop.SHOWN_IN.clear()
    with override_settings(ROOT_ROUTER=ROUTES):
        register_action(RENAME_PRODUCT, domain="shop")
        register_action(shop.RELABEL_PRODUCT, domain="shop")
        create_user("alice")
        grant("alice", "shop.view_product", "shop.change_product")
        yield Shopper(identity(), frozen)
    shop.SHOWN_IN.clear()


def stock(name: str) -> UUID:
    """Store a product called ``name``; its id."""

    async def add() -> UUID:
        product = Product(id=ProductId(uuid4()), name=name)
        async with ShopUnitOfWork() as uow:
            await uow.products.add(product)
            await uow.commit()
        return product.id

    return run_async(add())


def name_of(product: UUID) -> str:
    """The stored name of ``product``."""

    async def read() -> str:
        async with ShopUnitOfWork() as uow:
            return (await uow.products.get(ProductId(product))).name

    return run_async(read())


def ended(authorization: str) -> bool:
    """Whether the authorization ``<id>.<code>`` names is over: spent, or voided."""

    async def read() -> bool:
        async with connections.engine().connect() as connection:
            found = await connection.execute(
                select(authorizations.c.consumed_at).where(
                    authorizations.c.id == UUID(authorization.partition(".")[0])
                )
            )
            return found.scalar_one() is not None

    return run_async(read())


def code_of(response: Any) -> tuple[int, str]:  # noqa: ANN401 - a response.
    """The status and the error code of ``response``."""
    return response.status_code, response.json().get("code")


def test_a_resources_act_is_asked_confirmed_and_spent_once(alice: Shopper) -> None:
    """401 bare; 201 from the twin, in the server's words; ``<id>.<code>``; once.

    The twin answers what the server built from the stored product, not
    from the page; confirming with a code answers ``<id>.<code>``; the act
    passes with it, and its commit spends it; sent again, it is refused.
    """
    lamp = stock("Lamp")

    bare = alice.rename(lamp, "Desk lamp")
    asked = alice.ask(lamp, "Desk lamp")
    authorization_id = asked.json()["id"]
    confirmed = alice.confirm(authorization_id)
    authorization = confirmed.json()["authorization"]
    renamed = alice.rename(lamp, "Desk lamp", authorization)
    replayed = alice.rename(lamp, "Desk lamp", authorization)

    assert code_of(bare) == (401, "authorization_required")
    assert 'purpose="shop.rename_product"' in bare.headers["WWW-Authenticate"]
    assert asked.status_code == 201, asked.json()
    assert asked.headers["Cache-Control"] == "no-store"
    assert set(asked.json()) == {"id", "purpose", "shown", "expires_at", "methods"}
    assert asked.json()["purpose"] == "shop.rename_product"
    assert asked.json()["methods"] == ["otp"]  # As GET /authorizations/{id} says.
    assert asked.json()["shown"] == {
        "title": "Rename a product",
        "details": [
            {"label": "Product", "value": "Lamp"},
            {"label": "New name", "value": "Desk lamp"},
        ],
    }
    assert confirmed.status_code == 200, confirmed.json()
    head, dot, code = authorization.partition(".")
    assert (head, dot) == (authorization_id, ".")
    assert code
    assert renamed.status_code == 200, renamed.json()
    assert renamed.json()["name"] == "Desk lamp"
    assert ended(authorization)
    assert code_of(replayed) == (403, "action_not_authorized")
    assert name_of(lamp) == "Desk lamp"
    assert len(audited("auth.action_authorization_used")) == 1


def test_a_resources_act_refuses_an_authorization_of_other_arguments(
    alice: Shopper,
) -> None:
    """Asked for one name, sent with another: refused, and voided for both.

    Another product is other arguments too: an authorization of one
    product's rename does not rename another. Nothing is renamed, and
    nothing is spent: each authorization is voided.
    """
    lamp = stock("Lamp")
    chair = stock("Chair")

    authorization = alice.authorized(lamp, "Floor lamp")
    other_name = alice.rename(lamp, "Wall lamp", authorization)
    asked_name = alice.rename(lamp, "Floor lamp", authorization)
    elsewhere = alice.authorized(lamp, "Stool")
    other_product = alice.rename(chair, "Stool", elsewhere)

    assert code_of(other_name) == (403, "action_not_authorized")
    assert code_of(asked_name) == (403, "action_not_authorized")
    assert code_of(other_product) == (403, "action_not_authorized")
    assert (name_of(lamp), name_of(chair)) == ("Lamp", "Chair")
    assert ended(authorization)
    assert ended(elsewhere)
    assert audited("auth.action_authorization_used") == []


def test_a_use_case_is_authorized_through_its_copy_and_stays_as_it_was(
    alice: Shopper,
) -> None:
    """The copy spends it with its commit; the use case itself asks nothing (A13).

    The action takes the use case as its command, and shows the stored
    name, read in the route's own unit of work, which the twin lends it.
    The route that runs the use case itself, as the phone's does, needs no
    authorization.
    """
    lamp = stock("Lamp")
    act = f"/products/{lamp}/relabel"

    bare = alice.client.post(act, json={"name": "Desk lamp"}, headers=alice.headers)
    asked = alice.client.post(
        f"{act}/authorization", json={"name": "Desk lamp"}, headers=alice.headers
    )
    confirmed = alice.confirm(asked.json()["id"])
    authorization = confirmed.json()["authorization"]
    with_it = {**alice.headers, "Action-Authorization": authorization}
    relabelled = alice.client.post(act, json={"name": "Desk lamp"}, headers=with_it)
    replayed = alice.client.post(act, json={"name": "Desk lamp"}, headers=with_it)
    named = name_of(lamp)
    directly = alice.client.post(
        f"/products/{lamp}/relabel-directly",
        json={"name": "Floor lamp"},
        headers=alice.headers,
    )

    assert code_of(bare) == (401, "authorization_required")
    assert asked.status_code == 201, asked.json()
    assert asked.json()["purpose"] == "shop.relabel_product"
    assert asked.json()["shown"] == {
        "title": "Relabel a product",
        "details": [
            {"label": "Product", "value": "Lamp"},
            {"label": "New label", "value": "Desk lamp"},
        ],
    }
    assert asked.json()["methods"] == ["otp"]
    assert [type(uow) for uow in shop.SHOWN_IN] == [ShopUnitOfWork]
    assert shop.SHOWN_IN[0].committed == 0  # Read in, never written through.
    assert relabelled.status_code == 200, relabelled.json()
    assert named == "Desk lamp"
    assert ended(authorization)
    assert code_of(replayed) == (403, "action_not_authorized")
    assert directly.status_code == 200, directly.json()
    assert name_of(lamp) == "Floor lamp"
    assert len(audited("auth.action_authorization_used")) == 1


def test_the_helpers_authorize_an_act_with_a_code_or_as_its_phone_would(
    alice: Shopper,
) -> None:
    """``authorize_act`` asks the twin and confirms; ``confirm_authorization`` too.

    With the user's app, through the confirm route; without, as proving
    what the act asks, in the test. Each answer is spent by the act once.
    """
    lamp = stock("Lamp")
    act = f"/products/{lamp}/relabel"
    client = alice.client
    client.headers.update(alice.headers)
    alice.clock.tick(30)  # The enrolment used this step's code.
    app = Authenticator(alice.secret)

    with_a_code = authorize_act(client, act, app, json={"name": "Desk lamp"})
    first = client.post(
        act, json={"name": "Desk lamp"}, headers={"Action-Authorization": with_a_code}
    )
    without = authorize_act(client, act, json={"name": "Floor lamp"})
    second = client.post(
        act, json={"name": "Floor lamp"}, headers={"Action-Authorization": without}
    )
    asked = client.post(f"{act}/authorization", json={"name": "Wall lamp"})
    confirmed = confirm_authorization(client, asked.json()["id"])
    third = client.post(
        act, json={"name": "Wall lamp"}, headers={"Action-Authorization": confirmed}
    )
    with pytest.raises(UnexpectedResponse, match=r"^Reading the authorization: 404"):
        confirm_authorization(client, str(uuid4()))
    with pytest.raises(UnexpectedResponse, match=r"^Asking for an authorization"):
        authorize_act(client, f"/products/{uuid4()}/relabel", json={"name": "x"})
    [confirmer] = [
        each for each in registry if isinstance(each, AuthorizationConfirmer)
    ]
    with pytest.raises(AuthorizationNotFound):
        run_async(confirmer.confirm_test_authorization(uuid4(), ["otp"]))

    assert [first.status_code, second.status_code, third.status_code] == [200] * 3
    assert name_of(lamp) == "Wall lamp"
    assert all(map(ended, (with_a_code, without, confirmed)))
    assert [row["details"]["methods"] for row in audited("auth.action_authorized")] == [
        ["otp"],
        ["otp"],
        ["otp"],
    ]
