"""Signed values, derived keys, and the ``SECRET_KEY`` they come from."""

import base64
import json
import logging
from collections.abc import Callable
from datetime import timedelta
from typing import Any

import pytest

from domainkit import signing
from domainkit.conf import (
    UNSET,
    Settings,
    override_settings,
    settings,
    unwrap_settings,
)
from domainkit.exceptions import ImproperlyConfigured
from domainkit.signing import (
    BadSignature,
    SignatureExpired,
    Signer,
    TimestampSigner,
    constant_time_compare,
    derive_key,
    derive_keys,
    dumps,
    hkdf,
    loads,
)

OLD_KEY = "old-0123456789-abcdefghijklmnopqrstuvwxyz-ABCDEFGHIJ"
NEW_KEY = "new-0123456789-abcdefghijklmnopqrstuvwxyz-ABCDEFGHIJ"


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """The time ``signing`` reads: set ``clock[0]``."""
    now = [1_700_000_000.0]
    monkeypatch.setattr(signing, "_now", lambda: now[0])
    return now


def test_signatures_hold_until_anything_changes() -> None:
    """A changed value or signature is refused; the value is readable as is."""
    signer = Signer(salt="tests")
    signed = signer.sign("hello:world")
    value, signature = signed.rsplit(":", 1)

    assert value == "hello:world"
    assert len(signature) == 43  # SHA-256, URL-safe base64, no padding.
    assert set(signature) <= set(
        "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
    )
    assert signer.unsign(signed) == "hello:world"
    assert signer.sign("") == f":{signer.signature('')}"
    assert signer.unsign(signer.sign("")) == ""
    for forged in (
        f"hello:World:{signature}",
        f"hello:world:{signature[:-1]}",
        f"hello:world:{signature}A",
        f"hello:world:{signature[:-1]}{'B' if signature[-1] == 'A' else 'A'}",
        "hello:world:",
        "hello",
        "",
    ):
        with pytest.raises(BadSignature):
            signer.unsign(forged)


def test_no_separator_means_no_signature() -> None:
    """A value without a separator says so."""
    with pytest.raises(BadSignature, match="No signature found"):
        Signer(salt="tests").unsign("unsigned")
    with pytest.raises(BadSignature, match="does not match"):
        Signer(salt="tests").unsign("value:nonsense")


def test_signatures_are_hmac_sha256_under_a_key_derived_for_the_salt() -> None:
    """The signature is HMAC-SHA256 under ``derive_key`` of the salt and kind."""
    import hashlib
    import hmac

    key = derive_key("signing:plain:tests", key=OLD_KEY)
    expected = hmac.new(key, b"value", hashlib.sha256).digest()

    signature = Signer(salt="tests", key=OLD_KEY).signature("value")

    assert base64.urlsafe_b64decode(signature + "=") == expected


def test_a_salt_is_a_purpose_of_its_own() -> None:
    """Values signed for one salt never verify for another, or for another kind."""
    signed = Signer(salt="invites").sign("42")

    assert Signer(salt="invites").unsign(signed) == "42"
    for other in (Signer(salt="resets"), Signer(), TimestampSigner(salt="invites")):
        with pytest.raises(BadSignature):
            other.unsign(signed)
    stamped = TimestampSigner(salt="invites").sign("42")
    with pytest.raises(BadSignature):
        Signer(salt="invites").unsign(stamped)


def test_secret_key_signs_and_fallbacks_still_verify() -> None:
    """Rotation: the new key signs, and what the old one signed still holds."""
    with override_settings(SECRET_KEY=OLD_KEY):
        old = Signer(salt="tests").sign("value")
        old_object = dumps({"a": 1})

    with override_settings(SECRET_KEY=NEW_KEY, SECRET_KEY_FALLBACKS=[OLD_KEY]):
        new = Signer(salt="tests").sign("value")
        assert Signer(salt="tests").unsign(old) == "value"
        assert Signer(salt="tests").unsign(new) == "value"
        assert loads(old_object) == {"a": 1}
        assert new != old

    with override_settings(SECRET_KEY=NEW_KEY):
        assert Signer(salt="tests").unsign(new) == "value"
        with pytest.raises(BadSignature):
            Signer(salt="tests").unsign(old)
        with pytest.raises(BadSignature):
            loads(old_object)


def test_explicit_keys_bring_only_their_own_fallbacks() -> None:
    """With ``key``, ``fallback_keys`` verify; ``SECRET_KEY`` plays no part."""
    old = Signer(salt="tests", key=OLD_KEY).sign("value")
    current = Signer(salt="tests").sign("value")

    rotated = Signer(salt="tests", key=NEW_KEY, fallback_keys=[OLD_KEY.encode()])

    assert rotated.unsign(old) == "value"
    assert rotated.sign("value") == Signer(salt="tests", key=NEW_KEY).sign("value")
    with pytest.raises(BadSignature):
        rotated.unsign(current)
    with pytest.raises(BadSignature):
        Signer(salt="tests", key=NEW_KEY).unsign(old)
    # Fallbacks add to SECRET_KEY when no key is given.
    assert Signer(salt="tests", fallback_keys=[OLD_KEY]).unsign(old) == "value"
    assert Signer(salt="tests", fallback_keys=[OLD_KEY]).unsign(current) == "value"


def test_keys_must_exist_and_never_show() -> None:
    """No ``SECRET_KEY``: signing is refused; reprs never show a key."""
    with (
        override_settings(SECRET_KEY=UNSET),
        pytest.raises(ImproperlyConfigured, match="Set SECRET_KEY"),
    ):
        Signer()
    with pytest.raises(ValueError, match="cannot be empty"):
        Signer(key="")
    with pytest.raises(ValueError, match="cannot be empty"):
        Signer(key=OLD_KEY, fallback_keys=[b""])

    signer = Signer(salt="tests", key=OLD_KEY)
    secret = settings.SECRET_KEY
    assert secret is not None

    assert repr(signer) == "Signer(salt='tests')"
    assert repr(TimestampSigner()) == "TimestampSigner(salt='domainkit.signing')"
    assert secret.get_secret_value() not in repr(unwrap_settings())
    assert secret.get_secret_value() not in unwrap_settings().model_dump_json()
    with pytest.raises(AttributeError):
        signer.key = "x"  # type: ignore[attr-defined]


def test_objects_are_compact_url_safe_json() -> None:
    """``sign_object`` writes compact JSON in URL-safe base64, readable by anyone."""
    signer = Signer(salt="tests")
    obj = {"name": "Zoë", "ids": [1, 2], "nested": {"ok": True, "none": None}}

    signed = signer.sign_object(obj)
    encoded, _ = signed.rsplit(":", 1)

    assert "=" not in encoded
    assert json.loads(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)))
    assert base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)).decode() == (
        '{"name":"Zoë","ids":[1,2],"nested":{"ok":true,"none":null}}'
    )
    assert signer.unsign_object(signed) == obj
    assert signer.unsign_object(signer.sign_object("é?" * 10)) == "é?" * 10
    with pytest.raises(BadSignature):
        signer.unsign_object(signed.replace(encoded, encoded[:-2] + "xx"))


@pytest.mark.parametrize("payload", ["not base64!", "e30x", "bm90IGpzb24", "_w"])
def test_signed_text_that_is_not_an_object_is_refused(payload: str) -> None:
    """A signature over something ``sign_object`` did not write is refused."""
    signer = Signer(salt="tests")

    with pytest.raises(BadSignature, match="not an encoded object"):
        signer.unsign_object(signer.sign(payload))


def test_timestamps_expire(clock: list[float]) -> None:
    """``max_age`` refuses older signatures, as seconds or a ``timedelta``."""
    signer = TimestampSigner(salt="tests")
    signed = signer.sign("value")
    value, stamp, _ = signed.split(":")

    assert (value, stamp) == ("value", signer.timestamp())
    clock[0] += 60
    assert signer.unsign(signed) == "value"
    assert signer.unsign(signed, max_age=60) == "value"
    assert signer.unsign(signed, max_age=timedelta(minutes=1)) == "value"
    with pytest.raises(SignatureExpired, match="older than 59s"):
        signer.unsign(signed, max_age=59)
    with pytest.raises(SignatureExpired, match=r"older than 59\.5s"):
        signer.unsign(signed, max_age=timedelta(seconds=59.5))
    # A clock behind the signer's is no reason to refuse.
    clock[0] -= 3600
    assert signer.unsign(signed, max_age=0) == "value"


def test_expired_is_a_bad_signature_and_checked_after_it(clock: list[float]) -> None:
    """A forged value is refused as forged, whatever its age."""
    signer = TimestampSigner(salt="tests")
    signed = signer.sign("value")
    clock[0] += 3600

    with pytest.raises(BadSignature) as caught:
        signer.unsign(signed, max_age=1)
    assert type(caught.value) is SignatureExpired
    with pytest.raises(BadSignature) as forged:
        signer.unsign("x" + signed, max_age=1)
    assert type(forged.value) is BadSignature


@pytest.mark.parametrize(
    ("seconds", "stamp"),
    [
        (0, "0"),
        (9, "9"),
        (10, "A"),
        (35, "Z"),
        (36, "a"),
        (61, "z"),
        (62, "10"),
        (3843, "zz"),
        (1_700_000_000, "1r31eq"),
    ],
)
def test_timestamps_are_base_62(clock: list[float], seconds: int, stamp: str) -> None:
    """Whole seconds since the epoch, in base 62, read back exactly."""
    clock[0] = seconds + 0.9
    signer = TimestampSigner(salt="tests")

    assert signer.timestamp() == stamp
    signed = signer.sign("v")
    clock[0] = seconds + 10
    with pytest.raises(SignatureExpired):
        signer.unsign(signed, max_age=9.0)
    assert signer.unsign(signed, max_age=10) == "v"


@pytest.mark.parametrize("stamp", ["", "!", "1-2", "ab c"])
def test_signed_timestamps_must_be_base_62(stamp: str) -> None:
    """A signature over a malformed timestamp is still refused."""
    signer = TimestampSigner(salt="tests")
    stamped = f"value:{stamp}"
    signed = Signer.sign(signer, stamped)

    with pytest.raises(BadSignature, match="timestamp"):
        signer.unsign(signed)


def test_dumps_and_loads(clock: list[float]) -> None:
    """Signed, timestamped JSON, with a default salt."""
    token = dumps({"user": 7, "scope": ["read"]})
    other = dumps({"user": 7}, salt="invites", key=OLD_KEY)

    assert loads(token) == {"user": 7, "scope": ["read"]}
    assert loads(other, salt="invites", key=OLD_KEY) == {"user": 7}
    assert loads(other, salt="invites", key=NEW_KEY, fallback_keys=[OLD_KEY]) == {
        "user": 7
    }
    with pytest.raises(BadSignature):
        loads(other, salt="invites")
    with pytest.raises(BadSignature):
        loads(token, salt="invites")
    clock[0] += 11
    assert loads(token, max_age=11) == {"user": 7, "scope": ["read"]}
    with pytest.raises(SignatureExpired):
        loads(token, max_age=10)
    with pytest.raises(SignatureExpired):
        TimestampSigner().unsign_object(token, max_age=timedelta(seconds=10))


def test_constant_time_compare() -> None:
    """Text and bytes compare by value, including non-ASCII text and empties."""
    assert constant_time_compare("abc", "abc")
    assert constant_time_compare(b"abc", "abc")
    assert constant_time_compare("é", "é".encode())
    assert constant_time_compare("", b"")
    assert not constant_time_compare("abc", "abd")
    assert not constant_time_compare("abc", "ab")
    assert not constant_time_compare("", "a")


def test_hkdf_matches_rfc_5869() -> None:
    """Test cases 3 (no salt, no info) and a long output of RFC 5869's SHA-256."""
    ikm = bytes.fromhex("0b" * 22)

    assert hkdf(ikm, info=b"", length=42).hex() == (
        "8da4e775a563c18f715f802a063c5a31b8a11f5c5ee1879ec3454e5f3c738d2d"
        "9d201395faa4b61a96c8"
    )
    long = hkdf(ikm, info=b"info", length=100)
    assert len(long) == 100
    assert long[:32] == hkdf(ikm, info=b"info", length=32)
    assert hkdf(ikm, info=b"info", length=1) == long[:1]
    assert len(hkdf(ikm, info=b"", length=255 * 32)) == 255 * 32
    for length in (0, 255 * 32 + 1, -1):
        with pytest.raises(ValueError, match="1 to 8160 bytes"):
            hkdf(ikm, info=b"", length=length)


def test_hkdf_with_info_matches_a_reference() -> None:
    """RFC 5869 test case 1's info and length, with no salt (zeros)."""
    import hashlib
    import hmac

    ikm = bytes.fromhex("0b" * 22)
    info = bytes.fromhex("f0f1f2f3f4f5f6f7f8f9")
    prk = hmac.new(bytes(32), ikm, hashlib.sha256).digest()
    first = hmac.new(prk, info + b"\x01", hashlib.sha256).digest()
    second = hmac.new(prk, first + info + b"\x02", hashlib.sha256).digest()

    assert hkdf(ikm, info=info, length=42) == (first + second)[:42]


def test_derived_keys_are_per_purpose_and_per_secret() -> None:
    """Each purpose gets its own key; fallbacks get theirs, in order."""
    first = derive_key("one")

    assert len(first) == 32
    assert len(derive_key("one", length=16)) == 16
    assert derive_key("one") == first
    assert derive_key("two") != first
    assert derive_key("one", key=OLD_KEY) != first
    assert derive_key("one", key=OLD_KEY) == derive_key("one", key=OLD_KEY.encode())
    assert derive_key("one", key=OLD_KEY) == hkdf(
        OLD_KEY.encode(), info=b"domainkit:one"
    )
    with override_settings(SECRET_KEY=NEW_KEY, SECRET_KEY_FALLBACKS=[OLD_KEY]):
        assert derive_keys("one") == (
            derive_key("one", key=NEW_KEY),
            derive_key("one", key=OLD_KEY),
        )
        assert derive_keys("one", length=8) == (
            derive_key("one", key=NEW_KEY, length=8),
            derive_key("one", key=OLD_KEY, length=8),
        )


DEPLOYED: dict[str, Any] = {
    "ENVIRONMENT": "production",
    "ALLOWED_HOSTS": ["api.example.com"],
    "SECRET_KEY": NEW_KEY,
}


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"SECRET_KEY": None}, "SECRET_KEY is required in production"),
        ({"SECRET_KEY": "x" * 60}, "SECRET_KEY must be random"),
        ({"SECRET_KEY": NEW_KEY[:49]}, "at least 50 characters, in production"),
        ({"SECRET_KEY": "abcd" * 20}, "SECRET_KEY must be random"),
        (
            {"SECRET_KEY": "domainkit-insecure-" + NEW_KEY},
            "SECRET_KEY holds a development key in production",
        ),
        ({"SECRET_KEY": "django-insecure-" + NEW_KEY}, "development key"),
        (
            {"SECRET_KEY_FALLBACKS": [OLD_KEY, "short"]},
            "SECRET_KEY_FALLBACKS must be random",
        ),
        (
            {"SECRET_KEY_FALLBACKS": ["domainkit-insecure-" + OLD_KEY]},
            "SECRET_KEY_FALLBACKS holds a development key",
        ),
        ({"ENVIRONMENT": "staging", "SECRET_KEY": None}, "required in staging"),
    ],
)
def test_deployments_refuse_weak_secret_keys(
    change: dict[str, Any], message: str
) -> None:
    """Missing, short, simple, or development keys stop staging and production.

    The error, as settings loading reports it, never quotes a key.
    """
    with pytest.raises(ValueError, match=message):
        Settings.model_validate({**DEPLOYED, **change})
    with pytest.raises(ImproperlyConfigured, match=message) as refused:
        override_settings(**{**DEPLOYED, **change}).__enter__()

    for key in (change.get("SECRET_KEY"), *change.get("SECRET_KEY_FALLBACKS", [])):
        if key:
            assert key not in str(refused.value)


def test_deployments_accept_random_keys() -> None:
    """Fifty characters, five of them different, and no development prefix."""
    key = "abcde" * 10

    deployed = Settings.model_validate(
        {**DEPLOYED, "SECRET_KEY": key, "SECRET_KEY_FALLBACKS": [OLD_KEY, NEW_KEY]}
    )

    assert deployed.SECRET_KEY is not None
    assert deployed.SECRET_KEY.get_secret_value() == key
    assert "abcde" not in repr(deployed)
    assert repr(deployed.SECRET_KEY_FALLBACKS) == (
        "(SecretStr('**********'), SecretStr('**********'))"
    )


@pytest.mark.parametrize("environment", ["local", "test"])
def test_development_keys_work_where_nothing_is_deployed(environment: str) -> None:
    """Locally, any key signs, and none is required until something signs."""
    local = Settings.model_validate(
        {"ENVIRONMENT": environment, "SECRET_KEY": "domainkit-insecure-dev"}
    )

    assert local.SECRET_KEY is not None
    assert Settings.model_validate({"ENVIRONMENT": environment}).SECRET_KEY is None
    with pytest.raises(ValueError, match="at least 1 item"):
        Settings.model_validate({"ENVIRONMENT": environment, "SECRET_KEY": ""})


def test_errors_and_logs_never_show_the_key(caplog: pytest.LogCaptureFixture) -> None:
    """A bad signature names no key, value, or signature."""
    caplog.set_level(logging.DEBUG)
    signer = Signer(salt="tests")
    signed = signer.sign("secret-value")

    with pytest.raises(BadSignature) as refused:
        signer.unsign(signed[:-1])

    assert "secret-value" not in str(refused.value)
    assert signed[-20:-1] not in str(refused.value)
    assert caplog.records == []


def test_refusals_say_why() -> None:
    """Each refusal names what is wrong, and nothing it was given."""
    signer = TimestampSigner(salt="tests")
    refusals: dict[str, Callable[[], object]] = {
        "No signature found.": lambda: signer.unsign("unsigned"),
        "The signature does not match.": lambda: signer.unsign("a:b"),
        "No timestamp found.": lambda: signer.unsign(Signer.sign(signer, "value:")),
        "The timestamp is not base 62.": lambda: signer.unsign(
            Signer.sign(signer, "value:!")
        ),
        "The signed value is not an encoded object.": lambda: signer.unsign_object(
            signer.sign("!")
        ),
    }
    for message, refuse in refusals.items():
        with pytest.raises(BadSignature) as refused:
            refuse()
        assert str(refused.value) == message
    with pytest.raises(ValueError, match="cannot be empty") as empty:
        Signer(key=b"")
    assert str(empty.value) == "A key cannot be empty."
    with (
        override_settings(SECRET_KEY=UNSET),
        pytest.raises(ImproperlyConfigured) as unset,
    ):
        derive_key("one")
    assert str(unset.value) == (
        "Set SECRET_KEY: it signs the values this service hands out "
        "(docs/reference.md)."
    )


def test_timestamped_values_may_hold_the_separator(clock: list[float]) -> None:
    """The timestamp is the last part before the signature, whatever the value."""
    signer = TimestampSigner(salt="tests")

    assert signer.unsign(signer.sign("a:b:c"), max_age=0) == "a:b:c"
    assert signer.unsign_object(signer.sign_object({"a:b": ":"})) == {"a:b": ":"}
