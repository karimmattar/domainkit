"""Password rules: each validator, and the configured set."""

import gzip
from pathlib import Path
from types import SimpleNamespace

import pytest

from domainkit.contrib.auth import (
    InvalidPassword,
    password_help_texts,
    validate_password,
)
from domainkit.contrib.auth.settings import PasswordValidatorConfig
from domainkit.contrib.auth.validators import (
    CommonPasswordValidator,
    MinimumLengthValidator,
    NumericPasswordValidator,
    PasswordValidationError,
    UserAttributeSimilarityValidator,
    load_validators,
)
from domainkit.exceptions import ImproperlyConfigured
from domainkit_tests.conftest import UseAuth

VALIDATORS = "domainkit.contrib.auth.validators"


def test_minimum_length() -> None:
    """Eight characters by default, or the configured minimum."""
    MinimumLengthValidator().validate("12345678")
    with pytest.raises(PasswordValidationError, match="at least 8 characters"):
        MinimumLengthValidator().validate("1234567")
    with pytest.raises(PasswordValidationError, match="at least 12"):
        MinimumLengthValidator(min_length=12).validate("12345678901")
    assert "12 characters" in MinimumLengthValidator(12).help_text()


def test_numeric_passwords() -> None:
    """Only digits is refused."""
    NumericPasswordValidator().validate("1234a")
    with pytest.raises(PasswordValidationError, match="entirely numeric"):
        NumericPasswordValidator().validate("98765432")
    assert NumericPasswordValidator().help_text() == (
        "Your password can't be entirely numeric."
    )


def test_common_passwords(tmp_path: Path) -> None:
    """Django's 20,000 by default, whatever the case; or a list of one's own."""
    validator = CommonPasswordValidator()
    validator.validate("wj8vC5-uncommon")
    for common in ("password", "PassWord", " qwerty "):
        with pytest.raises(PasswordValidationError, match="too common"):
            validator.validate(common)
    assert len(validator.passwords) > 19_000
    assert validator.help_text() == "Your password can't be a commonly used password."

    plain = tmp_path / "plain.txt"
    plain.write_text("letmein2\nopensesame\n", encoding="utf-8")
    zipped = tmp_path / "list.gz"
    zipped.write_bytes(gzip.compress(b"tr0ub4dor\n"))
    with pytest.raises(PasswordValidationError):
        CommonPasswordValidator(plain).validate("OpenSesame")
    with pytest.raises(PasswordValidationError):
        CommonPasswordValidator(str(zipped)).validate("tr0ub4dor")
    with pytest.raises(ImproperlyConfigured, match="Cannot read"):
        CommonPasswordValidator(tmp_path / "missing.txt")


def test_similarity_to_the_users_details() -> None:
    """Close to a detail, or to a part of it, is refused; far is fine."""
    user = SimpleNamespace(
        username="maximilian",
        first_name="",
        last_name=None,
        email="max.power@springfield.example",
    )
    validator = UserAttributeSimilarityValidator()
    validator.validate("maximilian1", None)
    with pytest.raises(PasswordValidationError, match="similar to the username"):
        validator.validate("maximilian1", user)
    with pytest.raises(PasswordValidationError, match="similar to the email"):
        validator.validate("springfield", user)
    validator.validate("correct-horse-battery", user)
    # A long password is never "like" a short part (here "max").
    validator.validate(
        "maxmaxmaxmaxmaxmaxmaxmaxmaxmaxmax", SimpleNamespace(email="max")
    )
    assert validator.help_text() == (
        "Your password can't be too similar to your other personal information."
    )
    with pytest.raises(ImproperlyConfigured) as low:
        UserAttributeSimilarityValidator(max_similarity=0.05)
    assert str(low.value) == "max_similarity must be at least 0.1."
    UserAttributeSimilarityValidator(max_similarity=0.1)


def test_similarity_edges() -> None:
    """Django's ratio and its shortcut for long passwords, at their limits."""
    validator = UserAttributeSimilarityValidator()
    # 7 letters in common out of 10 + 10: a ratio of exactly 0.7.
    with pytest.raises(PasswordValidationError) as named:
        validator.validate("abcdefgxyz", SimpleNamespace(first_name="abcdefghij"))
    assert str(named.value) == "The password is too similar to the first name."
    # "alice1234" is like "alice" (0.71), not ".alice" (0.67); the empty part
    # before "alice" is skipped, not the end of the search.
    with pytest.raises(PasswordValidationError, match="username"):
        validator.validate("alice1234", SimpleNamespace(username=".alice"))
    validator.validate("", SimpleNamespace(username="x."))
    # Ten times longer than "abc", but still within reach of a 0.1 ratio.
    loose = UserAttributeSimilarityValidator(max_similarity=0.1)
    with pytest.raises(PasswordValidationError):
        loose.validate("abc" + "x" * 27, SimpleNamespace(username="abc"))


def test_validate_password_reports_every_broken_rule(use_auth: UseAuth) -> None:
    """The default rules all run; the error lists each message."""
    use_auth()
    validate_password("vy3!Tq-pX9#mR2", SimpleNamespace(username="alice"))
    with pytest.raises(InvalidPassword) as caught:
        validate_password("123456")

    assert caught.value.errors == [
        "This password is too short. It must contain at least 8 characters.",
        "This password is too common.",
        "This password is entirely numeric.",
    ]
    assert caught.value.code == "invalid_password"
    with pytest.raises(InvalidPassword) as similar:
        validate_password("alice2026", SimpleNamespace(username="alice"))
    assert similar.value.errors == ["The password is too similar to the username."]
    assert password_help_texts() == [
        "Your password can't be too similar to your other personal information.",
        "Your password must contain at least 8 characters.",
        "Your password can't be a commonly used password.",
        "Your password can't be entirely numeric.",
    ]


def test_validators_follow_settings(use_auth: UseAuth) -> None:
    """Options reach the classes; ``()`` checks nothing; mistakes are refused."""
    use_auth(
        PASSWORD_VALIDATORS=[
            {
                "NAME": f"{VALIDATORS}.MinimumLengthValidator",
                "OPTIONS": {"min_length": 3},
            }
        ]
    )
    validate_password("abc")
    validate_password("ab", validators=[])
    with pytest.raises(InvalidPassword):
        validate_password("ab")

    use_auth(PASSWORD_VALIDATORS=[])
    validate_password("")

    with pytest.raises(ImproperlyConfigured, match=r"nowhere\.Rule"):
        load_validators((PasswordValidatorConfig(NAME="nowhere.Rule"),))
    with pytest.raises(ImproperlyConfigured, match="MinimumLengthValidator"):
        load_validators(
            (
                PasswordValidatorConfig(
                    NAME=f"{VALIDATORS}.MinimumLengthValidator", OPTIONS={"size": 3}
                ),
            )
        )


def test_validator_configs_hash_by_value() -> None:
    """Equal configurations share one cache entry."""
    first = PasswordValidatorConfig(NAME="a.B", OPTIONS={"x": [1], "y": 2})
    second = PasswordValidatorConfig(NAME="a.B", OPTIONS={"y": 2, "x": [1]})
    other = PasswordValidatorConfig(NAME="a.B", OPTIONS={"x": [2], "y": 2})
    path = PasswordValidatorConfig(NAME="a.B", OPTIONS={"path": Path("list.txt")})
    same_path = PasswordValidatorConfig(NAME="a.B", OPTIONS={"path": Path("list.txt")})

    assert hash(first) == hash(second)
    assert first == second
    assert hash(first) != hash(other)
    assert hash(path) == hash(same_path)
    assert hash(path) != hash(PasswordValidatorConfig(NAME="a.B"))
