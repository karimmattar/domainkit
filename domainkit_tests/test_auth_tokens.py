"""Opaque tokens: storage, lifetimes, revocation, and the token commands."""

import io
import sys
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from domainkit import setup
from domainkit.apps import registry
from domainkit.conf import override_settings
from domainkit.contrib.auth import AuthToken, AuthUnitOfWork, TokenKind
from domainkit.contrib.auth.management.commands import cleartokens, createtoken
from domainkit.contrib.auth.tokens import new_token, token_digest
from domainkit.contrib.messaging.registry import get_task, periodic_tasks
from domainkit.management import CommandError, call_command
from domainkit.management.base import BaseCommand, run_async
from domainkit_tests.conftest import ALPHA, AUTH, CONTENTTYPES, FAST_HASHER, UseAuth

NOW = datetime(2026, 1, 1, 12, tzinfo=UTC)


def in_uow[T](work: Callable[[AuthUnitOfWork], Awaitable[T]]) -> T:
    """Run ``work`` in a unit of work that commits."""

    async def run() -> T:
        async with AuthUnitOfWork() as uow:
            result = await work(uow)
            await uow.commit()
        return result

    return run_async(run())


def user_id(username: str = "alice") -> UUID:
    """Store a user; return the id."""

    async def create(uow: AuthUnitOfWork) -> UUID:
        return (await uow.users.create_user(username)).id

    return in_uow(create)


def test_tokens_are_random_and_stored_as_digests() -> None:
    """A prefix and 256 bits; the SHA-256 in hex is what is kept."""
    token = new_token("dkt_")

    assert token.startswith("dkt_")
    assert len(token) == 47
    assert new_token("dkt_") != token
    assert token_digest("abc") == (
        "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
    )


def test_token_lifetimes() -> None:
    """A token expires at ``expires_at``; without one it never does."""
    token = AuthToken(
        id="x", user_id=uuid4(), kind=TokenKind.ACCESS, created_at=NOW, expires_at=NOW
    )

    assert not token.expired(NOW - timedelta(microseconds=1))
    assert token.expired(NOW)
    assert token.expired()
    forever = AuthToken(id="y", user_id=uuid4(), kind="access", created_at=NOW)
    assert not forever.expired(datetime.max.replace(tzinfo=UTC))
    assert (forever.name, forever.family, forever.used_at) == ("", None, None)


def test_the_store_issues_finds_and_revokes(auth_project: str) -> None:
    """Issue, look up, and delete by token, family, user, or expiry."""
    alice, bob = user_id("alice"), user_id("bob")
    family = uuid4()

    async def issue(uow: AuthUnitOfWork) -> list[str]:
        return [
            await uow.tokens.issue(alice, ttl=timedelta(hours=1), name="cli"),
            await uow.tokens.issue(alice, ttl=None),
            await uow.tokens.issue(
                alice, kind=TokenKind.REFRESH, ttl=timedelta(days=1), family=family
            ),
            await uow.tokens.issue(bob, ttl=timedelta(seconds=-1)),
        ]

    cli, forever, refresh, _expired = in_uow(issue)

    async def check(uow: AuthUnitOfWork) -> None:
        stored = await uow.tokens.find(cli)
        assert stored is not None
        assert (stored.id, stored.user_id, stored.kind, stored.name) == (
            token_digest(cli),
            alice,
            "access",
            "cli",
        )
        assert stored.expires_at is not None
        assert stored.expires_at - stored.created_at == timedelta(hours=1)
        unlimited = await uow.tokens.find(forever)
        assert unlimited is not None
        assert unlimited.expires_at is None
        assert await uow.tokens.find("dkt_unknown") is None
        assert await uow.tokens.family_active(family)
        assert not await uow.tokens.family_active(uuid4())

    in_uow(check)

    async def clear(uow: AuthUnitOfWork) -> int:
        return await uow.tokens.clear_expired()

    assert in_uow(clear) == 1

    async def revoke_one(uow: AuthUnitOfWork) -> int:
        return await uow.tokens.revoke(token_digest(cli))

    assert in_uow(revoke_one) == 1
    assert in_uow(revoke_one) == 0

    async def keep(uow: AuthUnitOfWork) -> int:
        return await uow.tokens.revoke_user(alice, keep=token_digest(forever))

    assert in_uow(keep) == 1  # The refresh token went; ``forever`` stays.

    async def remaining(uow: AuthUnitOfWork) -> bool:
        return await uow.tokens.find(forever) is not None

    assert in_uow(remaining)
    assert not in_uow(lambda uow: uow.tokens.family_active(family))
    assert refresh != forever


def test_users_keep_one_family_when_others_go(auth_project: str) -> None:
    """``keep_family`` spares one login's tokens; tokens without one go."""
    alice = user_id()
    mine, theirs = uuid4(), uuid4()

    async def issue(uow: AuthUnitOfWork) -> None:
        for family in (mine, theirs, None):
            await uow.tokens.issue(
                alice, kind=TokenKind.REFRESH, ttl=timedelta(days=1), family=family
            )

    in_uow(issue)

    async def revoke(uow: AuthUnitOfWork) -> int:
        return await uow.tokens.revoke_user(alice, keep_family=mine)

    assert in_uow(revoke) == 2
    assert in_uow(lambda uow: uow.tokens.family_active(mine))

    async def revoke_family(uow: AuthUnitOfWork) -> int:
        return await uow.tokens.revoke_family(mine)

    assert in_uow(revoke_family) == 1


def test_refresh_tokens_are_used_once(auth_project: str) -> None:
    """Only the first of two concurrent uses marks a refresh token."""
    alice = user_id()

    async def issue(uow: AuthUnitOfWork) -> str:
        return await uow.tokens.issue(
            alice, kind=TokenKind.REFRESH, ttl=timedelta(days=1), family=uuid4()
        )

    raw = issue_token = in_uow(issue)

    async def use(uow: AuthUnitOfWork) -> tuple[bool, datetime | None]:
        token = await uow.tokens.find(raw)
        assert token is not None
        return await uow.tokens.mark_used(token), token.used_at

    first, used_at = in_uow(use)
    second, _ = in_uow(use)
    assert (first, second) == (True, False)
    assert used_at is not None
    assert issue_token == raw


def test_createtoken_prints_a_working_token(auth_project: str) -> None:
    """Shown once, stored as a digest, named, with a lifetime."""
    alice = user_id()
    output = io.StringIO()
    call_command("createtoken", "alice", "--name", "ci", "--days", "7", stdout=output)
    token = output.getvalue().strip()

    async def check(uow: AuthUnitOfWork) -> AuthToken | None:
        return await uow.tokens.find(token)

    stored = in_uow(check)
    assert stored is not None
    assert (stored.user_id, stored.name, stored.kind) == (alice, "ci", "access")
    assert stored.expires_at is not None
    assert stored.expires_at - stored.created_at == timedelta(days=7)

    output = io.StringIO()
    call_command("createtoken", "alice", "--no-expiry", stdout=output)
    forever = in_uow(lambda uow: uow.tokens.find(output.getvalue().strip()))
    assert forever is not None
    assert (forever.name, forever.expires_at) == ("cli", None)

    output = io.StringIO()
    call_command("createtoken", "alice", stdout=output)
    default = in_uow(lambda uow: uow.tokens.find(output.getvalue().strip()))
    assert default is not None
    assert default.expires_at is not None
    assert default.expires_at - default.created_at == timedelta(days=90)


def test_createtoken_refuses_and_warns(auth_project: str) -> None:
    """Unknown users and lifetimes under a day; a note without the class."""
    user_id()
    with pytest.raises(CommandError) as unknown:
        call_command("createtoken", "ghost", stdout=io.StringIO())
    assert str(unknown.value) == "User 'ghost' does not exist."
    with pytest.raises(CommandError) as short:
        call_command("createtoken", "alice", "--days", "0", stdout=io.StringIO())
    assert str(short.value) == "--days must be at least 1."
    with pytest.raises(CommandError) as long_name:
        call_command("createtoken", "alice", "--name", "x" * 101, stdout=io.StringIO())
    assert str(long_name.value) == "--name has at most 100 characters."
    call_command("createtoken", "alice", "--name", "x" * 100, stdout=io.StringIO())

    errors = io.StringIO()
    with override_settings(
        AUTH={
            "PASSWORD_HASHERS": [FAST_HASHER],
            "AUTHENTICATION_CLASSES": [f"{AUTH}.authentication.BaseAuthentication"],
        }
    ):
        call_command("createtoken", "alice", stdout=io.StringIO(), stderr=errors)
    assert errors.getvalue() == (
        "Note: AUTH.AUTHENTICATION_CLASSES does not list "
        f"{AUTH}.authentication.TokenAuthentication; requests cannot use this "
        "token yet.\n"
    )
    quiet = io.StringIO()
    call_command("createtoken", "alice", stdout=io.StringIO(), stderr=quiet)
    assert quiet.getvalue() == ""


def test_cleartokens(auth_project: str) -> None:
    """Expired tokens, or every token of one user."""
    alice, bob = user_id("alice"), user_id("bob")

    async def issue(uow: AuthUnitOfWork) -> None:
        await uow.tokens.issue(alice, ttl=timedelta(seconds=-1))
        await uow.tokens.issue(alice, ttl=None)
        await uow.tokens.issue(bob, ttl=None)

    in_uow(issue)
    output = io.StringIO()
    call_command("cleartokens", stdout=output)
    call_command("cleartokens", "--user", "alice", stdout=output)
    assert output.getvalue().splitlines() == [
        "Deleted 1 expired token(s).",
        "Deleted 1 token(s) of 'alice'.",
    ]
    with pytest.raises(CommandError) as unknown:
        call_command("cleartokens", "--user", "ghost", stdout=io.StringIO())
    assert str(unknown.value) == "User 'ghost' does not exist."
    assert bob


def test_expired_tokens_are_purged_hourly_with_messaging(
    database_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The worker clears expired tokens when messaging is installed."""
    # Installing the app imports (and so registers) the task afresh.
    monkeypatch.delitem(sys.modules, "domainkit.contrib.auth.tasks", raising=False)
    registry.clear()
    with override_settings(
        INSTALLED_DOMAINS=[
            "domainkit.contrib.messaging",
            CONTENTTYPES,
            AUTH,
            ALPHA,
        ],
        DATABASES={"default": {"URL": database_url}},
        MIGRATIONS_DIR=tmp_path / "migrations",
        AUTH={"PASSWORD_HASHERS": [FAST_HASHER]},
    ):
        setup()
        call_command("makemigrations", "-m", "auth", stdout=io.StringIO())
        call_command("migrate", stdout=io.StringIO())
        spec = get_task("domainkit.auth.clear_expired_tokens")
        assert spec is not None
        assert (spec, timedelta(hours=1)) in periodic_tasks()
        alice = user_id()
        in_uow(lambda uow: uow.tokens.issue(alice, ttl=timedelta(seconds=-1)))

        async def purge() -> None:
            await spec.function()

        run_async(purge())
        assert in_uow(lambda uow: uow.tokens.clear_expired()) == 0
    registry.clear()


def test_token_commands_explain_their_options(use_auth: UseAuth) -> None:
    """``--help`` names every option and what it does."""
    use_auth()
    created = usage(createtoken.Command, "createtoken")
    assert "username whose token" in created
    assert "--name NAME what it is for (cli)" in created
    assert "--days DAYS days it works (90)" in created
    assert "--no-expiry works until revoked (cleartokens --user)" in created
    assert (
        "Create a token for a user's scripts; it is shown once, then only hashed."
        in created
    )
    cleared = usage(cleartokens.Command, "cleartokens")
    assert "--user USERNAME delete all of this user's tokens" in cleared
    assert "Delete expired tokens; with --user, every token of that user." in cleared
    with pytest.raises(SystemExit):
        call_command("createtoken", "alice", "--days", "1", "--no-expiry")


def usage(command: type[BaseCommand], name: str) -> str:
    """The command's ``--help``, on one line."""
    return " ".join(command().create_parser("manage.py", name).format_help().split())


def test_the_store_touches_only_what_it_names(auth_project: str) -> None:
    """Other users' tokens, other refresh tokens, and names stay as they were."""
    alice, bob = user_id("alice"), user_id("bob")
    family = uuid4()

    async def issue(uow: AuthUnitOfWork) -> tuple[str, str, str]:
        return (
            await uow.tokens.issue(bob, ttl=None),
            await uow.tokens.issue(
                alice, kind=TokenKind.REFRESH, ttl=timedelta(days=1), family=family
            ),
            await uow.tokens.issue(
                alice, kind=TokenKind.REFRESH, ttl=timedelta(days=1), family=uuid4()
            ),
        )

    bobs, first, second = in_uow(issue)

    async def use_first(uow: AuthUnitOfWork) -> datetime | None:
        token = await uow.tokens.find(first)
        assert token is not None
        assert await uow.tokens.mark_used(token)
        other = await uow.tokens.find(second)
        assert other is not None
        return other.used_at

    assert in_uow(use_first) is None
    unnamed = in_uow(lambda uow: uow.tokens.find(bobs))
    assert unnamed is not None
    assert unnamed.name == ""

    async def revoke(uow: AuthUnitOfWork) -> tuple[int, int]:
        kept = await uow.tokens.revoke_user(alice, keep=token_digest(first))
        spared = await uow.tokens.revoke_user(alice, keep_family=family)
        return kept, spared

    assert in_uow(revoke) == (1, 0)
    assert in_uow(lambda uow: uow.tokens.find(bobs)) is not None
    assert in_uow(lambda uow: uow.tokens.find(first)) is not None


def test_lifetimes_end_at_their_instant(auth_project: str) -> None:
    """A token expiring now is expired: purged, and its family over."""
    alice = user_id()
    family = uuid4()

    async def issue(uow: AuthUnitOfWork) -> datetime:
        raw = await uow.tokens.issue(
            alice, kind=TokenKind.REFRESH, ttl=timedelta(hours=1), family=family
        )
        token = await uow.tokens.find(raw)
        assert token is not None
        assert token.expires_at is not None
        return token.expires_at

    end = in_uow(issue)
    early = end - timedelta(seconds=1)
    assert in_uow(lambda uow: uow.tokens.family_active(family, early))
    assert not in_uow(lambda uow: uow.tokens.family_active(family, end))
    assert in_uow(lambda uow: uow.tokens.clear_expired(early)) == 0
    assert in_uow(lambda uow: uow.tokens.clear_expired(end)) == 1


def test_logins_update_the_loaded_user(auth_project: str) -> None:
    """``record_login`` stores the time and keeps the object in step."""
    user_id()

    async def log_in(uow: AuthUnitOfWork) -> tuple[datetime | None, int]:
        user = await uow.users.get_by_natural_key("alice")
        assert user is not None
        await uow.users.record_login(user)
        return user.last_login, len(user.pending_events)

    when, events = in_uow(log_in)
    assert when is not None
    assert events == 1


def test_one_day_tokens(auth_project: str) -> None:
    """``--days 1`` is the shortest lifetime."""
    user_id()
    output = io.StringIO()
    call_command("createtoken", "alice", "--days", "1", stdout=output)
    token = in_uow(lambda uow: uow.tokens.find(output.getvalue().strip()))
    assert token is not None
    assert token.expires_at is not None
    assert token.expires_at - token.created_at == timedelta(days=1)
