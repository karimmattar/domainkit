"""An act binds the record it decides on, not its command alone (user report A24).

A command names the record by its id, so an authorization confirmed for a
product as it read, or a page as it read, decided on whatever was stored
when the act landed: a rename, or a page rewritten, between the screen and
the act went through. ``Action(binds=...)`` reads the record in the act's
unit of work where ``show`` reads, and again when the act opens its unit
of work; the digest covers what it read, so the phone signs it too, and
another value refuses the act (``409 action_record_changed``) and voids
the authorization.
"""

import io
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Final
from uuid import UUID, uuid4

import pytest
from sqlalchemy import update

from domainkit import setup
from domainkit.apps import registry
from domainkit.clock import FrozenClock
from domainkit.conf import override_settings
from domainkit.contrib.auth.actions import (
    Action,
    ActionRecordChanged,
    Shown,
    register_action,
)
from domainkit.contrib.auth.tables import authorizations
from domainkit.db import connections
from domainkit.ddd import Command, EventBus, UnitOfWork, fingerprint
from domainkit.exceptions import ImproperlyConfigured
from domainkit.management import call_command
from domainkit.management.base import run_async
from domainkit_tests import sample_action_resources as shop
from domainkit_tests import sample_bound_action_routes as pages
from domainkit_tests.conftest import ALPHA, AUTH, CONTENTTYPES, FAST_HASHER, Api
from domainkit_tests.sample_domains.shop.models import ProductId
from domainkit_tests.support.auth import audited, bearer, create_user, grant, login
from domainkit_tests.test_action_authorisations import rows
from domainkit_tests.test_action_authorisations_resources import (
    ROUTES,
    Shopper,
    code_of,
    ended,
    frozen,
    stock,
)
from domainkit_tests.test_auth_stepup import enrol, now_code

__all__ = ["frozen"]  # The fixture the tests take.

PAGES: Final = "domainkit_tests.sample_bound_action_routes.router"


class Publisher(Shopper):
    """Alice, who publishes products, and asks for an authorization of it."""

    def ask_to_publish(self, product: UUID) -> Any:  # noqa: ANN401 - a response.
        """The twin: ask for an authorization of publishing ``product`` on the web."""
        return self.client.post(
            f"/products/{product}/publish/authorization",
            json={"channel": "web"},
            headers=self.headers,
        )

    def publishing(self, product: UUID) -> str:
        """``<id>.<code>``: a confirmed authorization of publishing ``product``."""
        asked = self.ask_to_publish(product)
        assert asked.status_code == 201, asked.json()
        confirmed = self.confirm(asked.json()["id"])
        assert confirmed.status_code == 200, confirmed.json()
        authorization: str = confirmed.json()["authorization"]
        return authorization

    def publish(self, product: UUID, authorization: str) -> Any:  # noqa: ANN401
        """The act: publish ``product`` on the web, with ``authorization``."""
        return self.client.post(
            f"/products/{product}/publish",
            json={"channel": "web"},
            headers={**self.headers, "Action-Authorization": authorization},
        )


@pytest.fixture
def alice(identity: Api, frozen: FrozenClock) -> Iterator[Publisher]:
    """Alice's session at the products, the acts registered."""
    for kept in (shop.SHOWN_IN, shop.BOUND_IN, shop.PUBLISHED):
        kept.clear()
    with override_settings(ROOT_ROUTER=ROUTES):
        register_action(shop.RENAME_PRODUCT, domain="shop")
        register_action(shop.RELABEL_PRODUCT, domain="shop")
        register_action(shop.PUBLISH_PRODUCT, domain="shop")
        create_user("alice")
        grant("alice", "shop.view_product", "shop.change_product")
        yield Publisher(identity(), frozen)
    for kept in (shop.SHOWN_IN, shop.BOUND_IN, shop.PUBLISHED):
        kept.clear()


def rename(product: UUID, name: str) -> None:
    """Rename ``product`` as someone else does, between the screen and the act."""

    async def change() -> None:
        async with shop.ShopUnitOfWork() as uow:
            stored = await uow.products.get(ProductId(product))
            stored.name = name
            await uow.commit()

    run_async(change())


def reasons() -> list[str]:
    """The reasons of the refused authorizations the trail kept, in order."""
    return [row["details"]["reason"] for row in audited("auth.authorization")]


def test_an_act_refuses_its_record_changed_since_the_user_was_shown_it(
    alice: Publisher,
) -> None:
    """Confirmed for "Lamp", sent once it is "Lava lamp": refused, voided, unpublished.

    Before 2.7 the act bound its command alone, ``{"product_id",
    "channel"}``, and published whatever the product was called by then.
    Asked again, the screen shows the product as it is now, and that
    authorization publishes it.
    """
    lamp = stock("Lamp")

    authorization = alice.publishing(lamp)
    rename(lamp, "Lava lamp")
    refused = alice.publish(lamp, authorization)
    again = alice.ask_to_publish(lamp)
    confirmed = alice.confirm(again.json()["id"]).json()["authorization"]
    published = alice.publish(lamp, confirmed)

    assert code_of(refused) == (409, "action_record_changed")
    assert ended(authorization)
    assert reasons() == ["record_changed"]
    assert again.json()["shown"]["details"][0] == {
        "label": "Product",
        "value": "Lava lamp",
    }
    assert published.status_code == 200, published.json()
    assert list(shop.PUBLISHED) == [(lamp, "Lava lamp")]
    assert len(audited("auth.action_authorization_used")) == 1


def test_the_digest_covers_the_record_read_where_show_reads_and_again_at_the_act(
    alice: Publisher,
) -> None:
    """One read with ``show`` at the twin, one when the act opens its unit of work.

    The row keeps the SHA-256 of what ``binds`` read; the digest, which
    the phone signs, is of the command and that, not the command alone.
    """
    lamp = stock("Lamp")

    authorization = alice.publishing(lamp)
    published = alice.publish(lamp, authorization)

    assert published.status_code == 200, published.json()
    assert list(shop.PUBLISHED) == [(lamp, "Lamp")]
    asked, act = shop.BOUND_IN
    [shown] = shop.SHOWN_IN
    assert asked.products is shown.products  # One unit of work at the twin.
    assert act.products is not asked.products  # The act's own, opened by it.
    [row] = rows()
    command = shop.publish_product.command(product_id=lamp, channel="web")
    plain = fingerprint(
        "shop.publish_product", {"product_id": str(lamp), "channel": "web"}
    )
    assert row["bound"] == fingerprint_of({"name": "Lamp", "version": version_of(lamp)})
    assert row["digest"] == shop.PUBLISH_PRODUCT.digest_of(command, row["bound"])
    assert row["digest"] != plain == shop.PUBLISH_PRODUCT.digest_of(command)


def version_of(product: UUID) -> int:
    """The stored version of ``product``."""

    async def read() -> int:
        async with shop.ShopUnitOfWork() as uow:
            return (await uow.products.get(ProductId(product))).version

    return run_async(read())


def fingerprint_of(value: object) -> str:
    """The SHA-256 of ``value`` as canonical JSON, as the row keeps what is bound."""
    import hashlib
    import json

    text = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(text.encode()).hexdigest()


def test_a_bound_value_changed_in_the_row_fails_its_code(alice: Publisher) -> None:
    """The code covers the digest, which covers what is bound: a row changed fails."""
    lamp = stock("Lamp")
    authorization = alice.publishing(lamp)
    rename(lamp, "Lava lamp")
    now_bound = fingerprint_of({"name": "Lava lamp", "version": version_of(lamp)})

    async def rebind() -> None:
        async with connections.engine().begin() as connection:
            await connection.execute(update(authorizations).values(bound=now_bound))

    run_async(rebind())
    refused = alice.publish(lamp, authorization)

    assert code_of(refused) == (403, "action_not_authorized")
    assert shop.PUBLISHED == []


# Kept elsewhere: spent before the act, read when the act opens its unit of work.


class Editor:
    """Alice at the pages, with an authenticator app."""

    def __init__(self, api: Api, clock: FrozenClock) -> None:
        """Log alice in and turn her second factor on."""
        self.client = api()
        self.clock = clock
        self.headers = bearer(login(self.client, "alice")["access_token"])
        self.secret = enrol(self.client, self.headers, clock)

    def authorized(self, page: UUID) -> str:
        """``<id>.<code>``: a confirmed authorization of publishing ``page``."""
        asked = self.client.post(
            f"/pages/{page}/publish/authorization", headers=self.headers
        )
        assert asked.status_code == 201, asked.json()
        confirmed = self.client.post(
            f"/auth/authorizations/{asked.json()['id']}/confirm",
            json={"code": now_code(self.secret, self.clock)},
            headers=self.headers,
        )
        assert confirmed.status_code == 200, confirmed.json()
        authorization: str = confirmed.json()["authorization"]
        return authorization

    def publish(self, page: UUID, authorization: str, way: str = "") -> Any:  # noqa: ANN401
        """Publish ``page`` the route ``way`` names, with ``authorization``."""
        return self.client.post(
            f"/pages/{page}/publish{way}",
            headers={**self.headers, "Action-Authorization": authorization},
        )


@pytest.fixture
def editor(identity: Api, frozen: FrozenClock) -> Iterator[Editor]:
    """Alice's session at the pages, the act registered."""
    pages.PAGES.clear()
    pages.PUBLISHED.clear()
    with override_settings(ROOT_ROUTER=PAGES):
        register_action(pages.PUBLISH_PAGE, domain="shop")
        create_user("alice")
        yield Editor(identity, frozen)
    pages.PAGES.clear()
    pages.PUBLISHED.clear()


def page(text: str) -> UUID:
    """A page reading ``text``; its id."""
    page_id = uuid4()
    pages.PAGES[page_id] = text
    return page_id


@pytest.mark.parametrize("way", ["", "/in-code", "/committed"])
def test_an_act_elsewhere_reads_its_record_in_its_own_unit_of_work(
    editor: Editor, way: str
) -> None:
    """Rewritten after the screen: refused, voided; as shown: published.

    A service spends it before the act, alone, then reads the page as its
    unit of work opens, or commits; code that spends it reads it in the
    unit of work it gives, first.
    """
    draft, final = page("Draft"), page("Final")
    stale = editor.authorized(draft)
    pages.PAGES[draft] = "Rewritten"
    fresh = editor.authorized(final)

    refused = editor.publish(draft, stale, way)
    published = editor.publish(final, fresh, way)

    assert code_of(refused) == (409, "action_record_changed")
    assert published.status_code == 204, published.text
    assert list(pages.PUBLISHED) == [(final, "Final")]
    assert ended(stale)
    assert ended(fresh)
    assert reasons() == ["record_changed"]


@pytest.mark.parametrize("way", ["/unopened", "/no-unit"])
def test_an_act_that_binds_needs_its_unit_of_work_and_must_open_it(
    editor: Editor, way: str, caplog: pytest.LogCaptureFixture
) -> None:
    """A service given none, or that never opens it, cannot read the record again.

    ``ImproperlyConfigured``: a ``500``, and the log says why.
    """
    draft = page("Draft")
    authorization = editor.authorized(draft)

    failed = editor.publish(draft, authorization, way)

    assert failed.status_code == 500
    assert "binds the record it decides on" in caplog.text


# Declaring it ------------------------------------------------------------------


class Note(Command):
    """Publish a note."""

    note_id: UUID


class Notes(UnitOfWork):
    """A note's text, in memory, and whether anything tried to commit."""

    def __init__(self) -> None:
        """One note."""
        super().__init__(events=EventBus())
        self.texts = {NOTE: "Hello"}

    async def _commit(self) -> None:
        """Nothing to write."""

    async def _rollback(self) -> None:
        """Nothing to undo."""


NOTE: Final = UUID("00000000-0000-4000-8000-000000000044")


def noting(binds: Any = None, show: Any = None) -> Action:  # noqa: ANN401 - functions.
    """An act on a note that binds what ``binds`` reads."""
    return Action(
        "notes.publish",
        command=Note,
        subject=lambda command: f"notes.note:{command.note_id}",
        show=show or (lambda command: Shown("Publish a note")),
        binds=binds,
    )


def test_binds_is_a_function_that_reads_what_json_holds_and_never_commits() -> None:
    """Refused when declared, read, or asked without the act's unit of work."""

    async def committing(command: Note, uow: Any) -> str:  # noqa: ANN401 - a uow.
        await uow.commit()
        return "never"

    def unwritable(command: Note, uow: Any) -> object:  # noqa: ANN401 - a uow.
        return object()

    def text(command: Note, uow: Notes) -> str:
        return uow.texts[command.note_id]

    command = Note(note_id=NOTE)
    with pytest.raises(ImproperlyConfigured, match="binds is a function"):
        noting(binds="version")
    with pytest.raises(ImproperlyConfigured, match=r"binds\(command, uow\) only reads"):
        run_async(noting(committing).bound_in(command, Notes()))
    with pytest.raises(ImproperlyConfigured, match="must answer what JSON holds"):
        run_async(noting(unwritable).bound_in(command, Notes()))
    with pytest.raises(ImproperlyConfigured, match="binds nothing"):
        run_async(noting().bound_in(command, Notes()))
    with pytest.raises(ImproperlyConfigured, match="binds reads in the act's unit"):
        run_async(noting(text).read(command))

    shown, bound = run_async(noting(text).read(command, Notes))
    assert (shown.title, bound) == ("Publish a note", fingerprint_of("Hello"))
    assert run_async(noting().read(command)) == (Shown("Publish a note"), None)
    assert ActionRecordChanged().status_code == 409


# The table, from the release before ---------------------------------------------


@contextmanager
def at_2_6() -> Iterator[None]:
    """The authorizations table as 2.6 had it: no ``bound``, no ``held_until``."""
    added = [authorizations.c.bound, authorizations.c.held_until]
    for column in added:
        authorizations._columns.remove(column)
    try:
        yield
    finally:
        for column in added:
            authorizations._columns.add(column)


def test_a_2_6_authorizations_table_gets_its_two_columns_from_the_next_revision(
    database_url: str, tmp_path: Path
) -> None:
    """``makemigrations`` adds the two nullable columns, nothing else."""
    registry.clear()
    with override_settings(
        INSTALLED_DOMAINS=[CONTENTTYPES, AUTH, ALPHA],
        DATABASES={"default": {"URL": database_url}},
        MIGRATIONS_DIR=tmp_path / "migrations",
        AUTH={"PASSWORD_HASHERS": [FAST_HASHER]},
    ):
        setup()
        with at_2_6():
            call_command("makemigrations", "-m", "at 2.6", stdout=io.StringIO())
            call_command("migrate", stdout=io.StringIO())
        output = io.StringIO()
        call_command("makemigrations", "-m", "bound and held", stdout=output)
        revision = Path(output.getvalue().split()[1].rstrip("."))
        call_command("migrate", stdout=io.StringIO())
        checked = io.StringIO()
        call_command("makemigrations", "--check", stdout=checked)
    registry.clear()

    upgrade = revision.read_text(encoding="utf-8").split("def downgrade")[0]
    assert upgrade.count("add_column(") == 2
    assert '"bound"' in upgrade
    assert '"held_until"' in upgrade
    assert "drop_" not in upgrade
    assert checked.getvalue() == "No changes detected.\n"
