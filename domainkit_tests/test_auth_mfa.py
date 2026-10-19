"""Second factors: TOTP (RFC 6238), recovery codes, and encrypted secrets."""

import base64
import io
import re
import sys
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from domainkit import signing
from domainkit.conf import UNSET, override_settings
from domainkit.contrib.auth import mfa, totp
from domainkit.contrib.auth.encryption import DecryptionFailed, SecretBox
from domainkit.contrib.auth.settings import MfaSettings
from domainkit.contrib.auth.tables import mfa_devices, recovery_codes
from domainkit.db import connections
from domainkit.exceptions import ImproperlyConfigured
from domainkit.management import call_command
from domainkit.management.base import CommandError, run_async
from domainkit_tests.conftest import Api
from domainkit_tests.test_auth_http import (
    PASSWORD,
    audited,
    bearer,
    create_user,
    login,
    stored_tokens,
)

RFC_KEY = b"12345678901234567890"
OLD_KEY = "old-0123456789-abcdefghijklmnopqrstuvwxyz-ABCDEFGHIJ"
NEW_KEY = "new-0123456789-abcdefghijklmnopqrstuvwxyz-ABCDEFGHIJ"
NOW = 1_800_000_000.0
RECOVERY = re.compile(r"[a-z2-7]{4}(-[a-z2-7]{4}){3}")


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """The time codes are checked at: set ``clock[0]``."""
    now = [NOW]
    monkeypatch.setattr(mfa, "_now", lambda: now[0])
    return now


# TOTP ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("counter", "code"),
    list(
        enumerate(
            [
                "755224",
                "287082",
                "359152",
                "969429",
                "338314",
                "254676",
                "287922",
                "162583",
                "399871",
                "520489",
            ]
        )
    ),
)
def test_hotp_matches_rfc_4226(counter: int, code: str) -> None:
    """Appendix D's ten codes for the secret ``12345678901234567890``."""
    assert totp.hotp(RFC_KEY, counter) == code


@pytest.mark.parametrize(
    ("at", "code"),
    [
        (59, "94287082"),
        (1111111109, "07081804"),
        (1111111111, "14050471"),
        (1234567890, "89005924"),
        (2000000000, "69279037"),
        (20000000000, "65353130"),
    ],
)
def test_totp_matches_rfc_6238(at: int, code: str) -> None:
    """Appendix B's SHA-1 codes, of eight digits."""
    assert totp.totp(RFC_KEY, at, digits=8) == code
    assert totp.match(RFC_KEY, code, at, digits=8) == at // 30


def test_codes_match_within_the_window_only() -> None:
    """The current step, then the ones around it; nothing else is a code."""
    key = totp.new_secret()
    step = totp.time_step(NOW)
    code = {delta: totp.hotp(key, step + delta) for delta in range(-3, 4)}

    assert len(key) == 20
    assert totp.match(key, code[0], NOW) == step
    assert totp.match(key, code[-1], NOW) == step - 1
    assert totp.match(key, code[1], NOW) == step + 1
    assert totp.match(key, code[2], NOW) is None
    assert totp.match(key, code[2], NOW, window=2) == step + 2
    assert totp.match(key, code[-3], NOW, window=2) is None
    assert totp.match(key, code[1], NOW, window=0) is None
    for wrong in (
        "",
        "12345",
        "1234567",
        "abcdef",
        "\uff11\uff12\uff13\uff14\uff15\uff16",
        code[0] + " ",
    ):
        assert totp.match(key, wrong, NOW) is None


def test_the_current_step_wins_when_codes_repeat(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A code that two steps share counts for the current one."""
    monkeypatch.setattr(totp, "hotp", lambda key, counter, digits=6: "123456")

    assert totp.match(b"k", "123456", NOW, window=3) == totp.time_step(NOW)


def test_otpauth_uris_for_authenticator_apps() -> None:
    """Base 32 without padding, the issuer twice, and the parameters spelled out."""
    secret = bytes(range(20))

    assert totp.encode_secret(secret) == "AAAQEAYEAUDAOCAJBIFQYDIOB4IBCEQT"
    assert totp.encode_secret(b"\x00") == "AA"
    assert totp.otpauth_uri(secret, account="alice@x.org", issuer="Billing & Co") == (
        "otpauth://totp/Billing%20%26%20Co%3Aalice%40x.org"
        "?secret=AAAQEAYEAUDAOCAJBIFQYDIOB4IBCEQT&issuer=Billing%20%26%20Co"
        "&algorithm=SHA1&digits=6&period=30"
    )


# Encryption ---------------------------------------------------------------------


def test_secrets_are_encrypted_and_bound_to_their_owner() -> None:
    """AES-GCM: a version, a nonce, the ciphertext; other data fails."""
    box = SecretBox("tests")
    sealed = box.encrypt(b"secret", associated=b"alice")

    assert sealed[:1] == b"\x01"
    assert len(sealed) == 1 + 12 + len(b"secret") + 16
    assert b"secret" not in sealed
    assert box.encrypt(b"secret", associated=b"alice") != sealed  # A new nonce.
    assert box.decrypt(sealed, associated=b"alice") == (b"secret", True)
    tampered = sealed[:-1] + bytes([sealed[-1] ^ 1])
    for value, owner in (
        (sealed, b"bob"),
        (tampered, b"alice"),
        (b"\x02" + sealed[1:], b"alice"),
        (sealed[:5], b"alice"),
        (b"", b"alice"),
    ):
        with pytest.raises(DecryptionFailed):
            box.decrypt(value, associated=owner)
    with pytest.raises(DecryptionFailed):
        SecretBox("other").decrypt(sealed, associated=b"alice")
    assert repr(box) == "SecretBox(purpose='tests')"


def test_secrets_survive_key_rotation() -> None:
    """A fallback key still decrypts, and says the value should be sealed again."""
    with override_settings(SECRET_KEY=OLD_KEY):
        sealed = SecretBox("tests").encrypt(b"secret", associated=b"a")

    with override_settings(SECRET_KEY=NEW_KEY, SECRET_KEY_FALLBACKS=[OLD_KEY]):
        assert SecretBox("tests").decrypt(sealed, associated=b"a") == (b"secret", False)
    with (
        override_settings(SECRET_KEY=NEW_KEY),
        pytest.raises(DecryptionFailed, match="No key decrypts"),
    ):
        SecretBox("tests").decrypt(sealed, associated=b"a")


def test_encryption_needs_a_key_and_cryptography(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without either, it says what to install or set."""
    with override_settings(SECRET_KEY=UNSET), pytest.raises(ImproperlyConfigured):
        SecretBox("tests")
    monkeypatch.setitem(
        sys.modules, "cryptography.hazmat.primitives.ciphers.aead", None
    )
    with pytest.raises(ImproperlyConfigured, match=r"domainkit\[mfa\]"):
        SecretBox("tests")


def test_mfa_settings_are_bounded() -> None:
    """A window, a challenge, and a number of codes that stay sensible."""
    from pydantic import ValidationError

    for options in (
        {"WINDOW": 11},
        {"WINDOW": -1},
        {"RECOVERY_CODES": 0},
        {"RECOVERY_CODES": 51},
        {"CHALLENGE_TTL": 0},
        {"CHALLENGE_TTL": 3601},
        {"ISSUER": ""},
        {"RATE": "often"},
    ):
        with pytest.raises(ValidationError):
            MfaSettings(**options)


# The API ------------------------------------------------------------------------


def secret_of(enrolment: dict[str, Any]) -> bytes:
    """The raw secret behind an enrolment's base-32 one."""
    encoded: str = enrolment["secret"]
    return base64.b32decode(encoded + "=" * (-len(encoded) % 8))


def code_at(secret: bytes, at: float) -> str:
    """The TOTP code for ``at``."""
    return totp.totp(secret, at)


def enrol(
    client: TestClient, headers: dict[str, str], clock: list[float]
) -> tuple[bytes, list[str]]:
    """Turn the second factor on; return the secret and the recovery codes."""
    started = client.post(
        "/auth/mfa/totp", json={"password": PASSWORD}, headers=headers
    )
    assert started.status_code == 200, started.json()
    secret = secret_of(started.json())
    confirmed = client.post(
        "/auth/mfa/totp/confirm",
        json={"code": code_at(secret, clock[0])},
        headers=headers,
    )
    assert confirmed.status_code == 200, confirmed.json()
    codes: list[str] = confirmed.json()["recovery_codes"]
    return secret, codes


def log_in(client: TestClient, password: str = PASSWORD) -> Any:  # noqa: ANN401 - a response.
    """Post alice's credentials to ``/login``."""
    return client.post("/auth/login", json={"username": "alice", "password": password})


def finish(client: TestClient, token: str, code: str) -> Any:  # noqa: ANN401 - a response.
    """Post a challenge and a code to ``/mfa/login``."""
    return client.post("/auth/mfa/login", json={"mfa_token": token, "code": code})


def device_rows() -> list[dict[str, Any]]:
    """Every stored authenticator."""

    async def read() -> list[dict[str, Any]]:
        async with connections.engine().connect() as connection:
            rows = await connection.execute(select(mfa_devices))
            return [dict(row._mapping) for row in rows]

    return run_async(read())


def test_enrolling_turns_on_codes_at_login(identity: Api, clock: list[float]) -> None:
    """Secret, URI, first code, recovery codes; then logins need a code."""
    alice = create_user("alice", email="alice@example.com")
    client = identity(MFA={"ISSUER": "Billing"})
    headers = bearer(login(client, "alice")["access_token"])
    assert client.get("/auth/mfa", headers=headers).json() == {
        "enabled": False,
        "recovery_codes_left": 0,
    }

    started = client.post(
        "/auth/mfa/totp", json={"password": PASSWORD}, headers=headers
    )

    assert started.headers["Cache-Control"] == "no-store"
    body = started.json()
    assert set(body) == {"secret", "otpauth_uri"}
    assert body["otpauth_uri"].startswith("otpauth://totp/Billing%3Aalice?secret=")
    assert client.get("/auth/mfa", headers=headers).json()["enabled"] is False
    secret = secret_of(body)
    (row,) = device_rows()
    assert secret not in row["secret"]
    assert row["confirmed_at"] is None
    wrong = client.post(
        "/auth/mfa/totp/confirm", json={"code": "000000"}, headers=headers
    )
    assert (wrong.status_code, wrong.json()["code"]) == (401, "invalid_code")
    confirmed = client.post(
        "/auth/mfa/totp/confirm",
        json={"code": code_at(secret, NOW)},
        headers=headers,
    )
    assert confirmed.status_code == 200
    codes = confirmed.json()["recovery_codes"]
    assert len(codes) == 10
    assert all(RECOVERY.fullmatch(code) for code in codes)
    assert len(set(codes)) == 10
    assert client.get("/auth/mfa", headers=headers).json() == {
        "enabled": True,
        "recovery_codes_left": 10,
    }
    (enabled,) = audited("auth.mfa_enabled")
    assert enabled["details"] == {"user_id": str(alice)}

    challenged = log_in(client)

    assert challenged.status_code == 202
    challenge = challenged.json()
    assert set(challenge) == {"mfa_required", "mfa_token", "expires_in"}
    assert (challenge["mfa_required"], challenge["expires_in"]) == (True, 300)
    clock[0] += 30
    done = finish(client, challenge["mfa_token"], code_at(secret, clock[0]))
    assert done.status_code == 200
    assert done.json()["access_token"].startswith("dkt_")
    assert done.headers["Cache-Control"] == "no-store"
    again = finish(client, challenge["mfa_token"], code_at(secret, clock[0] + 30))
    assert (again.status_code, again.json()["code"]) == (400, "invalid_token")
    assert len(audited("auth.user_logged_in")) == 2


def test_a_code_works_once(identity: Api, clock: list[float]) -> None:
    """A code, or one of an earlier step, is refused once a step is used."""
    create_user("alice")
    client = identity()
    secret, _ = enrol(client, bearer(login(client, "alice")["access_token"]), clock)
    clock[0] += 60

    first = finish(
        client, log_in(client).json()["mfa_token"], code_at(secret, clock[0])
    )
    replayed = finish(
        client, log_in(client).json()["mfa_token"], code_at(secret, clock[0])
    )
    earlier = finish(
        client, log_in(client).json()["mfa_token"], code_at(secret, clock[0] - 30)
    )

    assert first.status_code == 200
    assert (replayed.status_code, replayed.json()["code"]) == (401, "invalid_code")
    assert earlier.status_code == 401
    refused = audited("auth.mfa")
    assert [row["details"] for row in refused] == [{"reason": "invalid_code"}] * 2


def test_recovery_codes_stand_in_once(identity: Api, clock: list[float]) -> None:
    """Each recovery code logs in once, typed in any case or spacing."""
    alice = create_user("alice")
    client = identity()
    _, codes = enrol(client, bearer(login(client, "alice")["access_token"]), clock)
    typed = codes[0].upper().replace("-", " ")

    used = finish(client, log_in(client).json()["mfa_token"], typed)
    reused = finish(client, log_in(client).json()["mfa_token"], codes[0])

    assert used.status_code == 200
    assert reused.status_code == 401
    (event,) = audited("auth.recovery_code_used")
    assert event["details"] == {"user_id": str(alice), "remaining": 9}
    for wrong in ("abcd-efgh-ijkl-mnop", "abcd-efgh", "ab1d-efgh-ijkl-mnop", "!"):
        assert (
            finish(client, log_in(client).json()["mfa_token"], wrong).status_code == 401
        )


def test_challenges_expire_and_name_their_user(
    identity: Api, clock: list[float], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Past ``CHALLENGE_TTL``, or forged, a challenge is refused with 400."""
    create_user("alice")
    client = identity(MFA={"CHALLENGE_TTL": 60})
    secret, _ = enrol(client, bearer(login(client, "alice")["access_token"]), clock)
    challenge = log_in(client).json()
    assert challenge["expires_in"] == 60
    later = signing._now() + 61
    monkeypatch.setattr(signing, "_now", lambda: later)

    expired = finish(client, challenge["mfa_token"], code_at(secret, clock[0]))
    forged = finish(client, f"{uuid4().hex}:1:{'A' * 43}", code_at(secret, clock[0]))
    garbage = finish(client, "garbage", code_at(secret, clock[0]))

    for response in (expired, forged, garbage):
        assert (response.status_code, response.json()["code"]) == (400, "invalid_token")
    assert [row["details"] for row in audited("auth.mfa")] == [
        {"reason": "invalid_token"}
    ] * 3


def test_code_checks_are_throttled_per_user(identity: Api, clock: list[float]) -> None:
    """Guesses for one user count wherever they come from."""
    create_user("alice")
    client = identity(MFA={"RATE": "3/minute"})
    enrol(client, bearer(login(client, "alice")["access_token"]), clock)
    token = log_in(client).json()["mfa_token"]
    elsewhere = TestClient(client.app, client=("203.0.113.9", 1))

    assert finish(client, token, "000000").status_code == 401
    assert finish(elsewhere, token, "111111").status_code == 401
    throttled = finish(elsewhere, token, "222222")
    assert (throttled.status_code, throttled.json()["code"]) == (429, "throttled")


def test_turning_it_off_needs_the_password_and_a_code(
    identity: Api, clock: list[float]
) -> None:
    """Re-authentication: both, or 400 and 401; then logins need no code."""
    alice = create_user("alice")
    client = identity()
    headers = bearer(login(client, "alice")["access_token"])
    secret, codes = enrol(client, headers, clock)
    clock[0] += 30

    wrong_password = client.post(
        "/auth/mfa/totp/disable",
        json={"password": "nope", "code": code_at(secret, clock[0])},
        headers=headers,
    )
    wrong_code = client.post(
        "/auth/mfa/totp/disable",
        json={"password": PASSWORD, "code": "000000"},
        headers=headers,
    )
    done = client.post(
        "/auth/mfa/totp/disable",
        json={"password": PASSWORD, "code": codes[0]},
        headers=headers,
    )

    assert (wrong_password.status_code, wrong_password.json()["code"]) == (
        400,
        "wrong_password",
    )
    assert (wrong_code.status_code, wrong_code.json()["code"]) == (401, "invalid_code")
    assert done.status_code == 204
    assert device_rows() == []
    assert log_in(client).status_code == 200
    (disabled,) = audited("auth.mfa_disabled")
    assert disabled["details"] == {"user_id": str(alice)}
    assert client.get("/auth/mfa", headers=headers).json() == {
        "enabled": False,
        "recovery_codes_left": 0,
    }


def test_new_recovery_codes_replace_the_old(identity: Api, clock: list[float]) -> None:
    """The old codes stop working; the new ones log in."""
    create_user("alice")
    client = identity()
    headers = bearer(login(client, "alice")["access_token"])
    secret, old = enrol(client, headers, clock)
    clock[0] += 30

    response = client.post(
        "/auth/mfa/recovery-codes",
        json={"password": PASSWORD, "code": code_at(secret, clock[0])},
        headers=headers,
    )

    new = response.json()["recovery_codes"]
    assert response.headers["Cache-Control"] == "no-store"
    assert len(new) == 10
    assert not set(new) & set(old)
    assert finish(client, log_in(client).json()["mfa_token"], old[1]).status_code == 401
    assert finish(client, log_in(client).json()["mfa_token"], new[1]).status_code == 200
    assert len(audited("auth.recovery_codes_regenerated")) == 1


def test_enrolment_states(identity: Api, clock: list[float]) -> None:
    """Restart replaces an unconfirmed secret; an active one must be turned off."""
    create_user("alice")
    client = identity()
    headers = bearer(login(client, "alice")["access_token"])

    no_enrolment = client.post(
        "/auth/mfa/totp/confirm", json={"code": "123456"}, headers=headers
    )
    first = client.post("/auth/mfa/totp", json={"password": PASSWORD}, headers=headers)
    second = client.post("/auth/mfa/totp", json={"password": PASSWORD}, headers=headers)
    stale = client.post(
        "/auth/mfa/totp/confirm",
        json={"code": code_at(secret_of(first.json()), NOW)},
        headers=headers,
    )
    confirmed = client.post(
        "/auth/mfa/totp/confirm",
        json={"code": code_at(secret_of(second.json()), NOW)},
        headers=headers,
    )
    once_more = client.post(
        "/auth/mfa/totp", json={"password": PASSWORD}, headers=headers
    )
    confirm_again = client.post(
        "/auth/mfa/totp/confirm",
        json={"code": code_at(secret_of(second.json()), NOW + 30)},
        headers=headers,
    )
    wrong_password = client.post(
        "/auth/mfa/totp", json={"password": "nope"}, headers=headers
    )

    assert (no_enrolment.status_code, no_enrolment.json()["code"]) == (
        400,
        "no_enrolment",
    )
    assert len(device_rows()) == 1
    assert stale.status_code == 401
    assert confirmed.status_code == 200
    assert (once_more.status_code, once_more.json()["code"]) == (409, "mfa_enabled")
    assert confirm_again.json()["code"] == "no_enrolment"
    assert wrong_password.json()["code"] == "wrong_password"
    assert client.post("/auth/mfa/totp", json={"password": PASSWORD}).status_code == 401


def test_rotated_keys_reseal_the_secret(identity: Api, clock: list[float]) -> None:
    """After ``SECRET_KEY`` changes, codes work, and the secret moves to the new key."""
    create_user("alice")
    with override_settings(SECRET_KEY=OLD_KEY):
        client = identity()
        secret, _ = enrol(client, bearer(login(client, "alice")["access_token"]), clock)
    (before,) = device_rows()

    with override_settings(SECRET_KEY=NEW_KEY, SECRET_KEY_FALLBACKS=[OLD_KEY]):
        clock[0] += 30
        token = log_in(client).json()["mfa_token"]
        assert finish(client, token, code_at(secret, clock[0])).status_code == 200
        (after,) = device_rows()
        assert after["secret"] != before["secret"]
        user_id: UUID = after["user_id"]
        assert SecretBox(mfa.BOX_PURPOSE).decrypt(
            after["secret"], associated=user_id.bytes
        ) == (secret, True)


def test_browser_sessions_take_a_second_factor_too(
    identity: Api, clock: list[float]
) -> None:
    """``/session`` answers 202; ``/session/mfa`` sets the cookies."""
    create_user("alice")
    client = identity()
    secret, _ = enrol(client, bearer(login(client, "alice")["access_token"]), clock)
    browser = TestClient(client.app, base_url="https://testserver")
    origin = {"Origin": "https://testserver"}

    challenged = browser.post(
        "/auth/session",
        json={"username": "alice", "password": PASSWORD},
        headers=origin,
    )
    assert challenged.status_code == 202
    assert "dk_session" not in browser.cookies
    clock[0] += 30
    forged = browser.post(
        "/auth/session/mfa",
        json={"mfa_token": challenged.json()["mfa_token"], "code": "000000"},
        headers={"Origin": "https://evil.example"},
    )
    done = browser.post(
        "/auth/session/mfa",
        json={
            "mfa_token": challenged.json()["mfa_token"],
            "code": code_at(secret, clock[0]),
        },
        headers=origin,
    )

    assert (forged.status_code, forged.json()["code"]) == (403, "csrf_failed")
    assert done.status_code == 200
    assert browser.get("/whoami").json() == {"user": "alice", "scheme": "cookie"}


def test_jwt_logins_take_a_second_factor_too(identity: Api, clock: list[float]) -> None:
    """The tokens of the first class come after the code: a pair for JWT."""
    create_user("alice")
    client = identity(
        AUTHENTICATION_CLASSES=[
            "domainkit.contrib.auth.authentication.JWTAuthentication",
            "domainkit.contrib.auth.cookies.CookieAuthentication",
        ],
        JWT={"SIGNING_KEY": "k" * 32},
    )
    secret, _ = enrol(client, bearer(login(client, "alice")["access_token"]), clock)
    clock[0] += 30

    done = finish(client, log_in(client).json()["mfa_token"], code_at(secret, clock[0]))

    assert set(done.json()) == {
        "access_token",
        "token_type",
        "expires_in",
        "refresh_token",
    }


def test_resetmfa_turns_it_off_for_a_user_who_lost_it(
    identity: Api, clock: list[float]
) -> None:
    """For staff, once they know who is asking: sessions end too."""
    alice = create_user("alice")
    client = identity()
    enrol(client, bearer(login(client, "alice")["access_token"]), clock)
    out = io.StringIO()

    call_command("resetmfa", "alice", stdout=out)
    call_command("resetmfa", "alice", stdout=out)

    assert out.getvalue() == (
        "The second factor of 'alice' is off; log in to set one.\n"
        "'alice' had no second factor.\n"
    )
    assert stored_tokens(alice) == []
    assert device_rows() == []
    assert log_in(client).status_code == 200
    assert len(audited("auth.mfa_disabled")) == 1
    with pytest.raises(CommandError, match="does not exist"):
        call_command("resetmfa", "nobody", stdout=out)


def test_recovery_codes_are_stored_as_digests(
    identity: Api, clock: list[float]
) -> None:
    """The table holds no working code, and each digest is bound to its user."""
    alice = create_user("alice")
    client = identity()
    _, codes = enrol(client, bearer(login(client, "alice")["access_token"]), clock)

    async def read() -> list[str]:
        async with connections.engine().connect() as connection:
            rows = await connection.execute(select(recovery_codes.c.id))
            return [row.id for row in rows]

    stored = run_async(read())
    assert sorted(stored) == sorted(mfa.recovery_digest(alice, code) for code in codes)
    assert not set(stored) & {code.replace("-", "") for code in codes}
    assert mfa.recovery_digest(uuid4(), codes[0]) not in stored


def test_codes_need_a_confirmed_authenticator(
    identity: Api, clock: list[float]
) -> None:
    """Without one, no code is right: turning nothing off is refused too."""
    create_user("alice")
    client = identity()
    headers = bearer(login(client, "alice")["access_token"])
    secret, _ = enrol(client, headers, clock)
    token = log_in(client).json()["mfa_token"]
    call_command("resetmfa", "alice", stdout=io.StringIO())

    late = finish(client, token, code_at(secret, clock[0] + 30))
    headers = bearer(login(client, "alice")["access_token"])
    nothing = client.post(
        "/auth/mfa/totp/disable",
        json={"password": PASSWORD, "code": "123456"},
        headers=headers,
    )

    assert (late.status_code, late.json()["code"]) == (401, "invalid_code")
    assert (nothing.status_code, nothing.json()["code"]) == (401, "invalid_code")


def test_codes_are_checked_against_the_real_clock() -> None:
    """Without a test clock, the time is the system's."""
    import time

    assert abs(mfa._now() - time.time()) < 5
