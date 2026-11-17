"""An act binds the version its page read, not the record as its twin reads it.

User report A25 (ADR 0178). A reviewer opened file A, the company
replaced it with B under the same name, and the reviewer approved: the
twin bound B, the phone showed the same lines for A and B, and B was
approved. A page that sends ``If-Match`` with the ``ETag`` it read now
gets ``412`` at the twin, before anything is stored or the phone is
asked; the act's digest covers the versions the twin held, so an act
without the same ``If-Match`` is refused, as any other arguments are. A
request without ``If-Match`` is bound as in 2.7.
"""

from collections.abc import Iterator
from types import ModuleType
from typing import Any, Final
from uuid import UUID

import pytest
from sqlalchemy import update

from domainkit.clock import FrozenClock
from domainkit.conf import override_settings
from domainkit.contrib.auth.actions import Action, Shown, register_action
from domainkit.db import connections
from domainkit.exceptions import ImproperlyConfigured
from domainkit.management.base import run_async
from domainkit.testing import UnexpectedResponse, authorize_act
from domainkit_tests import sample_action_resources as shop
from domainkit_tests.conftest import Api
from domainkit_tests.sample_domains.shop.tables import products
from domainkit_tests.support.auth import audited, create_user, grant
from domainkit_tests.test_action_authorisations import rows
from domainkit_tests.test_action_authorisations_resources import (
    ROUTES,
    Shopper,
    code_of,
    ended,
    frozen,
    stock,
)

__all__ = ["frozen"]  # The fixture the tests take.

FILES: Final = "domainkit_tests.sample_precondition_routes.router"
WEB: Final = {"channel": "web"}


class Reviewer(Shopper):
    """Alice, who reads a record's page, then asks to act on it, and acts."""

    def read(self, url: str) -> str:
        """The ``ETag`` of the page at ``url``, as the reviewer's page read it."""
        page = self.client.get(url, headers=self.headers)
        assert page.status_code == 200, page.json()
        etag: str = page.headers["etag"]
        return etag

    def asking(
        self,
        url: str,
        if_match: str | None = None,
        json: Any = None,  # noqa: ANN401
    ) -> Any:  # noqa: ANN401 - a response.
        """The twin of the act at ``url``, with ``If-Match`` if given."""
        headers = dict(self.headers)
        if if_match is not None:
            headers["If-Match"] = if_match
        return self.client.post(f"{url}/authorization", json=json, headers=headers)

    def authorizing(
        self,
        url: str,
        if_match: str | None = None,
        json: Any = None,  # noqa: ANN401
    ) -> str:
        """``<id>.<code>``: a confirmed authorization of the act at ``url``."""
        asked = self.asking(url, if_match, json)
        assert asked.status_code == 201, asked.json()
        confirmed = self.confirm(asked.json()["id"])
        assert confirmed.status_code == 200, confirmed.json()
        authorization: str = confirmed.json()["authorization"]
        return authorization

    def act(
        self,
        url: str,
        authorization: str,
        if_match: str | None = None,
        json: Any = None,  # noqa: ANN401 - the act's body.
    ) -> Any:  # noqa: ANN401 - a response.
        """The act at ``url``, with ``authorization``, and ``If-Match`` if given."""
        headers = {**self.headers, "Action-Authorization": authorization}
        if if_match is not None:
            headers["If-Match"] = if_match
        return self.client.post(url, json=json, headers=headers)


@pytest.fixture
def alice(identity: Api, frozen: FrozenClock) -> Iterator[Reviewer]:
    """Alice's session at the products, which publishes one as it reads."""
    shop.PUBLISHED.clear()
    with override_settings(ROOT_ROUTER=ROUTES):
        register_action(shop.PUBLISH_PRODUCT, domain="shop")
        create_user("alice")
        grant("alice", "shop.view_product", "shop.change_product")
        yield Reviewer(identity(), frozen)
    shop.PUBLISHED.clear()


@pytest.fixture
def files() -> Iterator[ModuleType]:
    """The routes that approve files: a resource's, and the project's own."""
    from domainkit_tests import sample_precondition_routes

    sample_precondition_routes.APPROVED.clear()
    sample_precondition_routes.RACING[0] = False
    yield sample_precondition_routes
    sample_precondition_routes.APPROVED.clear()
    sample_precondition_routes.RACING[0] = False


@pytest.fixture
def reviewer(
    identity: Api, frozen: FrozenClock, files: ModuleType
) -> Iterator[Reviewer]:
    """Alice's session at the files, the approvals registered."""
    with override_settings(ROOT_ROUTER=FILES):
        register_action(files.APPROVE_FILE, domain="shop")
        register_action(files.APPROVE_OWN, domain="shop")
        create_user("alice")
        grant("alice", "shop.view_product", "shop.change_product")
        yield Reviewer(identity(), frozen)


def replaced(product: UUID) -> None:
    """The company replaces the file under the same name: a new version, alike."""

    async def bump() -> None:
        async with connections.engine().begin() as connection:
            await connection.execute(
                update(products)
                .where(products.c.id == product)
                .values(version=products.c.version + 1)
            )

    run_async(bump())


def reasons() -> list[str]:
    """The reasons of the refused authorizations the trail kept, in order."""
    return [row["details"]["reason"] for row in audited("auth.authorization")]


# A resource's act ---------------------------------------------------------------


def test_a_twin_refuses_a_file_replaced_since_the_page_read_it(
    alice: Reviewer,
) -> None:
    """The project's attack: A read, B stored under its name, A's tag at the twin.

    Before 2.8 the twin read no ``If-Match``: it bound B, the phone showed
    the same lines for A and B, and B was published. Now ``412``, and
    nothing is stored for the phone to show; read again, the page asks.
    """
    lamp = stock("contract.pdf")
    page = f"/products/{lamp}"
    etag = alice.read(page)
    replaced(lamp)

    refused = alice.asking(f"{page}/publish", etag, WEB)
    stored = rows()
    asked = alice.asking(f"{page}/publish", alice.read(page), WEB)

    assert code_of(refused) == (412, "precondition_failed")
    assert stored == []
    assert asked.status_code == 201, asked.json()


@pytest.mark.parametrize("sent", ['"abc"', "abc", '"1", "2"'])
def test_a_twin_refuses_a_tag_that_names_no_version_it_has(
    alice: Reviewer, sent: str
) -> None:
    """A tag this API did not make, a malformed one, or none current: ``412``."""
    lamp = stock("Lamp")
    replaced(lamp)
    replaced(lamp)  # Version 3.

    refused = alice.asking(f"/products/{lamp}/publish", sent, WEB)

    assert code_of(refused) == (412, "precondition_failed")
    assert rows() == []


def test_a_twin_refuses_an_if_match_longer_than_a_route_reads(
    alice: Reviewer,
) -> None:
    """As the act refuses it: ``422``, nothing stored."""
    lamp = stock("Lamp")

    refused = alice.asking(f"/products/{lamp}/publish", '"1"' + " " * 4096, WEB)

    assert refused.status_code == 422, refused.json()
    assert rows() == []


@pytest.mark.parametrize("sent", [None, "*", '"2"', 'W/"2-0123456789abcdef"'])
def test_an_act_without_the_if_match_its_twin_held_is_refused(
    alice: Reviewer, sent: str | None
) -> None:
    """Asked with version 1, sent with none, any, or another: refused, voided."""
    lamp = stock("Lamp")
    url = f"/products/{lamp}/publish"
    authorization = alice.authorizing(url, alice.read(f"/products/{lamp}"), WEB)

    refused = alice.act(url, authorization, sent, WEB)

    assert code_of(refused) == (403, "action_not_authorized")
    assert ended(authorization)
    assert reasons() == ["changed"]
    assert shop.PUBLISHED == []


def test_an_act_with_the_version_its_twin_held_passes_however_it_is_written(
    alice: Reviewer,
) -> None:
    """``W/"1-…"`` at the twin, ``"1"`` at the act: one version, compared by it."""
    lamp = stock("Lamp")
    url = f"/products/{lamp}/publish"
    etag = alice.read(f"/products/{lamp}")
    authorization = alice.authorizing(url, etag, WEB)

    published = alice.act(url, authorization, '"1"', WEB)

    assert etag.startswith('W/"1-')
    assert published.status_code == 200, published.json()
    assert list(shop.PUBLISHED) == [(lamp, "Lamp")]
    assert ended(authorization)


def test_a_file_replaced_after_the_twin_is_refused_at_the_act(
    alice: Reviewer,
) -> None:
    """Asked and confirmed for A, B stored, sent with A's tag: ``412``, voided.

    ``binds`` loads the file where the act holds it to the tag: the file
    will never be at that version again, so the authorization is voided,
    as for a record that reads otherwise.
    """
    lamp = stock("Lamp")
    url = f"/products/{lamp}/publish"
    etag = alice.read(f"/products/{lamp}")
    authorization = alice.authorizing(url, etag, WEB)
    replaced(lamp)

    refused = alice.act(url, authorization, etag, WEB)

    assert code_of(refused) == (412, "precondition_failed")
    assert ended(authorization)
    assert reasons() == ["record_changed"]
    assert shop.PUBLISHED == []


@pytest.mark.parametrize("sent", [None, "current"])
def test_without_if_match_at_the_twin_an_act_is_bound_as_in_2_7(
    alice: Reviewer, sent: str | None
) -> None:
    """No tag at the twin: the digest is 2.7's, and the act may hold its write.

    An authorization stored before 2.8 holds as it did; a client that
    sends ``If-Match`` to the act alone, as 2.7 read it, is not refused.
    """
    lamp = stock("Lamp")
    url = f"/products/{lamp}/publish"
    authorization = alice.authorizing(url, None, WEB)
    etag = alice.read(f"/products/{lamp}") if sent else None

    published = alice.act(url, authorization, etag, WEB)

    assert published.status_code == 200, published.json()
    [row] = rows()
    command = shop.publish_product.command(product_id=lamp, channel="web")
    assert row["digest"] == shop.PUBLISH_PRODUCT.digest_of(command, row["bound"])


def test_without_if_match_at_the_twin_a_stale_tag_at_the_act_is_412_and_kept(
    alice: Reviewer,
) -> None:
    """As in 2.7: the act's own tag holds its write, and voids nothing."""
    lamp = stock("Lamp")
    url = f"/products/{lamp}/publish"
    etag = alice.read(f"/products/{lamp}")
    authorization = alice.authorizing(url, None, WEB)
    replaced(lamp)

    refused = alice.act(url, authorization, etag, WEB)

    assert code_of(refused) == (412, "precondition_failed")
    assert not ended(authorization)


def test_the_digest_covers_the_versions_the_twin_held(alice: Reviewer) -> None:
    """The SHA-256 of the command, what ``binds`` read, and the versions, sorted."""
    lamp = stock("Lamp")

    asked = alice.asking(f"/products/{lamp}/publish", 'W/"7-0a1b", "1"', WEB)

    assert asked.status_code == 201, asked.json()
    [row] = rows()
    command = shop.publish_product.command(product_id=lamp, channel="web")
    held = shop.PUBLISH_PRODUCT.digest_of(command, row["bound"], {1, 7})
    assert row["digest"] == held
    assert held != shop.PUBLISH_PRODUCT.digest_of(command, row["bound"])
    assert held != shop.PUBLISH_PRODUCT.digest_of(command, row["bound"], {1})
    assert held != shop.PUBLISH_PRODUCT.digest_of(command, None, {1, 7})


def test_a_twin_documents_the_if_match_it_reads(alice: Reviewer) -> None:
    """The twin of an item's act names ``If-Match``, optional, and ``412``."""
    schema = alice.client.get("/openapi.json").json()
    twin = schema["paths"]["/products/{product_id}/publish/authorization"]["post"]

    [header] = [each for each in twin["parameters"] if each["in"] == "header"]
    assert header["name"] == "If-Match"
    assert not header.get("required")
    assert "412" in twin["responses"]
    assert "428" not in twin["responses"]


def test_the_helper_asks_with_the_tag_the_page_read(alice: Reviewer) -> None:
    """``authorize_act(..., if_match=...)`` sends it to the twin."""
    lamp = stock("Lamp")
    url = f"/products/{lamp}/publish"
    etag = alice.read(f"/products/{lamp}")
    alice.client.headers.update(alice.headers)

    authorization = authorize_act(alice.client, url, json=WEB, if_match=etag)
    published = alice.act(url, authorization, etag, WEB)
    replaced(lamp)

    assert published.status_code == 200, published.json()
    with pytest.raises(UnexpectedResponse, match="412"):
        authorize_act(alice.client, url, json=WEB, if_match=etag)


# A resource's act that asks for If-Match, and acts of the project's own ----------


def test_a_file_replaced_while_the_twin_reads_it_is_refused_there(
    reviewer: Reviewer, files: ModuleType
) -> None:
    """Replaced between the twin's reach and ``binds``: ``412`` where binds reads."""
    lamp = stock("contract.pdf")
    etag = reviewer.read(f"/files/{lamp}")
    files.RACING[0] = True

    refused = reviewer.asking(f"/files/{lamp}/approve", etag)

    assert code_of(refused) == (412, "precondition_failed")
    assert rows() == []


@pytest.mark.parametrize("sent", [None, "*"])
def test_an_action_that_asks_for_if_match_refuses_a_twin_without_one(
    reviewer: Reviewer, sent: str | None
) -> None:
    """``Action(require_if_match=True)``: ``428`` before anything is stored."""
    lamp = stock("contract.pdf")

    refused = reviewer.asking(f"/files/{lamp}/approve", sent)

    assert code_of(refused) == (428, "precondition_required")
    assert rows() == []


def test_an_action_that_asks_for_if_match_refuses_an_act_without_one_and_keeps_it(
    reviewer: Reviewer, files: ModuleType
) -> None:
    """``428`` at the act, voiding nothing: sent with the tag, it is made."""
    lamp = stock("contract.pdf")
    url = f"/files/{lamp}/approve"
    etag = reviewer.read(f"/files/{lamp}")
    authorization = reviewer.authorizing(url, etag)

    refused = reviewer.act(url, authorization)
    kept = not ended(authorization)
    approved = reviewer.act(url, authorization, etag)

    assert code_of(refused) == (428, "precondition_required")
    assert kept
    assert approved.status_code == 200, approved.json()
    assert list(files.APPROVED) == [(lamp, 1)]


def test_a_projects_own_twin_refuses_a_file_replaced_since_the_page_read_it(
    reviewer: Reviewer, files: ModuleType
) -> None:
    """The attack on routes of the project's own: ``412`` at its twin."""
    lamp = stock("contract.pdf")
    etag = reviewer.read(f"/files/{lamp}")
    replaced(lamp)

    refused = reviewer.asking(f"/own/{lamp}/approve", etag)

    assert code_of(refused) == (412, "precondition_failed")
    assert rows() == []
    assert files.APPROVED == []


@pytest.mark.parametrize(
    ("sent", "status"), [("none", 403), ("another", 403), ("same", 204)]
)
def test_a_projects_own_act_holds_the_if_match_its_twin_held(
    reviewer: Reviewer, files: ModuleType, sent: str, status: int
) -> None:
    """Without it, or with another: refused and voided; with it: made."""
    lamp = stock("contract.pdf")
    url = f"/own/{lamp}/approve"
    etag = reviewer.read(f"/files/{lamp}")
    authorization = reviewer.authorizing(url, etag)
    tags = {"none": None, "another": '"2"', "same": etag}

    made = reviewer.act(url, authorization, tags[sent])

    assert made.status_code == status, made.text
    assert ended(authorization)
    assert list(files.APPROVED) == ([(lamp, 1)] if status == 204 else [])


def test_an_authorization_asked_without_if_match_is_refused_once_the_action_asks(
    reviewer: Reviewer, files: ModuleType
) -> None:
    """Asked before ``require_if_match``: no version held, refused, voided."""
    lamp = stock("contract.pdf")
    etag = reviewer.read(f"/files/{lamp}")
    authorization = reviewer.authorizing(f"/plain/{lamp}/approve")

    refused = reviewer.act(f"/requiring/{lamp}/approve", authorization, etag)

    assert code_of(refused) == (403, "action_not_authorized")
    assert ended(authorization)
    assert files.APPROVED == []


def test_require_if_match_is_true_or_false() -> None:
    """A truthy value of another kind is a mistake, refused when declared."""
    with pytest.raises(ImproperlyConfigured, match="require_if_match is True or False"):
        Action(
            "shop.rename_product",
            command=shop.RenameProduct,
            subject=lambda command: f"shop.product:{command.product_id}",
            show=lambda command: Shown("Rename a product", {}),
            require_if_match="yes",  # type: ignore[arg-type]
        )
