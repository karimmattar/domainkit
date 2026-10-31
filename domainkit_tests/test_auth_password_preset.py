"""PCI DSS v4.0's passwords: the preset, the history, and the change on first use."""

import io
import itertools
from datetime import timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from domainkit import clock
from domainkit.checks import CheckFailed
from domainkit.clock import freeze_time
from domainkit.contrib.auth import (
    AuthUnitOfWork,
    InvalidPassword,
    PasswordHash,
    UserId,
    acheck_password,
    amake_password,
    repositories,
)
from domainkit.contrib.auth.management.commands import changepassword
from domainkit.contrib.auth.settings import (
    AuthSettings,
    ScaSettings,
    StepUpSettings,
    _with_preset,
    with_a_factor,
)
from domainkit.contrib.auth.tables import password_history
from domainkit.contrib.auth.validators import (
    LettersAndDigitsValidator,
    PasswordValidationError,
    preset_contradictions,
)
from domainkit.db import connections
from domainkit.management import call_command
from domainkit.management.base import run_async
from domainkit_tests.conftest import Api, UseAuth
from domainkit_tests.test_auth_admin import ADMIN, ADMIN_PERMISSIONS, staff
from domainkit_tests.test_auth_http import (
    AUTHENTICATION,
    PASSWORD,
    audited,
    bearer,
    create_user,
)
from domainkit_tests.test_auth_stepup import enrol, now_code

VALIDATORS = "domainkit.contrib.auth.validators"
PCI = {"PASSWORD_PRESET": "pci-dss-4"}
STRONG = ["Kq7-zv93-Lmw2", "Pw4-tt81-Xcv9", "Rr5-yy62-Bnm3", "Ss6-uu73-Qaz4"]
"""Passwords the preset takes: 13 characters, letters and digits."""


def test_letters_and_digits() -> None:
    """Both, of any script."""
    validator = LettersAndDigitsValidator()
    for good in ("abc1", "1a", "مرحبا١"):
        validator.validate(good)
    for bad in ("abcdefgh", "12345678", "!!!!----", ""):
        with pytest.raises(PasswordValidationError, match="letters and digits"):
            validator.validate(bad)
    assert validator.help_text() == (
        "Your password must contain at least one letter and one digit."
    )


def test_the_preset_sets_what_is_left_unset() -> None:
    """Its validators, history, first use, rate, and idle timeout; nothing else."""
    config = AuthSettings(**PCI)
    plain = AuthSettings()

    assert [validator.NAME for validator in config.PASSWORD_VALIDATORS] == [
        f"{VALIDATORS}.UserAttributeSimilarityValidator",
        f"{VALIDATORS}.MinimumLengthValidator",
        f"{VALIDATORS}.CommonPasswordValidator",
        f"{VALIDATORS}.LettersAndDigitsValidator",
    ]
    assert config.PASSWORD_VALIDATORS[1].OPTIONS == {"min_length": 12}
    assert (4, True, "10/hour", timedelta(minutes=13)) == (
        config.PASSWORD_HISTORY,
        config.PASSWORD_CHANGE_ON_FIRST_USE,
        config.LOGIN_RATE,
        config.IDLE_TIMEOUT,
    )
    assert preset_contradictions(config) == []
    assert (
        plain.PASSWORD_PRESET,
        plain.PASSWORD_HISTORY,
        plain.PASSWORD_CHANGE_ON_FIRST_USE,
        plain.LOGIN_RATE,
        plain.IDLE_TIMEOUT,
    ) == (None, 0, False, "10/minute", None)
    assert preset_contradictions(plain) == []
    assert AuthSettings(**PCI, PASSWORD_HISTORY=6).PASSWORD_HISTORY == 6
    assert _with_preset([PCI]) == [PCI]  # Not AUTH's keys: left for pydantic.


def test_presets_are_named() -> None:
    """An unknown preset is refused; so are histories past 24."""
    with pytest.raises(ValueError, match="pci-dss-4"):
        AuthSettings(PASSWORD_PRESET="pci-dss-3")
    with pytest.raises(ValueError, match="less than or equal to 24"):
        AuthSettings(PASSWORD_HISTORY=25)


@pytest.mark.parametrize(
    ("setting", "found"),
    [
        (
            {
                "PASSWORD_VALIDATORS": [
                    {"NAME": f"{VALIDATORS}.LettersAndDigitsValidator"}
                ]
            },
            "under 12 characters (8.3.6)",
        ),
        (
            {
                "PASSWORD_VALIDATORS": [
                    {
                        "NAME": f"{VALIDATORS}.MinimumLengthValidator",
                        "OPTIONS": {"min_length": 11},
                    },
                    {"NAME": f"{VALIDATORS}.LettersAndDigitsValidator"},
                ]
            },
            "under 12 characters (8.3.6)",
        ),
        (
            {
                "PASSWORD_VALIDATORS": [
                    {
                        "NAME": f"{VALIDATORS}.MinimumLengthValidator",
                        "OPTIONS": {"min_length": 16},
                    }
                ]
            },
            "without both letters and digits (8.3.6)",
        ),
        ({"PASSWORD_HISTORY": 3}, "PASSWORD_HISTORY is 3, under 4 (8.3.7)"),
        ({"PASSWORD_CHANGE_ON_FIRST_USE": False}, "(8.3.5)"),
        ({"LOGIN_RATE": None}, "LOGIN_RATE None allows"),
        ({"LOGIN_RATE": "11/hour"}, "LOGIN_RATE '11/hour' allows"),
        ({"LOGIN_RATE": "10/minute"}, "(8.3.4)"),
        ({"LOGIN_RATE": "2/minute"}, "(8.3.4)"),
        ({"IDLE_TIMEOUT": None}, "IDLE_TIMEOUT None"),
        ({"IDLE_TIMEOUT": timedelta(minutes=14)}, "13 minutes at most"),
        ({"PASSWORD_CHECKS_COUNT_AS_LOGINS": False}, "(8.3.4)"),
        (
            {
                "AUTHENTICATION_CLASSES": [f"{AUTHENTICATION}.JWTAuthentication"],
                "JWT": {
                    "SIGNING_KEY": "k" * 32,
                    "ACCESS_TOKEN_TTL": timedelta(minutes=14),
                    "LEEWAY": timedelta(minutes=1, seconds=1),
                },
            },
            (
                "JWT.ACCESS_TOKEN_TTL 0:14:00 and LEEWAY 0:01:01 let a JSON Web "
                "Token login idle that long, unrefreshed (8.2.8): 15 minutes "
                "together at most"
            ),
        ),
    ],
)
def test_settings_that_contradict_the_preset_are_refused(
    use_auth: UseAuth, setting: dict[str, Any], found: str
) -> None:
    """``auth.E010``, in any environment, naming the requirement."""
    with pytest.raises(CheckFailed) as refused:
        use_auth(**PCI, **setting)

    [problem] = refused.value.messages
    assert getattr(problem, "id", None) == "auth.E010"
    assert found in str(problem)
    assert "AUTH.PASSWORD_PRESET 'pci-dss-4' is contradicted" in str(problem)


@pytest.mark.parametrize(
    "setting",
    [
        {"LOGIN_RATE": "10/day"},
        {"LOGIN_RATE": "5/hour"},
        {"LOGIN_RATE": "10/hour"},
        {"IDLE_TIMEOUT": timedelta(minutes=5)},
        {"PASSWORD_HISTORY": 12},
    ],
)
def test_stricter_settings_keep_the_preset(
    use_auth: UseAuth, setting: dict[str, Any]
) -> None:
    """What asks more than PCI holds."""
    use_auth(**PCI, **setting)


# The history ------------------------------------------------------------------------


def history_rows() -> list[dict[str, Any]]:
    """Every kept password hash, oldest first."""

    async def read() -> list[dict[str, Any]]:
        async with connections.engine().connect() as connection:
            result = await connection.execute(
                select(password_history).order_by(password_history.c.id)
            )
            return [dict(row._mapping) for row in result]

    return run_async(read())


def change(client: Any, headers: dict[str, str], old: str, new: str) -> Any:  # noqa: ANN401
    """Change the password at ``/auth/password``."""
    return client.post(
        "/auth/password",
        json={"old_password": old, "new_password": new},
        headers=headers,
    )


def log_in(client: Any, password: str, **body: str) -> Any:  # noqa: ANN401 - a response.
    """Post alice's credentials to ``/login``."""
    return client.post(
        "/auth/login", json={"username": "alice", "password": password, **body}
    )


def test_the_last_passwords_are_not_used_again(identity: Api) -> None:
    """The current one counts among them; older ones are let go."""
    client = identity(PASSWORD_HISTORY=2)
    create_user("alice", STRONG[0])
    headers = bearer(log_in(client, STRONG[0]).json()["access_token"])

    current = change(client, headers, STRONG[0], STRONG[0])
    changed = change(client, headers, STRONG[0], STRONG[1])
    back = change(client, headers, STRONG[1], STRONG[0])
    third = change(client, headers, STRONG[1], STRONG[2])
    first_again = change(client, headers, STRONG[2], STRONG[0])

    assert (current.status_code, current.json()["code"]) == (422, "invalid_password")
    assert "one of your last 2" in current.json()["detail"]
    assert changed.status_code == 204
    assert back.status_code == 422
    assert third.status_code == 204
    assert first_again.status_code == 204
    assert len(history_rows()) == 2
    assert back.json()["detail"].endswith("choose one you have not used.")


def test_nothing_is_kept_without_the_settings(identity: Api) -> None:
    """Neither the history nor the first use: no row, no check."""
    create_user("alice", STRONG[0])
    client = identity()
    headers = bearer(log_in(client, STRONG[0]).json()["access_token"])

    assert change(client, headers, STRONG[0], STRONG[0]).status_code == 204
    assert history_rows() == []


def test_unusable_passwords_are_not_kept(identity: Api) -> None:
    """Nor checked against."""
    create_user("alice", None)
    identity(PASSWORD_HISTORY=4)

    async def set_one() -> None:
        async with AuthUnitOfWork() as uow:
            user = await uow.users.get_by_natural_key("alice")
            assert user is not None
            await uow.users.set_password(user, None)
            await uow.users.set_password(user, STRONG[0])
            await uow.commit()

    run_async(set_one())

    assert len(history_rows()) == 1


def test_each_user_keeps_their_own(identity: Api) -> None:
    """Another user's password is no reuse; ``forget_credentials`` deletes them."""
    client = identity(PASSWORD_HISTORY=4)
    bob = create_user("bob", STRONG[1])
    alice = create_user("alice", STRONG[0])
    headers = bearer(log_in(client, STRONG[0]).json()["access_token"])

    assert change(client, headers, STRONG[0], STRONG[1]).status_code == 204

    async def forget() -> None:
        async with AuthUnitOfWork() as uow:
            await uow.users.forget_credentials(alice)
            await uow.commit()

    run_async(forget())
    assert [row["user_id"] for row in history_rows()] == [bob]


# The change on first use ------------------------------------------------------------


def admin(api: Api, **auth: Any) -> tuple[Any, dict[str, str]]:  # noqa: ANN401
    """A client of the API with the admin's routes, and a staff member's headers."""
    client = api(**auth)
    return client, staff("root", *ADMIN_PERMISSIONS)


def test_a_password_staff_set_is_changed_at_the_first_login(api: Api) -> None:
    """403 until the login sends a new one, which then logs in."""
    client, root = admin(api, PASSWORD_CHANGE_ON_FIRST_USE=True, PASSWORD_HISTORY=4)
    made = client.post(
        f"{ADMIN}/users",
        json={"username": "alice", "password": STRONG[0]},
        headers=root,
    )

    asked = log_in(client, STRONG[0])
    same = log_in(client, STRONG[0], new_password=STRONG[0])
    weak = log_in(client, STRONG[0], new_password="short")
    changed = log_in(client, STRONG[0], new_password=STRONG[1])
    old = log_in(client, STRONG[0])
    new = log_in(client, STRONG[1], new_password=STRONG[2])

    assert made.status_code == 201
    assert (asked.status_code, asked.json()["code"]) == (
        403,
        "password_change_required",
    )
    assert (same.status_code, same.json()["code"]) == (422, "invalid_password")
    assert weak.status_code == 422
    assert changed.status_code == 200, changed.json()
    assert old.status_code == 401
    assert new.status_code == 200  # Not asked again: new_password is ignored.
    alice = made.json()["id"]
    assert [
        row["temporary"] for row in history_rows() if str(row["user_id"]) == alice
    ] == [True, False]
    assert [row["details"]["reason"] for row in audited("auth.login")] == [
        "password_change_required",
        "invalid_credentials",
    ]


def test_staff_reset_another_users_password_for_a_change(api: Api) -> None:
    """``POST /users/{id}/password`` too; a staff member's own is theirs."""
    client, root = admin(api, PASSWORD_CHANGE_ON_FIRST_USE=True)
    alice = create_user("alice", STRONG[0])
    root_id = run_async(_id_of("root"))

    client.post(
        f"{ADMIN}/users/{alice}/password", json={"password": STRONG[1]}, headers=root
    )
    client.post(
        f"{ADMIN}/users/{root_id}/password", json={"password": STRONG[2]}, headers=root
    )

    assert log_in(client, STRONG[1]).json()["code"] == "password_change_required"
    assert (
        client.post(
            "/auth/login", json={"username": "root", "password": STRONG[2]}
        ).status_code
        == 200
    )


async def _id_of(username: str) -> UserId:
    """The id of ``username``."""
    async with AuthUnitOfWork() as uow:
        user = await uow.users.get_by_natural_key(username)
        assert user is not None
        return UserId(user.id)


def test_the_second_factor_comes_before_the_change(identity: Api) -> None:
    """With a second factor, the temporary password alone changes nothing.

    ``/login`` answers the challenge and keeps the password; the change is
    made at ``/mfa/login`` (and ``/session/mfa``), once the code holds
    (SEC-15c).
    """
    with freeze_time("2027-01-04T10:00:00Z") as clock:
        client = identity(PASSWORD_CHANGE_ON_FIRST_USE=True)
        create_user("alice", PASSWORD)
        headers = bearer(log_in(client, PASSWORD).json()["access_token"])
        secret = enrol(client, headers, clock)
        run_async(_set_for_alice(STRONG[0]))

        challenged = log_in(client, STRONG[0], new_password=STRONG[1])
        kept = history_rows()[-1]["temporary"]
        token = challenged.json()["mfa_token"]
        no_change = client.post(
            "/auth/mfa/login",
            json={"mfa_token": token, "code": now_code(secret, clock)},
        )
        wrong_code = client.post(
            "/auth/mfa/login",
            json={"mfa_token": token, "code": "000000", "new_password": STRONG[1]},
        )
        done = client.post(
            "/auth/mfa/login",
            json={
                "mfa_token": token,
                "code": now_code(secret, clock),
                "new_password": STRONG[1],
            },
        )
        browser = TestClient(client.app, base_url="https://testserver")
        session = browser.post(
            "/auth/session",
            json={"username": "alice", "password": STRONG[1]},
            headers={"Origin": "https://testserver"},
        )

    assert challenged.status_code == 202
    assert kept is True  # Still the temporary one: nothing changed yet.
    assert (no_change.status_code, no_change.json()["code"]) == (
        403,
        "password_change_required",
    )
    assert wrong_code.json()["code"] == "invalid_code"
    assert done.status_code == 200, done.json()
    assert session.status_code == 202  # The new password, then the code.
    assert log_in(client, STRONG[0]).status_code == 401


def test_a_browser_session_changes_it_after_the_code_too(identity: Api) -> None:
    """``/session/mfa`` takes ``new_password`` as ``/mfa/login`` does."""
    with freeze_time("2027-01-04T10:00:00Z") as clock:
        client = identity(PASSWORD_CHANGE_ON_FIRST_USE=True)
        create_user("alice", PASSWORD)
        headers = bearer(log_in(client, PASSWORD).json()["access_token"])
        secret = enrol(client, headers, clock)
        run_async(_set_for_alice(STRONG[0]))
        browser = TestClient(client.app, base_url="https://testserver")
        origin = {"Origin": "https://testserver"}

        token = browser.post(
            "/auth/session",
            json={"username": "alice", "password": STRONG[0]},
            headers=origin,
        ).json()["mfa_token"]
        done = browser.post(
            "/auth/session/mfa",
            json={
                "mfa_token": token,
                "code": now_code(secret, clock),
                "new_password": STRONG[1],
            },
            headers=origin,
        )

    assert done.status_code == 200, done.json()
    assert log_in(client, STRONG[1]).status_code == 202


async def _set_for_alice(password: str) -> None:
    """Set alice's password as staff do: for her to change."""
    async with AuthUnitOfWork() as uow:
        user = await uow.users.get_by_natural_key("alice")
        assert user is not None
        await uow.users.set_password(user, password, temporary=True)
        await uow.commit()


def test_the_change_ends_the_users_other_logins(
    identity: Api, monkeypatch: pytest.MonkeyPatch
) -> None:
    """As a change of password does; hashed in the places kept for changes."""
    client = identity(PASSWORD_CHANGE_ON_FIRST_USE=True)
    create_user("alice", PASSWORD)
    before = bearer(log_in(client, PASSWORD).json()["access_token"])
    run_async(_set_for_alice(STRONG[0]))
    asked: list[object] = []
    making = amake_password

    async def spy(password: str, **options: Any) -> str:  # noqa: ANN401
        asked.append(options.get("priority"))
        made: str = await making(password, **options)
        return made

    monkeypatch.setattr(repositories, "amake_password", spy)

    asked_for = log_in(client, STRONG[0])
    similar = log_in(client, STRONG[0], new_password="alice-x91")
    changed = log_in(client, STRONG[0], new_password=STRONG[1])

    assert asked_for.json()["detail"] == (
        "This password was set for you: choose a new one to log in."
    )
    assert "too similar to the username" in similar.json()["detail"]
    assert changed.status_code == 200
    assert asked == [True]
    assert client.get("/whoami", headers=before).status_code == 401


def test_hashing_for_a_change_keeps_its_priority(
    identity: Api, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The history's checks and the new hash both use the caller's priority."""
    identity(PASSWORD_HISTORY=4)
    create_user("alice", STRONG[0])
    seen: list[tuple[str, object]] = []
    checking, making = acheck_password, amake_password

    async def check(password: str, encoded: str, **options: Any) -> bool:  # noqa: ANN401
        seen.append(("check", options.get("priority")))
        held: bool = await checking(password, encoded, **options)
        return held

    async def make(password: str, **options: Any) -> str:  # noqa: ANN401
        seen.append(("make", options.get("priority")))
        made: str = await making(password, **options)
        return made

    monkeypatch.setattr(repositories, "acheck_password", check)
    monkeypatch.setattr(repositories, "amake_password", make)

    async def change_to(password: str, **options: bool) -> None:
        async with AuthUnitOfWork() as uow:
            user = await uow.users.get_by_natural_key("alice")
            assert user is not None
            await uow.users.set_password(user, password, **options)
            await uow.commit()

    run_async(change_to(STRONG[1], priority=True))
    urgent, seen[:] = list(seen), []
    run_async(change_to(STRONG[2]))

    assert urgent == [("check", True), ("make", True)]
    assert seen == [("check", False), ("check", False), ("make", False)]


def test_a_shorter_history_checks_the_newest_only(identity: Api) -> None:
    """Rows kept under a longer history: the newest of them count."""
    client = identity(PASSWORD_HISTORY=4)
    create_user("alice", STRONG[0])
    headers = bearer(log_in(client, STRONG[0]).json()["access_token"])
    for old, new in itertools.pairwise(STRONG):
        assert change(client, headers, old, new).status_code == 204
    client = identity(PASSWORD_HISTORY=2)
    headers = bearer(log_in(client, STRONG[3]).json()["access_token"])

    assert change(client, headers, STRONG[3], STRONG[2]).status_code == 422
    assert change(client, headers, STRONG[3], STRONG[1]).status_code == 204


def test_staff_mark_the_newest_password_only(identity: Api) -> None:
    """``require_change`` marks the current one; older ones stay as they were."""
    identity(PASSWORD_HISTORY=4, PASSWORD_CHANGE_ON_FIRST_USE=True)
    alice = create_user("alice", STRONG[0])

    async def mark() -> bool:
        async with AuthUnitOfWork() as uow:
            user = await uow.users.get_by_natural_key("alice")
            assert user is not None
            await uow.users.set_password(user, STRONG[1])
            await uow.commit()
        async with AuthUnitOfWork() as uow:
            await uow.passwords.require_change(alice)
            await uow.commit()
        async with AuthUnitOfWork() as uow:
            return await uow.passwords.must_change(alice)

    assert run_async(mark()) is True
    assert [row["temporary"] for row in history_rows()] == [False, True]


def test_methods_say_what_is_wrong() -> None:
    """A second factor is required; each method once; both settings check it."""
    with pytest.raises(ValueError, match="no second factor"):
        StepUpSettings(METHODS=("pwd",))
    with pytest.raises(ValueError, match="each method once"):
        ScaSettings(METHODS=("otp", "otp"))
    with pytest.raises(ValueError, match=r"^METHODS must") as alone:
        with_a_factor(("pwd",))
    with pytest.raises(ValueError, match=r"^METHODS lists") as twice:
        with_a_factor(("otp", "pwd", "otp"))

    assert str(alone.value) == (
        'METHODS must hold "otp": a password alone is no second factor'
    )
    assert str(twice.value) == "METHODS lists each method once"


def test_without_the_setting_a_staff_password_just_works(api: Api) -> None:
    """Nothing is kept, and nothing asked."""
    client, root = admin(api)
    client.post(
        f"{ADMIN}/users",
        json={"username": "alice", "password": STRONG[0]},
        headers=root,
    )

    assert log_in(client, STRONG[0]).status_code == 200
    assert history_rows() == []


def test_a_user_staff_made_without_a_password_is_asked_nothing(api: Api) -> None:
    """The first password is theirs to set (a reset link)."""
    client, root = admin(api, PASSWORD_CHANGE_ON_FIRST_USE=True)
    client.post(f"{ADMIN}/users", json={"username": "alice"}, headers=root)

    root_id = run_async(_id_of("root"))

    assert [row["user_id"] for row in history_rows()] == [root_id]
    assert log_in(client, "anything").status_code == 401


def test_invalid_password_names_the_history(identity: Api) -> None:
    """The refusal is the validators' error, listing the rule."""
    create_user("alice", STRONG[0])
    identity(PASSWORD_HISTORY=1)

    async def reuse() -> None:
        async with AuthUnitOfWork() as uow:
            user = await uow.users.get_by_natural_key("alice")
            assert user is not None
            await uow.users.set_password(user, STRONG[0])

    with pytest.raises(InvalidPassword) as refused:
        run_async(reuse())

    assert refused.value.errors == [
        "This password is one of your last 1; choose one you have not used."
    ]


def test_changepassword_asks_again_for_a_reused_password(
    identity: Api, monkeypatch: pytest.MonkeyPatch
) -> None:
    """As for a password a rule refuses."""
    identity(PASSWORD_HISTORY=4)
    create_user("alice", STRONG[0])
    asked = iter([STRONG[0], STRONG[0], STRONG[1], STRONG[1]])
    monkeypatch.setattr(
        changepassword.Command, "ask_secret", lambda self, _: next(asked)
    )
    stderr = io.StringIO()

    call_command("changepassword", "alice", stdout=io.StringIO(), stderr=stderr)

    assert stderr.getvalue() == (
        "This password is one of your last 4; choose one you have not used.\n"
    )
    assert len(history_rows()) == 2


def test_the_change_on_first_use_alone_keeps_one_password(identity: Api) -> None:
    """Without a history, the newest says whether it is to change; no more."""
    client = identity(PASSWORD_CHANGE_ON_FIRST_USE=True)
    create_user("alice", STRONG[0])
    headers = bearer(log_in(client, STRONG[0]).json()["access_token"])

    change(client, headers, STRONG[0], STRONG[1])
    change(client, headers, STRONG[1], STRONG[0])

    assert [row["temporary"] for row in history_rows()] == [False]


def test_each_contradiction_names_its_requirement_and_its_fix() -> None:
    """Every one at once, in words an operator can act on."""
    config = AuthSettings(
        **PCI,
        PASSWORD_VALIDATORS=(),
        PASSWORD_HISTORY=0,
        PASSWORD_CHANGE_ON_FIRST_USE=False,
        LOGIN_RATE=None,
        IDLE_TIMEOUT=None,
        PASSWORD_CHECKS_COUNT_AS_LOGINS=False,
    )

    assert preset_contradictions(config) == [
        (
            "PASSWORD_VALIDATORS take passwords under 12 characters (8.3.6): give "
            'MinimumLengthValidator {"min_length": 12} or more'
        ),
        (
            "PASSWORD_VALIDATORS take passwords without both letters and digits "
            "(8.3.6): add LettersAndDigitsValidator"
        ),
        "PASSWORD_HISTORY is 0, under 4 (8.3.7)",
        (
            "PASSWORD_CHANGE_ON_FIRST_USE is off: passwords staff set are not "
            "changed at their first use (8.3.5)"
        ),
        (
            "LOGIN_RATE None allows more than 10 wrong passwords, or more than 10 "
            "every 30 minutes (8.3.4)"
        ),
        (
            "IDLE_TIMEOUT None ends idle logins more than 15 minutes after their "
            "last use (8.2.8): a login ends up to a tenth after it, so 13 minutes "
            "at most"
        ),
        (
            "PASSWORD_CHECKS_COUNT_AS_LOGINS is off: password checks of "
            "signed-in users guess on a count of their own, besides the "
            "login's (8.3.4)"
        ),
    ]


def test_an_idle_timeout_that_ends_logins_at_15_minutes_holds() -> None:
    """Its tenth more lands on 15 minutes exactly: within 8.2.8."""
    config = AuthSettings(**PCI, IDLE_TIMEOUT=timedelta(microseconds=818_181_818))

    assert preset_contradictions(config) == []


@pytest.mark.parametrize(
    ("classes", "ttl", "contradicted"),
    [
        ([f"{AUTHENTICATION}.TokenAuthentication"], timedelta(hours=1), False),
        ([f"{AUTHENTICATION}.JWTAuthentication"], timedelta(minutes=14), False),
        (
            ["no.such.Authentication", f"{AUTHENTICATION}.JWTAuthentication"],
            timedelta(minutes=14),
            False,
        ),
        (
            ["no.such.Authentication", f"{AUTHENTICATION}.JWTAuthentication"],
            timedelta(minutes=14, seconds=1),
            True,
        ),
    ],
)
def test_only_jwts_beyond_the_idle_bound_contradict_the_preset(
    classes: list[str], ttl: timedelta, contradicted: bool
) -> None:
    """15 minutes together hold; tokens other than JWTs have no such bound."""
    config = AuthSettings(
        **PCI,
        AUTHENTICATION_CLASSES=classes,
        JWT={
            "SIGNING_KEY": "k" * 32,
            "ACCESS_TOKEN_TTL": ttl,
            "LEEWAY": timedelta(minutes=1),
        },
    )

    assert bool(preset_contradictions(config)) is contradicted


def test_signed_in_password_checks_count_as_logins_when_set(identity: Api) -> None:
    """``PASSWORD_CHECKS_COUNT_AS_LOGINS``: one count of guesses per account."""
    client = identity(LOGIN_RATE="2/hour", PASSWORD_CHECKS_COUNT_AS_LOGINS=True)
    create_user("alice", STRONG[0])
    headers = bearer(log_in(client, STRONG[0]).json()["access_token"])

    guesses = [
        change(client, headers, "wrong", STRONG[1]).status_code for _ in range(2)
    ]
    stranger = TestClient(client.app, client=("203.0.113.9", 1))
    login_after = stranger.post(
        "/auth/login", json={"username": "alice", "password": STRONG[0]}
    )

    assert guesses == [400, 400]
    assert (login_after.status_code, login_after.json()["code"]) == (
        429,
        "login_throttled",
    )


def test_wrong_logins_hold_back_signed_in_checks_when_set(identity: Api) -> None:
    """And the other way: the account's wrong logins count against changes."""
    client = identity(LOGIN_RATE="2/hour", PASSWORD_CHECKS_COUNT_AS_LOGINS=True)
    create_user("alice", STRONG[0])
    headers = bearer(log_in(client, STRONG[0]).json()["access_token"])
    for number in (1, 2):
        TestClient(client.app, client=(f"203.0.113.{number}", 1)).post(
            "/auth/login", json={"username": "alice", "password": "wrong"}
        )

    held = change(client, headers, STRONG[0], STRONG[1])

    assert (held.status_code, held.json()["code"]) == (429, "login_throttled")
    assert "throttled" in [row["details"]["reason"] for row in audited("auth.login")]


def test_a_hash_no_hasher_reads_is_skipped_with_a_warning(
    identity: Api, caplog: pytest.LogCaptureFixture
) -> None:
    """Refusing it would refuse every password; it is logged and skipped."""
    identity(PASSWORD_HISTORY=4)
    alice = create_user("alice", STRONG[0])

    async def keep_unreadable() -> None:
        async with connections.engine().begin() as connection:
            await connection.execute(
                password_history.insert().values(
                    user_id=alice,
                    password=PasswordHash("md5$salt$0123456789abcdef"),
                    created_at=clock.now(),
                    temporary=False,
                )
            )

    run_async(keep_unreadable())
    with caplog.at_level("WARNING", logger="domainkit.security"):
        run_async(_set_for_alice_own(STRONG[1]))
        with pytest.raises(InvalidPassword):
            run_async(_set_for_alice_own(STRONG[0]))

    assert (
        caplog.messages.count(
            f"A kept password hash of user {alice} was made by a hasher no longer in "
            "AUTH.PASSWORD_HASHERS: reuse of that password is not checked"
        )
        == 2
    )


async def _set_for_alice_own(password: str) -> None:
    """Set alice's password as she would."""
    async with AuthUnitOfWork() as uow:
        user = await uow.users.get_by_natural_key("alice")
        assert user is not None
        await uow.users.set_password(user, password)
        await uow.commit()


@pytest.mark.parametrize(
    ("arguments", "temporary"),
    [
        (["alice"], True),
        (["alice", "--no-temporary"], False),
        ([], False),
        (["--temporary"], True),
    ],
)
def test_changepassword_marks_what_the_operator_sets(
    identity: Api,
    monkeypatch: pytest.MonkeyPatch,
    arguments: list[str],
    temporary: bool,
) -> None:
    """A user named is to change it; one's own login name, or a flag, decides."""
    identity(PASSWORD_CHANGE_ON_FIRST_USE=True)
    create_user("alice", STRONG[0])
    monkeypatch.setattr("getpass.getuser", lambda: "alice")
    monkeypatch.setattr(changepassword.Command, "ask_secret", lambda self, _: STRONG[1])

    call_command("changepassword", *arguments, stdout=io.StringIO())

    assert history_rows()[-1]["temporary"] is temporary


def test_changepassword_sets_no_temporary_password_without_the_setting(
    identity: Api, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Nothing is kept, and the password just works."""
    client = identity()
    create_user("alice", STRONG[0])
    monkeypatch.setattr(changepassword.Command, "ask_secret", lambda self, _: STRONG[1])

    call_command("changepassword", "alice", stdout=io.StringIO())

    assert log_in(client, STRONG[1]).status_code == 200
    assert history_rows() == []
