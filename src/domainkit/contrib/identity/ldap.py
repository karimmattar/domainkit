"""Logging in with a directory's password: LDAP or Active Directory (``[ldap]``).

::

    AUTH = {**AUTH, "BACKENDS": [
        "domainkit.contrib.auth.backends.ModelBackend",
        "domainkit.contrib.identity.ldap.LDAPBackend",
    ]}
    IDENTITY = {"LDAP": {
        "URL": "ldaps://dc.example.com",
        "BIND_DN": "cn=reader,ou=services,dc=example,dc=com",
        "BIND_PASSWORD": env.str("LDAP_READER_PASSWORD"),
        "USER_SEARCH_BASE": "ou=people,dc=example,dc=com",
        "USER_FILTER": "(sAMAccountName={username})",
        "GROUPS": {"cn=finance,ou=groups,dc=example,dc=com": "finance"},
    }}

``/auth/login`` then also checks a password against the directory: the
service account finds the one entry the filter names (the username
escaped, so a ``*`` or a ``)`` in it is only a character), and a bind as
that entry with the password proves it. An empty password is refused
before any bind: a directory takes one as an anonymous bind, which would
"succeed". The user is made here on their first login (a password that
only the directory checks: the local one is unusable), linked by the
entry's DN, and kept in step: the email, and membership of the groups
``GROUPS`` maps (others are left as they are). An account here with the
same username that the directory did not make is never taken over.
The directory is called in a thread; ``TIMEOUT`` bounds each step.
"""

import asyncio
import logging
import math
import ssl
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Final

from domainkit.apps import registry
from domainkit.contrib.auth import AbstractBaseUser, AbstractUser
from domainkit.contrib.auth.backends import BaseBackend
from domainkit.contrib.auth.models import UserId

from .settings import IdentitySettings, LdapSettings
from .stores import IdentityUnitOfWork

__all__ = ["LDAPBackend"]

logger = logging.getLogger(__name__)
PROVIDER: Final = "ldap"
MAX_USERNAME: Final = 150


@dataclass(frozen=True, slots=True)
class DirectoryEntry:
    """What the directory says of a user whose password it accepted."""

    dn: str
    email: str | None = None
    groups: frozenset[str] = field(default_factory=frozenset)


class LDAPBackend(BaseBackend):
    """Passwords checked by the directory ``IDENTITY.LDAP`` names."""

    async def authenticate(
        self, credentials: Mapping[str, object]
    ) -> AbstractBaseUser | None:
        """The user whose directory entry the credentials bind as, made or updated."""
        config = registry.settings(IdentitySettings).LDAP
        username = credentials.get("username")
        password = credentials.get("password")
        if (
            config is None
            or not isinstance(username, str)
            or not isinstance(password, str)
            or not username
            or not password  # An empty password would be an anonymous bind.
            or len(username) > MAX_USERNAME
        ):
            return None
        entry = await asyncio.to_thread(check_password, config, username, password)
        if entry is None:
            return None
        return await self._user(config, username, entry)

    async def _user(
        self, config: LdapSettings, username: str, entry: DirectoryEntry
    ) -> AbstractBaseUser | None:
        """The local user of ``entry``: found by its DN, or made."""
        async with IdentityUnitOfWork() as uow:
            linked = await uow.identities.find(PROVIDER, entry.dn.lower())
            user = (
                None if linked is None else await uow.users.find(UserId(linked.user_id))
            )
            if user is None:
                if await uow.users.username_taken(username):
                    logger.warning(
                        "A directory login named an account it did not make."
                    )
                    return None
                user = await uow.users.create_user(username, None)
                await uow.identities.link(
                    user.id, PROVIDER, entry.dn.lower(), entry.email
                )
            if not user.is_active:
                return None
            if (
                isinstance(user, AbstractUser)
                and entry.email
                and user.email != entry.email
            ):
                user.email = entry.email
            await _sync_groups(uow, user, config, entry)
            await uow.commit()
        return user


async def _sync_groups(
    uow: IdentityUnitOfWork,
    user: AbstractBaseUser,
    config: LdapSettings,
    entry: DirectoryEntry,
) -> None:
    """Join the mapped groups the entry is in; leave the mapped ones it is not in."""
    if not config.GROUPS:
        return
    member_of = {dn.lower() for dn in entry.groups}
    wanted = {name for dn, name in config.GROUPS.items() if dn.lower() in member_of}
    mapped = set(config.GROUPS.values())
    groups = {group.name: group for group in await uow.groups.get_many_by_name(mapped)}
    for name, group in groups.items():
        if name in wanted and group not in user.groups:
            user.join(group)
        elif name not in wanted and group in user.groups:
            user.leave(group)


def check_password(
    config: LdapSettings, username: str, password: str
) -> DirectoryEntry | None:
    """The entry ``username`` names, if ``password`` binds as it (blocking).

    None for no entry, several, or a password the directory refuses; a
    directory that cannot be reached, or proves no certificate, is logged
    and refuses too: whatever goes wrong, the login is refused, not
    answered with an error.
    """
    try:
        return _check(config, username, password)
    except Exception as exc:  # noqa: BLE001 - every failure refuses the login.
        logger.warning("The directory could not check a login: %s.", type(exc).__name__)
        return None


def _check(config: LdapSettings, username: str, password: str) -> DirectoryEntry | None:
    """``check_password``, letting ldap3's errors out."""
    from ldap3.utils.conv import escape_filter_chars

    reader = connect(config, config.BIND_DN, _secret(config))
    try:
        found = reader.search(
            config.USER_SEARCH_BASE,
            config.USER_FILTER.format(username=escape_filter_chars(username)),
            attributes=[config.EMAIL_ATTRIBUTE, "memberOf"],
            size_limit=2,
        )
        if not found or len(reader.entries) != 1:
            return None
        dn = reader.entries[0].entry_dn
        attributes = reader.entries[0].entry_attributes_as_dict
    finally:
        reader.unbind()
    user = connect(config, dn, password)
    try:
        if not user.bind():
            return None
    finally:
        user.unbind()
    emails = attributes.get(config.EMAIL_ATTRIBUTE) or []
    return DirectoryEntry(
        dn=dn,
        email=str(emails[0]) if emails else None,
        groups=frozenset(str(each) for each in attributes.get("memberOf") or []),
    )


def connect(config: LdapSettings, user: str | None, password: str | None) -> Any:  # noqa: ANN401 - ldap3's Connection, untyped.
    """A connection as ``user``: bound for the reader, not yet for a user.

    TLS (``ldaps://``, or ``START_TLS``) checks the directory's certificate
    against ``CA_CERTIFICATES`` (or the system's) and its name: ldap3 checks
    neither by default, which let anyone on the path read every password.
    """
    from ldap3 import NONE, Connection, Server, Tls

    tls = Tls(validate=ssl.CERT_REQUIRED, ca_certs_file=config.CA_CERTIFICATES)
    server = Server(config.URL, get_info=NONE, connect_timeout=config.TIMEOUT, tls=tls)
    connection = Connection(
        server,
        user=user,
        password=password,
        read_only=True,
        # Whole seconds: ldap3 packs it as an integer on POSIX.
        receive_timeout=math.ceil(config.TIMEOUT),
        raise_exceptions=False,
    )
    if config.START_TLS:
        connection.open()
        connection.start_tls()
    if user == config.BIND_DN:
        connection.bind()
    return connection


def _secret(config: LdapSettings) -> str | None:
    """The reader's password, if it has one."""
    return (
        None
        if config.BIND_PASSWORD is None
        else config.BIND_PASSWORD.get_secret_value()
    )
