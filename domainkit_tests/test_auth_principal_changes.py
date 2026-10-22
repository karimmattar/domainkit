"""Changes that must reach kept principals: savepoints, bulk statements, stamps.

Each test makes a change the way a project could, and reads the principal
where a stale one would show: between a savepoint and the commit, after a
bulk statement, after the shared cache lost its stamps, after a cancelled
commit. Interleavings are forced with events, never with sleeps.
"""

from datetime import timedelta
from typing import Final

from domainkit.contrib.auth import AuthenticationFailed, AuthUnitOfWork
from domainkit.contrib.auth.principals import principal_cache
from domainkit.contrib.auth.tokens import token_digest
from domainkit.management.base import run_async
from domainkit_tests.conftest import Api
from domainkit_tests.test_auth_http import bearer, create_user
from domainkit_tests.test_auth_principals import credentials_of, issue

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
