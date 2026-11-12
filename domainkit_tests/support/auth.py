"""Users, logins and what they left behind, for tests of anything behind auth."""

from typing import Any
from uuid import UUID

from fastapi.testclient import TestClient
from sqlalchemy import select

from domainkit.contrib.audit.tables import audit_log
from domainkit.contrib.auth import AuthUnitOfWork
from domainkit.contrib.auth.tables import tokens
from domainkit.db import connections
from domainkit.management.base import run_async

PASSWORD = "vy3!Tq-pX9#mR2"
AUTHENTICATION = "domainkit.contrib.auth.authentication"
JWT: dict[str, Any] = {
    "AUTHENTICATION_CLASSES": [
        f"{AUTHENTICATION}.JWTAuthentication",
        f"{AUTHENTICATION}.TokenAuthentication",
    ],
    "JWT": {"SIGNING_KEY": "k" * 32},
}


def create_user(
    username: str | None,
    password: str | None = PASSWORD,
    **fields: Any,  # noqa: ANN401 - user fields.
) -> UUID:
    """Store a user; return the id."""

    async def create() -> UUID:
        async with AuthUnitOfWork() as uow:
            user = await uow.users.create_user(username, password, **fields)
            await uow.commit()
        return user.id

    return run_async(create())


def grant(username: str, *keys: str) -> None:
    """Give ``username`` the permissions ``keys``."""

    async def give() -> None:
        async with AuthUnitOfWork() as uow:
            user = await uow.users.get_by_natural_key(username)
            assert user is not None
            user.grant(*await uow.permissions.get_many(keys))
            await uow.commit()

    run_async(give())


def login(
    client: TestClient, username: str, password: str = PASSWORD
) -> dict[str, Any]:
    """Log in; return the token response."""
    response = client.post(
        "/auth/login", json={"username": username, "password": password}
    )
    assert response.status_code == 200, response.json()
    body: dict[str, Any] = response.json()
    return body


def bearer(token: str) -> dict[str, str]:
    """The ``Authorization`` header for ``token``."""
    return {"Authorization": f"Bearer {token}"}


def audited(action: str) -> list[dict[str, Any]]:
    """Audit rows of ``action``, oldest first."""

    async def read() -> list[dict[str, Any]]:
        async with connections.engine().connect() as connection:
            rows = await connection.execute(
                select(audit_log)
                .where(audit_log.c.action == action)
                .order_by(audit_log.c.occurred_at)
            )
            return [dict(row._mapping) for row in rows]

    return run_async(read())


def stored_tokens(user_id: UUID, *, devices: bool = False) -> list[dict[str, Any]]:
    """The user's stored tokens; with ``devices``, the devices that logged in too."""

    async def read() -> list[dict[str, Any]]:
        async with connections.engine().connect() as connection:
            statement = select(tokens).where(tokens.c.user_id == user_id)
            if not devices:
                statement = statement.where(tokens.c.kind != "device")
            rows = await connection.execute(statement)
            return [dict(row._mapping) for row in rows]

    return run_async(read())
