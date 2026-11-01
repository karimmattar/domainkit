"""AUTH-17: a TOTP secret no key decrypts answers 500, and nothing moves secrets ahead.

TOTP secrets are sealed under ``SECRET_KEY`` (``SecretBox``), and move to
a new key only when their user logs in with a code (``_spend_totp``). No
command re-encrypts them, and nothing says how many still rest on a
fallback, so "keep the old key until every user with a second factor has
logged in" (``docs/auth/logging-in.md``) cannot be known to hold. Once the
fallback goes, ``DecryptionFailed`` escapes ``spend_code``: ``/mfa/login``
(and ``/mfa/totp/disable``, ``/step-up``, an SCA confirmation) answer
``500 internal_error`` for that user, unaudited, after counting a wrong
code against ``MFA.RATE``.
"""

from typing import Any

from domainkit.conf import override_settings
from domainkit_tests.conftest import Api
from domainkit_tests.test_auth_http import bearer, create_user, login
from domainkit_tests.test_auth_mfa import (
    NEW_KEY,
    OLD_KEY,
    clock,  # noqa: F401 - the fixture.
    code_at,
    enrol,
    finish,
    log_in,
)


def test_a_secret_under_a_dropped_key_is_refused_not_a_crash(
    identity: Api,
    clock: list[float],  # noqa: F811 - the fixture.
) -> None:
    """The login is refused with an answer of its own, not a 500."""
    create_user("alice")
    with override_settings(SECRET_KEY=OLD_KEY):
        client = identity()
        secret, _ = enrol(client, bearer(login(client, "alice")["access_token"]), clock)

    with override_settings(SECRET_KEY=NEW_KEY):
        clock[0] += 30
        token = log_in(client).json()["mfa_token"]
        answer: Any = finish(client, token, code_at(secret, clock[0]))

    assert answer.status_code != 500, answer.json()
