"""Auth with a database: migrations, backends, repositories, and settings."""

import asyncio
import io
import re
import sys
from collections.abc import Coroutine, Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import (
    Boolean,
    Column,
    Integer,
    MetaData,
    String,
    Table,
    Uuid,
    insert,
    inspect,
    select,
)

from domainkit import setup
from domainkit.apps import registry
from domainkit.conf import override_settings
from domainkit.contrib.auth import (
    AbstractBaseUser,
    AnonymousUser,
    AnyUser,
    AuthUnitOfWork,
    BaseBackend,
    GroupNameTaken,
    InvalidPassword,
    InvalidUsername,
    ModelBackend,
    PasswordHash,
    UnknownPermission,
    UserId,
    UsernameTaken,
    authenticate,
    get_all_permissions,
    get_user,
    get_user_model,
    has_perm,
    has_perms,
    make_password,
)
from domainkit.contrib.auth import permissions as permissions_module
from domainkit.contrib.auth import users as users_module
from domainkit.contrib.auth.apps import AuthConfig
from domainkit.contrib.auth.backends import load_backends
from domainkit.contrib.auth.permissions import declared_permissions, sync_permissions
from domainkit.contrib.auth.tables import abstract_user_columns, permissions, users
from domainkit.contrib.auth.users import get_user_table
from domainkit.contrib.contenttypes import InstalledModel, ModelOptions
from domainkit.contrib.contenttypes.tables import content_types
from domainkit.db import connections, metadata
from domainkit.db.migrations import include_object, run_migrated_hooks
from domainkit.ddd import PermissionDenied
from domainkit.exceptions import ImproperlyConfigured
from domainkit.http.ratelimit import MemoryBackend
from domainkit.management import call_command
from domainkit_tests.conftest import (
    ALPHA,
    AUTH,
    CONTENTTYPES,
    FAST_HASHER,
    SHOP,
    UseAuth,
)
from domainkit_tests.sample_domains.accounts.models import Account
from domainkit_tests.sample_domains.members.models import Member
from domainkit_tests.sample_domains.shop.models import Product, ProductId
from domainkit_tests.test_auth_hashers import TwoRoundsHasher

__all__ = ["Account", "TwoRoundsHasher"]
"""Named in settings by these tests."""

HERE = "domainkit_tests.test_auth"
PASSWORD = "vy3!Tq-pX9#mR2"


class OwnerBackend(BaseBackend):
    """Object permissions: owners may change their own products."""

    async def get_all_permissions(
        self, user: AnyUser, obj: object | None = None
    ) -> frozenset[str]:
        """``shop.change_product`` on products the user owns."""
        if isinstance(obj, Product) and obj.name == user.get_username():
            return frozenset({"shop.change_product"})
        return frozenset()


class DenyBackend(BaseBackend):
    """Ends every search with "no"."""

    async def authenticate(self, credentials: object) -> None:
        """Refuse everyone."""
        raise PermissionDenied("closed")

    async def has_perm(
        self, user: AnyUser, perm: str, obj: object | None = None
    ) -> bool:
        """Refuse everything."""
        raise PermissionDenied("closed")


class ReaderBackend(BaseBackend):
    """Everyone may view products."""

    async def get_all_permissions(
        self, user: AnyUser, obj: object | None = None
    ) -> frozenset[str]:
        """``shop.view_product``, always."""
        return frozenset({"shop.view_product"})


NoUsernameField = type("NoUsernameField", (AbstractBaseUser,), {})


class EditsMissing(Account):
    """Lets staff edit a field it does not have."""

    EDITABLE_FIELDS = frozenset({"phone", "fax", "pager"})


class ShowsMissing(Account):
    """Shows staff a field it does not have."""

    READ_ONLY_FIELDS = frozenset({"date_joined", "badge"})


class Unlocking(Account):
    """Lets staff edit the username and a protected field."""

    EDITABLE_FIELDS = Account.EDITABLE_FIELDS | {"username", "is_superuser"}


class Narrowing(Account):
    """Protects nothing of its own, and lets staff edit status flags."""

    PROTECTED_FIELDS = frozenset()
    EDITABLE_FIELDS = Account.EDITABLE_FIELDS | {"is_superuser", "email_verified"}


not_a_table = object()
detached = Table("detached_users", MetaData(), Column("id", Uuid, primary_key=True))
narrow = Table(
    "narrow_users",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column("version", Integer, nullable=False),
    info={"domain": "narrow"},
)
ghost = Table(
    "ghost_users",
    metadata,
    *abstract_user_columns(),
    Column("phone", String(32), nullable=False),
    Column("risk_flag", Boolean, nullable=False),
    info={"domain": "ghost"},
)


def run[T](coroutine: Coroutine[Any, Any, T]) -> T:
    """Run ``coroutine`` and close its connections."""

    async def wrapped() -> T:
        try:
            return await coroutine
        finally:
            await connections.close_all()

    return asyncio.run(wrapped())


async def create_user(
    username: str = "alice",
    password: str | None = PASSWORD,
    **fields: Any,  # noqa: ANN401 - user fields.
) -> AbstractBaseUser:
    """Store a user."""
    async with AuthUnitOfWork() as uow:
        user = await uow.users.create_user(username, password, **fields)
        await uow.commit()
    return user


async def permission_keys() -> set[str]:
    """Every stored permission's key."""
    async with AuthUnitOfWork() as uow:
        return set(await uow.permissions.keys())


def test_migrate_creates_content_types_and_permissions(auth_project: str) -> None:
    """Default and declared permissions, once, however often ``migrate`` runs."""
    keys = run(permission_keys())

    assert {
        "auth.add_user",
        "auth.view_group",
        "auth.change_permission",
        "contenttypes.view_contenttype",
        "shop.add_product",
        "shop.publish_product",
        "shop.change_stocklevel",
        "shop.view_stocklevel",
    } <= keys
    assert "shop.add_stocklevel" not in keys

    call_command("migrate", stdout=io.StringIO())
    assert run(permission_keys()) == keys

    async def names() -> dict[str, str]:
        async with connections.engine().connect() as connection:
            rows = await connection.execute(
                select(permissions.c.codename, permissions.c.name)
            )
            return dict(rows.all())

    stored = run(names())
    assert stored["add_product"] == "Can add product"
    assert stored["change_stocklevel"] == "Can change stock"


def test_authenticate(auth_project: str) -> None:
    """Right passwords pass; wrong ones, unknown users, and inactive users fail."""
    run(create_user())
    run(create_user("bob", is_active=False))

    user = run(authenticate(username="alice", password=PASSWORD))
    assert user is not None
    assert user.get_username() == "alice"
    assert run(authenticate(username="alice", password="wrong")) is None
    assert run(authenticate(username="nobody", password=PASSWORD)) is None
    assert run(authenticate(username="bob", password=PASSWORD)) is None
    assert run(authenticate(username="alice")) is None
    assert run(authenticate(username=7, password=PASSWORD)) is None


def test_logins_upgrade_password_hashes(auth_project: str) -> None:
    """A login under newer hasher settings stores the newer hash."""
    run(create_user())
    with override_settings(
        AUTH={"PASSWORD_HASHERS": [f"{HERE}.TwoRoundsHasher", FAST_HASHER]}
    ):
        assert run(authenticate(username="alice", password=PASSWORD)) is not None

        async def stored() -> str:
            async with AuthUnitOfWork() as uow:
                user = await uow.users.get_by_natural_key("alice")
                assert user is not None
                return user.password.value

        assert run(stored()).startswith("pbkdf2_sha256$2$")


def test_permissions_through_backends(auth_project: str) -> None:
    """Grants, groups, superusers, inactive users, and anonymous ones."""

    async def arrange() -> tuple[AbstractBaseUser, AbstractBaseUser]:
        async with AuthUnitOfWork() as uow:
            alice = await uow.users.create_user("alice", PASSWORD)
            root = await uow.users.create_superuser("root", PASSWORD)
            editors = await uow.groups.create("editors")
            editors.grant(await uow.permissions.get("shop.publish_product"))
            alice.grant(await uow.permissions.get("shop.add_product"))
            alice.join(editors)
            await uow.commit()
        return alice, root

    alice, root = run(arrange())
    loaded = run(get_user(alice.id))
    assert loaded is not None
    assert run(has_perm(loaded, "shop.publish_product"))
    assert run(has_perm(loaded, "shop.add_product"))
    assert not run(has_perm(loaded, "shop.delete_product"))
    assert run(has_perms(loaded, ["shop.add_product", "shop.publish_product"]))
    assert not run(has_perms(loaded, ["shop.add_product", "shop.delete_product"]))
    assert run(get_all_permissions(loaded)) == {
        "shop.add_product",
        "shop.publish_product",
    }
    assert run(has_perm(root, "shop.delete_product"))
    assert run(get_all_permissions(root)) == run(permission_keys())
    assert not run(has_perm(AnonymousUser(), "shop.add_product"))
    assert run(get_all_permissions(AnonymousUser())) == frozenset()

    loaded.set_active(False)
    assert not run(has_perm(loaded, "shop.add_product"))
    assert run(get_all_permissions(loaded)) == frozenset()
    assert run(get_user(uuid4())) is None


def test_inactive_users_are_not_loaded(auth_project: str) -> None:
    """``get_user`` vouches only for active users."""
    bob = run(create_user("bob", is_active=False))
    assert run(get_user(bob.id)) is None


def test_object_permissions_come_from_other_backends(auth_project: str) -> None:
    """``ModelBackend`` ignores objects; a project backend decides."""
    alice = run(create_user())
    own = Product(id=ProductId(uuid4()), name="alice")
    other = Product(id=ProductId(uuid4()), name="bob")
    backends = [f"{AUTH}.backends.ModelBackend", f"{HERE}.OwnerBackend"]
    with override_settings(
        AUTH={"PASSWORD_HASHERS": [FAST_HASHER], "BACKENDS": backends}
    ):
        assert run(has_perm(alice, "shop.change_product", own))
        assert run(has_perms(alice, ["shop.change_product"], own))
        assert not run(has_perm(alice, "shop.change_product", other))
        assert not run(has_perm(alice, "shop.change_product"))
        assert run(get_all_permissions(alice, own)) == {"shop.change_product"}
    root = run(create_user("root", is_superuser=True))
    assert run(ModelBackend().get_all_permissions(root, own)) == frozenset()


def test_backends_add_up_permissions(auth_project: str) -> None:
    """Each backend's permissions join the others'."""

    async def arrange() -> AbstractBaseUser:
        async with AuthUnitOfWork() as uow:
            alice = await uow.users.create_user("alice", PASSWORD)
            alice.grant(await uow.permissions.get("shop.add_product"))
            await uow.commit()
        return alice

    alice = run(arrange())
    backends = [f"{AUTH}.backends.ModelBackend", f"{HERE}.ReaderBackend"]
    with override_settings(
        AUTH={"PASSWORD_HASHERS": [FAST_HASHER], "BACKENDS": backends}
    ):
        assert run(get_all_permissions(alice)) == {
            "shop.add_product",
            "shop.view_product",
        }


def test_permission_denied_ends_the_search(auth_project: str) -> None:
    """A backend that raises ``PermissionDenied`` answers "no" for all."""
    alice = run(create_user())
    backends = [f"{HERE}.DenyBackend", f"{AUTH}.backends.ModelBackend"]
    with override_settings(
        AUTH={"PASSWORD_HASHERS": [FAST_HASHER], "BACKENDS": backends}
    ):
        assert run(authenticate(username="alice", password=PASSWORD)) is None
        alice.set_superuser(False)
        assert not run(has_perm(alice, "shop.add_product"))
        assert run(get_user(alice.id)) is not None


def test_base_backend_knows_nobody() -> None:
    """Every hook of ``BaseBackend`` says no."""
    backend = BaseBackend()

    assert run(backend.authenticate({"username": "x"})) is None
    assert run(backend.get_user(uuid4())) is None
    assert not run(backend.has_perm(AnonymousUser(), "x.y"))


def test_racing_signups_get_username_taken(auth_project: str) -> None:
    """When both checks pass before either commits, the database refuses one."""

    async def scenario() -> UsernameTaken:
        async with AuthUnitOfWork() as first, AuthUnitOfWork() as second:
            await first.users.create_user("ada")
            await second.users.create_user("ada")
            await first.commit()
            with pytest.raises(UsernameTaken) as taken:
                await second.commit()
        return taken.value

    taken = run(scenario())

    assert taken.code == "username_taken"
    assert taken.message == "The username is already taken."
    assert (taken.constraint, taken.fields) == (
        "uq_domainkit_users_username",
        ("username",),
    )


def test_racing_groups_get_group_name_taken(auth_project: str) -> None:
    """Group names are refused the same way."""

    async def scenario() -> GroupNameTaken:
        async with AuthUnitOfWork() as first, AuthUnitOfWork() as second:
            await first.groups.create("editors")
            await second.groups.create("editors")
            await first.commit()
            with pytest.raises(GroupNameTaken) as taken:
                await second.commit()
        return taken.value

    assert run(scenario()).code == "group_name_taken"


def test_repositories(auth_project: str) -> None:
    """Taken names, superusers, unusable passwords, and unknown permissions."""

    async def scenario() -> None:
        async with AuthUnitOfWork() as uow:
            root = await uow.users.create_superuser("root", PASSWORD)
            assert (root.is_superuser, getattr(root, "is_staff", None)) == (True, True)
            nobody = await uow.users.create_user("nopass")
            assert not nobody.has_usable_password()
            await uow.users.set_password(nobody, PASSWORD)
            assert nobody.has_usable_password()
            await uow.users.set_password(nobody, None)
            assert not nobody.has_usable_password()
            await uow.groups.create("editors")
            await uow.commit()
        async with AuthUnitOfWork() as uow:
            with pytest.raises(UsernameTaken) as taken:
                await uow.users.create_user("ROOT".lower(), PASSWORD)
            assert taken.value.message == "The username 'root' is taken."
            with pytest.raises(GroupNameTaken) as exists:
                await uow.groups.create(" editors ")
            assert exists.value.message == "A group called 'editors' exists."
            assert await uow.groups.get_by_name("writers") is None
            group = await uow.groups.get_by_name("editors")
            assert group is not None
            group.rename("writers")
            await uow.commit()
        async with AuthUnitOfWork() as uow:
            assert await uow.groups.get_by_name("writers") is not None
            with pytest.raises(UnknownPermission) as unknown:
                await uow.permissions.get_many(
                    ["shop.add_product", "shop.fly_product", "shop.swim_product"]
                )
            assert unknown.value.message == (
                "No such permission: shop.fly_product, shop.swim_product."
            )
            pair = await uow.permissions.get_many(
                ["shop.view_product", "shop.add_product"]
            )
            assert [permission.key for permission in pair] == [
                "shop.view_product",
                "shop.add_product",
            ]

    run(scenario())


def test_backend_settings_are_checked(use_auth: UseAuth) -> None:
    """Unknown paths and other classes are configuration errors."""
    use_auth()
    with pytest.raises(ImproperlyConfigured, match=r"AUTH\.BACKENDS"):
        load_backends(("nowhere.Backend",))
    with pytest.raises(ImproperlyConfigured, match="is not a BaseBackend"):
        load_backends((f"{HERE}.Product",))
    assert isinstance(
        load_backends((f"{AUTH}.backends.ModelBackend",))[0], ModelBackend
    )


@pytest.mark.parametrize(
    ("auth", "message"),
    [
        ({"USER_MODEL": "nowhere.User"}, "AUTH.USER_MODEL: "),
        (
            {"USER_MODEL": f"{AUTH}.models.Group"},
            (
                f"AUTH.USER_MODEL: '{AUTH}.models.Group' must be a subclass of "
                "AbstractUser or AbstractBaseUser."
            ),
        ),
        (
            {"USER_MODEL": f"{AUTH}.models.AbstractUser"},
            f"AUTH.USER_MODEL: '{AUTH}.models.AbstractUser' must be a subclass",
        ),
        (
            {"USER_MODEL": f"{HERE}.NoUsernameField"},
            f"AUTH.USER_MODEL: '{HERE}.NoUsernameField' sets no USERNAME_FIELD.",
        ),
        (
            {"USER_MODEL": f"{HERE}.EditsMissing"},
            (
                "AUTH.USER_MODEL: EDITABLE_FIELDS names fields "
                f"'{HERE}.EditsMissing' lacks: fax, pager."
            ),
        ),
        (
            {"USER_MODEL": f"{HERE}.ShowsMissing"},
            (
                "AUTH.USER_MODEL: READ_ONLY_FIELDS names fields "
                f"'{HERE}.ShowsMissing' lacks: badge."
            ),
        ),
        (
            {"USER_MODEL": f"{HERE}.Unlocking"},
            (
                f"AUTH.USER_MODEL: EDITABLE_FIELDS of '{HERE}.Unlocking' names fields "
                "that change only through their own methods: is_superuser, username."
            ),
        ),
        (
            {"USER_MODEL": f"{HERE}.Narrowing"},
            (
                f"AUTH.USER_MODEL: EDITABLE_FIELDS of '{HERE}.Narrowing' names fields "
                "that change only through their own methods: email_verified, "
                "is_superuser."
            ),
        ),
        (
            {"USER_MODEL": f"{HERE}.Account"},
            "AUTH.USER_TABLE must name the table of a custom AUTH.USER_MODEL.",
        ),
        (
            {"USER_MODEL": f"{HERE}.Account", "USER_TABLE": "nowhere.users"},
            "AUTH.USER_TABLE: ",
        ),
        (
            {"USER_MODEL": f"{HERE}.Account", "USER_TABLE": f"{HERE}.not_a_table"},
            (
                f"AUTH.USER_TABLE: '{HERE}.not_a_table' must be a Table on "
                "domainkit.db.metadata."
            ),
        ),
        (
            {"USER_MODEL": f"{HERE}.Account", "USER_TABLE": f"{HERE}.detached"},
            f"AUTH.USER_TABLE: '{HERE}.detached' must be a Table",
        ),
        (
            {"USER_MODEL": f"{HERE}.Account", "USER_TABLE": f"{HERE}.narrow"},
            "AUTH.USER_TABLE 'narrow_users' has no column for",
        ),
        (
            {"USER_MODEL": f"{HERE}.Account", "USER_TABLE": f"{HERE}.ghost"},
            (
                "AUTH.USER_TABLE 'ghost_users' needs info={'domain': ...} naming "
                "the installed domain that defines it."
            ),
        ),
    ],
)
def test_user_model_settings_are_checked(
    use_auth: UseAuth, auth: dict[str, str], message: str
) -> None:
    """A swapped user needs a concrete class and a table that fits it."""
    with pytest.raises(ImproperlyConfigured) as caught:
        use_auth(**auth)
    assert str(caught.value).startswith(message)


def test_user_tables_name_their_domain(
    use_auth: UseAuth, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Migrations and routing follow the domain, so untagged tables are refused."""
    untagged = Table(
        "untagged_users",
        metadata,
        *abstract_user_columns(),
        Column("phone", String(32), nullable=False),
        Column("risk_flag", Boolean, nullable=False),
    )
    monkeypatch.setattr(sys.modules[__name__], "untagged", untagged, raising=False)
    try:
        with pytest.raises(ImproperlyConfigured, match="needs info="):
            use_auth(USER_MODEL=f"{HERE}.Account", USER_TABLE=f"{HERE}.untagged")
    finally:
        metadata.remove(untagged)


def test_auth_needs_content_types() -> None:
    """Permissions hang off content types."""
    registry.clear()
    with (
        override_settings(INSTALLED_DOMAINS=[AUTH, ALPHA]),
        pytest.raises(ImproperlyConfigured, match="contenttypes"),
    ):
        setup()
    registry.clear()


@pytest.mark.parametrize(
    ("routing", "where"),
    [
        (
            {"auth": "other"},
            "auth in 'other', contenttypes in 'default', domainkit_users in 'other'",
        ),
        (
            {"contenttypes": "other"},
            (
                "auth in 'default', contenttypes in 'other', "
                "domainkit_users in 'default'"
            ),
        ),
        (
            {"accounts": "other"},
            ("auth in 'default', contenttypes in 'default', accounts_users in 'other'"),
        ),
    ],
)
def test_auth_tables_share_one_database(
    use_auth: UseAuth, tmp_path: Path, routing: dict[str, str], where: str
) -> None:
    """Foreign keys join users, groups, permissions, and content types."""
    databases = {
        "default": {"URL": f"sqlite+aiosqlite:///{tmp_path / 'a.sqlite3'}"},
        "other": {"URL": f"sqlite+aiosqlite:///{tmp_path / 'b.sqlite3'}"},
    }
    account = {}
    if "accounts" in routing:
        account = {
            "USER_MODEL": "domainkit_tests.sample_domains.accounts.models.Account",
            "USER_TABLE": "domainkit_tests.sample_domains.accounts.tables.accounts",
        }
    message = (
        "Users, groups, permissions, and content types need one database "
        f"(foreign keys join them): {where}."
    )
    with pytest.raises(ImproperlyConfigured, match=re.escape(message)):
        use_auth(
            settings={
                "DATABASES": databases,
                "DATABASE_ROUTING": routing,
                "INSTALLED_DOMAINS": [
                    CONTENTTYPES,
                    AUTH,
                    "domainkit_tests.sample_domains.accounts",
                    ALPHA,
                ],
            },
            **account,
        )


def test_deployments_refuse_test_hashers(use_auth: UseAuth) -> None:
    """The fast hasher is for tests only."""
    production = {"ENVIRONMENT": "production", "ALLOWED_HOSTS": ["api.example.com"]}
    with pytest.raises(ImproperlyConfigured, match="are for tests"):
        use_auth(settings=production)
    use_auth(
        PASSWORD_HASHERS=[f"{AUTH}.hashers.PBKDF2PasswordHasher"], settings=production
    )


@pytest.mark.parametrize("environment", ["staging", "production"])
def test_deployments_refuse_links_over_plain_http(
    use_auth: UseAuth, environment: str
) -> None:
    """Reset, verification, and signup links carry tokens: https only."""
    deployed = {"ENVIRONMENT": environment, "ALLOWED_HOSTS": ["api.example.com"]}
    hashers = [f"{AUTH}.hashers.PBKDF2PasswordHasher"]
    plain = "HTTP://app.example.com/link?token={token}"

    with pytest.raises(ImproperlyConfigured) as refused:
        use_auth(
            settings=deployed,
            PASSWORD_HASHERS=hashers,
            PASSWORD_RESET_URL=plain,
            SIGNUP_URL=plain,
        )
    for key in ("EMAIL_VERIFICATION_URL", "SIGNUP_URL"):
        with pytest.raises(ImproperlyConfigured, match=rf"AUTH\.{key} send"):
            use_auth(settings=deployed, PASSWORD_HASHERS=hashers, **{key: plain})
    secure = "https://app.example.com/link?token={token}"
    use_auth(
        settings=deployed,
        PASSWORD_HASHERS=hashers,
        PASSWORD_RESET_URL=secure,
        EMAIL_VERIFICATION_URL=secure,
        SIGNUP_URL=secure,
    )
    use_auth(settings={"ENVIRONMENT": "test"}, PASSWORD_RESET_URL=plain)

    assert str(refused.value) == (
        "AUTH.PASSWORD_RESET_URL, AUTH.SIGNUP_URL send links with their tokens "
        f"over plain HTTP; use https:// in {environment}."
    )


def test_deployments_warn_of_guesses_without_limit(use_auth: UseAuth) -> None:
    """``LOGIN_RATE`` and ``MFA.RATE`` set to None are warnings there."""
    deployed = {
        "ENVIRONMENT": "production",
        "ALLOWED_HOSTS": ["api.example.com"],
        "RATE_LIMIT_BACKEND": "domainkit.http.ratelimit.RedisBackend",
    }
    hashers = [f"{AUTH}.hashers.PBKDF2PasswordHasher"]

    use_auth(
        settings=deployed, PASSWORD_HASHERS=hashers, LOGIN_RATE=None, MFA={"RATE": None}
    )
    unlimited = registry.get_domain_config("auth").deployment_warnings()
    use_auth(settings=deployed, PASSWORD_HASHERS=hashers)
    limited = registry.get_domain_config("auth").deployment_warnings()

    assert unlimited == [
        (
            "AUTH.LOGIN_RATE is None, so passwords can be guessed without limit; "
            'set it, such as "10/minute".'
        ),
        (
            "AUTH.MFA.RATE is None, so one-time codes can be guessed without "
            'limit; set it, such as "10/hour".'
        ),
    ]
    assert limited == []


def test_deployments_warn_of_attempts_counted_per_process(use_auth: UseAuth) -> None:
    """In memory, each process allows the attempt rates on its own (AUTH-09)."""
    deployed = {"ENVIRONMENT": "production", "ALLOWED_HOSTS": ["api.example.com"]}
    hashers = [f"{AUTH}.hashers.PBKDF2PasswordHasher"]
    no_rates = {"LOGIN_RATE": None, "EMAIL_RATE": None}
    no_codes = {"RATE": None, "ADDRESS_RATE": None}

    def warnings(backend: str | None = None, **auth: Any) -> list[str]:  # noqa: ANN401
        chosen = {} if backend is None else {"RATE_LIMIT_BACKEND": backend}
        use_auth(settings={**deployed, **chosen}, PASSWORD_HASHERS=hashers, **auth)
        found: list[str] = registry.get_domain_config("auth").deployment_warnings()
        return [warning for warning in found if "RATE_LIMIT_BACKEND" in warning]

    assert warnings() == [
        (
            "RATE_LIMIT_BACKEND domainkit.http.ratelimit.MemoryBackend counts in "
            "each process, so with several processes or replicas each allows "
            "AUTH.LOGIN_RATE, AUTH.MFA.RATE, and the other attempt rates on its "
            "own: passwords and codes can be guessed that many times faster. Set "
            "RATE_LIMIT_BACKEND to domainkit.http.ratelimit.RedisBackend, shared "
            "by every process."
        )
    ]
    assert len(warnings(f"{__name__}.CountsInProcess")) == 1  # A subclass too.
    assert warnings(**no_rates, MFA={"RATE": None}) != []  # ADDRESS_RATE is one.
    assert warnings(**no_rates, MFA=no_codes) == []
    assert warnings("domainkit.http.ratelimit.RedisBackend") == []
    assert warnings(f"{__name__}.Elsewhere") == []
    assert warnings("nowhere.Backend") == []  # Loading it says what is wrong.
    use_auth(PASSWORD_HASHERS=hashers)
    assert registry.get_domain_config("auth").deployment_warnings() == []


class CountsInProcess(MemoryBackend):
    """A memory backend of a project's own."""


class Elsewhere:
    """A backend of a project's own that counts somewhere shared."""


def test_declared_permissions_are_checked(monkeypatch: pytest.MonkeyPatch) -> None:
    """Malformed codenames, long names, and clashing keys stop startup."""

    def models(*declared: tuple[str, tuple[tuple[str, str], ...]]) -> None:
        installed = [
            InstalledModel("shop", Product, ModelOptions(model, model, (), permissions))
            for model, permissions in declared
        ]
        monkeypatch.setattr(permissions_module, "installed_models", lambda: installed)

    models(("product", (("Fly", "Can fly"),)))
    with pytest.raises(ImproperlyConfigured) as malformed:
        declared_permissions()
    assert str(malformed.value) == (
        "Product: permission codename 'Fly' must be lower-case letters, digits, "
        "and underscores (at most 100), starting with a letter."
    )
    for name in ("", "x" * 256):
        models(("product", (("fly", name),)))
        with pytest.raises(ImproperlyConfigured) as unnamed:
            declared_permissions()
        assert str(unnamed.value) == (
            "Product: permission 'fly' needs a name of 1 to 255 characters."
        )
    for name in ("F", "x" * 255):
        models(("product", (("fly", name),)))
        assert [spec.name for spec in declared_permissions()] == [name]
    models(("product", (("fly", "Can fly"),)), ("plane", (("fly", "Can fly"),)))
    with pytest.raises(ImproperlyConfigured, match=r"'shop.fly'.*product and plane"):
        declared_permissions()
    models(("product", (("fly", "Can fly"), ("fly", "Again"))))
    assert [spec.name for spec in declared_permissions()] == ["Can fly"]


def test_migrated_hooks_wait_for_their_tables(
    use_auth: UseAuth, tmp_path: Path
) -> None:
    """Before ``makemigrations``, and on other databases, nothing happens."""
    url = f"sqlite+aiosqlite:///{tmp_path / 'empty.sqlite3'}"
    use_auth(settings={"DATABASES": {"default": {"URL": url}}})
    run_migrated_hooks("default")

    async def other_alias() -> int:
        async with connections.engine().begin() as connection:
            for config in registry:
                await config.migrated(connection, "elsewhere")
            return len(
                await connection.run_sync(lambda sync: inspect(sync).get_table_names())
            )

    assert run(other_alias()) == 0


@pytest.fixture
def accounts(database_url: str, tmp_path: Path) -> Iterator[Path]:
    """A project whose own ``Account`` replaces the default user."""
    registry.clear()
    with override_settings(
        INSTALLED_DOMAINS=[
            CONTENTTYPES,
            AUTH,
            "domainkit_tests.sample_domains.accounts",
            SHOP,
            ALPHA,
        ],
        DATABASES={"default": {"URL": database_url}},
        MIGRATIONS_DIR=tmp_path / "migrations",
        AUTH={
            "PASSWORD_HASHERS": [FAST_HASHER],
            "USER_MODEL": "domainkit_tests.sample_domains.accounts.models.Account",
            "USER_TABLE": "domainkit_tests.sample_domains.accounts.tables.accounts",
        },
    ):
        setup()
        call_command("makemigrations", "-m", "accounts", stdout=io.StringIO())
        call_command("migrate", stdout=io.StringIO())
        yield tmp_path / "migrations"
    registry.clear()


def test_projects_swap_in_their_own_user(accounts: Path) -> None:
    """The custom user gets tables, links, a content type, and logins."""
    revision = next(accounts.glob("*.py")).read_text(encoding="utf-8")
    assert "accounts_users" in revision
    assert "accounts_users_groups" in revision
    assert "domainkit_users" not in revision
    assert get_user_model() is Account
    assert get_user_table().name == "accounts_users"
    config = registry.get_domain_config("auth")
    assert isinstance(config, AuthConfig)
    assert config.swapped
    assert not config.holds_table(users)
    # The default table, left in the database, is never dropped.
    assert not include_object(None, "domainkit_users", "table", True, None)

    async def scenario() -> AbstractBaseUser | None:
        async with AuthUnitOfWork() as uow:
            account = await uow.users.create_user("carol", PASSWORD, phone="+20 100")
            account.grant(await uow.permissions.get("accounts.view_account"))
            await uow.commit()
        return await authenticate(username="carol", password=PASSWORD)

    carol = run(scenario())
    assert isinstance(carol, Account)
    assert carol.phone == "+20 100"
    assert carol.has_perm("accounts.view_account")
    assert "auth.add_user" not in run(permission_keys())


def test_permissions_are_listed_by_domain_model_and_codename(
    auth_project: str,
) -> None:
    """Whatever order the rows were stored in."""

    async def add_late(domain: str, model: str, codename: str) -> None:
        async with connections.engine().begin() as connection:
            content_type_id = await connection.scalar(
                select(content_types.c.id).where(
                    content_types.c.domain == domain, content_types.c.model == model
                )
            ) or await connection.scalar(
                insert(content_types)
                .values(domain=domain, model=model)
                .returning(content_types.c.id)
            )
            await connection.execute(
                insert(permissions).values(
                    content_type_id=content_type_id,
                    codename=codename,
                    name=codename,
                )
            )

    async def listed() -> list[tuple[str, str, str]]:
        async with AuthUnitOfWork() as uow:
            return [
                (p.content_type.domain, p.content_type.model, p.codename)
                for p in await uow.permissions.all()
            ]

    run(add_late("shop", "aardvark", "view_aardvark"))
    run(add_late("aaa", "thing", "view_thing"))
    run(add_late("shop", "product", "archive_product"))
    rows = run(listed())

    assert rows == sorted(rows)
    assert rows[0] == ("aaa", "thing", "view_thing")
    assert len(rows) == len(set(rows)) == len(run(permission_keys()))
    shop = [codename for domain, model, codename in rows if model == "product"]
    assert shop == [
        "add_product",
        "archive_product",
        "change_product",
        "delete_product",
        "publish_product",
        "view_product",
    ]


def test_sync_reports_what_it_created(auth_project: str) -> None:
    """A second sync finds nothing missing."""

    async def again() -> int:
        async with connections.engine().begin() as connection:
            return await sync_permissions(connection)

    assert run(again()) == 0

    async def content_type_count() -> int:
        async with connections.engine().connect() as connection:
            return len((await connection.execute(select(content_types))).all())

    assert run(content_type_count()) >= 6


@pytest.fixture
def members(database_url: str, tmp_path: Path) -> Iterator[None]:
    """A project whose users log in with their email (``AbstractBaseUser``)."""
    registry.clear()
    with override_settings(
        INSTALLED_DOMAINS=[
            CONTENTTYPES,
            AUTH,
            "domainkit_tests.sample_domains.members",
            ALPHA,
        ],
        DATABASES={"default": {"URL": database_url}},
        MIGRATIONS_DIR=tmp_path / "migrations",
        AUTH={
            "PASSWORD_HASHERS": [FAST_HASHER],
            "USER_MODEL": "domainkit_tests.sample_domains.members.models.Member",
            "USER_TABLE": "domainkit_tests.sample_domains.members.tables.members",
        },
    ):
        setup()
        call_command("makemigrations", "-m", "members", stdout=io.StringIO())
        call_command("migrate", stdout=io.StringIO())
        yield
    registry.clear()


def test_users_can_log_in_with_their_email(members: None) -> None:
    """``USERNAME_FIELD = "email"``: no username, and no staff flag."""

    async def scenario() -> AbstractBaseUser | None:
        async with AuthUnitOfWork() as uow:
            boss = await uow.users.create_superuser("boss@example.org", PASSWORD)
            assert boss.is_superuser
            assert not hasattr(boss, "is_staff")
            await uow.commit()
        return await authenticate(email="boss@example.org", password=PASSWORD)

    boss = run(scenario())
    assert isinstance(boss, Member)
    assert str(boss) == "boss@example.org"
    # Clients that always send "username" log in too.
    again = run(authenticate(username="boss@example.org", password=PASSWORD))
    assert again is not None
    assert again.id == boss.id


def test_names_kept_for_erasure_are_refused_for_any_user_model(members: None) -> None:
    """A model without ``validate_username`` cannot take ``erased-`` names either."""

    async def create(name: str) -> None:
        async with AuthUnitOfWork() as uow:
            await uow.users.create_user(name, PASSWORD)
            await uow.commit()

    for name in ("erased-boss@example.org", "ERASED-boss@example.org"):
        with pytest.raises(InvalidUsername, match="kept for erased accounts"):
            run(create(name))
    run(create("unerased-boss@example.org"))


def test_logins_that_rehash_at_once_never_conflict(auth_project: str) -> None:
    """The upgraded hash is stored without the user's version: no login fails."""
    newer = f"{TwoRoundsHasher.__module__}.TwoRoundsHasher"

    async def scenario() -> tuple[list[AbstractBaseUser | None], int, int, str]:
        async with AuthUnitOfWork() as uow:
            created = await uow.users.create_user("alice", PASSWORD)
            await uow.commit()
        with override_settings(AUTH={"PASSWORD_HASHERS": [newer, FAST_HASHER]}):
            logged_in = await asyncio.gather(
                *(authenticate(username="alice", password=PASSWORD) for _ in range(4))
            )
        async with AuthUnitOfWork() as uow:
            stored = await uow.users.get_by_natural_key("alice")
        assert stored is not None
        return logged_in, created.version, stored.version, stored.password.value

    logged_in, before, after, stored_hash = run(scenario())

    assert all(user is not None for user in logged_in)
    assert after == before  # Nobody's changes are refused for a rehash.
    assert stored_hash.startswith("pbkdf2_sha256$2$")


def test_rehashing_skips_users_deleted_meanwhile(auth_project: str) -> None:
    """A user gone before the new hash is stored is left alone."""
    old = PasswordHash(make_password(PASSWORD))
    run(ModelBackend()._rehash(UserId(uuid4()), old, make_password(PASSWORD)))


def test_rehashing_touches_only_the_user_who_logged_in(auth_project: str) -> None:
    """Users sharing a hash (imported from an unsalted one) keep theirs."""
    shared = PasswordHash(make_password(PASSWORD))

    async def stored(username: str) -> AbstractBaseUser:
        async with AuthUnitOfWork() as uow:
            user = await uow.users.get_by_natural_key(username)
        assert user is not None
        return user

    async def scenario() -> list[str]:
        async with AuthUnitOfWork() as uow:
            for name in ("alice", "bob"):
                user = await uow.users.create_user(name)
                user.set_password(shared)
            await uow.commit()
        alice = await stored("alice")
        await ModelBackend()._rehash(alice.id, shared, make_password(PASSWORD))
        return [(await stored(name)).password.value for name in ("alice", "bob")]

    alice, bob = run(scenario())

    assert alice != shared.value
    assert bob == shared.value


def test_rehashing_never_undoes_a_new_password(auth_project: str) -> None:
    """A login's late rehash of the old password leaves a new one in place."""

    async def scenario() -> tuple[AbstractBaseUser | None, AbstractBaseUser | None]:
        async with AuthUnitOfWork() as uow:
            user = await uow.users.create_user("alice", PASSWORD)
            await uow.commit()
        verified = user.password
        async with AuthUnitOfWork() as uow:
            changed = await uow.users.get(user.id)
            changed.set_password(PasswordHash(make_password("a new passphrase")))
            await uow.commit()
        await ModelBackend()._rehash(user.id, verified, make_password(PASSWORD))
        return (
            await authenticate(username="alice", password=PASSWORD),
            await authenticate(username="alice", password="a new passphrase"),
        )

    old, new = run(scenario())

    assert old is None
    assert new is not None


def test_get_user_model_needs_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    """Without the app there is no user model, even with a domain called auth."""
    with pytest.raises(ImproperlyConfigured) as missing:
        get_user_model()
    assert str(missing.value) == (
        "Install 'domainkit.contrib.auth' in INSTALLED_DOMAINS."
    )

    own = SimpleNamespace(get_domain_config=lambda label: SimpleNamespace(label=label))
    monkeypatch.setattr(users_module, "registry", own)
    with pytest.raises(ImproperlyConfigured, match="Install"):
        get_user_model()


def test_invalid_passwords_list_every_rule() -> None:
    """The message joins the broken rules; ``errors`` keeps them apart."""
    error = InvalidPassword(["Too short.", "Too common."])

    assert error.message == "Too short. Too common."
    assert error.errors == ["Too short.", "Too common."]
