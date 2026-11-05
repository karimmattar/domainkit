"""Users and groups for tests, stored as the application stores them.

What factory_boy's model factories and pytest-django's ``admin_user`` give,
for ``domainkit.contrib.auth``: a user granted permissions, in groups, with
a password, stored through ``AuthUnitOfWork``, so its events are published
and the principal cache hears of it. Sign in as one with the clients'
``authenticated_as``::

    from domainkit.contrib.auth.testing import create_group, create_user

    async def test_clerks_read(async_client: AsyncTestClient) -> None:
        clerks = await create_group("billing.view_invoice", name="clerks")
        ann = await create_user(groups=[clerks.name], email="ann@example.com")
        client = await async_client.authenticated_as(ann)
        assert (await client.get("/api/v1/billing/invoices")).status_code == 200

In a sync test, run them in the client's event loop:
``client.run(create_user, "billing.view_invoice")``. Names are unique unless
given (``user-<hex>``, ``group-<hex>``), as ``client_for``'s users' are.
"""

from collections.abc import Sequence
from typing import Any
from uuid import uuid4

from .models import AbstractBaseUser, Group
from .settings import identifier_fields
from .uow import AuthUnitOfWork

__all__ = ["create_group", "create_user"]


async def create_user(
    *permissions: str,
    password: str | None = None,
    groups: Sequence[str] = (),
    superuser: bool = False,
    **fields: Any,  # noqa: ANN401 - the user model's fields.
) -> AbstractBaseUser:
    """Store a user granted ``permissions`` (keys), in ``groups`` (names).

    Without ``password`` the user gets an unusable one, as ``create_user``
    gives; with one, it is hashed with ``AUTH.PASSWORD_HASHERS``, so a test
    can log in through the API. ``superuser`` makes a superuser (and staff,
    if the model has it). Keyword arguments set the user's fields; the
    username is ``user-<hex>`` unless given. Where the username does not
    identify users (``AUTH.IDENTIFIERS``), and no field that does is given,
    the first of them gets one: ``user-<hex>@example.com``, or a number.

    Raises:
        UnknownPermission: A key names no permission.
        UnknownGroup: A name names no group.
        UsernameTaken: Another user has the username.
    """
    async with AuthUnitOfWork() as uow:
        field = uow.users.model.USERNAME_FIELD
        username = fields.pop(field, None) or f"user-{uuid4().hex}"
        fill_identifier(uow.users.model, username, fields)
        create = uow.users.create_superuser if superuser else uow.users.create_user
        user = await create(username, password, **fields)
        user.grant(*await uow.permissions.get_many(permissions))
        for group in await uow.groups.get_many_by_name(groups):
            user.join(group)
        await uow.commit()
    return user


def fill_identifier(
    model: type[AbstractBaseUser], username: str, fields: dict[str, Any]
) -> None:
    """Give a test user one of ``AUTH.IDENTIFIERS``, if its username is not one.

    Nothing changes when ``fields`` gives one already. The first identifier
    gets ``<username>@example.com`` for an email field, a number for
    ``mobile_number``, or the username for any other field.
    """
    identifiers = identifier_fields(model)
    if model.USERNAME_FIELD in identifiers or any(fields.get(n) for n in identifiers):
        return
    first = identifiers[0]
    if first == "mobile_number":
        fields[first] = f"+1555{uuid4().int % 10**7:07d}"
    else:
        fields[first] = f"{username}@example.com" if first == "email" else username


async def create_group(*permissions: str, name: str | None = None) -> Group:
    """Store a group granted ``permissions`` (keys); ``group-<hex>`` unless named.

    Raises:
        UnknownPermission: A key names no permission.
        GroupNameTaken: Another group has the name.
    """
    async with AuthUnitOfWork() as uow:
        group = await uow.groups.create(name or f"group-{uuid4().hex}")
        group.grant(*await uow.permissions.get_many(permissions))
        await uow.commit()
    return group
