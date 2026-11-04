"""Users identified by the fields a project chooses (ADR 0126)."""

import pytest

from domainkit.checks import CheckFailed
from domainkit.contrib.auth import (
    IdentifierRequired,
    InvalidMobileNumber,
    PasswordHash,
    User,
    normalize_mobile_number,
)
from domainkit.contrib.auth.events import MobileNumberVerified
from domainkit.contrib.auth.settings import identifier_fields
from domainkit_tests.conftest import UseAuth

HASH = PasswordHash("pbkdf2_sha256$1$salt$hash")


@pytest.mark.parametrize(
    ("text", "number"),
    [
        ("+33612345678", "+33612345678"),
        ("+33 6 12 34 56 78", "+33612345678"),
        ("+1 (415) 555-0100", "+14155550100"),
        ("+44.20.7946.0958", "+442079460958"),
    ],
)
def test_a_mobile_number_is_kept_in_international_form(text: str, number: str) -> None:
    """Spaces, dots, dashes, and brackets are dropped."""
    assert normalize_mobile_number(text) == number


@pytest.mark.parametrize(
    "text",
    [
        "0612345678",
        "+0612345678",
        "+12345",
        "+1234567890123456",
        "+٣٣٦١٢٣٤٥٦٧٨",
        "+33612345678x",
    ],
)
def test_a_mobile_number_that_is_not_international_is_refused(text: str) -> None:
    """No country code, too short or long, or digits that are not ASCII."""
    with pytest.raises(InvalidMobileNumber) as refused:
        normalize_mobile_number(text)
    assert refused.value.message == (
        "Give the number in international form, such as +33612345678."
    )
    assert refused.value.code == "invalid_mobile_number"


def test_a_user_needs_no_username() -> None:
    """An email address or a mobile number is enough, and names the user."""
    by_number = User.create(password=HASH, mobile_number="+33 6 12 34 56 78")
    by_email = User.create(password=HASH, email="Ann@Example.com", username="")

    assert (by_number.username, by_number.mobile_number) == (None, "+33612345678")
    assert by_number.get_username() == "+33612345678"
    assert (by_email.username, by_email.get_username()) == (None, "Ann@example.com")
    assert (
        User.create(password=HASH, username="ann", email="a@x.example").get_username()
        == "ann"
    )


def test_a_user_with_no_identifier_names_none() -> None:
    """``get_username()`` is empty, never None."""
    user = User.create(password=HASH, username="ann")
    user.username = None

    assert user.get_username() == ""


def test_a_mobile_number_is_verified_once() -> None:
    """A second verification records nothing."""
    user = User.create(password=HASH, mobile_number="+33612345678")
    user.verify_mobile_number()
    user.verify_mobile_number()

    assert user.mobile_number_verified
    assert [type(event) for event in user.pull_events()].count(
        MobileNumberVerified
    ) == 1


def test_a_new_number_or_address_is_not_verified_yet() -> None:
    """Changing one resets its flag only."""
    user = User.create(password=HASH, email="a@x.example", mobile_number="+33612345678")
    user.verify_email()
    user.verify_mobile_number()

    user.update(first_name="Ann")
    assert (user.email_verified, user.mobile_number_verified) == (True, True)

    user.update(mobile_number="+33 6 00 00 00 00")
    assert user.mobile_number == "+33600000000"
    assert (user.email_verified, user.mobile_number_verified) == (True, False)

    user.update(email="b@x.example")
    assert user.email_verified is False


def test_an_update_keeps_one_identifier() -> None:
    """Removing the last one is refused, and changes nothing."""
    user = User.create(password=HASH, email="a@x.example", mobile_number="+33612345678")
    user.update(mobile_number="")
    assert user.mobile_number is None

    with pytest.raises(IdentifierRequired) as refused:
        user.update(email="")
    assert refused.value.message == (
        "Keep a username, an email address, or a mobile number."
    )
    assert (user.email, user.mobile_number) == ("a@x.example", None)


def test_identifiers_default_to_the_username_field(use_auth: UseAuth) -> None:
    """None: ``USERNAME_FIELD`` alone, as before 1.8."""
    use_auth()
    assert identifier_fields(User) == ("username",)

    use_auth(IDENTIFIERS=("email", "mobile_number"))
    assert identifier_fields(User) == ("email", "mobile_number")


def test_identifiers_name_fields_of_the_user_model(use_auth: UseAuth) -> None:
    """``auth.E011`` names each unknown field."""
    with pytest.raises(CheckFailed) as refused:
        use_auth(IDENTIFIERS=("email", "phone", "nickname"))

    [problem] = refused.value.messages
    assert getattr(problem, "id", None) == "auth.E011"
    assert str(problem).startswith(
        "AUTH.IDENTIFIERS names 'phone', 'nickname', which User has no field for"
    )


def test_identifiers_name_each_field_once(use_auth: UseAuth) -> None:
    """``auth.E011`` refuses a field named twice."""
    with pytest.raises(CheckFailed) as refused:
        use_auth(IDENTIFIERS=("email", "email"))

    [problem] = refused.value.messages
    assert getattr(problem, "id", None) == "auth.E011"
    assert "AUTH.IDENTIFIERS names a field twice: name each once." in str(problem)
