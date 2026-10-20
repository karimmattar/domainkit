"""The auth domain model: users, groups, permissions, and password hashes."""

from dataclasses import fields
from typing import ClassVar
from uuid import uuid4

import pytest

from domainkit.contrib.auth import (
    AbstractBaseUser,
    AnonymousUser,
    Group,
    InvalidEmail,
    InvalidGroupName,
    InvalidUsername,
    PasswordHash,
    Permission,
    UnknownUserField,
    User,
    UserId,
)
from domainkit.contrib.auth.events import (
    UserJoinedGroup,
    UserLeftGroup,
    UserPermissionGranted,
    UserPermissionRevoked,
    UserStatusChanged,
)
from domainkit.contrib.auth.models import (
    erased_username,
    normalize_email,
    reserved_username,
    same_email,
    validate_email,
)
from domainkit.contrib.contenttypes import ContentType
from domainkit.ddd import DomainEvent

HASH = PasswordHash("pbkdf2_sha256$1$salt$aGFzaA==")


def permission(key: str, number: int) -> Permission:
    """A permission ``<domain>.<codename>`` with its content type."""
    domain, codename = key.split(".")
    content_type = ContentType(id=number, domain=domain, model=codename.split("_")[-1])
    return Permission(id=number, codename=codename, name=key, content_type=content_type)


def new_user(**fields: object) -> User:
    """A user created as code does, with its creation event taken."""
    user = User.create(password=HASH, **{"username": "alice", **fields})
    user.pull_events()
    return user


def kinds(events: list[DomainEvent]) -> list[type[DomainEvent]]:
    """The classes of ``events``."""
    return [type(event) for event in events]


def facts(events: list[DomainEvent]) -> list[tuple[str, dict[str, object]]]:
    """Each event's class and own fields (not its id or time)."""
    common = {"event_id", "occurred_at"}
    return [
        (
            type(event).__name__,
            {
                each.name: getattr(event, each.name)
                for each in fields(event)
                if each.name not in common
            },
        )
        for event in events
    ]


def test_password_hashes_refuse_raw_passwords() -> None:
    """Only hasher output or unusable markers, and never shown in reprs."""
    for raw in ("hunter2", "Pa$$word", ""):
        with pytest.raises(ValueError, match="never a raw password") as refused:
            PasswordHash(raw)
        assert str(refused.value) == (
            "A PasswordHash holds hashers.make_password output, never a raw password."
        )
    assert repr(HASH) == "PasswordHash(...)"
    assert HASH.usable
    unusable = PasswordHash.unusable()
    assert unusable.value.startswith("!")
    assert len(unusable.value) == 41  # As Django's: "!" and 40 random characters.
    assert not unusable.usable
    assert unusable != PasswordHash.unusable()


def test_creating_users_checks_and_tidies_names() -> None:
    """NFKC usernames (full-width letters too), lower-case email domains."""
    user = User.create(
        password=HASH,
        username="\uff41\uff4c\uff49\uff43\uff45",
        email=" Al@Example.COM ",
    )

    assert (user.username, user.email) == ("alice", "Al@example.com")
    assert str(user) == user.get_username() == "alice"
    assert facts(user.pull_events()) == [
        ("UserCreated", {"user_id": user.id, "username": "alice"})
    ]
    for bad in ("", "has space", "x" * 151, "semi;colon"):
        with pytest.raises(InvalidUsername) as refused:
            User.create(password=HASH, username=bad)
        assert refused.value.message == (
            "A username has 1 to 150 letters, digits, and @ . + - _ characters."
        )
    with pytest.raises(InvalidUsername):
        User.create(password=HASH)
    assert User.create(password=HASH, username="x" * 150).email == ""
    assert normalize_email("not-an-email ") == "not-an-email"
    assert normalize_email("Q@Q@EXAMPLE.COM") == "Q@Q@example.com"


@pytest.mark.parametrize(
    "name", ["erased-x", "Erased-bob", "ERASED-0123", "\uff45rased-x", "erased-"]
)
def test_names_erasure_leaves_are_kept_for_it(name: str) -> None:
    """Nobody picks a name that starts with ``erased-``, in any case or width."""
    with pytest.raises(InvalidUsername) as refused:
        new_user(username=name)

    assert refused.value.message == (
        "Usernames that start with erased- are kept for erased accounts."
    )
    assert reserved_username(name)
    assert not reserved_username("unerased-x")
    assert new_user(username="erasedx").username == "erasedx"


def test_erasing_renames_names_that_only_look_erased() -> None:
    """One picked before the prefix was kept is erased too; erasing twice is one."""
    user = new_user(email="eve@example.com")
    user.username = "erased-eve"  # Stored before the prefix was reserved.

    user.erase_personal_data(frozenset({"username", "email"}))
    renamed = user.username
    user.erase_personal_data(frozenset({"username", "email"}))

    assert renamed == erased_username("erased-eve")
    assert user.username == renamed


NOT_PLAIN: list[str] = [
    "ceo\uff20corp.example<eve@evil.example>",  # A display name: mail goes to eve.
    "Boss <boss@example.com>",
    "<boss@example.com>",
    "boss@example.com (Boss)",
    "a@example.com, b@example.com",
    "\u212aelly@corp.example",  # The KELVIN SIGN, which lower() makes a k.
    "kelly@\u212aorp.example",
    "j\u00f6rg@example.com",
    "jo@b\u00fccher.example",
    "not-an-email",
    "a@b@example.com",
    "a@-example.com",
    "line\n@break.example",
    "x" * 65 + "@example.com",
]


@pytest.mark.parametrize("email", NOT_PLAIN)
def test_users_have_plain_addresses_only(email: str) -> None:
    """One ASCII address alone, the one mail goes to: as created, as changed."""
    message = "Enter one email address, such as user@example.com, without a name."
    with pytest.raises(InvalidEmail) as refused:
        new_user(email=email)
    assert refused.value.message == message
    user = new_user(email="a@example.com")
    with pytest.raises(InvalidEmail):
        user.update(email=email)
    assert user.email == "a@example.com"
    with pytest.raises(InvalidEmail):
        validate_email(email)


def test_plain_addresses_are_trimmed_and_compared_by_ascii_letters() -> None:
    """``same_email`` folds ASCII letters only: no look-alike is another's."""
    assert validate_email(" kelly+tag@xn--bcher-kva.example ") == (
        "kelly+tag@xn--bcher-kva.example"
    )
    assert new_user(email="").email == ""  # No address at all.
    user = new_user(email="a@example.com")
    user.update(email=" ")
    assert user.email == ""
    assert same_email(" Kelly@Corp.Example", "kelly@corp.example ")
    assert not same_email("\u212aelly@corp.example", "kelly@corp.example")
    assert "\u212aelly@corp.example".lower() == "kelly@corp.example"  # Python's.
    assert not same_email("kelly@corp.example", "kelly@corp.example.org")


def test_passwords_and_logins() -> None:
    """Setting or disabling a password is recorded; re-hashing is not."""
    user = new_user()
    user.set_password(HASH)
    user.set_unusable_password()
    assert not user.has_usable_password()
    user.rehash_password(HASH)
    assert user.has_usable_password()
    assert facts(user.pull_events()) == [("PasswordChanged", {"user_id": user.id})] * 2

    user.record_login()
    assert user.last_login is not None
    earlier = user.last_login.replace(year=2020)
    user.record_login(earlier)
    assert user.last_login == earlier


def test_status_changes_are_recorded_once() -> None:
    """Activation, staff, and superuser flags each record a change."""
    user = new_user()
    user.set_active(False)
    user.set_active(False)
    user.set_staff(True)
    user.set_superuser(True)

    assert facts(user.pull_events()) == [
        ("UserStatusChanged", {"user_id": user.id, "field": field, "value": value})
        for field, value in (
            ("is_active", False),
            ("is_staff", True),
            ("is_superuser", True),
        )
    ]


def test_permissions_from_users_and_groups() -> None:
    """Direct and group grants add up; inactive users have none."""
    add, publish, view = (
        permission("shop.add_product", 1),
        permission("shop.publish_product", 2),
        permission("billing.view_invoice", 3),
    )
    editors = Group.create("editors")
    editors.grant(publish, publish)
    user = new_user()
    user.grant(add, add)
    user.join(editors)
    user.join(editors)

    assert user.get_user_permissions() == {"shop.add_product"}
    assert user.get_group_permissions() == {"shop.publish_product"}
    assert user.has_perm("shop.publish_product")
    assert not user.has_perm(view.key)
    assert user.has_perms(["shop.add_product", "shop.publish_product"])
    assert not user.has_perms(["shop.add_product", view.key])
    assert user.has_domain_perms("shop")
    assert not user.has_domain_perms("billing")

    user.set_active(False)
    assert user.get_all_permissions() == frozenset()
    assert not user.has_perm("shop.add_product")
    assert not user.has_domain_perms("shop")

    user.set_active(True)
    user.revoke(add, add)
    user.leave(editors)
    user.leave(editors)
    assert user.get_all_permissions() == frozenset()
    member = {"user_id": user.id, "group_id": editors.id, "group": "editors"}
    assert kinds(user.pull_events()) == [
        UserPermissionGranted,
        UserJoinedGroup,
        UserStatusChanged,
        UserStatusChanged,
        UserPermissionRevoked,
        UserLeftGroup,
    ]
    user.grant(add)
    user.join(editors)
    user.revoke(add)
    user.leave(editors)
    assert facts(user.pull_events()) == [
        ("UserPermissionGranted", {"user_id": user.id, "permission": add.key}),
        ("UserJoinedGroup", member),
        ("UserPermissionRevoked", {"user_id": user.id, "permission": add.key}),
        ("UserLeftGroup", member),
    ]


def test_superusers_have_every_permission() -> None:
    """Without a single grant, while active."""
    user = new_user(is_superuser=True)

    assert user.has_perm("anything.at_all")
    assert user.has_domain_perms("anything")
    assert user.get_all_permissions() == frozenset()


def test_groups() -> None:
    """Names are trimmed and checked; changes are recorded once."""
    group = Group.create("  editors ")
    assert str(group) == group.name == "editors"
    group.rename("editors ")
    group.rename(" writers")
    assert group.name == "writers"
    publish = permission("shop.publish_product", 1)
    group.grant(publish)
    group.revoke(publish, publish)
    assert group.permission_keys() == frozenset()
    granted = {"group_id": group.id, "permission": "shop.publish_product"}
    assert facts(group.pull_events()) == [
        ("GroupCreated", {"group_id": group.id, "name": "editors"}),
        ("GroupRenamed", {"group_id": group.id, "name": "writers"}),
        ("GroupPermissionGranted", granted),
        ("GroupPermissionRevoked", granted),
    ]
    for bad in ("", "   ", "x" * 151):
        with pytest.raises(InvalidGroupName) as refused:
            Group.create(bad)
        assert refused.value.message == "A group name has 1 to 150 characters."
    with pytest.raises(InvalidGroupName):
        group.rename(" ")
    assert [Group.create(name).name for name in ("x", "y" * 150)] == ["x", "y" * 150]


def test_users_have_names_and_permissions_keys() -> None:
    """Full and short names; permissions and content types print as keys."""
    user = new_user(first_name="Alice", last_name="Liddell")
    publish = permission("shop.publish_product", 1)

    assert (user.get_full_name(), user.get_short_name()) == ("Alice Liddell", "Alice")
    assert str(publish) == publish.key == "shop.publish_product"
    assert str(publish.content_type) == "shop.product"


def test_anonymous_users_have_nothing() -> None:
    """No name, no permissions, all alike."""
    anonymous = AnonymousUser()

    assert anonymous == AnonymousUser()
    assert anonymous != new_user()
    assert hash(anonymous) == hash(AnonymousUser())
    assert str(anonymous) == "AnonymousUser"
    assert anonymous.get_username() == ""
    assert (anonymous.is_authenticated, anonymous.is_anonymous) == (False, True)
    assert not anonymous.has_perm("x.y")
    assert not anonymous.has_perms(["x.y"])
    assert not anonymous.has_domain_perms("x")
    assert anonymous.get_all_permissions() == frozenset()
    assert anonymous.get_user_permissions() == frozenset()
    assert anonymous.get_group_permissions() == frozenset()


class Badge(AbstractBaseUser):
    """A user named by a badge code, with a nickname staff may change."""

    USERNAME_FIELD: ClassVar[str] = "code"
    EDITABLE_FIELDS: ClassVar[frozenset[str]] = frozenset({"nickname"})
    READ_ONLY_FIELDS: ClassVar[frozenset[str]] = frozenset({"issued"})

    code: str
    nickname: str = ""
    issued: str = ""
    risk_flag: bool = False


class Unlisted(AbstractBaseUser):
    """Names no fields for staff, and tries to open protected ones."""

    USERNAME_FIELD: ClassVar[str] = "code"

    code: str
    nickname: str = ""


class Overreaching(Unlisted):
    """Lists the username and protected fields as editable."""

    EDITABLE_FIELDS: ClassVar[frozenset[str]] = frozenset(
        {"code", "is_superuser", "password", "nickname"}
    )


def test_staff_see_and_change_only_the_fields_a_model_names() -> None:
    """Allowlists, never the username or protected fields; nothing by default."""
    assert User.editable_fields() == {"email", "first_name", "last_name"}
    assert User.admin_fields() == {
        "email",
        "email_verified",
        "first_name",
        "last_name",
        "date_joined",
    }
    assert Badge.admin_fields() == {"nickname", "issued"}
    assert Unlisted.editable_fields() == Unlisted.admin_fields() == frozenset()
    assert Overreaching.editable_fields() == {"nickname"}

    badge = Badge(id=UserId(uuid4()), password=HASH, code="B-7")
    for given in ({"risk_flag": True}, {"issued": "2026"}):
        with pytest.raises(UnknownUserField):
            badge.update(**given)
    assert (badge.risk_flag, badge.issued) == (False, "")


def test_users_update_their_editable_fields() -> None:
    """Only the named fields outside ``PROTECTED_FIELDS`` and the username change."""
    user = new_user(email="a@example.com")

    user.update(email="Alice@EXAMPLE.com", first_name="Al@EXAMPLE", last_name="")
    assert (user.email, user.first_name) == ("Alice@example.com", "Al@EXAMPLE")
    user.update(email="Alice@example.com")
    with pytest.raises(UnknownUserField) as refused:
        user.update(username="bob", is_staff=True, last_name="B")
    assert str(refused.value) == (
        "These user fields cannot be changed here: is_staff, username."
    )
    assert user.last_name == ""
    assert facts(user.pull_events()) == [
        ("UserUpdated", {"user_id": user.id, "fields": ("email", "first_name")})
    ]

    badge = Badge(id=UserId(uuid4()), password=HASH, code="B-7")
    assert Badge.editable_fields() == {"nickname"}
    badge.update(nickname=" Seven ")
    assert badge.nickname == " Seven "
    assert facts(badge.pull_events()) == [
        ("UserUpdated", {"user_id": badge.id, "fields": ("nickname",)})
    ]


def test_deleting_records_who_went() -> None:
    """Users and groups record their deletion; repositories remove them."""
    user = new_user()
    group = Group.create("crew")
    group.pull_events()

    user.delete()
    group.delete()

    assert facts(user.pull_events()) == [
        ("UserDeleted", {"user_id": user.id, "username": "alice"})
    ]
    assert facts(group.pull_events()) == [
        ("GroupDeleted", {"group_id": group.id, "name": "crew"})
    ]
