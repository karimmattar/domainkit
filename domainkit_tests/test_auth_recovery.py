"""Password resets and email verification by links sent through the outbox."""

import re
import uuid
from datetime import timedelta
from types import SimpleNamespace
from typing import Any, cast
from urllib.parse import unquote

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from domainkit import setup, signing
from domainkit.apps import registry
from domainkit.conf import UNSET, override_settings
from domainkit.contrib import mail
from domainkit.contrib.auth import AbstractBaseUser, AuthUnitOfWork, UserId
from domainkit.contrib.auth.onetime import (
    email_of,
    email_verification_tokens,
    password_reset_tokens,
)
from domainkit.contrib.auth.recovery import describe
from domainkit.contrib.auth.settings import AuthSettings
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
    PASSWORD,
    audited,
    bearer,
    create_user,
    login,
    stored_tokens,
)

NEW_PASSWORD = "an0ther-Strong-pass!"
MAIL_APP = "domainkit.contrib.mail"
TOKEN = re.compile(r"[0-9a-f]{32}:[0-9A-Za-z]+:[A-Za-z0-9_-]{43}")


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


def token_in(message: mail.EmailMessage) -> str:
    """The token in a link message."""
    found = TOKEN.search(unquote(message.body))
    assert found is not None, message.body
    return found[0]


def from_address(client: TestClient, address: str) -> TestClient:
    """Another client of the same application, from ``address``."""
    return TestClient(client.app, client=(address, 1))


def reset(client: TestClient, email: str) -> dict[str, Any]:
    """Ask for a reset; return the 202 body."""
    response = client.post("/auth/password-reset", json={"email": email})
    assert response.status_code == 202, response.json()
    body: dict[str, Any] = response.json()
    return body


def confirm(client: TestClient, token: str, password: str = NEW_PASSWORD) -> Any:  # noqa: ANN401 - a response.
    """Set a new password with ``token``."""
    return client.post(
        "/auth/password-reset/confirm",
        json={"token": token, "new_password": password},
    )


def test_a_reset_link_sets_a_new_password_and_ends_every_session(
    identity: Api,
) -> None:
    """The link goes by mail; using it changes the password, once."""
    alice = create_user("alice", email="Alice@Example.com")
    client = identity()
    session = login(client, "alice")["access_token"]

    body = reset(client, "alice@EXAMPLE.com")

    assert body == {
        "detail": "If an account uses this address, a reset link is on its way."
    }
    assert outbox_payloads() == [{"email": "alice@EXAMPLE.com"}]
    assert mail.outbox == []
    assert run_worker() == 1
    (message,) = mail.outbox
    assert message.to == ("Alice@example.com",)
    assert message.subject == "Reset your password"
    assert "Hello alice," in message.body
    assert "within 1 hour" in message.body
    token = token_in(message)

    assert confirm(client, token).status_code == 204

    assert client.get("/auth/me", headers=bearer(session)).status_code == 401
    assert stored_tokens(alice) == []
    login(client, "alice", NEW_PASSWORD)
    refused = client.post(
        "/auth/login", json={"username": "alice", "password": PASSWORD}
    )
    assert refused.status_code == 401
    again = confirm(client, token, "yet-another-Pass-9")
    assert (again.status_code, again.json()["code"]) == (400, "invalid_token")
    assert again.json()["detail"] == "The link is invalid or has expired."
    (requested,) = audited("auth.password_reset_requested")
    done, refused_again = audited("auth.password_reset")
    assert requested["details"] == done["details"] == {"user_id": str(alice)}
    assert (done["outcome"], done["actor"]) == ("success", "anonymous")
    assert (refused_again["outcome"], refused_again["details"]) == (
        "denied",
        {"reason": "invalid_token"},
    )
    assert len(audited("auth.password_changed")) == 1


def test_unknown_addresses_get_the_same_answer_and_no_mail(identity: Api) -> None:
    """Whether an account uses the address shows in neither status nor body."""
    create_user("alice", email="alice@example.com")
    client = identity()

    known = client.post("/auth/password-reset", json={"email": "alice@example.com"})
    unknown = client.post("/auth/password-reset", json={"email": "nobody@example.com"})

    assert known.status_code == unknown.status_code == 202
    assert known.json() == unknown.json()
    assert set(known.headers) == set(unknown.headers)
    assert len(outbox_payloads()) == 2
    run_worker()
    assert [message.to for message in mail.outbox] == [("alice@example.com",)]


def test_only_active_users_with_a_password_get_a_link(identity: Api) -> None:
    """Every such user of the address gets one; the others get nothing."""
    create_user("one", email="shared@example.com")
    create_user("two", email="SHARED@example.com")
    create_user("off", email="shared@example.com", is_active=False)
    create_user("sso", None, email="shared@example.com")
    client = identity()

    reset(client, "shared@example.com")
    run_worker()

    assert sorted(message.to[0] for message in mail.outbox) == [
        "SHARED@example.com",
        "shared@example.com",
    ]
    assert sorted(message.body.split(",")[0] for message in mail.outbox) == [
        "Hello one",
        "Hello two",
    ]


def test_links_point_to_the_configured_page(identity: Api) -> None:
    """``PASSWORD_RESET_URL`` takes the token, quoted, where it says."""
    create_user("alice", email="alice@example.com")
    client = identity(
        PASSWORD_RESET_URL="https://app.example.com/reset?token={token}&v=1"
    )

    reset(client, "alice@example.com")
    run_worker()

    (message,) = mail.outbox
    (link,) = [line for line in message.body.splitlines() if line.startswith("https")]
    prefix, _, rest = link.partition("token=")
    token, _, suffix = rest.partition("&")
    assert (prefix, suffix) == ("https://app.example.com/reset?", "v=1")
    assert "%3A" in token
    assert confirm(client, unquote(token)).status_code == 204


def test_link_urls_need_a_place_for_the_token() -> None:
    """A URL without ``{token}`` would send links that do nothing."""
    with pytest.raises(ValueError, match=r"needs \{token\}"):
        AuthSettings(PASSWORD_RESET_URL="https://app.example.com/reset")
    with pytest.raises(ValueError, match=r"needs \{token\}"):
        AuthSettings(EMAIL_VERIFICATION_URL="https://app.example.com/verify")


def test_reset_links_expire(identity: Api, monkeypatch: pytest.MonkeyPatch) -> None:
    """Past ``PASSWORD_RESET_TIMEOUT`` the link is refused, like a wrong one."""
    create_user("alice", email="alice@example.com")
    client = identity(PASSWORD_RESET_TIMEOUT=timedelta(minutes=30))
    reset(client, "alice@example.com")
    run_worker()
    (message,) = mail.outbox
    assert "within 30 minutes" in message.body
    now = signing._now()
    monkeypatch.setattr(signing, "_now", lambda: now + 30 * 60 + 1)

    response = confirm(client, token_in(message))

    assert (response.status_code, response.json()["code"]) == (400, "invalid_token")


def test_logging_in_ends_a_reset_link(identity: Api) -> None:
    """The link rests on the last login: a login since then ends it."""
    create_user("alice", email="alice@example.com")
    client = identity()
    reset(client, "alice@example.com")
    run_worker()

    login(client, "alice")

    assert confirm(client, token_in(mail.outbox[0])).status_code == 400


def test_weak_passwords_leave_the_link_working(identity: Api) -> None:
    """The rules refuse a password with 422; the link still works after."""
    create_user("alice", email="alice@example.com")
    client = identity()
    reset(client, "alice@example.com")
    run_worker()
    token = token_in(mail.outbox[0])

    weak = confirm(client, token, "alice")

    assert weak.status_code == 422
    assert "too similar to the username" in weak.json()["detail"]
    assert confirm(client, token).status_code == 204


@pytest.mark.parametrize(
    "token",
    [
        "nonsense",
        f"6f1c9a3e2b7d4e8f9a0b1c2d3e4f5a6b:1abc:{'A' * 43}",
        f"6F1C9A3E2B7D4E8F9A0B1C2D3E4F5A6B:1abc:{'A' * 43}",
        "x" * 256,
    ],
    ids=["nonsense", "no-such-user", "upper-case-id", "longest"],
)
def test_wrong_tokens_are_refused_and_audited(identity: Api, token: str) -> None:
    """Any wrong token gets the same 400, and an audit row without the token."""
    client = identity()

    response = confirm(client, token)

    assert (response.status_code, response.json()["code"]) == (400, "invalid_token")
    (refused,) = audited("auth.password_reset")
    assert (refused["outcome"], refused["details"]) == (
        "denied",
        {"reason": "invalid_token"},
    )


def test_tokens_too_long_are_refused_before_any_work(identity: Api) -> None:
    """Over 256 characters, the body itself is refused."""
    response = confirm(identity(), "x" * 257)

    assert response.status_code == 422
    assert audited("auth.password_reset") == []


def test_a_token_for_one_purpose_or_user_fits_no_other(identity: Api) -> None:
    """A verification token resets nothing; one user's token is not another's."""
    alice = create_user("alice", email="alice@example.com")
    bob = create_user("bob", email="bob@example.com")

    async def tokens() -> tuple[str, str, str]:
        async with AuthUnitOfWork() as uow:
            a = await uow.users.get(UserId(alice))
            b = await uow.users.get(UserId(bob))
        reset_a = password_reset_tokens.make_token(a)
        forged = f"{bob.hex}{reset_a[32:]}"
        assert password_reset_tokens.check_token(a, reset_a)
        assert not password_reset_tokens.check_token(b, reset_a)
        assert not password_reset_tokens.check_token(None, reset_a)
        assert not email_verification_tokens.check_token(a, reset_a)
        return email_verification_tokens.make_token(a), forged, reset_a

    verification, forged, _ = run_async(tokens())
    client = identity()

    assert confirm(client, verification).status_code == 400
    assert confirm(client, forged).status_code == 400


def test_reading_a_token_never_trusts_it() -> None:
    """The user id is read only from a token's form, for the lookup."""
    user = uuid.uuid4()

    assert password_reset_tokens.user_id(f"{user.hex}:1:sig") == user
    for token in (
        "",
        f"{user.hex}",
        f"{user.hex[:-1]}:1:s",
        f"{user.hex.upper()}:1:s",
        f"{user}:1:s",
        f"{user.hex}:" + "x" * 256,
    ):
        expected = user if token == f"{user.hex}" else None
        assert password_reset_tokens.user_id(token) == expected


def test_resets_and_checks_are_throttled(identity: Api) -> None:
    """Mail per address and per recipient; token checks per address alone."""
    create_user("alice", email="alice@example.com")
    client = identity(EMAIL_RATE="2/hour", LOGIN_RATE="2/minute")
    other = from_address(client, "198.51.100.7")

    reset(client, "alice@example.com")
    reset(client, "nobody@example.com")
    by_address = client.post("/auth/password-reset", json={"email": "x@example.com"})
    reset(other, "alice@example.com")
    by_recipient = other.post(
        "/auth/password-reset", json={"email": "ALICE@example.com"}
    )

    for response in (by_address, by_recipient):
        assert (response.status_code, response.json()["code"]) == (429, "throttled")
        assert response.headers["Retry-After"]
    assert len(outbox_payloads()) == 3
    throttled = audited("auth.password_reset")
    assert [row["details"] for row in throttled] == [{"reason": "throttled"}] * 2

    run_worker()
    token = token_in(mail.outbox[0])
    third = from_address(client, "203.0.113.9")
    assert confirm(third, "wrong").status_code == 400
    assert confirm(third, "wrong").status_code == 400
    assert confirm(third, token).status_code == 429  # This address is done.
    fourth = from_address(client, "203.0.113.10")
    assert confirm(fourth, f"{token[:-1]}x").status_code == 400
    assert confirm(fourth, f"{token[:-2]}xx").status_code == 400
    # Forged tokens naming alice use up no count of hers: her link works.
    assert confirm(from_address(client, "203.0.113.11"), token).status_code == 204


def test_email_verification_marks_the_address_verified(identity: Api) -> None:
    """A user asks for a link; using it verifies the address, once."""
    alice = create_user("alice", email="alice@example.com")
    client = identity()
    headers = bearer(login(client, "alice")["access_token"])
    assert client.get("/auth/me", headers=headers).json()["email_verified"] is False

    response = client.post("/auth/email-verification", headers=headers)

    assert response.status_code == 202
    assert response.json() == {"detail": "A link to verify your address is on its way."}
    assert outbox_payloads() == [{"user_id": str(alice)}]
    run_worker()
    (message,) = mail.outbox
    assert (message.subject, message.to) == (
        "Verify your email address",
        ("alice@example.com",),
    )
    assert "within 3 days" in message.body
    token = token_in(message)
    verified = client.post("/auth/email-verification/confirm", json={"token": token})
    assert verified.status_code == 204
    assert client.get("/auth/me", headers=headers).json()["email_verified"] is True
    again = client.post("/auth/email-verification/confirm", json={"token": token})
    assert (again.status_code, again.json()["code"]) == (400, "invalid_token")
    (event,) = audited("auth.email_verified")
    assert event["details"] == {"user_id": str(alice)}
    (refused,) = audited("auth.email_verification")
    assert refused["details"] == {"reason": "invalid_token"}

    client.post("/auth/email-verification", headers=headers)
    run_worker()
    assert len(mail.outbox) == 1  # Verified already: nothing more is sent.


def test_a_new_address_must_be_verified_again(identity: Api) -> None:
    """Changing the email unverifies it, and ends links sent to the old one."""
    alice = create_user("alice", email="alice@example.com")
    client = identity()
    headers = bearer(login(client, "alice")["access_token"])
    client.post("/auth/email-verification", headers=headers)
    run_worker()
    token = token_in(mail.outbox[0])

    async def change_email() -> None:
        async with AuthUnitOfWork() as uow:
            user = await uow.users.get(UserId(alice))
            user.update(email="new@example.com")
            await uow.commit()

    run_async(change_email())

    assert (
        client.post("/auth/email-verification/confirm", json={"token": token})
    ).status_code == 400


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
    from domainkit.contrib.auth.recovery import send_email_verification

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


def test_models_without_the_mark_get_no_verification_link(
    identity: Api, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A user model without ``email_verified`` has nothing a link could set."""
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


def test_verification_tokens_hold_for_models_without_the_mark(identity: Api) -> None:
    """The token rests on the address alone then."""
    user = cast(AbstractBaseUser, unmarked_user())

    token = email_verification_tokens.make_token(user)

    assert email_verification_tokens.check_token(user, token)


def test_verification_links_point_to_the_configured_page(identity: Api) -> None:
    """``EMAIL_VERIFICATION_URL`` takes the token where it says."""
    create_user("alice", email="alice@example.com")
    client = identity(EMAIL_VERIFICATION_URL="https://app.example.com/verify/{token}")
    headers = bearer(login(client, "alice")["access_token"])

    client.post("/auth/email-verification", headers=headers)
    run_worker()

    (message,) = mail.outbox
    (link,) = [line for line in message.body.splitlines() if line.startswith("https")]
    prefix, token = link[:31], unquote(link[31:])
    assert prefix == "https://app.example.com/verify/"
    confirmed = client.post("/auth/email-verification/confirm", json={"token": token})
    assert confirmed.status_code == 204


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
    from domainkit.contrib.auth.access import open_routes

    identity()

    assert open_routes(get_asgi_application()) == []


def test_unmounted_routers_need_nothing(api: Api) -> None:
    """Without the mailing routes mounted, mail is not required."""
    api()

    assert application_problems(get_asgi_application()) == []


def test_reset_tasks_need_mail_installed(identity: Api) -> None:
    """A link the worker cannot mail fails the task, to be retried."""
    from domainkit.contrib.auth.recovery import send_password_reset

    create_user("alice", email="alice@example.com")
    identity()
    with (
        override_settings(MAIL={"BACKEND": "domainkit_tests.test_auth_recovery.Nope"}),
        pytest.raises(ImproperlyConfigured, match=r"MAIL\.BACKEND"),
    ):
        run_async(send_password_reset("alice@example.com"))


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
    "email", ["a\x00@example.com", "a@exam\x7fple.com", "a@b@c", "no-at", "a b@c.d"]
)
def test_addresses_that_cannot_be_one_are_refused_at_once(
    identity: Api, email: str
) -> None:
    """Control characters, spaces, and extra @ never reach the outbox."""
    response = identity().post("/auth/password-reset", json={"email": email})

    assert response.status_code == 422
    assert outbox_payloads() == []
