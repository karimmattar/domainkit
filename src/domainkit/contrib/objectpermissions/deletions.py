"""Grants go with their object: deleting a row deletes the grants about it.

When the ORM deletes an object's row (a repository's ``remove``, unless the
aggregate is soft-deletable), the grants on it are deleted in the same
transaction. An object stored later with the same id, such as a slug or an
integer the database hands out again, starts without them. A soft delete
keeps them, for the object may be restored.

Grants to a user go with the user the same way: deleting a user's row
(``UserRepository.remove``) deletes the grants held by that user in the
user's database, in the same transaction.

Bulk deletes (``delete_rows``) bypass the ORM, and leave their grants:
remove such objects one by one, or delete their grants in the same
transaction.
"""

from typing import Any

from sqlalchemy import Connection, delete, event
from sqlalchemy.orm import Mapper

from domainkit.apps import registry
from domainkit.contrib.auth.models import AbstractBaseUser

from .models import object_column
from .services import describe
from .tables import object_permissions

APP = "domainkit.contrib.objectpermissions"


def forget_grants(mapper: Mapper[Any], connection: Connection, target: object) -> None:
    """Delete the grants on ``target``, whose row the ORM just deleted.

    A user's own grants go too.
    """
    if mapper.local_table is object_permissions or not registry.is_installed(APP):
        return
    if isinstance(target, AbstractBaseUser):
        connection.execute(
            delete(object_permissions).where(object_permissions.c.user_id == target.id)
        )
    try:
        content_type, object_id = describe(target)
    except (LookupError, TypeError):
        return
    column = object_permissions.c[object_column(type(object_id))]
    connection.execute(
        delete(object_permissions).where(
            object_permissions.c.content_type == content_type, column == object_id
        )
    )


def listen() -> None:
    """Delete grants with every row the ORM deletes, once per process."""
    if not event.contains(Mapper, "after_delete", forget_grants):
        event.listen(Mapper, "after_delete", forget_grants)
