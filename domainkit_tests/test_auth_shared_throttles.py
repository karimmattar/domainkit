"""Attempt throttles are shared where deployed, and fail closed there (AUTH-09).

Until 0.11 staging and production counted wrong passwords and codes in
each process by default (``MemoryBackend``), with a warning, so each
process allowed the whole ``LOGIN_RATE``; and attempts that Redis could not
count were let through unless ``AUTH.THROTTLE_FAIL_CLOSED`` was set.
"""

import asyncio
from typing import Any, Final

import pytest
from fastapi import FastAPI
from starlette.requests import Request

from domainkit.cache import caches
from domainkit.checks import CheckMessage
from domainkit.conf import override_settings
from domainkit.contrib.auth import throttling
from domainkit.http.errors import APIError
from domainkit.http.ratelimit import Decision, MemoryBackend, RedisBackend
from domainkit_tests.conftest import (
    AUTH,
    Api,
    UseAuth,
    domain_problems,
    domain_warnings,
)

PBKDF2: Final = [f"{AUTH}.hashers.PBKDF2PasswordHasher"]
REDIS_BACKEND: Final = "domainkit.http.ratelimit.RedisBackend"
CLOSED: Final = "redis://127.0.0.1:9/0"
"""A Redis URL nothing listens on (port 9, discard): a Redis that is down."""


def deployed(environment: str = "production", **more: Any) -> dict[str, Any]:  # noqa: ANN401
    """Settings that load in ``environment``, with ``more``."""
    return {
        "ENVIRONMENT": environment,
        "ALLOWED_HOSTS": ["api.example.com"],
        "DATABASES": {"default": {"URL": "sqlite+aiosqlite:///:memory:"}},
        **more,
    }


class Uncounted:
    """A backend whose store is down: it lets every request through, uncounted."""

    async def hit(self, key: str, limit: int, period: float) -> Decision:
        """Let the request through without counting it, as ``RedisBackend`` does."""
        return Decision(True, limit - 1, 0.0, 0.0, counted=False)


def login_attempt(backend: object) -> int:
    """The status of one login attempt counted in ``backend``: 200 if let through."""
    app = FastAPI()
    app.state.login_limiter = backend
    request = Request({"type": "http", "headers": [], "app": app})
    limits = [("login:ip:unknown", "5/minute"), ("login:user:alice", "5/minute")]
    try:
        asyncio.run(
            throttling.attempt(
                request, limits, action="auth.login", detail="No.", code="no"
            )
        )
    except APIError as refused:
        return refused.status_code
    return 200


# The proof: counted per process -----------------------------------------------


def test_each_process_counts_attempts_on_its_own_in_memory(api: Api) -> None:
    """Two applications, two counts: each allows the whole ``LOGIN_RATE``.

    Every process of a deployment is such an application, so with N
    processes a guesser gets N times the rate. Hence the refusal below.
    """
    from domainkit_tests.test_auth_http import create_user

    create_user("alice")
    wrong = {"username": "alice", "password": "wrong"}
    first, second = api(LOGIN_RATE="2/minute"), api(LOGIN_RATE="2/minute")

    codes = [
        [client.post("/auth/login", json=wrong).status_code for _ in range(3)]
        for client in (first, second)
    ]

    assert codes == [[401, 401, 429], [401, 401, 429]]  # Four guesses, not two.


def shared_problems(use_auth: UseAuth, settings: dict[str, Any], **auth: Any) -> Any:  # noqa: ANN401
    """The ``auth.E105`` findings and the ``auth.W004`` warnings, with ``settings``."""
    use_auth(settings=settings, PASSWORD_HASHERS=PBKDF2, **auth)
    errors = domain_problems("auth", FastAPI())
    warnings = domain_warnings("auth")
    return (
        [found for found in errors if getattr(found, "id", None) == "auth.E105"],
        [found for found in warnings if getattr(found, "id", None) == "auth.W004"],
    )


@pytest.mark.parametrize("environment", ["staging", "production"])
def test_deployments_refuse_attempts_counted_per_process(
    use_auth: UseAuth, environment: str
) -> None:
    """A per-process backend is an error where deployed, which no list silences.

    ``REQUIRE_SHARED_THROTTLES = False`` keeps 0.10's warning instead.
    """
    errors, warnings = shared_problems(use_auth, deployed(environment))

    assert [(error.id, error.message, error.hint) for error in errors] == [
        (
            "auth.E105",
            (
                "RATE_LIMIT_BACKEND domainkit.http.ratelimit.MemoryBackend counts "
                "in each process, so with several processes or replicas each "
                "allows AUTH.LOGIN_RATE, AUTH.MFA.RATE, and the other attempt "
                "rates on its own: passwords and codes can be guessed that many "
                "times faster."
            ),
            (
                "Set RATE_LIMIT_BACKEND to domainkit.http.ratelimit.RedisBackend, "
                "shared by every process; with one process only, set "
                "AUTH.REQUIRE_SHARED_THROTTLES = False to only warn (auth.W004), "
                "as 0.10 did."
            ),
        )
    ]
    assert warnings == []
    errors, warnings = shared_problems(
        use_auth, deployed(environment), REQUIRE_SHARED_THROTTLES=False
    )
    assert errors == []
    assert [warning.id for warning in warnings] == ["auth.W004"]


def test_shared_or_absent_throttles_and_development_pass(use_auth: UseAuth) -> None:
    """Redis, a backend of the project's own, no rates at all, or not deployed."""
    no_rates = {"LOGIN_RATE": None, "EMAIL_RATE": None}
    no_codes = {"RATE": None, "ADDRESS_RATE": None}
    redis = deployed(RATE_LIMIT_BACKEND=REDIS_BACKEND)
    own = deployed(RATE_LIMIT_BACKEND=f"{__name__}.Uncounted")

    assert shared_problems(use_auth, redis) == ([], [])
    assert shared_problems(use_auth, own) == ([], [])
    assert shared_problems(use_auth, deployed(), **no_rates, MFA=no_codes) == ([], [])
    assert shared_problems(use_auth, {"ENVIRONMENT": "local"}) == ([], [])
    assert shared_problems(use_auth, {"ENVIRONMENT": "test"}) == ([], [])


def test_the_refusal_is_an_error_no_list_can_silence(use_auth: UseAuth) -> None:
    """``SILENCED_CHECKS`` cannot name it, and ``check`` lists it."""
    from domainkit.checks import silence, validate_silenced

    errors, _ = shared_problems(use_auth, deployed())

    assert [error.level.name for error in errors] == ["ERROR"]
    assert silence(errors, ["auth.E105"]) == errors
    with pytest.raises(ValueError, match=r"cannot silence the error auth\.E105"):
        validate_silenced(["auth.E105"])
    assert isinstance(errors[0], CheckMessage)


# The proof: failing open ------------------------------------------------------


@pytest.mark.parametrize(
    ("settings", "explicit", "status"),
    [
        (deployed("production"), None, 503),
        (deployed("staging"), None, 503),
        ({"ENVIRONMENT": "local"}, None, 200),
        ({"ENVIRONMENT": "test"}, None, 200),
        (deployed("production"), False, 200),
        ({"ENVIRONMENT": "local"}, True, 503),
    ],
)
def test_attempts_that_cannot_be_counted_fail_closed_where_deployed(
    use_auth: UseAuth, settings: dict[str, Any], explicit: bool | None, status: int
) -> None:
    """``THROTTLE_FAIL_CLOSED`` unset follows the environment; set, it decides."""
    chosen = {} if explicit is None else {"THROTTLE_FAIL_CLOSED": explicit}
    use_auth(settings=settings, PASSWORD_HASHERS=PBKDF2, **chosen)

    assert login_attempt(Uncounted()) == status


def test_a_redis_outage_refuses_attempts_where_deployed(use_auth: UseAuth) -> None:
    """Redis down: production answers 503 rather than check unlimited guesses.

    The local fallback (``RATE_LIMIT_FALLBACK_REPLICAS``) counts in this
    process only, and marks its decisions not counted: failing closed, they
    are refused too. Failing open (``THROTTLE_FAIL_CLOSED = False``), the
    attempts go through, up to the fallback's share.
    """
    down = {"URL": CLOSED, "KEY_PREFIX": "down", "CONNECT_TIMEOUT": 0.5}

    def attempts(backend: str, **auth: Any) -> list[int]:  # noqa: ANN401
        settings = deployed(CACHES={"default": down})
        use_auth(settings=settings, PASSWORD_HASHERS=PBKDF2, **auth)
        try:
            limiter = (
                RedisBackend()
                if backend == "plain"
                else RedisBackend(fallback=MemoryBackend(full="refuse"), replicas=2)
            )
            return [login_attempt(limiter) for _ in range(4)]
        finally:
            asyncio.run(caches.close_all())

    assert attempts("plain") == [503] * 4
    # The fallback counted the refused attempts: once its share (5 // 2) is
    # spent, it refuses them itself. Refused either way; nothing is checked.
    assert attempts("fallback") == [503, 503, 429, 429]
    assert attempts("plain", THROTTLE_FAIL_CLOSED=False) == [200] * 4
    # 5 a minute over 2 replicas: 2 here, then 429.
    assert attempts("fallback", THROTTLE_FAIL_CLOSED=False) == [200, 200, 429, 429]


def test_the_setting_is_unset_by_default() -> None:
    """None: it follows ``ENVIRONMENT``."""
    from domainkit.contrib.auth.settings import AuthSettings

    with override_settings(ENVIRONMENT="test"):
        assert AuthSettings().THROTTLE_FAIL_CLOSED is None
        assert AuthSettings().REQUIRE_SHARED_THROTTLES is True
