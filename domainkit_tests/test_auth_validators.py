"""Password rules: each validator, and the configured set."""

import gzip
from pathlib import Path
from types import SimpleNamespace
from typing import Final

import pytest

from domainkit.conf import override_settings
from domainkit.contrib.auth import (
    InvalidPassword,
    password_help_texts,
    validate_password,
)
from domainkit.contrib.auth.settings import PasswordValidatorConfig
from domainkit.contrib.auth.validators import (
    CommonPasswordValidator,
    LettersAndDigitsValidator,
    MinimumLengthValidator,
    NumericPasswordValidator,
    PasswordValidationError,
    UserAttributeSimilarityValidator,
    load_validators,
)
from domainkit.exceptions import ImproperlyConfigured
from domainkit.i18n import override
from domainkit_tests.conftest import MIDDLEWARE, Api, UseAuth
from domainkit_tests.support.auth import PASSWORD, bearer, create_user, login

VALIDATORS = "domainkit.contrib.auth.validators"
RULES: Final = [
    {"NAME": f"{VALIDATORS}.UserAttributeSimilarityValidator"},
    {"NAME": f"{VALIDATORS}.MinimumLengthValidator", "OPTIONS": {"min_length": 12}},
    {"NAME": f"{VALIDATORS}.CommonPasswordValidator"},
    {"NAME": f"{VALIDATORS}.NumericPasswordValidator"},
    {"NAME": f"{VALIDATORS}.LettersAndDigitsValidator"},
]
"""The four common rules, 12 characters long, and the PCI preset's own."""
ARABIC_REFUSALS: Final = [
    "كلمة المرور تشبه الاسم الأول كثيرًا.",
    "كلمة المرور هذه قصيرة جدًا. يجب ألا يقل عدد أحرفها عن ١٢.",
    "كلمة المرور هذه شائعة جدًا.",
    "كلمة المرور هذه مكوّنة من أرقام فقط.",
    "يجب أن تحتوي كلمة المرور هذه على حروف وأرقام معًا.",
]
ARABIC_RULES: Final = [
    "لا يمكن أن تشبه كلمة مرورك معلوماتك الشخصية الأخرى كثيرًا.",
    "يجب ألا يقل عدد أحرف كلمة مرورك عن ١٢.",
    "لا يمكن أن تكون كلمة مرورك من كلمات المرور الشائعة.",
    "لا يمكن أن تتكوّن كلمة مرورك من أرقام فقط.",
    "يجب أن تحتوي كلمة مرورك على حرف واحد ورقم واحد على الأقل.",
]


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


# Their language (user report I4) -----------------------------------------------------


def too_similar(name: str) -> str:
    """The refusal of a password that is the user's ``name`` field."""
    password = "Kh4lid-Rahman"
    with pytest.raises(PasswordValidationError) as caught:
        UserAttributeSimilarityValidator((name,)).validate(
            password, SimpleNamespace(**{name: password})
        )
    return str(caught.value)


def test_the_rules_speak_arabic_with_their_values_filled_in(use_auth: UseAuth) -> None:
    """Each refusal and rule is translated first, then given its value.

    The length and the detail named were filled in before translating, so
    no catalog could hold the text; the shipped catalogs held none of them.
    """
    use_auth(PASSWORD_VALIDATORS=RULES)
    with override("ar"):
        with pytest.raises(InvalidPassword) as refused:
            validate_password("1234567", SimpleNamespace(first_name="1234567"))
        rules = password_help_texts()
        similar = [
            too_similar(name)
            for name in ("username", "last_name", "email", "mobile_number", "pin")
        ]

    assert refused.value.errors == ARABIC_REFUSALS
    assert rules == ARABIC_RULES
    assert similar == [
        "كلمة المرور تشبه اسم المستخدم كثيرًا.",
        "كلمة المرور تشبه اسم العائلة كثيرًا.",
        "كلمة المرور تشبه البريد الإلكتروني كثيرًا.",
        "كلمة المرور تشبه رقم الجوال كثيرًا.",
        "كلمة المرور تشبه pin كثيرًا.",  # A field no catalog names.
    ]


def test_a_length_of_one_is_one_character() -> None:
    """The length chooses the plural form, as the language counts."""
    with pytest.raises(PasswordValidationError) as short:
        MinimumLengthValidator(1).validate("")

    assert str(short.value) == (
        "This password is too short. It must contain at least 1 character."
    )
    assert MinimumLengthValidator(1).help_text() == (
        "Your password must contain at least 1 character."
    )
    assert LettersAndDigitsValidator().help_text() == (
        "Your password must contain at least one letter and one digit."
    )


def test_a_client_in_arabic_reads_the_rules_in_arabic(identity: Api) -> None:
    """``Accept-Language: ar`` on a password change: every refusal in Arabic."""
    with override_settings(
        MIDDLEWARE=["domainkit.http.locale.LocaleMiddleware", *MIDDLEWARE]
    ):
        client = identity(PASSWORD_VALIDATORS=RULES)
        create_user("alice", first_name="1234567")
        headers = bearer(login(client, "alice")["access_token"])
        refused = client.post(
            "/auth/password",
            json={"old_password": PASSWORD, "new_password": "1234567"},
            headers={**headers, "Accept-Language": "ar"},
        )

    assert (refused.status_code, refused.json()["code"]) == (422, "invalid_password")
    assert refused.json()["detail"] == " ".join(ARABIC_REFUSALS)
    assert refused.headers["Content-Language"] == "ar"
