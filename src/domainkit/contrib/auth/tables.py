"""Tables of users, groups, and permissions, mapped onto the domain model.

A custom user (``AUTH.USER_MODEL``) needs its own table, named in
``AUTH.USER_TABLE``: start it with ``abstract_user_columns()`` (or
``abstract_base_user_columns()``) and add the model's own fields::

    users = Table(
        "accounts_users",
        metadata,
        *abstract_user_columns(),
        Column("phone", String(32), nullable=False, default=""),
        info={"domain": "accounts"},
    )

Its links to groups and permissions get tables of their own, named after
it (``accounts_users_groups``, ``accounts_users_permissions``).
"""

from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    Table,
    Text,
    UniqueConstraint,
    Uuid,
    false,
)
from sqlalchemy.engine import Dialect
from sqlalchemy.orm import relationship
from sqlalchemy.types import TypeDecorator

from domainkit.contrib.contenttypes import ContentType
from domainkit.contrib.contenttypes.tables import content_types
from domainkit.contrib.contenttypes.tables import start_mappers as start_content_types
from domainkit.db import UTCDateTime, ValueObjectType, map_imperatively, metadata

from .exceptions import GroupNameTaken, UsernameTaken
from .models import (
    EMAIL_MAX_LENGTH,
    GROUP_NAME_MAX_LENGTH,
    USERNAME_MAX_LENGTH,
    AbstractBaseUser,
    Group,
    PasswordHash,
    Permission,
)
from .tokens import AuthToken


class PasswordHashType(ValueObjectType[PasswordHash]):
    """Store a ``PasswordHash`` as text.

    A single-field value object's column, so fixtures write the hash as the
    text it is stored as, and read it back through ``PasswordHash``, which
    refuses anything that is not a hash.
    """

    cache_ok = True

    def __init__(self, length: int = 255) -> None:
        """Hold hashes of up to ``length`` characters."""
        super().__init__(PasswordHash, String(length))
        self.length = length

    def __repr__(self) -> str:
        """``PasswordHashType(length=255)``, as migrations have it."""
        return f"PasswordHashType(length={self.length})"

    def loaded(self, value: object) -> PasswordHash:
        """A fixture's hash, if ``AUTH.PASSWORD_HASHERS`` would make it so.

        A hash no configured hasher identifies (a password kept in clear,
        ``plaintext$...``), or one it would make again more strongly (the
        test hasher's single iteration, under production's), is refused:
        either is a password a copy of the table gives up. An unusable
        password is kept (SEC-12a-3).

        Raises:
            ValueError: Not a hash at all.
            FixtureError: No configured hasher would make it.
        """
        from domainkit.db.fixtures import FixtureError

        from .hashers import identify_hasher

        hashed = PasswordHash(value)  # type: ignore[arg-type]
        if not hashed.usable:
            return hashed
        try:
            weaker = identify_hasher(hashed.value).must_update(hashed.value)
        except ValueError:
            raise FixtureError("no hasher of AUTH.PASSWORD_HASHERS made it") from None
        if weaker:
            raise FixtureError(
                "AUTH.PASSWORD_HASHERS would make it again, more strongly"
            )
        return hashed


class ScopesType(TypeDecorator[frozenset[str]]):
    """Store a token's scopes as permission keys separated by spaces."""

    impl = Text
    cache_ok = True

    def process_bind_param(
        self, value: frozenset[str] | None, dialect: Dialect
    ) -> str | None:
        """Join the keys, sorted; None (no limit) stays None."""
        return None if value is None else " ".join(sorted(value))

    def process_result_value(
        self, value: str | None, dialect: Dialect
    ) -> frozenset[str] | None:
        """Split the stored keys."""
        return None if value is None else frozenset(value.split())


def abstract_base_user_columns() -> list[Column[Any]]:
    """New columns for the fields of ``AbstractBaseUser``."""
    return [
        Column("id", Uuid, primary_key=True),
        Column("password", PasswordHashType, nullable=False),
        Column("last_login", UTCDateTime),
        Column("is_active", Boolean, nullable=False),
        Column("is_superuser", Boolean, nullable=False),
        # Optimistic locking: every UPDATE checks and bumps it.
        Column("version", Integer, nullable=False),
    ]


def abstract_user_columns() -> list[Column[Any]]:
    """New columns for the fields of ``AbstractUser``."""
    return [
        *abstract_base_user_columns(),
        Column(
            "username",
            String(USERNAME_MAX_LENGTH),
            nullable=False,
            unique=True,
            info={"duplicate": UsernameTaken},
        ),
        Column("email", String(254), nullable=False),
        # Existing rows start unverified when the column is added.
        Column("email_verified", Boolean, nullable=False, server_default=false()),
        Column("first_name", String(150), nullable=False),
        Column("last_name", String(150), nullable=False),
        Column("is_staff", Boolean, nullable=False),
        Column("date_joined", UTCDateTime, nullable=False),
    ]


users = Table(
    "domainkit_users",
    metadata,
    *abstract_user_columns(),
    info={"domain": "auth", "guarded": True},
)
"""The default user's table; unused while ``AUTH.USER_TABLE`` names another."""

groups = Table(
    "domainkit_groups",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column(
        "name",
        String(GROUP_NAME_MAX_LENGTH),
        nullable=False,
        unique=True,
        info={"duplicate": GroupNameTaken},
    ),
    Column("version", Integer, nullable=False),
    info={"domain": "auth", "guarded": True},
)

permissions = Table(
    "domainkit_permissions",
    metadata,
    Column("id", Integer, primary_key=True),
    Column(
        "content_type_id",
        Integer,
        ForeignKey(content_types.c.id, ondelete="CASCADE"),
        nullable=False,
    ),
    Column("codename", String(100), nullable=False),
    Column("name", String(255), nullable=False),
    UniqueConstraint("content_type_id", "codename"),
    info={"domain": "auth", "guarded": True},
)

group_permissions = Table(
    "domainkit_group_permissions",
    metadata,
    Column(
        "group_id",
        Uuid,
        ForeignKey(groups.c.id, ondelete="CASCADE"),
        primary_key=True,
    ),
    Column(
        "permission_id",
        Integer,
        ForeignKey(permissions.c.id, ondelete="CASCADE"),
        primary_key=True,
    ),
    info={"domain": "auth", "guarded": True},
)


tokens = Table(
    "domainkit_tokens",
    metadata,
    # The SHA-256 of the token: the table never holds a working token.
    Column("id", String(64), primary_key=True),
    # No foreign key, so a swapped user model keeps this table: tokens of a
    # deleted user name no one and fail.
    Column("user_id", Uuid, nullable=False),
    Column("kind", String(16), nullable=False),
    Column("name", String(100), nullable=False),
    Column("family", Uuid),
    Column("created_at", UTCDateTime, nullable=False),
    Column("expires_at", UTCDateTime),
    Column("used_at", UTCDateTime),
    # Permission keys the token is limited to; NULL: every one of its user's.
    Column("scopes", ScopesType),
    Column("last_used_at", UTCDateTime),
    Index("ix_domainkit_tokens_user_id", "user_id"),
    Index("ix_domainkit_tokens_family", "family"),
    Index("ix_domainkit_tokens_expires_at", "expires_at"),
    info={"domain": "auth", "guarded": True},
)
"""Opaque access tokens and refresh tokens, by digest."""

mfa_devices = Table(
    "domainkit_mfa_devices",
    metadata,
    Column("id", Uuid, primary_key=True),
    # One authenticator per user; no foreign key, as for tokens.
    Column("user_id", Uuid, nullable=False, unique=True),
    # Encrypted (domainkit.encryption.SecretBox), bound to the user's id.
    Column("secret", LargeBinary(256), nullable=False),
    Column("created_at", UTCDateTime, nullable=False),
    Column("confirmed_at", UTCDateTime),
    # The step of the last code accepted: no code is accepted twice.
    Column("last_step", BigInteger),
    # Secrets: never in fixtures (dumpdata, loaddata).
    info={"domain": "auth", "guarded": True, "fixtures": False},
)
"""TOTP authenticators; unconfirmed until a first code proves the app has it."""

recovery_codes = Table(
    "domainkit_recovery_codes",
    metadata,
    # SHA-256 of the user's id and the code: the table holds no working code.
    Column("id", String(64), primary_key=True),
    Column("user_id", Uuid, nullable=False),
    Column("used_at", UTCDateTime),
    Index("ix_domainkit_recovery_codes_user_id", "user_id"),
    info={"domain": "auth", "guarded": True, "fixtures": False},
)
"""Single-use codes that stand in for a lost authenticator."""

pending_signups = Table(
    "domainkit_pending_signups",
    metadata,
    # Random: the signup link carries it signed, so the id alone opens nothing.
    Column("id", Uuid, primary_key=True),
    # Neither unique: a pending signup reserves no name and no address.
    Column("username", String(USERNAME_MAX_LENGTH), nullable=False),
    Column("email", String(EMAIL_MAX_LENGTH), nullable=False),
    # No password: whoever reads the mail chooses it, when confirming.
    Column("created_at", UTCDateTime, nullable=False),
    Column("expires_at", UTCDateTime, nullable=False),
    Index("ix_domainkit_pending_signups_expires_at", "expires_at"),
    # Addresses of people who may never have asked: never in fixtures.
    info={"domain": "auth", "fixtures": False},
)
"""Accounts asked for at ``/signup``, made once their address is confirmed."""


def user_link_tables(user_table: Table) -> tuple[Table, Table]:
    """The tables linking ``user_table`` to groups and to permissions.

    They are named after it (``<users>_groups``, ``<users>_permissions``),
    belong to its domain (``info["domain"]``) and metadata, and are created
    once. The three are guarded (``info["guarded"]``): ``loaddata`` and
    ``flush`` leave a user table and its grants alone unless told.
    """
    user_table.info["guarded"] = True
    groups_name = f"{user_table.name}_groups"
    permissions_name = f"{user_table.name}_permissions"
    tables = user_table.metadata.tables
    if groups_name in tables:
        return tables[groups_name], tables[permissions_name]
    info = {"domain": user_table.info["domain"], "guarded": True}
    # Each column takes its type from the column its foreign key names.
    user_groups = Table(
        groups_name,
        user_table.metadata,
        Column(
            "user_id",
            ForeignKey(user_table.c.id, ondelete="CASCADE"),
            primary_key=True,
        ),
        Column(
            "group_id",
            ForeignKey(groups.c.id, ondelete="CASCADE"),
            primary_key=True,
        ),
        info=info,
    )
    user_permissions = Table(
        permissions_name,
        user_table.metadata,
        Column(
            "user_id",
            ForeignKey(user_table.c.id, ondelete="CASCADE"),
            primary_key=True,
        ),
        Column(
            "permission_id",
            ForeignKey(permissions.c.id, ondelete="CASCADE"),
            primary_key=True,
        ),
        info=info,
    )
    return user_groups, user_permissions


DEFAULT_USER_TABLES = (users, *user_link_tables(users))
"""The tables the default user needs; a swapped user leaves them unused."""


def start_mappers(user_model: type[AbstractBaseUser], user_table: Table) -> None:
    """Map permissions, groups, and the user model (idempotent).

    Groups and permissions load with their user in the same session
    (``selectin``), so permission checks never query lazily.
    """
    start_content_types()
    map_imperatively(
        Permission,
        permissions,
        properties={
            "content_type": relationship(ContentType, lazy="joined", innerjoin=True)
        },
    )
    map_imperatively(
        Group,
        groups,
        properties={
            "permissions": relationship(
                Permission,
                secondary=group_permissions,
                lazy="selectin",
                collection_class=set,
            )
        },
    )
    map_imperatively(AuthToken, tokens)
    user_groups, user_permissions = user_link_tables(user_table)
    map_imperatively(
        user_model,
        user_table,
        properties={
            "groups": relationship(
                Group, secondary=user_groups, lazy="selectin", collection_class=set
            ),
            "user_permissions": relationship(
                Permission,
                secondary=user_permissions,
                lazy="selectin",
                collection_class=set,
            ),
        },
    )
