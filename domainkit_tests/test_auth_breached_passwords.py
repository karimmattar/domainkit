"""Breached passwords (``AUTH.BREACHED_PASSWORDS``), and rules that are async.

Every call to the range API is faked (``stub``): nothing reaches the network.
"""

import asyncio
import io
import logging
import sys
from collections.abc import Awaitable, Generator, Iterator
from typing import Any

import httpx2
import pytest
from pydantic import ValidationError

from domainkit import deadlines
from domainkit.conf import override_settings
from domainkit.conf.env import project_prefix
from domainkit.contrib import mail
from domainkit.contrib.auth import (
    BreachedPasswordSettings,
    InvalidPassword,
    PasswordValidationError,
    avalidate_password,
    password_help_texts,
    validate_password,
)
from domainkit.contrib.auth.management.commands import changepassword
from domainkit.contrib.auth.validators import (
    BREACHED,
    BreachedPasswordsUnavailable,
    BreachedPasswordValidator,
    MinimumLengthValidator,
    NumericPasswordValidator,
    PasswordValidator,
)
from domainkit.exceptions import ImproperlyConfigured
from domainkit.i18n import override
from domainkit.management import CommandError, call_command
from domainkit.management.base import run_async
from domainkit.outbound.client import CallTimeout, stub
from domainkit_tests.conftest import FAST_HASHER, Api, UseAuth
from domainkit_tests.support.auth import PASSWORD, bearer, create_user, login
from domainkit_tests.support.passwords import BREACHED_ON, LEAKED, pwned, sha1
from domainkit_tests.test_auth_admin import ADMIN, error, staff
from domainkit_tests.test_auth_commands import Prompts, prompts, stored
from domainkit_tests.test_auth_password_preset import log_in
from domainkit_tests.test_auth_recovery import run_worker
from domainkit_tests.test_auth_reset_codes import ask_reset, code_in, reset
from domainkit_tests.test_auth_signup_codes import BY_MAIL, ask, finish

__all__ = ["prompts"]  # The fixture the command tests take.

LOGGER = "domainkit.contrib.auth.validators"
CLIENT_LOGGER = "domainkit.http.client"
RANGE = "https://api.pwnedpasswords.com/range/"
NEW = "Zq8!wL-d4#Kp7v"
ARABIC = "ظهرت كلمة المرور هذه في تسريب للبيانات: اختر كلمة مرور أخرى."
HELP = "Your password can't be one that has appeared in a data breach."
NO_RETRIES = {"HTTP_CLIENT": {"RETRIES": 0}}
COMMANDS = {"PASSWORD_HASHERS": [FAST_HASHER], **BREACHED_ON}


@pytest.fixture
def no_retries() -> Iterator[None]:
    """Outgoing calls fail on their first try: no backoff to wait for."""
    with override_settings(**NO_RETRIES):
        yield


def checker(**settings: Any) -> BreachedPasswordValidator:  # noqa: ANN401 - keys.
    """A breached-password check of its own (its own circuit), with ``settings``."""
    return BreachedPasswordValidator(BreachedPasswordSettings(**settings))


async def hang(request: httpx2.Request) -> httpx2.Response:
    """A range API that never answers."""
    await asyncio.Event().wait()
    raise AssertionError  # pragma: no cover - never reached.


def down(request: httpx2.Request) -> httpx2.Response:
    """A range API that cannot be reached."""
    raise httpx2.ConnectError("no route")


def answering(status: int, text: str) -> Any:  # noqa: ANN401 - a stub's handler.
    """A range API that answers ``status`` with ``text``."""
    return lambda request: httpx2.Response(status, text=text)


class SlowRule:
    """An async rule: no password with "slow" in it."""

    async def validate(self, password: str, user: object | None = None) -> None:
        """Refuse "slow", after a turn of the loop, as a call would take."""
        await asyncio.sleep(0)
        if "slow" in password:
            raise PasswordValidationError("This password is slow.")

    def help_text(self) -> str:
        """Describe the rule."""
        return "Your password can't be slow."


class Later:
    """An awaitable that is not a coroutine, and refuses when awaited."""

    def __await__(self) -> Generator[None]:
        """Refuse, without suspending."""
        raise PasswordValidationError("This password is for later.")
        yield  # pragma: no cover - makes this a generator.


class LaterRule:
    """A sync ``validate`` that returns an awaitable (``Later``)."""

    def validate(self, password: str, user: object | None = None) -> Awaitable[None]:
        """Hand back what refuses once awaited."""
        return Later()

    def help_text(self) -> str:
        """Describe the rule."""
        return "Later."


# The check -----------------------------------------------------------------------


@pytest.mark.anyio
async def test_a_breached_password_is_refused_from_a_prefix_alone() -> None:
    """Five characters of the SHA-1 are sent, padded; the rest is compared here."""
    seen: list[httpx2.Request] = []
    digest = sha1(LEAKED)

    with stub(pwned(LEAKED, seen=seen)), pytest.raises(PasswordValidationError) as no:
        await checker().validate(LEAKED)

    assert str(no.value) == BREACHED
    (request,) = seen
    assert (request.method, str(request.url)) == ("GET", RANGE + digest[:5])
    assert request.headers["Add-Padding"] == "true"
    sent = f"{request.url} {request.headers} {request.content!r}"
    assert digest[5:] not in sent.upper()
    assert LEAKED not in sent


@pytest.mark.anyio
async def test_a_clean_password_passes_and_padding_never_refuses() -> None:
    """A suffix not listed, or listed with the count 0 of padding, passes."""
    clean = sha1(PASSWORD)

    with stub(pwned(LEAKED)):
        await checker().validate(PASSWORD)
    with stub(answering(200, f"{clean[5:]}:0\r\n{'F' * 35}:9")):
        await checker().validate(PASSWORD)
    # A mirror may answer in lower case, and end lines with \n alone.
    lower = answering(200, f"{clean[5:].lower()}:2\n")
    with stub(lower), pytest.raises(PasswordValidationError):
        await checker().validate(PASSWORD)


@pytest.mark.anyio
async def test_min_count_sets_how_often_a_password_was_seen() -> None:
    """A password seen fewer times than ``MIN_COUNT`` passes."""
    with stub(pwned(LEAKED, count=2)):
        await checker(MIN_COUNT=3).validate(LEAKED)
        with pytest.raises(PasswordValidationError):
            await checker(MIN_COUNT=2).validate(LEAKED)


@pytest.mark.anyio
async def test_the_url_names_a_mirror() -> None:
    """``URL`` replaces Have I Been Pwned's; the prefix follows it."""
    seen: list[httpx2.Request] = []
    with stub(pwned(LEAKED, seen=seen)), pytest.raises(PasswordValidationError):
        await checker(URL="http://hashes.internal/range/").validate(LEAKED)

    assert str(seen[0].url) == f"http://hashes.internal/range/{sha1(LEAKED)[:5]}"


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("answer", "failure"),
    [
        (down, "ConnectError"),
        (answering(503, "busy"), "HTTPStatusError"),
        (answering(404, "Not found"), "HTTPStatusError"),
        (answering(200, "<html>Sign in to the proxy</html>"), "ValueError"),
        (answering(200, ""), "ValueError"),
        (answering(200, f"{'A' * 35}:1\r\n{'B' * 35}:many"), "ValueError"),
    ],
    ids=["unreachable", "unavailable", "not-found", "a-page", "empty", "garbled"],
)
async def test_an_outage_lets_the_password_through_by_default(
    no_retries: None,
    caplog: pytest.LogCaptureFixture,
    answer: Any,  # noqa: ANN401 - a stub's handler.
    failure: str,
) -> None:
    """Fail open: the other rules still apply; a warning says what happened."""
    caplog.set_level(logging.DEBUG)
    with stub(answer):
        await checker().validate(LEAKED)
        with pytest.raises(BreachedPasswordsUnavailable) as refused:
            await checker(FAIL_CLOSED=True).validate(LEAKED)

    assert (refused.value.status_code, refused.value.code) == (
        503,
        "breached_passwords_unavailable",
    )
    assert refused.value.detail == (
        "Passwords cannot be checked against data breaches now; try again later."
    )
    failed = f"The breached-password check failed ({failure}); the password was"
    assert [r.getMessage() for r in caplog.records if r.name == LOGGER] == [
        f"{failed} let through",
        f"{failed} refused",
    ]
    # domainkit's logs name the API's origin, never the prefix that was sent
    # (build_logging_config quiets httpx2's own lines, which show it).
    assert not [
        record
        for record in caplog.records
        if record.name.startswith("domainkit")
        and sha1(LEAKED)[:5] in record.getMessage()
    ]


@pytest.mark.anyio
async def test_a_slow_api_follows_the_fail_mode(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Past ``TIMEOUT``, retries included: through, or refused when closed."""
    caplog.set_level(logging.WARNING)
    with stub(hang):
        await checker(TIMEOUT=0.05).validate(LEAKED)
        with pytest.raises(BreachedPasswordsUnavailable) as refused:
            await checker(TIMEOUT=0.05, FAIL_CLOSED=True).validate(LEAKED)

    assert isinstance(refused.value.__cause__, CallTimeout)
    assert str(refused.value.__cause__) == (
        "GET https://api.pwnedpasswords.com took longer than 0.05s."
    )
    assert [r.getMessage() for r in caplog.records if r.name == CLIENT_LOGGER] == [
        "GET https://api.pwnedpasswords.com took longer than its 0.05s"
    ] * 2


@pytest.mark.anyio
async def test_the_requests_deadline_is_not_waited_out() -> None:
    """The work's own deadline goes up as it is: nothing is let through for it."""
    with stub(hang), pytest.raises(TimeoutError):
        async with deadlines.timeout(0.05):
            await checker(TIMEOUT=10).validate(LEAKED)


@pytest.mark.parametrize(
    "settings",
    [
        {"URL": "ftp://hashes.internal/range/"},
        {"URL": "https://api.pwnedpasswords.com/range"},
        {"URL": "https://api.pwnedpasswords.com/range/?mode=ntlm/"},
        {"TIMEOUT": 0},
        {"TIMEOUT": 10.5},
        {"TIMEOUT": float("inf")},
        {"MIN_COUNT": 0},
        {"ON": True},
    ],
)
def test_the_settings_are_bounded(settings: dict[str, Any]) -> None:
    """An http(s) URL ending in ``/``, up to 10 seconds, a count of 1 or more."""
    with pytest.raises(ValidationError):
        BreachedPasswordSettings(**settings)


def test_the_settings_refuse_to_start_on_a_bad_value(use_auth: UseAuth) -> None:
    """Startup names the key, as for every ``AUTH`` setting."""
    with pytest.raises(ImproperlyConfigured, match="BREACHED_PASSWORDS"):
        use_auth(BREACHED_PASSWORDS={"ENABLED": True, "TIMEOUT": 60})


def test_without_the_httpclient_extra_it_refuses_to_start(
    use_auth: UseAuth, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Turned on, it needs ``domainkit[httpclient]``; off, nothing is imported."""
    monkeypatch.setitem(sys.modules, "domainkit.outbound.client", None)
    use_auth()
    with pytest.raises(ImproperlyConfigured, match=r"domainkit\[httpclient\]"):
        BreachedPasswordValidator()
    with pytest.raises(ImproperlyConfigured, match="the httpclient extra"):
        use_auth(BREACHED_PASSWORDS={"ENABLED": True, "TIMEOUT": 1.25})


# Async rules -----------------------------------------------------------------------


@pytest.mark.anyio
async def test_async_and_sync_rules_run_together_in_order() -> None:
    """Each is awaited if it must be; every refusal is listed, in order."""
    rules: list[PasswordValidator] = [
        MinimumLengthValidator(),
        SlowRule(),
        NumericPasswordValidator(),
    ]

    await avalidate_password("quick-enough", validators=rules)
    with pytest.raises(InvalidPassword) as refused:
        await avalidate_password("slow", validators=rules)
    with pytest.raises(InvalidPassword) as later:
        await avalidate_password("any-thing-1", validators=[LaterRule()])

    assert refused.value.errors == [
        "This password is too short. It must contain at least 8 characters.",
        "This password is slow.",
    ]
    assert later.value.errors == ["This password is for later."]


@pytest.mark.parametrize("rule", [SlowRule(), LaterRule()], ids=["async", "awaitable"])
def test_validate_password_refuses_to_skip_an_async_rule(rule: Any) -> None:  # noqa: ANN401
    """The sync function cannot await it, so it never takes the password unchecked."""
    with pytest.raises(ImproperlyConfigured) as refused:
        validate_password("slow", validators=[NumericPasswordValidator(), rule])

    assert str(refused.value) == (
        f"{type(rule).__name__} checks passwords asynchronously: await "
        "avalidate_password() instead of validate_password()."
    )


def test_the_setting_adds_the_check_after_the_rules(use_auth: UseAuth) -> None:
    """``ENABLED`` runs it last, once per settings; off, the rules are as before."""
    use_auth()
    assert HELP not in password_help_texts()

    use_auth(**BREACHED_ON)
    with stub(pwned(LEAKED)), pytest.raises(InvalidPassword) as refused:
        run_async(avalidate_password(LEAKED))
    with stub(pwned(LEAKED)):
        run_async(avalidate_password(PASSWORD))

    assert refused.value.errors == [BREACHED]
    assert password_help_texts()[-1] == HELP
    with pytest.raises(ImproperlyConfigured, match="BreachedPasswordValidator"):
        validate_password(PASSWORD)


def test_the_refusal_and_the_rule_speak_arabic(use_auth: UseAuth) -> None:
    """The message is translated into the request's language, as the rule is."""
    use_auth(**BREACHED_ON)

    with override("ar"), stub(pwned(LEAKED)):
        with pytest.raises(InvalidPassword) as refused:
            run_async(avalidate_password(LEAKED))
        rules = password_help_texts()

    assert refused.value.errors == [ARABIC]
    assert rules[-1] == "لا يمكن أن تكون كلمة مرورك قد ظهرت في تسريب للبيانات."


# Where passwords are chosen --------------------------------------------------------


def test_a_password_change_refuses_a_breached_one(identity: Api) -> None:
    """``POST /auth/password``: 422 with the reason; a clean one is set."""
    client = identity(**BREACHED_ON)
    create_user("alice")
    headers = bearer(login(client, "alice")["access_token"])

    def change(new: str) -> Any:  # noqa: ANN401 - a response.
        return client.post(
            "/auth/password",
            json={"old_password": PASSWORD, "new_password": new},
            headers=headers,
        )

    with stub(pwned(LEAKED)):
        refused = change(LEAKED)
        changed = change(NEW)

    assert refused.status_code == 422
    assert refused.json()["code"] == "invalid_password"
    assert refused.json()["detail"] == BREACHED
    assert changed.status_code == 204


def test_an_async_rule_of_the_projects_is_awaited(identity: Api) -> None:
    """A rule with ``async def validate`` refuses, as a sync one would."""
    client = identity(
        PASSWORD_VALIDATORS=[{"NAME": f"{__name__}.SlowRule"}],
    )
    create_user("alice")
    headers = bearer(login(client, "alice")["access_token"])

    refused = client.post(
        "/auth/password",
        json={"old_password": PASSWORD, "new_password": "slow-and-long"},
        headers=headers,
    )

    assert (refused.status_code, refused.json()["detail"]) == (
        422,
        "This password is slow.",
    )


def test_a_reset_with_a_breached_password_keeps_the_code(identity: Api) -> None:
    """``/password-reset/code``: refused, the code still works for a clean one."""
    client = identity(IDENTIFIERS=("email",), CODES_TO_UNPROVED=True, **BREACHED_ON)
    create_user(None, email="ann@example.com")
    ask_reset(client, identifier="ann@example.com")
    run_worker()
    code = code_in(mail.outbox[-1].body)

    with stub(pwned(LEAKED)):
        refused = reset(client, "ann@example.com", code, LEAKED)
        done = reset(client, "ann@example.com", code, NEW)

    assert refused == (422, "invalid_password")
    assert done == (204, None)


def test_a_signup_with_a_breached_password_keeps_the_code(identity: Api) -> None:
    """``/signup/code``: refused, the code still makes the account after."""
    client = identity(**BY_MAIL, **BREACHED_ON)
    ask(client, email="nina@example.com")
    run_worker()
    code = code_in(mail.outbox[-1].body)

    with stub(pwned(LEAKED)):
        refused = finish(client, "nina@example.com", code, LEAKED)
        done = finish(client, "nina@example.com", code, NEW)

    assert refused == (422, "invalid_password")
    assert done == (204, None)


def test_the_admin_refuses_a_breached_password(api: Api) -> None:
    """Making a user, and setting a user's password."""
    client = api(**BREACHED_ON)
    headers = staff("admin", "auth.add_user", "auth.change_user")
    bob = create_user("bob")

    with stub(pwned(LEAKED)):
        made = client.post(
            f"{ADMIN}/users",
            json={"username": "carol", "password": LEAKED},
            headers=headers,
        )
        set_ = client.post(
            f"{ADMIN}/users/{bob}/password", json={"password": LEAKED}, headers=headers
        )
        clean = client.post(
            f"{ADMIN}/users/{bob}/password", json={"password": NEW}, headers=headers
        )

    assert error(made, 422) == ("invalid_password", BREACHED)
    assert error(set_, 422) == ("invalid_password", BREACHED)
    assert clean.status_code == 204


def test_a_first_login_refuses_a_breached_new_password(api: Api) -> None:
    """A password staff set is replaced by a clean one only."""
    client = api(PASSWORD_CHANGE_ON_FIRST_USE=True, **BREACHED_ON)
    headers = staff("admin", "auth.add_user")
    with stub(pwned(LEAKED)):
        made = client.post(
            f"{ADMIN}/users",
            json={"username": "alice", "password": NEW},
            headers=headers,
        )
        refused = log_in(client, NEW, new_password=LEAKED)
        changed = log_in(client, NEW, new_password=PASSWORD)

    assert made.status_code == 201
    assert (refused.status_code, refused.json()["detail"]) == (422, BREACHED)
    assert changed.status_code == 200


def test_fail_closed_answers_503_where_the_api_is_down(identity: Api) -> None:
    """The route answers ``breached_passwords_unavailable``; nothing changes."""
    client = identity(
        BREACHED_PASSWORDS={"ENABLED": True, "FAIL_CLOSED": True, "TIMEOUT": 0.05}
    )
    create_user("alice")
    headers = bearer(login(client, "alice")["access_token"])

    with stub(hang):
        refused = client.post(
            "/auth/password",
            json={"old_password": PASSWORD, "new_password": NEW},
            headers=headers,
        )
    relogged = client.post(
        "/auth/login", json={"username": "alice", "password": PASSWORD}
    )

    assert refused.status_code == 503
    assert refused.json()["code"] == "breached_passwords_unavailable"
    assert relogged.status_code == 200


# Commands --------------------------------------------------------------------------


def test_the_commands_check_breached_passwords(
    auth_project: str, prompts: Prompts, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``changepassword`` asks again; ``createsuperuser`` refuses what it was given."""
    create_user("alice")
    prompts(changepassword.Command, secret=[LEAKED, LEAKED, NEW, NEW])
    monkeypatch.setenv(f"{project_prefix()}SUPERUSER_PASSWORD", LEAKED)
    stderr = io.StringIO()
    with override_settings(AUTH=COMMANDS), stub(pwned(LEAKED)):
        call_command("changepassword", "alice", stdout=io.StringIO(), stderr=stderr)
        with pytest.raises(CommandError) as refused:
            call_command(
                "createsuperuser",
                "--noinput",
                "--username",
                "root",
                stdout=io.StringIO(),
            )

    assert stderr.getvalue().splitlines()[-1] == BREACHED
    assert str(refused.value) == BREACHED
    assert stored("root") is None


def test_the_commands_stop_where_the_api_fails_closed(
    auth_project: str, prompts: Prompts
) -> None:
    """A rule that could not check is no reason to ask again: the command stops."""
    create_user("alice")
    prompts(changepassword.Command, secret=[NEW, NEW])
    closed = {**COMMANDS, "BREACHED_PASSWORDS": {"ENABLED": True, "FAIL_CLOSED": True}}
    with (
        override_settings(AUTH=closed, **NO_RETRIES),
        stub(down),
        pytest.raises(CommandError) as refused,
    ):
        call_command("changepassword", "alice", stdout=io.StringIO())

    assert str(refused.value) == BreachedPasswordsUnavailable.detail
