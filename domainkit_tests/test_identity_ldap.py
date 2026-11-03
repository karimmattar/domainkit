"""Logging in with a directory's password (``LDAPBackend``), on ldap3's mock."""

from collections.abc import Callable, Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from ldap3 import MOCK_SYNC, OFFLINE_SLAPD_2_4, Connection, Server
from ldap3.core.exceptions import LDAPSocketOpenError

from domainkit.conf import override_settings
from domainkit.contrib.auth import AuthUnitOfWork
from domainkit.contrib.identity import ldap as ldap_module
from domainkit.contrib.identity.settings import LdapSettings
from domainkit.management.base import run_async
from domainkit_tests.conftest import IDENTITY, ExitStackClients, Migrated
from domainkit_tests.test_auth_http import bearer, create_user
from domainkit_tests.test_identity_providers import PROVIDERS

type App = Callable[..., TestClient]

READER = "cn=reader,ou=services,dc=example,dc=com"
ANN = "uid=ann,ou=people,dc=example,dc=com"
FINANCE = "cn=finance,ou=groups,dc=example,dc=com"
LDAP = {
    "URL": "ldaps://dc.example.com",
    "BIND_DN": READER,
    "BIND_PASSWORD": "reader-pw",
    "USER_SEARCH_BASE": "ou=people,dc=example,dc=com",
    "USER_FILTER": "(uid={username})",
    "GROUPS": {FINANCE: "finance"},
}
BACKENDS = [
    "domainkit.contrib.auth.backends.ModelBackend",
    "domainkit.contrib.identity.ldap.LDAPBackend",
]


@pytest.fixture
def directory(monkeypatch: pytest.MonkeyPatch) -> Server:
    """A mock directory with the reader and ann, a member of finance."""
    server = Server("mock", get_info=OFFLINE_SLAPD_2_4)

    def connect(config: LdapSettings, user: str | None, password: str | None) -> Any:  # noqa: ANN401
        connection = Connection(
            server, user=user, password=password, client_strategy=MOCK_SYNC
        )
        if user == config.BIND_DN:
            connection.bind()
        return connection

    seed = Connection(server, client_strategy=MOCK_SYNC)
    seed.strategy.add_entry(
        READER, {"userPassword": "reader-pw", "objectClass": "person"}
    )
    seed.strategy.add_entry(
        ANN,
        {
            "uid": "ann",
            "userPassword": "directory-pw",
            "mail": "ann@example.com",
            "memberOf": [FINANCE],
            "objectClass": "person",
        },
    )
    monkeypatch.setattr(ldap_module, "connect", connect)
    return server


@pytest.fixture
def app(migrated: Migrated) -> Iterator[App]:
    """``app(**IDENTITY)``: a client whose AUTH lists the LDAP backend."""
    auth = IDENTITY.settings["AUTH"]
    with migrated(PROVIDERS), ExitStackClients() as clients:

        def make(**identity: Any) -> TestClient:  # noqa: ANN401 - IDENTITY keys.
            clients.enter(
                override_settings(
                    IDENTITY=identity, AUTH={**auth, "BACKENDS": BACKENDS}
                )
            )
            return clients.client(("testclient", 50000))

        yield make


def group_names(username: str) -> set[str]:
    """The groups ``username`` is in."""

    async def read() -> set[str]:
        async with AuthUnitOfWork() as uow:
            user = await uow.users.get_by_natural_key(username)
            assert user is not None
            return {group.name for group in user.groups}

    return run_async(read())


def make_group(name: str) -> None:
    """A group of that name."""

    async def create() -> None:
        async with AuthUnitOfWork() as uow:
            await uow.groups.create(name)
            await uow.commit()

    run_async(create())


def test_the_directory_password_logs_in_and_makes_the_user(
    app: App, directory: Server
) -> None:
    """The first login makes ann, in finance; the email follows the directory."""
    client = app(LDAP=LDAP)
    make_group("finance")

    answer = client.post(
        "/auth/login", json={"username": "ann", "password": "directory-pw"}
    )

    assert answer.status_code == 200, answer.json()
    me = client.get("/auth/me", headers=bearer(answer.json()["access_token"])).json()
    assert (me["username"], me["email"]) == ("ann", "ann@example.com")
    assert group_names("ann") == {"finance"}
    again = client.post(
        "/auth/login", json={"username": "ann", "password": "directory-pw"}
    )
    assert again.status_code == 200


def test_wrong_empty_and_unknown_passwords_are_refused(
    app: App, directory: Server
) -> None:
    """401 for each; an empty password never reaches a bind."""
    client = app(LDAP=LDAP)

    for username, password in (
        ("ann", "wrong"),
        ("ann", ""),
        ("bob", "x"),
        ("*", "directory-pw"),
    ):
        answer = client.post(
            "/auth/login", json={"username": username, "password": password}
        )
        assert answer.status_code in {401, 422}, (username, answer.json())


def test_a_local_account_is_never_taken_over(app: App, directory: Server) -> None:
    """An account named ann the directory did not make keeps its own password."""
    client = app(LDAP=LDAP)
    create_user("ann")

    answer = client.post(
        "/auth/login", json={"username": "ann", "password": "directory-pw"}
    )

    assert answer.status_code == 401


def test_leaving_a_directory_group_leaves_its_group_here(
    app: App, directory: Server
) -> None:
    """Mapped groups follow the directory; unmapped ones are left alone."""
    client = app(LDAP=LDAP)
    make_group("finance")
    client.post("/auth/login", json={"username": "ann", "password": "directory-pw"})
    directory.dit[ANN]["memberOf"] = []  # ann left finance in the directory.

    client.post("/auth/login", json={"username": "ann", "password": "directory-pw"})

    assert group_names("ann") == set()


def test_without_ldap_settings_the_backend_knows_no_one(app: App) -> None:
    """No IDENTITY.LDAP: the backend answers None; passwords go to the model backend."""
    client = app()
    create_user("ann")

    answer = client.post("/auth/login", json={"username": "ann", "password": "nope"})

    assert answer.status_code == 401


def test_an_unreachable_directory_refuses(monkeypatch: pytest.MonkeyPatch) -> None:
    """A socket that will not open is logged and refuses the password."""

    def down(config: LdapSettings, user: str | None, password: str | None) -> Any:  # noqa: ANN401
        raise LDAPSocketOpenError("no route")

    monkeypatch.setattr(ldap_module, "connect", down)
    config = LdapSettings(**LDAP)

    assert ldap_module.check_password(config, "ann", "pw") is None


def test_an_inactive_user_is_refused_and_no_groups_are_synced(
    app: App, directory: Server
) -> None:
    """An inactive user is refused; with no GROUPS, groups are left alone."""
    client = app(LDAP={**LDAP, "GROUPS": {}})
    assert (
        client.post(
            "/auth/login", json={"username": "ann", "password": "directory-pw"}
        ).status_code
        == 200
    )

    async def deactivate() -> None:
        async with AuthUnitOfWork() as uow:
            user = await uow.users.get_by_natural_key("ann")
            assert user is not None
            user.set_active(False)
            await uow.commit()

    run_async(deactivate())
    assert (
        client.post(
            "/auth/login", json={"username": "ann", "password": "directory-pw"}
        ).status_code
        == 401
    )
