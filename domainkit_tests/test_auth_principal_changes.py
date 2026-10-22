"""Changes that must reach kept principals: savepoints, bulk statements, stamps.

Each test makes a change the way a project could, and reads the principal
where a stale one would show: between a savepoint and the commit, after a
bulk statement, after the shared cache lost its stamps, after a cancelled
commit. Interleavings are forced with events, never with sleeps.
"""

from collections.abc import Awaitable, Callable
from datetime import timedelta
from typing import Final
from uuid import UUID, uuid4

import pytest
from fakeredis import FakeServer
from sqlalchemy import delete, insert, update

from domainkit.cache import caches
from domainkit.cache import redis as redis_cache
from domainkit.clock import freeze_time
from domainkit.contrib.auth import (
    AuthenticationFailed,
    AuthUnitOfWork,
    User,
    UserId,
    principals,
)
from domainkit.contrib.auth.principals import principal_cache, user_target
from domainkit.contrib.auth.tables import group_permissions, tokens, user_link_tables
from domainkit.contrib.auth.tokens import token_digest, utc_now
from domainkit.contrib.auth.users import get_user_table
from domainkit.contrib.contenttypes.tables import content_types
from domainkit.db import connections
from domainkit.ddd import F
from domainkit.management.base import run_async
from domainkit_tests.conftest import Api
from domainkit_tests.test_auth_http import bearer, create_user
from domainkit_tests.test_auth_principals import (
    STAMPED,
    credentials_of,
    editors_with,
    issue,
)
from domainkit_tests.test_auth_principals import shared as shared
from domainkit_tests.test_auth_principals import statements as statements

LONG: Final = {"TTL": timedelta(minutes=5)}
"""Principals kept long enough that only an invalidation drops them."""


async def outcome(raw: str) -> str:
    """``"served"``, or why the token ``raw`` is refused."""
    try:
        found = await credentials_of(headers=bearer(raw))
    except AuthenticationFailed as exc:
        return str(exc.detail)
    return "served" if found is not None else "anonymous"


# A savepoint's release is not the commit.


def test_a_change_in_a_savepoint_drops_principals_at_the_commit(api: Api) -> None:
    """Read between ``RELEASE SAVEPOINT`` and the commit, a principal is not kept.

    SQLAlchemy reports a savepoint's release as a commit (``after_commit``):
    dropping then, and forgetting the change, kept what a request read before
    the real commit.
    """
    create_user("alice")
    api(PRINCIPAL_CACHE=LONG)
    raw = run_async(issue("alice"))

    async def scenario() -> list[str]:
        seen = [await outcome(raw)]
        async with AuthUnitOfWork() as uow:
            user = await uow.users.get_by_natural_key("alice")
            assert user is not None
            async with uow.savepoint():
                user.set_active(False)
            seen.append(await outcome(raw))  # Another request, before the commit.
            await uow.commit()
        seen.append(await outcome(raw))
        return seen

    before, meanwhile, after = run_async(scenario())
    assert before == "served"
    # Whatever the database shows before the commit (PostgreSQL: the old row)...
    assert meanwhile in {"served", "User inactive or deleted."}
    assert after == "User inactive or deleted."  # ... is not kept past it.
    assert len(principal_cache) == 0


def test_a_revocation_before_a_savepoint_drops_its_principal_at_the_commit(
    api: Api,
) -> None:
    """A savepoint after a revocation (as batch routes open) waits for the commit."""
    create_user("alice")
    api(PRINCIPAL_CACHE=LONG)
    raw = run_async(issue("alice"))

    async def scenario() -> list[str]:
        seen = [await outcome(raw)]
        async with AuthUnitOfWork() as uow:
            await uow.tokens.revoke(token_digest(raw))
            async with uow.savepoint():
                pass
            seen.append(await outcome(raw))
            await uow.commit()
        seen.append(await outcome(raw))
        return seen

    before, meanwhile, after = run_async(scenario())
    assert before == "served"
    assert meanwhile in {"served", "Invalid token."}
    assert after == "Invalid token."
    assert len(principal_cache) == 0


# Bulk statements through a session.


async def deactivate_with_update_rows(uow: AuthUnitOfWork, alice: UUID) -> None:
    """``UserRepository.update_rows``: an ORM ``UPDATE``, no aggregate loaded."""
    await uow.users.update_rows(F("username") == "alice", is_active=False)


async def deactivate_with_orm_update(uow: AuthUnitOfWork, alice: UUID) -> None:
    """``update(User)`` executed by the session."""
    await uow.session.execute(
        update(User).where(User.id == alice).values(is_active=False)  # type: ignore[arg-type,misc]
    )


async def deactivate_with_core_update(uow: AuthUnitOfWork, alice: UUID) -> None:
    """``update(table)``, Core, executed by the session."""
    table = get_user_table()
    await uow.session.execute(
        update(table).where(table.c.id == alice).values(is_active=False)
    )


async def delete_with_delete_rows(uow: AuthUnitOfWork, alice: UUID) -> None:
    """``UserRepository.delete_rows``."""
    await uow.users.delete_rows(F("username") == "alice")


async def delete_tokens_with_core(uow: AuthUnitOfWork, alice: UUID) -> None:
    """A Core ``DELETE`` of the user's tokens, executed by the session."""
    await uow.session.execute(delete(tokens).where(tokens.c.user_id == alice))


@pytest.mark.parametrize(
    ("change", "refusal"),
    [
        (deactivate_with_update_rows, "User inactive or deleted."),
        (deactivate_with_orm_update, "User inactive or deleted."),
        (deactivate_with_core_update, "User inactive or deleted."),
        (delete_with_delete_rows, "User inactive or deleted."),
        (delete_tokens_with_core, "Invalid token."),
    ],
)
def test_bulk_statements_on_users_and_tokens_drop_principals(
    api: Api, change: Callable[[AuthUnitOfWork, UUID], Awaitable[None]], refusal: str
) -> None:
    """No mapper event runs for them: the session's statements are watched."""
    alice = create_user("alice")
    api(PRINCIPAL_CACHE=LONG)
    raw = run_async(issue("alice"))

    async def scenario() -> list[str]:
        seen = [await outcome(raw)]
        async with AuthUnitOfWork() as uow:
            await change(uow, alice)
            await uow.commit()
        seen.append(await outcome(raw))
        return seen

    assert run_async(scenario()) == ["served", refusal]


async def delete_group_with_delete_rows(uow: AuthUnitOfWork) -> None:
    """``GroupRepository.delete_rows``: the group, and its links by cascade."""
    await uow.groups.delete_rows(F("name") == "editors")


async def empty_group_with_core(uow: AuthUnitOfWork) -> None:
    """A Core ``DELETE`` of the group's permissions."""
    await uow.session.execute(delete(group_permissions))


async def rename_content_type_with_core(uow: AuthUnitOfWork) -> None:
    """A Core ``UPDATE`` of content types: their permissions' keys change."""
    await uow.session.execute(
        update(content_types)
        .where(content_types.c.domain == "shop")
        .values(domain="store")
    )


@pytest.mark.parametrize(
    "change",
    [
        delete_group_with_delete_rows,
        empty_group_with_core,
        rename_content_type_with_core,
    ],
)
def test_bulk_statements_on_grants_drop_principals(
    api: Api, change: Callable[[AuthUnitOfWork], Awaitable[None]]
) -> None:
    """A permission taken by a bulk statement is gone at the commit."""
    create_user("alice")
    api(PRINCIPAL_CACHE=LONG)
    run_async(editors_with("alice", "shop.view_product"))
    raw = run_async(issue("alice"))

    async def holds() -> bool:
        found = await credentials_of(headers=bearer(raw))
        assert found is not None
        return found.user.has_perm("shop.view_product")

    async def scenario() -> list[bool]:
        seen = [await holds()]
        async with AuthUnitOfWork() as uow:
            await change(uow)
            await uow.commit()
        seen.append(await holds())
        return seen

    assert run_async(scenario()) == [True, False]


def test_bulk_grants_reach_principals_at_once(api: Api) -> None:
    """A Core ``INSERT`` of a link gives the permission at the commit."""
    alice = create_user("alice")
    api(PRINCIPAL_CACHE=LONG)
    raw = run_async(issue("alice"))
    _, user_permissions = user_link_tables(get_user_table())

    async def holds() -> bool:
        found = await credentials_of(headers=bearer(raw))
        assert found is not None
        return found.user.has_perm("shop.view_product")

    async def scenario() -> list[bool]:
        seen = [await holds()]
        async with AuthUnitOfWork() as uow:
            permission = await uow.permissions.get("shop.view_product")
            await uow.session.execute(
                insert(user_permissions).values(
                    user_id=alice, permission_id=permission.id
                )
            )
            await uow.commit()
        seen.append(await holds())
        return seen

    assert run_async(scenario()) == [False, True]


def test_the_repositories_own_writes_keep_principals(
    api: Api, statements: list[str]
) -> None:
    """A last use, a login, a rehash, expired tokens cleared: nothing is read again."""
    alice = create_user("alice")
    api(PRINCIPAL_CACHE=LONG)
    raw = run_async(issue("alice"))
    bob = create_user("bob")

    async def writes() -> None:
        async with AuthUnitOfWork() as uow:
            user = await uow.users.get(UserId(bob))
            await uow.users.record_login(user)
            await uow.users.store_rehash(alice, user.password, user.password)
            await uow.tokens.touch_digest(token_digest(raw), utc_now())
            await uow.tokens.clear_expired()
            await uow.commit()

    with freeze_time("2026-05-01T10:00:00Z"):
        assert run_async(outcome(raw)) == "served"
        run_async(writes())
        statements.clear()
        assert run_async(outcome(raw)) == "served"
    assert "SELECT" not in statements
    assert len(principal_cache) == 1


# Stamps the shared cache lost.


@pytest.mark.parametrize("lost", ["everything", "the user's stamp"])
def test_lost_stamps_never_confirm_a_principal(
    api: Api, shared: FakeServer, lost: str
) -> None:
    """A stamp lost (a restart without persistence, an eviction, a flush) is a change.

    An entry kept while its user had no stamp recorded none; once the stamp
    a change wrote was lost, none matched again.
    """
    alice = create_user("alice")
    api(PRINCIPAL_CACHE=STAMPED)
    raw = run_async(issue("alice"))
    stamp = principals.stamp_key(user_target(alice))

    async def scenario() -> list[str]:
        seen = [await outcome(raw)]
        async with connections.engine().begin() as connection:  # Another process.
            table = get_user_table()
            await connection.execute(
                update(table).where(table.c.id == alice).values(is_active=False)
            )
        await principals.write_stamps("shared", [user_target(alice)])
        if lost == "everything":
            await caches["shared"].clear()
        else:
            await caches["shared"].delete(stamp)
        seen.append(await outcome(raw))
        return seen

    assert run_async(scenario()) == ["served", "User inactive or deleted."]


def test_guessed_tokens_write_no_stamps(api: Api, shared: FakeServer) -> None:
    """Stamps are given to credentials that exist: guesses only read."""
    create_user("alice")
    api(PRINCIPAL_CACHE=STAMPED)
    raw = run_async(issue("alice"))
    guesses = [f"dkt_{uuid4().hex}{uuid4().hex[:11]}" for _ in range(5)]

    async def scenario() -> list[str]:
        for guess in guesses:
            assert await outcome(guess) == "Invalid token."
        assert await outcome(raw) == "served"
        client = redis_cache.create_client(None)  # type: ignore[arg-type]
        return sorted(str(key) for key in await client.keys("*"))

    keys = run_async(scenario())
    digests = {token_digest(guess) for guess in guesses}
    assert keys  # alice's stamps, and the one of every principal.
    assert not [key for key in keys if any(digest in key for digest in digests)]
