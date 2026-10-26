"""SEC-11a-4: ``flush`` keeps object grants while it deletes their objects.

A grant on one object goes with it: "a hard delete takes its grants: an
object with the same id starts bare" (``objectpermissions.deletions``).
Since 0.11 (DATA-12, ADR 0100) ``flush`` keeps the guarded tables, object
grants among them, unless ``--include-guarded``, and still empties the
tables of the objects those grants are about, behind the mappers, so no
grant goes with its object. Ids are then given out again: a serial id
restarts (``flush`` resets PostgreSQL's sequences; SQLite reuses the
rowid), and seeds and fixtures bring their own. The next object with a
flushed one's id inherits every grant on it, for users ``flush`` kept too:
Ada, who could see her own ticket 1, sees Bob's ticket 1 after a reset.
"""

import io
from collections.abc import Iterator
from pathlib import Path
from typing import Final

import pytest
from sqlalchemy import insert

from domainkit import setup
from domainkit.apps import registry
from domainkit.conf import override_settings
from domainkit.contrib.auth.backends import has_perm
from domainkit.contrib.objectpermissions import grant_permission, grants_on
from domainkit.contrib.objectpermissions.tables import object_permissions
from domainkit.db import connections
from domainkit.management import call_command
from domainkit.management.base import run_async
from domainkit_tests.conftest import ALPHA, AUTH, CONTENTTYPES, FAST_HASHER, SHOP
from domainkit_tests.sample_domains.oddities.models import Ticket
from domainkit_tests.sample_domains.oddities.tables import tickets
from domainkit_tests.test_data_commands import count, operator_rows
from domainkit_tests.test_object_permissions import BACKENDS, OBJECTPERMISSIONS, person

ODDITIES: Final = "domainkit_tests.sample_domains.oddities"
DOMAINS: Final = [
    CONTENTTYPES,
    AUTH,
    OBJECTPERMISSIONS,
    "domainkit.contrib.audit",
    SHOP,
    ALPHA,
    ODDITIES,
]
VIEW_TICKET: Final = "oddities.view_ticket"


@pytest.fixture
def project(database_url: str, tmp_path: Path) -> Iterator[str]:
    """Auth, object permissions, and tickets with serial ids, migrated."""
    registry.clear()
    with override_settings(
        INSTALLED_DOMAINS=DOMAINS,
        DATABASES={"default": {"URL": database_url}},
        MIGRATIONS_DIR=tmp_path / "migrations",
        AUTH={"PASSWORD_HASHERS": [FAST_HASHER], "BACKENDS": BACKENDS},
    ):
        setup()
        call_command("makemigrations", "-m", "init", stdout=io.StringIO())
        call_command("migrate", stdout=io.StringIO())
        yield database_url
    registry.clear()


def new_ticket(owner: str, code: str) -> Ticket:
    """Store a ticket; the database gives its id, as a serial column does."""

    async def store() -> Ticket:
        try:
            async with connections.engine().begin() as connection:
                made = await connection.execute(
                    insert(tickets).values(owner=owner, code=code)
                )
                (number,) = made.inserted_primary_key or (None,)
        finally:
            await connections.close_all()
        assert isinstance(number, int)
        return Ticket(id=number, owner=owner, code=code)

    return run_async(store())


def test_flushed_objects_take_their_grants_with_them(project: str) -> None:
    """After ``flush``, the next ticket with Ada's old ticket's id starts bare.

    The fixed code empties the grants on what it empties (or refuses to
    keep them); the branch hands Bob's ticket to Ada.
    """
    root = person("root", is_superuser=True)
    ada = person("ada")
    person("bob")
    adas = new_ticket("ada", "A-1")
    run_async(grant_permission(root, VIEW_TICKET, adas, user=ada.id))
    assert run_async(has_perm(ada, VIEW_TICKET, adas))

    call_command("flush", stdout=io.StringIO())  # Users and grants are kept.
    bobs = new_ticket("bob", "B-1")

    assert bobs.id == adas.id  # The id is given out again.
    assert not run_async(has_perm(ada, VIEW_TICKET, bobs)), (
        "Ada sees Bob's ticket through the grant on her flushed one: "
        f"{run_async(grants_on(bobs))}"
    )


def test_flush_says_which_grants_go_and_records_them(project: str) -> None:
    """The plan and the audit row count them; grants on kept objects stay.

    A grant on a user (``auth.user``) is about an object ``flush`` keeps,
    so it stays with it; with ``--include-guarded`` every grant goes, as
    the table is emptied.
    """
    root = person("root", is_superuser=True)
    ada = person("ada")
    bob = person("bob")
    run_async(
        grant_permission(root, VIEW_TICKET, new_ticket("ada", "A-1"), user=ada.id)
    )
    run_async(grant_permission(root, "auth.view_user", ada, user=bob.id))

    plan = io.StringIO()
    call_command("flush", "--dry-run", stdout=plan)
    call_command("flush", "--operator", "ops", stdout=io.StringIO())
    kept = count(object_permissions)
    everything = io.StringIO()
    call_command("flush", "--dry-run", "--include-guarded", stdout=everything)
    call_command(
        "flush", "--include-guarded", "--operator", "ops", stdout=io.StringIO()
    )

    line = "default: domainkit_object_permissions: kept (guarded)"
    assert f"{line}; delete 1 row about emptied tables\n" in plan.getvalue()
    assert kept == 1  # Bob's grant on Ada, who stays.
    assert "domainkit_object_permissions: delete 1 row\n" in everything.getvalue()
    assert count(object_permissions) == 0
    flushed, _ = operator_rows()
    assert flushed["details"]["about"] == {object_permissions.name: 1}
