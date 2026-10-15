"""The permissions ``migrate`` creates (Django's ``create_permissions``).

Every content type gets ``<action>_<model>`` for each action in its
``Meta.default_permissions`` (``add``, ``change``, ``delete``, ``view``),
named "Can <action> <verbose name>", plus each ``(codename, name)`` in
``Meta.permissions``. Existing permissions are never changed or removed:
grants hang off them.
"""

import re
from dataclasses import dataclass
from typing import Final

from sqlalchemy import insert, select
from sqlalchemy.ext.asyncio import AsyncConnection

from domainkit.contrib.contenttypes import installed_models, sync_content_types
from domainkit.exceptions import ImproperlyConfigured

from .tables import permissions

_CODENAME: Final = re.compile(r"[a-z][a-z0-9_]{0,99}")
_NAME_LENGTH: Final = 255


@dataclass(frozen=True, slots=True)
class PermissionSpec:
    """A permission an installed model declares."""

    content_type: tuple[str, str]
    """``(domain, model)``."""
    codename: str
    name: str

    @property
    def key(self) -> str:
        """``<domain>.<codename>``."""
        return f"{self.content_type[0]}.{self.codename}"


def declared_permissions() -> list[PermissionSpec]:
    """Every permission the installed models declare, checked.

    Raises:
        ImproperlyConfigured: A codename is malformed, a name too long, or
            two models of one domain declare the same codename (their
            keys would clash).
    """
    specs: dict[str, PermissionSpec] = {}
    for model in installed_models():
        options = model.options
        declared = [
            (f"{action}_{options.model}", f"Can {action} {options.verbose_name}")
            for action in options.default_permissions
        ]
        for codename, name in [*declared, *options.permissions]:
            spec = PermissionSpec(model.key, codename, name)
            _check(spec, model.cls.__qualname__)
            existing = specs.setdefault(spec.key, spec)
            if existing.content_type != spec.content_type:
                first, second = existing.content_type[1], spec.content_type[1]
                raise ImproperlyConfigured(
                    f"Two models declare the permission {spec.key!r}: "
                    f"{first} and {second}."
                )
    return list(specs.values())


async def sync_permissions(connection: AsyncConnection) -> int:
    """Create content types and the permissions missing; return how many."""
    content_type_ids = await sync_content_types(connection)
    rows = await connection.execute(
        select(permissions.c.content_type_id, permissions.c.codename)
    )
    existing = {(row.content_type_id, row.codename) for row in rows}
    missing = [
        {
            "content_type_id": content_type_ids[spec.content_type],
            "codename": spec.codename,
            "name": spec.name,
        }
        for spec in declared_permissions()
        if (content_type_ids[spec.content_type], spec.codename) not in existing
    ]
    if missing:
        await connection.execute(insert(permissions), missing)
    return len(missing)


def _check(spec: PermissionSpec, owner: str) -> None:
    """Refuse codenames and names the tables cannot hold, or keys cannot parse."""
    if not _CODENAME.fullmatch(spec.codename):
        raise ImproperlyConfigured(
            f"{owner}: permission codename {spec.codename!r} must be lower-case "
            "letters, digits, and underscores (at most 100), starting with a letter."
        )
    if not 0 < len(spec.name) <= _NAME_LENGTH:
        raise ImproperlyConfigured(
            f"{owner}: permission {spec.codename!r} needs a name of 1 to "
            f"{_NAME_LENGTH} characters."
        )
