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

import hashlib
from collections.abc import Callable
from functools import partial
from typing import Any

__all__ = ["abstract_base_user_columns", "abstract_user_columns"]

from sqlalchemy import (
    JSON,
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
    text,
)
from sqlalchemy.engine import Dialect
from sqlalchemy.orm import relationship
from sqlalchemy.types import TypeDecorator

from domainkit.contrib.contenttypes import ContentType
from domainkit.contrib.contenttypes.tables import content_types
from domainkit.contrib.contenttypes.tables import start_mappers as start_content_types
from domainkit.db import (
    MoneyColumns,
    UTCDateTime,
    ValueObjectType,
    map_imperatively,
    metadata,
    register_column_type,
)
from domainkit.db.derive import IDENTIFIER_LENGTH
from domainkit.db.encrypted import EncryptedType

from .exceptions import GroupNameTaken, MobileNumberTaken, UsernameTaken
from .models import (
    EMAIL_MAX_LENGTH,
    GROUP_NAME_MAX_LENGTH,
    MOBILE_NUMBER_MAX_LENGTH,
    USERNAME_MAX_LENGTH,
    AbstractBaseUser,
    Group,
    PasswordHash,
    Permission,
)
from .settings import identifier_fields
from .tokens import AuthToken


class PasswordHashType(ValueObjectType[PasswordHash]):
    """Store a ``PasswordHash`` as text.

    A single-field value object's column, so fixtures write the hash as the
    text it is stored as, and read it back through ``PasswordHash``, which
    refuses anything that is not a hash.

    It is registered for ``PasswordHash`` (``register_column_type``), as
    ``Text``, the column a ``PasswordHash`` field always derived: a model's
    own field, or one inside another value object, gets the fixture checks
    of the users table's ``password`` (SEC-13a-1).
    """

    cache_ok = True

    def __init__(self, length: int | None = 255) -> None:
        """Hold hashes of up to ``length`` characters; None, of any length."""
        super().__init__(PasswordHash, Text() if length is None else String(length))
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


register_column_type(PasswordHash, partial(PasswordHashType, None))


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
            nullable=True,  # Optional since 1.8 (ADR 0126); unique when set.
            unique=True,
            info={"duplicate": UsernameTaken},
        ),
        Column("email", String(254), nullable=False),
        # Existing rows start unverified when the column is added.
        Column("email_verified", Boolean, nullable=False, server_default=text("false")),
        Column(
            "mobile_number",
            String(MOBILE_NUMBER_MAX_LENGTH),
            nullable=True,
            unique=True,
            info={"duplicate": MobileNumberTaken},
        ),
        Column(
            "mobile_number_verified",
            Boolean,
            nullable=False,
            server_default=text("false"),
        ),
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


def lookup_indexes(
    model: type[AbstractBaseUser],
    table: Table,
    listed: str,
    quote: Callable[[str], str] = str,
) -> dict[str, str]:
    """The indexes ``migrate`` makes on PostgreSQL for the user lookups (since 2.9).

    By name, each with what it indexes, as SQL (``quote`` quotes a name).
    Logins, signups, and the checks for a taken address or username compare
    ``lower()`` of ``EMAIL_FIELD`` and ``USERNAME_FIELD``: each text column
    of those gets an index of it. The admin API lists users by ``listed``
    (``listing_field``), then the id: that pair gets one, unless the column
    is unique, as its own index serves the order then. An encrypted
    column, or one the table lacks, gets none.
    """
    columns = {
        column.name: column
        for column in table.columns
        if not isinstance(column.type, EncryptedType)
    }
    found = {
        _index_name(table.name, f"{field}_lower"): f"lower({quote(field)})"
        for field in dict.fromkeys((model.EMAIL_FIELD, model.USERNAME_FIELD))
        if field is not None and field in columns and _is_text(columns[field])
    }
    order = columns.get(listed)
    if order is not None and "id" in columns and not order.unique:
        found[_index_name(table.name, f"{listed}_id")] = (
            f"{quote(listed)}, {quote('id')}"
        )
    return found


def listing_field(model: type[AbstractBaseUser], table: Table) -> str:
    """The field the admin API lists users by, before the id (since 2.9).

    The first of ``AUTH.IDENTIFIERS`` that ``table`` does not keep
    encrypted, else the ``USERNAME_FIELD``.
    """
    for name in identifier_fields(model):
        if not isinstance(table.c[name].type, EncryptedType):
            return name
    return model.USERNAME_FIELD


def _is_text(column: Column[Any]) -> bool:
    """Whether ``column`` stores text, itself or through a ``TypeDecorator``."""
    kind = column.type
    if isinstance(kind, TypeDecorator):
        kind = kind.impl_instance
    return isinstance(kind, String)


def _index_name(table: str, suffix: str) -> str:
    """``ix_<table>_<suffix>``, cut to PostgreSQL's 63 characters with a digest."""
    name = f"ix_{table}_{suffix}"
    if len(name) <= IDENTIFIER_LENGTH:
        return name
    digest = hashlib.sha256(name.encode()).hexdigest()[:8]
    return f"{name[: IDENTIFIER_LENGTH - 9]}_{digest}"


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
    # Where a login came from (ADR 0130): personal data, ending with it.
    Column("method", String(16)),
    Column("agent", String(200)),
    Column("ip", String(45)),
    Column("device_id", Uuid),
    # How it was made (ADR 0146): what it proved (RFC 8176's names, as the
    # scopes are kept), and the seconds it may idle where a hook shortened it.
    Column("amr", ScopesType),
    Column("idle_timeout", Integer),
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
    # None unique: a pending signup reserves no name, address, or number.
    # Each may be missing, but not all: a code goes to the address or number.
    Column("username", String(USERNAME_MAX_LENGTH), nullable=True),
    Column("email", String(EMAIL_MAX_LENGTH), nullable=True),
    Column("mobile_number", String(MOBILE_NUMBER_MAX_LENGTH), nullable=True),
    # The user model's other REQUIRED_FIELDS, as the client gave them.
    Column("fields", JSON, nullable=True),
    # No password: whoever reads the mail chooses it, when confirming.
    Column("created_at", UTCDateTime, nullable=False),
    Column("expires_at", UTCDateTime, nullable=False),
    Index("ix_domainkit_pending_signups_expires_at", "expires_at"),
    # Addresses of people who may never have asked: never in fixtures.
    info={"domain": "auth", "fixtures": False},
)
"""Accounts asked for at ``/signup``, made once their address is confirmed."""

pending_identifiers = Table(
    "domainkit_pending_identifiers",
    metadata,
    # No foreign key: the user model may be swapped. One per user and field.
    Column("user_id", Uuid, primary_key=True),
    # "email" or "mobile_number": the field it is to replace once proved.
    Column("field", String(32), primary_key=True),
    # Not unique: a held value reserves nothing; whoever proves it first
    # gets it (ADR 0153).
    Column("value", String(EMAIL_MAX_LENGTH), nullable=False),
    Column("created_at", UTCDateTime, nullable=False),
    # When its last code expires: it lasts no longer.
    Column("expires_at", UTCDateTime, nullable=False),
    Index("ix_domainkit_pending_identifiers_expires_at", "expires_at"),
    # Addresses and numbers nobody proved yet: never in fixtures.
    info={"domain": "auth", "fixtures": False},
)
"""A user's new address or number, held until its code proves it (ADR 0153)."""


step_ups = Table(
    "domainkit_step_ups",
    metadata,
    # The digest of the login token or browser session that stepped up (a
    # JSON Web Token carries its own, as claims): its last step-up only.
    Column("id", String(64), primary_key=True),
    Column("user_id", Uuid, nullable=False),
    Column("auth_time", UTCDateTime, nullable=False),
    # What it proved, RFC 8176's names separated by spaces: "otp", "otp pwd".
    Column("amr", String(64), nullable=False),
    Index("ix_domainkit_step_ups_user_id", "user_id"),
    Index("ix_domainkit_step_ups_auth_time", "auth_time"),
    info={"domain": "auth", "guarded": True, "fixtures": False},
)
"""When each login last authenticated again, and how (``auth.stepup``)."""

auth_codes = Table(
    "domainkit_auth_codes",
    metadata,
    Column("id", Uuid, primary_key=True),
    # What the code proves: "email_verification", "password_reset", ...
    Column("purpose", String(32), nullable=False),
    # An HMAC of the address or the number, keyed from SECRET_KEY: neither
    # is kept here. One code per purpose and target: a new one replaces it.
    Column("target", String(64), nullable=False),
    # No foreign key: the user model may be swapped. None: a signup's code.
    Column("user_id", Uuid, nullable=True),
    # The pending signup a signup's code finishes; None for other codes.
    Column("signup_id", Uuid, nullable=True),
    # An HMAC of the code, keyed from SECRET_KEY: the code is never kept.
    Column("digest", String(64), nullable=False),
    Column("attempts", Integer, nullable=False),
    Column("created_at", UTCDateTime, nullable=False),
    Column("expires_at", UTCDateTime, nullable=False),
    UniqueConstraint(
        "purpose", "target", name="uq_domainkit_auth_codes_purpose_target"
    ),
    Index("ix_domainkit_auth_codes_user_id", "user_id"),
    Index("ix_domainkit_auth_codes_expires_at", "expires_at"),
    info={"domain": "auth", "guarded": True, "fixtures": False},
)
"""Codes sent to prove someone reads an address or a number (ADR 0126)."""

PAYEE_MAX_LENGTH = 255
"""The longest payee a transaction authorization names (an IBAN, an account id)."""
PURPOSE_MAX_LENGTH = 100
"""The longest purpose a transaction authorization names (``transfer``)."""

sca_challenges = Table(
    "domainkit_sca_challenges",
    metadata,
    # Random: the authorization names it, with its authentication code.
    Column("id", Uuid, primary_key=True),
    Column("user_id", Uuid, nullable=False),
    *MoneyColumns("amount"),
    Column("payee", String(PAYEE_MAX_LENGTH), nullable=False),
    Column("purpose", String(PURPOSE_MAX_LENGTH), nullable=False),
    Column("created_at", UTCDateTime, nullable=False),
    Column("expires_at", UTCDateTime, nullable=False),
    Column("confirmed_at", UTCDateTime),
    Column("amr", String(64)),
    # Once set, the authorization never works again: used, or voided.
    Column("consumed_at", UTCDateTime),
    Index("ix_domainkit_sca_challenges_user_id", "user_id"),
    Index("ix_domainkit_sca_challenges_expires_at", "expires_at"),
    # Payees may be personal data, and rows live minutes: never in fixtures.
    info={"domain": "auth", "guarded": True, "fixtures": False},
)
"""Transactions waiting for, or holding, an authorization (``auth.sca``)."""

SUBJECT_MAX_LENGTH = 200
"""The longest subject an action authorization names (``orders.order:42``)."""
LOGIN_DIGEST_LENGTH = 64
"""A login's digest: the SHA-256, in hex, of its scheme and token (``auth.actions``)."""

authorizations = Table(
    "domainkit_authorizations",
    metadata,
    # Random: the authorization names it, with its code (``<id>.<code>``).
    Column("id", Uuid, primary_key=True),
    # No foreign key, as for tokens: the user model may be swapped.
    Column("user_id", Uuid, nullable=False),
    # The digest of the login that asked: only it spends the authorization.
    Column("login", String(LOGIN_DIGEST_LENGTH), nullable=False),
    Column("purpose", String(PURPOSE_MAX_LENGTH), nullable=False),
    Column("subject", String(SUBJECT_MAX_LENGTH), nullable=False),
    # The act's fingerprint: SHA-256 of the purpose and the arguments.
    Column("digest", String(64), nullable=False),
    # What the user is shown: {"title": ..., "details": [[label, value], ...]}.
    Column("shown", JSON, nullable=False),
    Column("created_at", UTCDateTime, nullable=False),
    Column("expires_at", UTCDateTime, nullable=False),
    Column("confirmed_at", UTCDateTime),
    # What confirmed it, RFC 8176's names and "phone", separated by spaces.
    Column("amr", String(64)),
    # Once set, the authorization never works again: spent, voided, or ended.
    Column("consumed_at", UTCDateTime),
    # The bound device that confirmed it, and its proof, checkable again
    # once the device is unbound: the key, what it signed, and when.
    Column("device_id", Uuid),
    Column("algorithm", Integer),
    Column("public_key", LargeBinary(2048)),
    Column("signature", LargeBinary(512)),
    Column("signed", LargeBinary(8192)),
    Column("signed_at", UTCDateTime),
    # What confirms it: the phone's sign-in request, or a passkey's challenge.
    Column("sign_in_request_id", Uuid),
    Column("passkey_challenge", String(64)),
    # SHA-256 of what the act's ``binds`` read of its record when it was
    # asked; the digest covers it (ADR 0174).
    Column("bound", String(64)),
    # While its act waits for approval: kept past ``expires_at`` until then,
    # within ``AUTHORIZATIONS.HELD_TTL`` of it, while what it is held for
    # (``approvals.request:<id>``) still waits (ADR 0174).
    Column("held_until", UTCDateTime),
    Column("held_for", String(SUBJECT_MAX_LENGTH)),
    Index("ix_domainkit_authorizations_user_id", "user_id"),
    Index("ix_domainkit_authorizations_login", "login"),
    Index("ix_domainkit_authorizations_expires_at", "expires_at"),
    # What is shown may be personal data, and rows live minutes: never in
    # fixtures.
    info={"domain": "auth", "guarded": True, "fixtures": False},
)
"""Acts waiting for, or holding, an authorization (``auth.actions``, ADR 0144)."""

password_history = Table(
    "domainkit_password_history",
    metadata,
    # In the order they were set: the clock may stand still between two.
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("user_id", Uuid, nullable=False),
    Column("password", PasswordHashType(), nullable=False),
    Column("created_at", UTCDateTime, nullable=False),
    # Set by staff, for the user to change at the first login.
    Column("temporary", Boolean, nullable=False),
    Index("ix_domainkit_password_history_user_id", "user_id", "id"),
    info={"domain": "auth", "guarded": True, "fixtures": False},
)
"""Hashes of each user's last passwords (``AUTH.PASSWORD_HISTORY``), newest
last, and whether the current one is to be changed at the next login."""


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
