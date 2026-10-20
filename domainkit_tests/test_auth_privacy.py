"""Users' data out of the service: fixtures never hold secrets; privacy requests."""

import hashlib
import hmac
import io
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any, Final
from unittest.mock import Mock
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from domainkit import setup
from domainkit.apps import registry
from domainkit.conf import override_settings
from domainkit.contrib import mail
from domainkit.contrib.auth import AuthUnitOfWork, UsernameTaken, erasure
from domainkit.contrib.auth.erasure import forget_credentials
from domainkit.contrib.auth.models import ERASED_USERNAME, erased_username
from domainkit.contrib.auth.tables import mfa_devices, recovery_codes, users
from domainkit.contrib.privacy import (
    PersonalDataErased,
    erase_subject,
    personal_models,
    problems,
)
from domainkit.db import connections
from domainkit.management import call_command
from domainkit.management.base import CommandError, run_async
from domainkit_tests.conftest import (
    FAST_HASHER,
    IDENTITY_AUTHENTICATION,
    IDENTITY_DOMAINS,
    MEMORY_MAIL,
    MIDDLEWARE,
    Api,
    ExitStackClients,
)
from domainkit_tests.test_auth_http import (
    PASSWORD,
    audited,
    bearer,
    create_user,
    login,
    stored_tokens,
)
from domainkit_tests.test_auth_mfa import enrol
from domainkit_tests.test_auth_recovery import run_worker

PRIVACY: Final = "domainkit.contrib.privacy"
NOW: Final = 1_700_000_000.0


@pytest.fixture
def private(database_url: str, tmp_path: Path) -> Iterator[Api]:
    """The identity API with ``domainkit.contrib.privacy`` installed too."""
    registry.clear()
    mail.outbox.clear()
    auth = {
        "PASSWORD_HASHERS": [FAST_HASHER],
        "AUTHENTICATION_CLASSES": IDENTITY_AUTHENTICATION,
    }
    with override_settings(
        INSTALLED_DOMAINS=[*IDENTITY_DOMAINS, PRIVACY],
        DATABASES={"default": {"URL": database_url}},
        MIGRATIONS_DIR=tmp_path / "migrations",
        ROOT_ROUTER="domainkit_tests.sample_identity_routes.router",
        MIDDLEWARE=MIDDLEWARE,
        MAIL=MEMORY_MAIL,
        AUTH=auth,
    ):
        setup()
        call_command("makemigrations", "-m", "private", stdout=io.StringIO())
        call_command("migrate", stdout=io.StringIO())
        with ExitStackClients() as clients:

            def make(**options: Any) -> TestClient:  # noqa: ANN401 - AUTH keys.
                if options:
                    clients.enter(override_settings(AUTH={**auth, **options}))
                return clients.client(("testclient", 50000))

            yield make
    registry.clear()


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """The time codes are checked at."""
    from domainkit.contrib.auth import mfa

    now = [NOW]
    monkeypatch.setattr(mfa, "_now", lambda: now[0])
    return now


def rows(table: Any) -> list[dict[str, Any]]:  # noqa: ANN401 - a table.
    """Every row of ``table``."""

    async def read() -> list[dict[str, Any]]:
        async with connections.engine().connect() as connection:
            return [
                dict(row._mapping) for row in await connection.execute(select(table))
            ]

    return run_async(read())


def run(*args: str) -> str:
    """Run a command; return what it printed."""
    output = io.StringIO()
    call_command(*args, stdout=output)
    return output.getvalue()


def alice_with_everything(client: TestClient, clock: list[float]) -> tuple[UUID, str]:
    """Alice, with a second factor, a login, a personal token, and a certificate."""
    alice = create_user(
        "alice", email="alice@example.com", first_name="Alice", last_name="Smith"
    )
    access = login(client, "alice")["access_token"]
    enrol(client, bearer(access), clock)
    made = client.post("/auth/tokens", json={"name": "ci"}, headers=bearer(access))
    assert made.status_code == 201

    async def register() -> None:
        async with AuthUnitOfWork() as uow:
            await uow.tokens.add_certificate(alice, "c" * 64, name="laptop")
            await uow.commit()

    run_async(register())
    return alice, made.json()["token"]


# Fixtures --------------------------------------------------------------------------


def test_fixtures_hold_no_secret_and_no_one_time_state(
    private: Api, clock: list[float]
) -> None:
    """Tokens, sessions, certificates, second factors: never dumped or loaded."""
    client = private()
    _, token = alice_with_everything(client, clock)
    create_user("bob")
    browser = TestClient(client.app, base_url="https://testserver")
    browser.post(
        "/auth/session",
        json={"username": "bob", "password": PASSWORD},
        headers={"Origin": "https://testserver"},
    )
    assert rows(mfa_devices)
    assert rows(recovery_codes)

    raw = run("dumpdata")

    names = {entry["table"] for entry in json.loads(raw)["tables"]}
    assert users.name in names
    assert not names & {"domainkit_tokens", mfa_devices.name, recovery_codes.name}
    (device,) = rows(mfa_devices)
    assert token not in raw
    assert browser.cookies["__Host-dk_session"] not in raw
    for secret in (device["secret"], device["id"]):
        assert str(secret) not in raw
    for table in (mfa_devices.name, recovery_codes.name, "domainkit_tokens"):
        with pytest.raises(CommandError, match="names no fixture"):
            run("dumpdata", table)


# Privacy ---------------------------------------------------------------------------


def test_users_are_people_to_privacy(private: Api) -> None:
    """Username, email, and names are about the user; the user erases them."""
    private()
    (user,) = [model for model in personal_models() if model.key == "auth.user"]

    assert dict(user.subjects) == {
        "id": ("username", "email", "first_name", "last_name")
    }
    assert user.erases_itself()
    assert problems() == []


def test_exports_hold_the_users_personal_fields(private: Api) -> None:
    """``privacy export``: the user's fields, never the password's hash."""
    private()
    alice = create_user(
        "alice", email="alice@example.com", first_name="Alice", last_name="Smith"
    )

    document = json.loads(run("privacy", "export", "--subject", str(alice)))

    assert document["domains"]["auth"]["user"] == [
        {
            "id": str(alice),
            "fields": {
                "username": "alice",
                "email": "alice@example.com",
                "first_name": "Alice",
                "last_name": "Smith",
            },
        }
    ]
    assert "pbkdf2" not in json.dumps(document)


def test_erasure_ends_the_account_and_its_credentials(
    private: Api, clock: list[float]
) -> None:
    """Fields erased, account off, password unusable, tokens and factors gone."""
    client = private()
    alice, token = alice_with_everything(client, clock)
    bob = create_user("bob", email="bob@example.com")

    printed = run("privacy", "erase", "--subject", str(alice), "--noinput")

    assert printed.startswith(f"Erased the personal data of {alice} in 1 aggregate")
    (row,) = [row for row in rows(users) if row["id"] == alice]
    assert row["username"] == erased_username("alice")
    assert row["username"].startswith(ERASED_USERNAME)
    assert (row["email"], row["first_name"], row["last_name"]) == ("", "", "")
    assert (row["is_active"], row["email_verified"]) == (False, False)
    assert not row["password"].usable
    assert stored_tokens(alice) == []
    assert rows(mfa_devices) == rows(recovery_codes) == []
    assert client.get("/whoami", headers=bearer(token)).status_code == 401
    refused = client.post(
        "/auth/login", json={"username": "alice", "password": PASSWORD}
    )
    assert refused.status_code == 401
    bobs = bearer(login(client, "bob")["access_token"])
    assert client.get("/whoami", headers=bobs).json()["user"] == "bob"
    assert stored_tokens(bob)  # Others keep theirs.
    (erased,) = audited("privacy.personal_data_erased")
    assert erased["details"]["fields"] == [
        "username",
        "email",
        "first_name",
        "last_name",
    ]
    assert [row["details"] for row in audited("auth.user_status_changed")] == [
        {"user_id": str(alice), "field": "is_active", "value": False}
    ]
    run("privacy", "erase", "--subject", str(alice), "--noinput")  # Again: no change.
    assert [again for again in rows(users) if again["id"] == alice] == [row]
    assert len(audited("auth.password_changed")) == 1
    assert len(audited("auth.user_status_changed")) == 1


def test_an_erased_username_stays_taken(private: Api) -> None:
    """In any case: nobody can sign up, or be created, as the person erased."""
    client = private(SIGNUP_ENABLED=True)
    alice = create_user("alice", email="alice@example.com")
    run_async(erase_subject(str(alice)))

    with pytest.raises(
        UsernameTaken, match=r"^The username is already taken\.$"
    ) as refused:
        create_user("alice")
    assert refused.value.fields == ("username",)
    with pytest.raises(UsernameTaken):
        create_user("ALICE")

    async def taken(name: str) -> bool:
        async with AuthUnitOfWork() as uow:
            return await uow.users.username_taken(name)

    assert run_async(taken("Alice"))
    assert not run_async(taken("alicia"))
    signed = client.post(
        "/auth/signup",
        json={"username": "alice", "email": "new@example.com", "password": PASSWORD},
    )
    assert signed.status_code == 202
    run_worker()  # The worker tells the address that the name is taken.
    assert [message.subject for message in mail.outbox] == ["Your sign-up"]


def test_erased_usernames_are_keyed_digests() -> None:
    """One value per name as lookups fold it; nothing of the name without the key."""
    key, other = b"k" * 32, b"o" * 32

    erased = erased_username("Alice", key=key)

    digest = hmac.new(key, b"alice", hashlib.sha256).hexdigest()
    assert erased == f"erased-{digest[:32]}"
    assert erased == erased_username("alice", key=key)
    assert erased == erased_username("\uff41lice", key=key)  # Full-width a.
    assert erased != erased_username("alice", key=other)
    assert erased != erased_username("alicia", key=key)
    assert len(erased) == len(ERASED_USERNAME) + 32
    assert "alice" not in erased


def test_names_erased_under_a_fallback_key_stay_taken(private: Api) -> None:
    """Rotating ``SECRET_KEY`` keeps them while the old key is a fallback."""
    from domainkit_tests.settings import SECRET_KEY

    private()
    alice = create_user("alice")
    run_async(erase_subject(str(alice)))
    new = "rotated-" + "x9Y8z7W6v5U4t3S2r1Q0p" * 3

    with (
        override_settings(SECRET_KEY=new, SECRET_KEY_FALLBACKS=[SECRET_KEY]),
        pytest.raises(UsernameTaken),
    ):
        create_user("alice")
    with override_settings(SECRET_KEY=new):
        assert create_user("alice")  # The old key is gone: so is the name's hold.


def test_credentials_go_only_with_a_user(
    private: Api, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Erasures of other models, of a user model no domain holds, or without auth."""
    client = private()
    alice = create_user("alice")
    login(client, "alice")

    def erased(model: str) -> PersonalDataErased:
        return PersonalDataErased(
            model=model, aggregate_id=str(alice), fields=("email",), reason="request"
        )

    run_async(forget_credentials(erased("shop.customer")))
    with monkeypatch.context() as patched:
        patched.setattr(erasure, "content_type_key", Mock(side_effect=LookupError))
        run_async(forget_credentials(erased("auth.user")))
    assert stored_tokens(alice)
    registry.clear()  # Auth is no longer installed: nothing is looked up.
    run_async(forget_credentials(erased("auth.user")))
    assert stored_tokens(alice)


def test_the_user_erases_only_the_fields_it_is_given(private: Api) -> None:
    """A field left out stays; the account ends either way."""
    from domainkit.contrib.auth import PasswordHash, User

    private()
    user = User.create(
        password=PasswordHash.unusable(),
        username="alice",
        email="alice@example.com",
        first_name="Alice",
    )

    user.erase_personal_data(frozenset({"email"}))

    assert (user.username, user.email, user.first_name) == ("alice", "", "Alice")
    assert user.is_active is False


def test_without_a_key_no_name_is_held(private: Api) -> None:
    """Names are erased under ``SECRET_KEY``; without one, none was."""
    from domainkit.conf import UNSET

    private()
    with override_settings(SECRET_KEY=UNSET):
        assert create_user("carol")
