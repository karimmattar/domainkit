"""An upload act, authorized from start to end by the real core (user report U3).

The twin of ``upload_route`` stores nothing, and the act stores the file
under a new name: an ``Action`` that declares nothing about the upload
binds it by its content, so the authorization the twin asked for is spent
by the act on the same file, and refused for another (ADR 0167).
"""

import sys
from collections.abc import Iterator
from dataclasses import dataclass, replace
from typing import Any, Final
from urllib.parse import quote

import pytest
from fastapi.testclient import TestClient

from domainkit.clock import FrozenClock
from domainkit.conf import override_settings
from domainkit.contrib.auth.actions import Action, Shown, register_action
from domainkit.ddd import Command
from domainkit.storage import StoredUpload
from domainkit_tests.conftest import Api
from domainkit_tests.sample_upload_action_resources import ATTACH_LEAFLET, LEAFLETS
from domainkit_tests.support.auth import bearer, create_user, grant, login
from domainkit_tests.support.documents import one_page
from domainkit_tests.test_action_authorisations_resources import (
    ended,
    frozen,  # noqa: F401 - fixture.
    name_of,
    stock,
)
from domainkit_tests.test_auth_stepup import enrol, now_code

ROUTES: Final = "domainkit_tests.sample_upload_action_resources.router"
LEAFLET: Final = one_page()
OTHER: Final = one_page("/Lang (ar)")


class Seller:
    """Alice, logged in with an authenticator app, who may change products."""

    def __init__(self, client: TestClient, clock: FrozenClock) -> None:
        """Log alice in and turn her second factor on."""
        self.client = client
        self.clock = clock
        self.headers = bearer(login(client, "alice")["access_token"])
        self.secret = enrol(client, self.headers, clock)

    def attach(self, product: object, file: bytes, authorization: str) -> Any:  # noqa: ANN401 - a response.
        """The act: send ``file`` as the leaflet, with ``authorization``."""
        headers = {
            **self.headers,
            "X-Filename": quote("leaflet.pdf"),
            "Action-Authorization": authorization,
        }
        return self.client.put(
            f"/products/{product}/leaflet", content=file, headers=headers
        )

    def authorized(self, product: object, file: bytes) -> str:
        """``<id>.<code>``: a confirmed authorization of attaching ``file``."""
        asked = self.client.post(
            f"/products/{product}/leaflet/authorization",
            content=file,
            headers={**self.headers, "X-Filename": "leaflet.pdf"},
        )
        assert asked.status_code == 201, asked.json()
        assert asked.json()["shown"]["details"] == [
            {"label": "File", "value": "leaflet.pdf"}
        ]
        confirmed = self.client.post(
            f"/auth/authorizations/{asked.json()['id']}/confirm",
            json={"code": now_code(self.secret, self.clock)},
            headers=self.headers,
        )
        assert confirmed.status_code == 200, confirmed.json()
        authorization: str = confirmed.json()["authorization"]
        return authorization


@pytest.fixture
def alice(identity: Api, frozen: FrozenClock) -> Iterator[Seller]:  # noqa: F811 - the fixture.
    """Alice's session at the products, the action registered."""
    LEAFLETS.files.clear()
    with override_settings(ROOT_ROUTER=ROUTES):
        register_action(ATTACH_LEAFLET, domain="shop")
        create_user("alice")
        grant("alice", "shop.view_product", "shop.change_product")
        yield Seller(identity(), frozen)
    LEAFLETS.files.clear()


def test_the_file_the_twin_was_asked_about_is_the_one_the_act_spends_on(
    alice: Seller,
) -> None:
    """The same bytes pass; others are refused, deleted, and void the authorization."""
    lamp = stock("Lamp")

    for_leaflet = alice.authorized(lamp, LEAFLET)
    other = alice.attach(lamp, OTHER, for_leaflet)
    stored_after_other = dict(LEAFLETS.files)
    again = alice.authorized(lamp, LEAFLET)
    attached = alice.attach(lamp, LEAFLET, again)

    assert (other.status_code, other.json()["code"]) == (403, "action_not_authorized")
    assert stored_after_other == {}
    assert ended(for_leaflet)
    assert attached.status_code == 200, attached.json()
    assert name_of(lamp) == "leaflet.pdf"
    assert ended(again)
    assert list(LEAFLETS.files.values()) == [LEAFLET]


class AttachMany(Command):
    """Attach several files at once."""

    product_id: str
    files: tuple[StoredUpload, ...]
    cover: StoredUpload | None = None


def test_an_action_binds_uploads_by_content_never_by_their_storage_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One, or a tuple of them; any other value as it is (ADR 0167).

    A process that never loaded the storage holds no upload, and loads
    nothing to look for one.
    """
    many = Action(
        "shop.attach_many",
        command=AttachMany,
        subject=lambda command: f"shop.product:{command.product_id}",
        show=lambda command: Shown("Attach files", {}),
    )
    first = StoredUpload("a/1.pdf", "application/pdf", 3, "a.pdf", "1" * 64)
    second = StoredUpload("a/2.pdf", "application/pdf", 4, "b.pdf", "2" * 64)
    twin = AttachMany(
        product_id="p",
        files=(replace(first, name=""), replace(second, name="")),
        cover=replace(first, name=""),
    )
    act = AttachMany(product_id="p", files=(first, second), cover=first)
    other = AttachMany(product_id="p", files=(first, replace(second, size=5)))

    arguments = many.arguments(act)
    assert arguments["files"][0] == {
        "content_type": "application/pdf",
        "size": 3,
        "filename": "a.pdf",
        "sha256": "1" * 64,
    }
    assert many.digest_of(twin) == many.digest_of(act) != many.digest_of(other)
    monkeypatch.delitem(sys.modules, "domainkit.storage.base")
    assert many.arguments(act)["files"][0]["name"] == "a/1.pdf"


@dataclass(frozen=True)
class Signed:
    """A file, and who signed it: a value inside a command."""

    signer: str
    file: StoredUpload


class AttachInside(Command):
    """Uploads held deep in a command."""

    product_id: str
    signed: Signed
    by_role: dict[str, StoredUpload]


def test_an_upload_anywhere_is_bound_by_content_one_without_a_digest_by_name() -> None:
    """In a value or a mapping too; with no SHA-256 there is no content to bind.

    A file A's twin then matched a file B's act of the same type, size and
    name (second review): such an upload keeps its storage's name.
    """
    inside = Action(
        "shop.attach_inside",
        command=AttachInside,
        subject=lambda command: f"shop.product:{command.product_id}",
        show=lambda command: Shown("Attach", {}),
    )
    stored = StoredUpload("a/1.pdf", "application/pdf", 3, "a.pdf", "1" * 64)
    unstored = replace(stored, name="")

    def command(file: StoredUpload) -> AttachInside:
        return AttachInside(
            product_id="p", signed=Signed("me", file), by_role={"buyer": file}
        )

    blank = replace(stored, sha256="")
    assert inside.digest_of(command(unstored)) == inside.digest_of(command(stored))
    assert inside.digest_of(command(replace(blank, name=""))) != inside.digest_of(
        command(replace(blank, name="a/2.pdf"))
    )
    assert inside.arguments(command(blank))["signed"]["file"]["name"] == "a/1.pdf"
