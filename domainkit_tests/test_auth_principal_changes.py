"""Changes that must reach kept principals: savepoints, bulk statements, stamps.

Each test makes a change the way a project could, and reads the principal
where a stale one would show: between a savepoint and the commit, after a
bulk statement, after the shared cache lost its stamps, after a cancelled
commit. Interleavings are forced with events, never with sleeps.
"""

import asyncio
from collections.abc import Awaitable, Callable, Collection
from datetime import timedelta
from pathlib import Path
from typing import Any, Final
from uuid import UUID, uuid4

import pytest
from fakeredis import FakeServer
from sqlalchemy import delete, event, insert, select, update
from sqlalchemy.orm import Session

from domainkit.cache import Cache, CacheUnavailable, caches
from domainkit.cache import redis as redis_cache
from domainkit.clock import freeze_time
from domainkit.contrib.auth import (
    AuthenticationFailed,
    AuthUnitOfWork,
    User,
    UserId,
    principals,
)
from domainkit.contrib.auth.principals import (
    invalidate_after_commit,
    principal_cache,
    token_target,
    user_target,
)
from domainkit.contrib.auth.tables import group_permissions, tokens, user_link_tables
from domainkit.contrib.auth.tokens import TokenKind, token_digest, utc_now
from domainkit.contrib.auth.users import get_user_table
from domainkit.contrib.contenttypes.tables import content_types
from domainkit.db import connections
from domainkit.ddd import F
from domainkit.management.base import run_async
from domainkit.tasks import owned
from domainkit_tests.conftest import Api
from domainkit_tests.conftest import identity as identity
from domainkit_tests.fake_smtp import certificate
from domainkit_tests.test_auth_http import bearer, create_user, grant, login
from domainkit_tests.test_auth_principals import (
    CERTIFICATES,
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


# Stamps written whatever happens after the commit.


async def stamps_written() -> None:
    """Wait for the stamp writes commits left running."""
    await asyncio.gather(
        *(task for task in owned() if task.get_name() == "principals.stamps")
    )


async def token_gone(digest: str) -> bool:
    """Whether the deletion of token ``digest`` is committed, as others see."""
    async with connections.engine().connect() as connection:
        found = await connection.scalar(
            select(tokens.c.id).where(tokens.c.id == digest)
        )
    return found is None


def test_a_request_cancelled_after_its_commit_still_stamps(
    api: Api, shared: FakeServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A deadline or a client gone between the commit and the stamp loses nothing."""
    create_user("alice")
    api(PRINCIPAL_CACHE=STAMPED)
    raw = run_async(issue("alice"))
    digest = token_digest(raw)
    key = principals.stamp_key(token_target(digest))
    write = principals.write_stamps

    async def scenario() -> tuple[object, object]:
        entered, release = asyncio.Event(), asyncio.Event()

        async def gated(alias: str, targets: Collection[str]) -> None:
            if await token_gone(digest):  # The write after the commit.
                entered.set()
                await release.wait()
            await write(alias, targets)

        monkeypatch.setattr(principals, "write_stamps", gated)

        async def revoke() -> None:
            async with AuthUnitOfWork() as uow:
                await uow.tokens.revoke(digest)
                await uow.commit()

        request = asyncio.create_task(revoke())
        async with asyncio.timeout(30):  # A guard: the interleaving is the events'.
            await entered.wait()
        before = await caches["shared"].get(key)
        request.cancel()
        with pytest.raises(asyncio.CancelledError):
            await request
        release.set()
        await stamps_written()
        return before, await caches["shared"].get(key)

    before, after = run_async(scenario())
    assert isinstance(before, str)  # Stamped before the commit too.
    assert isinstance(after, str)
    assert after != before


def test_a_stamp_lost_after_the_commit_was_given_before_it(
    api: Api, shared: FakeServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The process ends between the commit and its stamp: the first one holds."""
    create_user("alice")
    api(PRINCIPAL_CACHE=STAMPED)
    raw = run_async(issue("alice"))
    digest = token_digest(raw)
    key = principals.stamp_key(token_target(digest))
    write = principals.write_stamps

    async def lost_after_the_commit(alias: str, targets: Collection[str]) -> None:
        if await token_gone(digest):
            raise CacheUnavailable("The process ended.")
        await write(alias, targets)

    async def scenario() -> tuple[object, object]:
        assert await outcome(raw) == "served"  # Kept, with the token's stamp.
        kept_with = await caches["shared"].get(key)
        monkeypatch.setattr(principals, "write_stamps", lost_after_the_commit)
        async with AuthUnitOfWork() as uow:
            await uow.tokens.revoke(digest)
            await uow.commit()
        await stamps_written()
        return kept_with, await caches["shared"].get(key)

    kept_with, now = run_async(scenario())
    assert isinstance(kept_with, str)
    assert isinstance(now, str)
    assert now != kept_with  # Another process's principal no longer matches.


def test_bare_session_commits_stamp(api: Api, shared: FakeServer) -> None:
    """A commit outside a unit of work stamps what it changed too."""
    alice = create_user("alice")
    api(PRINCIPAL_CACHE=STAMPED)
    raw = run_async(issue("alice"))
    key = principals.stamp_key(user_target(alice))

    async def scenario() -> tuple[object, object]:
        assert await outcome(raw) == "served"
        kept_with = await caches["shared"].get(key)
        async with connections.session() as session:
            user = await session.get(User, alice)
            assert user is not None
            user.set_active(False)
            await session.commit()
        await stamps_written()
        return kept_with, await caches["shared"].get(key)

    kept_with, now = run_async(scenario())
    assert isinstance(kept_with, str)
    assert isinstance(now, str)
    assert now != kept_with


# What kept principals cost.


def test_principals_share_their_grants(api: Api) -> None:
    """A user's credentials, and the users of a group, hold one copy of its grants."""
    alice, bob = create_user("alice"), create_user("bob")
    api(PRINCIPAL_CACHE=LONG)
    run_async(editors_with("alice", "shop.view_product", "shop.change_product"))
    grant("bob", "shop.view_product")

    async def join_bob() -> None:
        async with AuthUnitOfWork() as uow:
            user = await uow.users.get(UserId(bob))
            editors = await uow.groups.get_by_name("editors")
            assert editors is not None
            user.join(editors)
            await uow.commit()

    run_async(join_bob())
    raws = [run_async(issue(name)) for name in ("alice", "alice", "bob")]

    async def kept() -> list[Any]:
        for raw in raws:
            assert await outcome(raw) == "served"
        found = [principal_cache.get(("token", token_digest(raw)), 0) for raw in raws]
        return [entry.principal for entry in found if entry is not None]

    first, second, third = run_async(kept())
    editors = {each.name: each for each in first.groups}["editors"]
    assert {each.name: each for each in second.groups}["editors"] is editors
    assert {each.name: each for each in third.groups}["editors"] is editors
    (view,) = third.permissions  # Given directly, and through the group.
    assert view in editors.permissions
    assert view is next(each for each in editors.permissions if each == view)
    assert alice != bob


def test_sharing_grants_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    """Past ``INTERNED_MAX`` values, interning starts afresh, and still shares."""
    monkeypatch.setattr(principals, "INTERNED_MAX", 2)
    monkeypatch.setattr(principals, "_INTERNED", {})

    def value(n: int) -> tuple[int, str]:
        return (n, f"grant {n}")  # A new object each time.

    first = principals.shared(value(1))
    assert principals.shared(value(1)) is first
    principals.shared(value(2))
    principals.shared(value(3))  # A third value: interning restarts.
    again = principals.shared(value(1))
    assert again is not first
    assert principals.shared(value(1)) is again
    assert len(principals._INTERNED) == 2


def test_the_cache_keeps_at_most_100000_principals() -> None:
    """``MAX_ENTRIES``' ceiling: a hundred thousand, not a million."""
    from pydantic import ValidationError

    from domainkit.contrib.auth.settings import PrincipalCacheSettings

    assert PrincipalCacheSettings(MAX_ENTRIES=100_000).MAX_ENTRIES == 100_000
    with pytest.raises(ValidationError):
        PrincipalCacheSettings(MAX_ENTRIES=100_001)


def test_users_hold_a_bounded_number_of_live_tokens(identity: Api) -> None:
    """Past ``MAX_LIVE_TOKENS``, no more personal tokens: 409 ``too_many_tokens``."""
    create_user("alice")
    client = identity(MAX_LIVE_TOKENS=3)
    headers = bearer(login(client, "alice")["access_token"])  # One live token.

    def make() -> Any:  # noqa: ANN401 - a response.
        return client.post("/auth/tokens", json={"name": "ci"}, headers=headers)

    made = [make() for _ in range(3)]
    assert [each.status_code for each in made] == [201, 201, 409]
    assert made[2].json()["code"] == "too_many_tokens"
    revoked = client.delete(f"/auth/tokens/{made[0].json()['id']}", headers=headers)
    assert revoked.status_code == 204
    assert make().status_code == 201


@pytest.mark.parametrize("credential", ["token", "certificate subject"])
def test_the_first_stamps_are_read_before_a_connection_is_taken(
    api: Api,
    shared: FakeServer,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    credential: str,
) -> None:
    """``ALL``'s (and a token's): a slow shared cache holds no connection for them."""
    create_user("alice")
    if credential == "token":
        api(PRINCIPAL_CACHE=STAMPED)
        raw = run_async(issue("alice"))
        options: dict[str, Any] = {"headers": bearer(raw)}
    else:
        api(
            PRINCIPAL_CACHE=STAMPED,
            AUTHENTICATION_CLASSES=CERTIFICATES,
            CLIENT_CERT={"MATCH": "subject"},
        )
        path, _ = certificate(tmp_path, "alice")
        options = {"certificate_pem": path.read_text(encoding="ascii")}
    taken = [0]
    while_reading: list[int] = []
    read_holding: list[str] = []
    get_many = Cache.get_many

    async def counted(self: Cache, keys: Any, *args: Any) -> Any:  # noqa: ANN401
        keys = list(keys)
        while_reading.append(taken[0])
        if taken[0]:
            read_holding.extend(keys)
        return await get_many(self, keys, *args)

    def checked_out(*_: object) -> None:
        taken[0] += 1

    def checked_in(*_: object) -> None:
        taken[0] -= 1

    async def first() -> None:  # Its stamps given, then dropped here only.
        assert await credentials_of(**options) is not None
        principals.invalidate_all()

    run_async(first())
    monkeypatch.setattr(Cache, "get_many", counted)

    async def scenario() -> bool:
        engine = connections.engine().sync_engine
        event.listen(engine, "checkout", checked_out)
        event.listen(engine, "checkin", checked_in)
        try:
            return await credentials_of(**options) is not None
        finally:
            event.remove(engine, "checkout", checked_out)
            event.remove(engine, "checkin", checked_in)

    assert run_async(scenario())
    assert while_reading[0] == 0  # Read ahead, no connection held.
    assert max(while_reading) == 1  # The user's, between the two queries.
    assert principals.stamp_key(principals.ALL) not in read_holding  # Not again.
    if credential == "token":
        token = principals.stamp_key(token_target(token_digest(raw)))
        assert token not in read_holding
    assert len(principal_cache) == 1


def test_stamps_given_at_a_read_expire_as_the_others(
    api: Api, shared: FakeServer
) -> None:
    """A stamp given to an absent one lives ``STAMP_TTL``, and is as short."""
    alice = create_user("alice")
    api(PRINCIPAL_CACHE=STAMPED)
    raw = run_async(issue("alice"))

    async def scenario() -> tuple[object, int]:
        assert await outcome(raw) == "served"
        key = principals.stamp_key(user_target(alice))
        client = redis_cache.create_client(None)  # type: ignore[arg-type]
        (stored,) = [each for each in await client.keys("*") if key in str(each)]
        return await caches["shared"].get(key), await client.pttl(stored)

    stamp, lives = run_async(scenario())
    assert isinstance(stamp, str)
    assert len(stamp) == 16
    ttl = int(principals.STAMP_TTL.total_seconds() * 1000)
    assert ttl - 60_000 < lives <= ttl


def test_stamp_writes_are_bounded(
    api: Api,
    shared: FakeServer,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A write that hangs is given up after ``STAMP_WRITE_TIMEOUT``, and logged."""
    create_user("alice")
    api(PRINCIPAL_CACHE=STAMPED)
    raw = run_async(issue("alice"))
    monkeypatch.setattr(principals, "STAMP_WRITE_TIMEOUT", 0)

    async def scenario() -> None:
        never = asyncio.Event()

        async def hangs(alias: str, targets: Collection[str]) -> None:
            await never.wait()

        monkeypatch.setattr(principals, "write_stamps", hangs)
        async with AuthUnitOfWork() as uow:
            await uow.tokens.revoke(token_digest(raw))
            await uow.commit()
        await stamps_written()

    run_async(scenario())
    reasons = [m for m in caplog.messages if m.startswith("Could not stamp")]
    assert len(reasons) == 2  # Before the commit, and after it.
    assert all("(TimeoutError)" in each for each in reasons)


def test_live_tokens_are_counted_by_user_kind_and_expiry(api: Api) -> None:
    """``TokenStore.live_count``: a user's access tokens that still work."""
    alice, bob = create_user("alice"), create_user("bob")
    api()

    async def count() -> int:
        async with AuthUnitOfWork() as uow:
            return await uow.tokens.live_count(alice)

    async def give(user: UUID, kind: TokenKind, ttl: timedelta | None) -> None:
        async with AuthUnitOfWork() as uow:
            await uow.tokens.issue(user, kind=kind, ttl=ttl)
            await uow.commit()

    with freeze_time("2026-05-01T10:00:00Z") as clock:
        seen = [run_async(count())]
        run_async(give(alice, TokenKind.ACCESS, timedelta(hours=1)))
        run_async(give(alice, TokenKind.ACCESS, None))
        run_async(give(alice, TokenKind.SESSION, timedelta(hours=1)))
        run_async(give(alice, TokenKind.REFRESH, timedelta(hours=1)))
        run_async(give(bob, TokenKind.ACCESS, None))
        run_async(give(alice, TokenKind.ACCESS, timedelta(minutes=1)))
        seen.append(run_async(count()))
        clock.tick(60)  # The last one ends now.
        seen.append(run_async(count()))

    assert seen == [0, 3, 2]


def test_a_cache_that_keeps_no_stamp_keeps_no_principal(
    api: Api, shared: FakeServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stamp given and gone at once (an eviction) confirms nothing: read, not kept."""
    create_user("alice")
    api(PRINCIPAL_CACHE=STAMPED)
    raw = run_async(issue("alice"))

    async def keeps_nothing(*_: object, **__: object) -> bool:
        return False

    monkeypatch.setattr(Cache, "add", keeps_nothing)
    assert run_async(outcome(raw)) == "served"
    assert len(principal_cache) == 0


def test_synchronous_sessions_say_they_cannot_stamp(
    api: Api, shared: FakeServer, caplog: pytest.LogCaptureFixture
) -> None:
    """With no event loop to write from, the commit drops here and logs."""
    api(PRINCIPAL_CACHE=STAMPED)
    session = Session()
    invalidate_after_commit(session, [user_target(uuid4())])

    principals._committed(session)

    (warning,) = [m for m in caplog.messages if m.startswith("Could not stamp")]
    assert warning == (
        "Could not stamp 1 changed principals in cache 'shared' (no event loop); "
        "other processes keep theirs for up to 0:05:00"
    )
