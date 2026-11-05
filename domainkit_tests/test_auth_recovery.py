"""Password resets and email verification through the outbox, by codes.

The flows themselves are in ``test_auth_codes`` and ``test_auth_reset_codes``;
here is what every one of them shares: who gets mail, the checks at
startup, the rates, and the addresses that cannot take mail.
"""

import re
import uuid
from datetime import timedelta
from types import SimpleNamespace
from typing import Any, cast

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, update

from domainkit import setup
from domainkit.apps import registry
from domainkit.conf import UNSET, override_settings
from domainkit.contrib import mail
from domainkit.contrib.auth import AbstractBaseUser, AuthUnitOfWork, UserId
from domainkit.contrib.auth.onetime import email_of
from domainkit.contrib.auth.recovery import (
    describe,
    send_email_verification,
    send_password_reset_code,
)
from domainkit.contrib.auth.tables import users
from domainkit.contrib.messaging import Worker
from domainkit.contrib.messaging.tables import messages
from domainkit.db import connections
from domainkit.exceptions import ImproperlyConfigured
from domainkit.http import get_asgi_application
from domainkit.http.application import application_problems
from domainkit.management.base import run_async
from domainkit_tests.conftest import IDENTITY_DOMAINS, Api
from domainkit_tests.sample_domains.members.models import Member
from domainkit_tests.test_auth_http import (
    audited,
    bearer,
    create_user,
    login,
)

NEW_PASSWORD = "an0ther-Strong-pass!"
MAIL_APP = "domainkit.contrib.mail"
CODE = re.compile(r"\b[0-9]{6}\b")


def run_worker() -> int:
    """Run every due message; return how many ran."""
    return run_async(Worker().run_once())


def outbox_payloads() -> list[dict[str, Any]]:
    """The payloads of every outbox row."""

    async def read() -> list[dict[str, Any]]:
        async with connections.engine().connect() as connection:
            rows = await connection.execute(select(messages.c.payload))
            return [row.payload for row in rows]

    return run_async(read())


def outbox_targets() -> list[str]:
    """The task each outbox row runs."""

    async def read() -> list[str]:
        async with connections.engine().connect() as connection:
            rows = await connection.execute(select(messages.c.target))
            return [row.target for row in rows]

    return run_async(read())


def from_address(client: TestClient, address: str) -> TestClient:
    """Another client of the same application, from ``address``."""
    return TestClient(client.app, client=(address, 1))


def reset(client: TestClient, email: str) -> dict[str, Any]:
    """Ask for a reset; return the 202 body."""
    response = client.post("/auth/password-reset", json={"email": email})
    assert response.status_code == 202, response.json()
    body: dict[str, Any] = response.json()
    return body


def code_in(message: mail.EmailMessage) -> str:
    """The code in a message."""
    found = CODE.search(message.body)
    assert found is not None, message.body
    return found[0]


def test_unknown_addresses_get_the_same_answer_and_no_mail(identity: Api) -> None:
    """Whether an account uses the address shows in neither status nor body."""
    create_user("alice", email="alice@example.com")
    client = identity()

    known = client.post("/auth/password-reset", json={"email": "alice@example.com"})
    unknown = client.post("/auth/password-reset", json={"email": "nobody@example.com"})

    assert known.status_code == unknown.status_code == 202
    assert known.json() == unknown.json()
    assert set(known.headers) == set(unknown.headers)
    assert known.json() == {"detail": "If an account uses it, a code is on its way."}
    assert outbox_payloads() == [
        {"identifier": "alice@example.com"},
        {"identifier": "nobody@example.com"},
    ]
    assert outbox_targets() == ["domainkit.auth.send_password_reset_code"] * 2
    run_worker()
    assert [message.to for message in mail.outbox] == [("alice@example.com",)]


def test_only_an_active_user_with_a_password_gets_a_code(identity: Api) -> None:
    """A user who cannot log in with a password gets nothing to reset it."""
    create_user("off", email="off@example.com", is_active=False)
    create_user("sso", None, email="sso@example.com")
    client = identity()

    reset(client, "off@example.com")
    reset(client, "sso@example.com")
    run_worker()

    assert mail.outbox == []


def test_resets_and_codes_are_throttled(identity: Api) -> None:
    """Mail per client address and per recipient; wrong codes per address."""
    create_user("alice", email="alice@example.com")
    client = identity(EMAIL_RATE="2/hour", LOGIN_RATE="2/minute")
    other = from_address(client, "198.51.100.7")

    reset(client, "alice@example.com")
    reset(client, "nobody@example.com")
    by_address = client.post("/auth/password-reset", json={"email": "x@example.com"})
    reset(other, "alice@example.com")
    by_recipient = other.post(
        "/auth/password-reset", json={"identifier": "ALICE@example.com"}
    )

    for response in (by_address, by_recipient):
        assert (response.status_code, response.json()["code"]) == (429, "rate_limited")
        assert response.json()["detail"] == (
            "Too many requests for mail; try again later."
        )
        assert response.headers["Retry-After"]
    assert len(outbox_payloads()) == 3

    run_worker()
    code = code_in(mail.outbox[-1])
    wrong = "000000" if code != "000000" else "111111"
    third = from_address(client, "203.0.113.9")

    def confirm(client: TestClient, code: str) -> Any:  # noqa: ANN401 - a response.
        body = {"identifier": "alice@example.com", "code": code}
        return client.post(
            "/auth/password-reset/code", json={**body, "new_password": NEW_PASSWORD}
        )

    assert confirm(third, wrong).status_code == 400
    assert confirm(third, wrong).status_code == 400
    refused = confirm(third, wrong)  # This address is done guessing.
    assert (refused.status_code, refused.json()["code"]) == (429, "rate_limited")
    assert refused.json()["detail"] == "Too many attempts; try again later."
    assert int(refused.headers["Retry-After"]) > 0
    assert [row["details"]["reason"] for row in audited("auth.password_reset")] == [
        "throttled",
        "throttled",
        "invalid_verification_code",
        "invalid_verification_code",
        "throttled",
    ]
    # A right code is never counted, nor held back by wrong ones: the code
    # spends itself after AUTH.CODE_ATTEMPTS wrong tries instead.
    assert confirm(third, code).status_code == 204


def test_verified_users_who_change_their_address_lose_the_mark() -> None:
    """``update`` resets ``email_verified`` when the email changes, only then."""
    from domainkit.contrib.auth import PasswordHash, User

    user = User.create(
        password=PasswordHash.unusable(), username="alice", email="a@example.com"
    )
    user.verify_email()
    user.verify_email()  # Once is recorded.
    user.update(first_name="Alice")
    assert user.email_verified
    user.update(email="a@EXAMPLE.com")  # The same address, normalized.
    assert user.email_verified
    user.update(email="b@example.com")
    assert not user.email_verified
    assert [type(event).__name__ for event in user.pull_events()] == [
        "UserCreated",
        "EmailVerified",
        "UserUpdated",
        "UserUpdated",
    ]


def test_verification_needs_a_user_and_an_address(identity: Api) -> None:
    """Anonymous callers get 401; users without an address get no mail."""
    create_user("noemail")
    client = identity()

    assert client.post("/auth/email-verification").status_code == 401
    headers = bearer(login(client, "noemail")["access_token"])
    assert client.post("/auth/email-verification", headers=headers).status_code == 202
    run_worker()
    assert mail.outbox == []


def test_verification_skips_gone_or_inactive_users(identity: Api) -> None:
    """A user deleted or deactivated before the worker runs gets nothing."""
    gone = uuid.uuid4()
    off = create_user("off", email="off@example.com", is_active=False)

    run_async(send_email_verification(gone))
    run_async(send_email_verification(off))

    assert mail.outbox == []


def unmarked_user() -> Any:  # noqa: ANN401 - a stand-in for a user.
    """An active user of a model without ``email_verified``."""
    return SimpleNamespace(
        id=uuid.uuid4(), is_active=True, EMAIL_FIELD="email", email="a@example.com"
    )


def test_models_without_the_mark_get_no_verification_code(
    identity: Api, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A user model without ``email_verified`` has nothing a code could set."""
    from domainkit.contrib.auth import recovery

    user = unmarked_user()

    class Users:
        async def find(self, user_id: UserId) -> Any:  # noqa: ANN401 - the stand-in.
            return user

    class StandIn:
        users = Users()

        async def __aenter__(self) -> "StandIn":
            return self

        async def __aexit__(self, *exc_info: object) -> None:
            return None

    monkeypatch.setattr(recovery, "AuthUnitOfWork", StandIn)

    run_async(recovery.send_email_verification(user.id))

    assert mail.outbox == []


def test_verification_requests_are_throttled_per_user(identity: Api) -> None:
    """``EMAIL_RATE`` counts the requests of each user."""
    create_user("alice", email="alice@example.com")
    client = identity(EMAIL_RATE="1/hour")
    headers = bearer(login(client, "alice")["access_token"])

    assert client.post("/auth/email-verification", headers=headers).status_code == 202
    assert client.post("/auth/email-verification", headers=headers).status_code == 429


def test_email_of_reads_the_email_field() -> None:
    """The model's ``EMAIL_FIELD``, if set and not empty."""
    from domainkit.contrib.auth import PasswordHash, User

    user = User.create(password=PasswordHash.unusable(), username="a")
    assert email_of(user) is None
    user.email = "a@example.com"
    assert email_of(user) == "a@example.com"
    user.EMAIL_FIELD = None  # type: ignore[misc]
    assert email_of(user) is None
    assert email_of(cast(AbstractBaseUser, SimpleNamespace(EMAIL_FIELD="mail"))) is None


@pytest.mark.parametrize(
    ("duration", "words"),
    [
        (timedelta(days=3), "3 days"),
        (timedelta(days=1), "1 day"),
        (timedelta(hours=1), "1 hour"),
        (timedelta(hours=25), "25 hours"),
        (timedelta(minutes=30), "30 minutes"),
        (timedelta(minutes=1), "1 minute"),
        (timedelta(seconds=61), "2 minutes"),
        (timedelta(seconds=0.5), "1 minute"),
    ],
)
def test_durations_in_words(duration: timedelta, words: str) -> None:
    """Days, hours, or minutes, whichever divides it, rounded up."""
    assert describe(duration) == words


def test_mailing_routes_need_mail_and_an_address(
    identity: Api, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``check`` lists what routes that send mail lack: apps, or an address."""
    identity()
    registry.clear()
    without_mail = [domain for domain in IDENTITY_DOMAINS if domain != MAIL_APP]
    with override_settings(INSTALLED_DOMAINS=without_mail, MAIL=UNSET):
        setup()
        app = get_asgi_application()
        problems = application_problems(app)
        auth = registry.get_domain_config("auth")
        monkeypatch.setattr(auth, "user_model", Member)
        member_problems = application_problems(app)

    missing = "send mail: add domainkit.contrib.mail to INSTALLED_DOMAINS."
    no_address = (
        "need a user model with an EMAIL_FIELD and email_verified, as AbstractUser has."
    )
    assert problems == [
        f"Routes of domainkit.contrib.auth.recovery {missing}",
        f"Routes of domainkit.contrib.auth.signup {missing}",
    ]
    assert member_problems == [
        f"Routes of domainkit.contrib.auth.recovery {missing}",
        f"Routes of domainkit.contrib.auth.recovery {no_address}",
        f"Routes of domainkit.contrib.auth.signup {missing}",
        f"Routes of domainkit.contrib.auth.signup {no_address}",
    ]


def test_every_identity_route_says_who_may_call_it(identity: Api) -> None:
    """Deny by default holds: each route is guarded or ``Public`` on purpose."""
    from domainkit.http.access import open_routes

    identity()

    assert open_routes(get_asgi_application()) == []


def test_unmounted_routers_need_nothing(api: Api) -> None:
    """Without the mailing routes mounted, mail is not required."""
    api()

    assert application_problems(get_asgi_application()) == []


def test_reset_tasks_need_mail_installed(identity: Api) -> None:
    """A code the worker cannot mail fails the task, to be retried."""
    create_user("alice", email="alice@example.com")
    identity()
    with (
        override_settings(MAIL={"BACKEND": "domainkit_tests.test_auth_recovery.Nope"}),
        pytest.raises(ImproperlyConfigured, match=r"MAIL\.BACKEND"),
    ):
        run_async(send_password_reset_code("alice@example.com"))


def test_addresses_are_looked_up_only_where_they_can_match(identity: Api) -> None:
    """A blank address, or a model without one, finds nobody."""
    from typing import ClassVar

    from domainkit.contrib.auth import User

    class NoEmail(User):
        EMAIL_FIELD: ClassVar[str | None] = None

    create_user("blank", email="")

    async def find() -> tuple[int, int, int]:
        async with AuthUnitOfWork() as uow:
            blank = await uow.users.find_by_email("  ")
            found = await uow.users.find_by_email("")
            uow.users.model = NoEmail
            none = await uow.users.find_by_email("blank@example.com")
        return len(blank), len(found), len(none)

    assert run_async(find()) == (0, 0, 0)


@pytest.mark.parametrize(
    "email",
    [
        "a\x00@example.com",
        "a@exam\x7fple.com",
        "a@b@c",
        "no-at",
        "a b@c.d",
        "x0<victim@example.com>",  # Display names would count apart.
        "\u212aelly@corp.example",  # The KELVIN SIGN.
    ],
)
def test_addresses_that_cannot_be_one_are_refused_at_once(
    identity: Api, email: str
) -> None:
    """Control characters, spaces, and extra @ never reach the outbox."""
    response = identity().post("/auth/password-reset", json={"email": email})

    assert response.status_code == 422
    assert outbox_payloads() == []


def set_email(username: str, email: str) -> None:
    """Store ``email`` for the user as it is, unchecked (as before the checks)."""

    async def store() -> None:
        async with connections.engine().begin() as connection:
            await connection.execute(
                update(users).where(users.c.username == username).values(email=email)
            )

    run_async(store())


def test_look_alike_addresses_are_other_addresses(identity: Api) -> None:
    """Only ASCII letters fold, in Python and in SQL alike.

    PostgreSQL's ``lower()`` makes the KELVIN SIGN a ``k``: the look-alike's
    account was found for kelly's address, and its address, which no mail
    can take, stopped the task before kelly's mail went out.
    """
    create_user("eve", email="eve@corp.example")
    set_email("eve", "\u212aelly@corp.example")
    create_user("kelly", email="kelly@corp.example")
    client = identity()

    async def found() -> list[str]:
        async with AuthUnitOfWork() as uow:
            users = await uow.users.find_by_email("KELLY@corp.example")
        return [user.get_username() for user in users]

    reset(client, "kelly@corp.example")
    run_worker()

    assert run_async(found()) == ["kelly"]
    (message,) = mail.outbox
    assert message.to == ("kelly@corp.example",)
    assert message.body.startswith("Hello kelly,")


def test_accounts_of_one_address_come_by_id(identity: Api) -> None:
    """Accounts that share an address, in any case, come in the order of ids."""
    from domainkit.contrib.auth import PasswordHash, User

    identity()

    async def scenario() -> list[str]:
        async with AuthUnitOfWork() as uow:
            for number, name, email in (
                (2, "second", "Kelly@corp.example"),
                (1, "first", "kelly@corp.example"),
            ):
                user = User(
                    id=UserId(uuid.UUID(int=number)),
                    password=PasswordHash.unusable(),
                    username=name,
                    email=email,
                )
                await uow.users.add(user)
            await uow.commit()
        async with AuthUnitOfWork() as uow:
            users = await uow.users.find_by_email("KELLY@corp.example")
        return [user.get_username() for user in users]

    assert run_async(scenario()) == ["first", "second"]


def test_addresses_that_cannot_take_mail_are_skipped(
    identity: Api, caplog: pytest.LogCaptureFixture
) -> None:
    """One stored before the checks is logged by user id; nothing is sent there."""
    eve = create_user("eve", email="eve@corp.example")
    set_email("eve", "eve@-corp.example")
    identity()

    with caplog.at_level("WARNING", "domainkit.contrib.auth.recovery"):
        run_async(send_password_reset_code("eve"))
        run_async(send_email_verification(eve))

    assert mail.outbox == []
    assert caplog.messages == [f"User {eve} has an address that cannot take mail."] * 2
    assert "corp.example" not in caplog.text
