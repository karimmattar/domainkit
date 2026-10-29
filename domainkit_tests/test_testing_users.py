"""Users, groups, and clients that sign in as them, for projects' tests (TEST-08).

``domainkit.contrib.auth.testing`` stores users and groups as the
application does; the clients' ``authenticated_as`` and ``token_for`` sign
in as a user a test made, as DRF's ``force_authenticate`` does; and the
plugin's ``settings`` fixture changes settings until the test ends, as
pytest-django's does. Until 0.12 a test could only sign in as a new user
``client_for`` made, and reached for ``AuthUnitOfWork`` to make one.
"""

from collections.abc import Iterator
from contextlib import ExitStack
from uuid import UUID

import pytest

from domainkit.apps import registry
from domainkit.conf import override_settings, settings
from domainkit.contrib.auth import AbstractUser, AuthUnitOfWork
from domainkit.contrib.auth.exceptions import (
    GroupNameTaken,
    UnknownGroup,
    UnknownPermission,
)
from domainkit.contrib.auth.testing import create_group, create_user
from domainkit.ddd import NotFound
from domainkit.exceptions import ImproperlyConfigured
from domainkit.http import get_asgi_application
from domainkit.testing import (
    IN_MEMORY,
    AsyncTestClient,
    SettingsOverrides,
    TestClient,
    token_for,
    user_token,
)
from domainkit_tests.conftest import MIDDLEWARE

ROUTES = {
    "ROOT_ROUTER": "domainkit_tests.sample_auth_routes.router",
    "MIDDLEWARE": MIDDLEWARE[:3],
}


@pytest.fixture
def routes(auth_project: str) -> Iterator[None]:
    """``auth_project``, serving the sample auth routes."""
    with override_settings(**ROUTES):
        yield


def test_a_sync_test_signs_in_as_the_user_it_made(routes: None) -> None:
    """``client.run`` stores a user in its loop; ``authenticated_as`` signs in."""
    with TestClient(get_asgi_application()) as client:
        viewer = client.run(create_user, "shop.view_product", username="vera")
        signed_in = client.authenticated_as(viewer)
        by_id = client.authenticated_as(viewer.id)

        assert signed_in.get("/products").json() == {"user": "vera"}
        assert signed_in.post("/products").status_code == 403
        assert by_id.get("/private").json() == {"user": "vera"}
        assert signed_in.portal is client.portal
        assert client.get("/private").status_code == 401


@pytest.mark.anyio
async def test_an_async_test_signs_in_through_groups(routes: None) -> None:
    """Permissions through a group; a password the API's login accepts."""
    editors = await create_group("shop.view_product", "shop.add_product")
    ann = await create_user(
        groups=[editors.name], password="correct-horse-battery", email="a@x.example"
    )
    assert isinstance(ann, AbstractUser)  # The default user model.
    async with AsyncTestClient(get_asgi_application()) as client:
        signed_in = await client.authenticated_as(ann)
        listed = await signed_in.get("/products")
        added = await signed_in.post("/products")
        logged_in = await client.post(
            "/auth/login",
            json={"username": ann.username, "password": "correct-horse-battery"},
        )
        await signed_in.aclose()

    assert editors.name.startswith("group-")
    assert ann.username.startswith("user-")
    assert (ann.email, ann.is_superuser) == ("a@x.example", False)
    assert listed.json() == added.json() == {"user": ann.username}
    assert logged_in.status_code == 200
    async with AuthUnitOfWork() as uow:
        stored = await uow.users.get(ann.id)
        assert stored.has_usable_password()
        assert [group.name for group in stored.groups] == [editors.name]


@pytest.mark.anyio
async def test_superusers_and_what_cannot_be_made(routes: None) -> None:
    """``superuser`` makes staff too; unknown keys, groups and names are refused."""
    root = await create_user(superuser=True, username="root")
    assert isinstance(root, AbstractUser)
    nameless = await create_user()
    named = await create_group(name="clerks")

    assert (root.is_superuser, root.is_staff) == (True, True)
    assert not nameless.has_usable_password()
    assert named.name == "clerks"
    with pytest.raises(UnknownPermission):
        await create_user("shop.fly_product")
    with pytest.raises(UnknownGroup):
        await create_user(groups=["nobody"])
    with pytest.raises(GroupNameTaken):
        await create_group(name="clerks")
    with pytest.raises(UnknownPermission):
        await create_group("shop.fly_product")
    with pytest.raises(NotFound):
        await token_for(UUID(int=0))


@pytest.mark.anyio
async def test_token_for_needs_a_domain_that_issues_tokens() -> None:
    """Without auth installed, nobody can sign the user in."""
    registry.clear()
    with (
        override_settings(
            INSTALLED_DOMAINS=["domainkit_tests.sample_domains.alpha"],
            DATABASES={"default": {"URL": IN_MEMORY}},
        ),
        pytest.raises(ImproperlyConfigured) as refused,
    ):
        await token_for("anyone")
    registry.clear()

    assert str(refused.value) == (
        "token_for needs an installed domain that issues tokens, such as "
        "domainkit.contrib.auth."
    )


def test_a_client_runs_setup_only_once_entered() -> None:
    """Not entered, ``run`` and ``authenticated_as`` refuse, as ``another`` does."""
    client = TestClient(get_asgi_application())
    refusal = r"^Enter the client first: with TestClient\(app\) as \.\.\.$"
    with pytest.raises(RuntimeError, match=refusal):
        client.run(create_user)
    with pytest.raises(RuntimeError, match=refusal):
        client.authenticated_as("anyone")


def test_settings_change_until_the_test_ends() -> None:
    """Set, delete for the default, read; undone in reverse order at the end."""
    before = (settings.DEBUG, settings.SHUTDOWN_TIMEOUT)
    with ExitStack() as stack:
        changed = SettingsOverrides(stack)
        changed.DEBUG = not before[0]
        changed.SHUTDOWN_TIMEOUT = 7
        changed.SHUTDOWN_TIMEOUT = 8
        during = (settings.DEBUG, changed.DEBUG, settings.SHUTDOWN_TIMEOUT)
        del changed.SHUTDOWN_TIMEOUT
        default = settings.SHUTDOWN_TIMEOUT
        with pytest.raises(ImproperlyConfigured, match="Invalid settings override"):
            changed.SHUTDOWN_TIMEOUT = -1

    assert during == (not before[0], not before[0], 8)
    assert default == 15.0
    assert before == (settings.DEBUG, settings.SHUTDOWN_TIMEOUT)


@pytest.mark.anyio
@pytest.mark.parametrize("environment", ["local", "production"])
async def test_test_tokens_are_refused_outside_the_test_settings(
    routes: None, environment: str
) -> None:
    """Neither a new user's token nor a stored one's: no credentials (SEC-12a-2)."""
    stored = await create_user(superuser=True)
    # Deployed settings refuse PostgreSQL without TLS; the refusal reads no
    # database, so any accepted URL will do.
    elsewhere = {"default": {"URL": IN_MEMORY}}
    with override_settings(ENVIRONMENT=environment, DATABASES=elsewhere):
        with pytest.raises(ImproperlyConfigured) as for_stored:
            await token_for(stored)
        with pytest.raises(ImproperlyConfigured) as for_new:
            await user_token(is_superuser=True)

    assert str(for_stored.value) == (
        "issue_test_token_for signs users in without credentials, so it needs "
        f'ENVIRONMENT = "test", not {environment!r}.'
    )
    assert str(for_new.value).startswith("issue_test_token signs users in")
