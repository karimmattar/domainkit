"""How a screen names what a grant gives: groups, keys, and fields (since 2.8).

The admin API's act (``auth.admin_change``) and approvals' holds of its
changes show the same things the same way: a group added in a few words,
"readers (…9abc) 3 perms + 2 object grants"; the keys a setting names,
by name, wherever they would be counted
(``AUTH.AUTHORIZATIONS.NAMED_PERMISSIONS``); and a user's field as a
person reads it ("Email", "Display name"), its value ``Typed`` where staff
wrote it (``typed_field``). Other apps reach these names through
``domainkit.contrib.auth.integration``.
"""

import hashlib
from collections.abc import Callable, Iterable
from typing import TYPE_CHECKING, Final

from domainkit.conf import settings
from domainkit.i18n import gettext, gettext_noop, ngettext, pgettext

from .screens import TooMuchToShow, Typed, laid_out
from .settings import auth_settings, identifier_fields
from .users import get_user_model

if TYPE_CHECKING:
    from .backends import ObjectGrant

SHOWN_ID: Final = 4
"""How many of a group's id's last characters its item shows: a check for the
person beside the name, short enough that two groups share a line; the act
or the request binds the whole id."""
FIELDS: Final = {
    "email": gettext_noop("Email"),
    "mobile_number": gettext_noop("Mobile number"),
    "first_name": gettext_noop("First name"),
    "last_name": gettext_noop("Last name"),
}
"""The labels of the built-in user's fields; another is named after its field."""


def named_permissions() -> frozenset[str]:
    """The permission keys a screen names wherever it would count them.

    ``AUTH.AUTHORIZATIONS.NAMED_PERMISSIONS``; unset, the key of
    ``TENANCY.ALL_TENANTS_PERMISSION``, where it is set: a key that
    matters more than the others a group grants.
    """
    named = auth_settings().AUTHORIZATIONS.NAMED_PERMISSIONS
    if named is not None:
        return frozenset(named)
    tenancy = settings.TENANCY
    reach = None if tenancy is None else tenancy.ALL_TENANTS_PERMISSION
    return frozenset() if reach is None else frozenset({reach})


def named_first(keys: Iterable[str]) -> list[str]:
    """``keys``, sorted, those ``named_permissions`` names first.

    A list a screen cuts and counts keeps its first items: a key named
    there is never among those counted.
    """
    named = named_permissions()
    return sorted(keys, key=lambda key: (key not in named, key))


def group_item(
    name: str, group_id: object, permissions: int, object_grants: int = 0
) -> str:
    """A group added, in a few words: its name, the end of its id, what it grants.

    "readers (…9abc) 3 perms", and "+ 2 object grants" where it holds any:
    short, so that a screen puts two or more on a line. A name too long for
    its line is cut, never refused: the act binds the id.
    """
    ident = str(group_id)
    ident = "…" + ident[-SHOWN_ID:] if len(ident) > SHOWN_ID else ident
    grants = ngettext("{count} perm", "{count} perms", permissions).format(
        count=permissions
    )
    if object_grants:
        more = ngettext("{count} object grant", "{count} object grants", object_grants)
        grants += " + " + more.format(count=object_grants)
    line = pgettext("held group", "{name} ({id}) {grants}")
    return _fitting(lambda text: line.format(name=text, id=ident, grants=grants), name)


def list_item(name: str) -> str:
    """``name``, cut where need be, so that a list shows it on one line."""
    return _fitting(lambda text: text, name)


def _fitting(make: Callable[[str], str], name: str) -> str:
    """``make(name)``, the name cut and ended with ``…`` where a line needs it.

    Measured as a screen lays a list's item out (quoted, escaped), so what
    a grant gives is never refused for the length of a name.
    """

    def fits(text: str) -> bool:
        try:
            laid_out([("·", [text])], whole=True)
        except TooMuchToShow:
            return False
        return True

    whole = make(name)
    if fits(whole):
        return whole
    low, high, best = 0, len(name) - 1, make("…")
    while low <= high:
        middle = (low + high) // 2
        candidate = make(name[:middle] + "…")
        if fits(candidate):
            best, low = candidate, middle + 1
        else:
            high = middle - 1
    return best


def field_label(name: str) -> str:
    """How a screen names the user's field ``name``: "Email", "Display name".

    The built-in user's fields have labels in the request's language;
    another is its name, its underscores spaces, its first letter a
    capital, as Django names a field without a ``verbose_name``.
    """
    if name in FIELDS:
        return gettext(FIELDS[name])
    spaced = name.replace("_", " ").strip()
    return spaced[:1].upper() + spaced[1:] if spaced else name


def identifying_fields() -> frozenset[str]:
    """The user's fields that name them: a screen shows them as set, never as typed.

    The username, the email, and each identifier (``IDENTIFIER_FIELDS``,
    ``AUTH.IDENTIFIERS``): an address or a number there is what is set.
    """
    model = get_user_model()
    named = {model.USERNAME_FIELD, model.EMAIL_FIELD}
    named |= {*getattr(model, "IDENTIFIER_FIELDS", ()), *identifier_fields(model)}
    return frozenset(each for each in named if each)


def typed_field(name: str, value: object) -> object:
    """A user's field ``name`` set to ``value``, as a screen shows it (since 2.8).

    Text staff wrote is ``Typed``, so that ``laid_out`` checks it (ADR
    0180), but in a field that identifies the user (``identifying_fields``);
    any other value as given.
    """
    if isinstance(value, str) and name not in identifying_fields():
        return Typed(value)
    return value


def grants_digest(grants: "Iterable[ObjectGrant]") -> tuple[int, str]:
    """How many ``grants`` there are, and the SHA-256 of them, sorted ("" for none).

    Each with when it ends. A grant on an object made since, or renewed to
    another date, changes the digest: what holds it is another act, or
    another request. Roles come as the grants of their permissions.
    """
    lines = sorted(
        "\t".join(
            (
                grant.permission,
                grant.content_type,
                str(grant.object_id),
                "" if grant.expires_at is None else grant.expires_at.isoformat(),
            )
        )
        for grant in grants
    )
    if not lines:
        return 0, ""
    return len(lines), hashlib.sha256("\n".join(lines).encode()).hexdigest()
