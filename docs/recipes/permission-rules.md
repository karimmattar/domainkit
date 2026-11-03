# A permission rule of your own

A rule decides who may call a route. The built-in ones ask whether the
caller logged in, holds a permission, or owns the object; a rule of your
own asks anything else about the caller, here whether they proved their
email address ([routes](../auth/routes.md#protecting-routes)).

You need a project with `domainkit[sqlite,testing]` installed. Every step
here is run as written by `domainkit_tests/test_recipes.py`.

## The rule and its route

```bash
domainkit init reportsAPI reports
cd reports
python manage.py makemigrations -m "auth"
python manage.py migrate
```

A rule subclasses `BasePermission` and answers `has_permission`; a
refusal is a 403 with its `message`. `Require` runs the rules in order,
and `IsAuthenticated` first makes a caller who did not log in a 401, not
a 403:

```python title="src/config/reports.py (new file)"
"""Reports: for users who proved their email address."""

from typing import Any

from domainkit.http.access import BasePermission, IsAuthenticated, Require
from fastapi import APIRouter, Depends
from starlette.requests import HTTPConnection


class EmailVerified(BasePermission):
    """The caller proved they read mail at their address."""

    message = "Verify your email address first."

    async def has_permission(self, request: HTTPConnection, user: Any) -> bool:
        """Whether the user's address is verified."""
        return getattr(user, "email_verified", False) is True


router = APIRouter()


@router.get("", dependencies=[Depends(Require(IsAuthenticated, EmailVerified))])
async def reports() -> dict[str, list[str]]:
    """The reports a verified user may read."""
    return {"reports": ["monthly"]}
```

Mount it with the others:

```python title="src/config/urls.py"
from config.reports import router as reports_router

router.include_router(reports_router, prefix="/reports")
```

`has_object_permission` is the hook for a rule about one object (the
caller's own order, say); `ResourceRouter` asks it of the object a route
reads or changes. Rules combine: `AnyOf(A, B)`, `AllOf(A, B)`, `Not(A)`.

## A test of the rule

```python title="tests/test_reports.py (new file)"
"""Verified users read reports; others are refused, and say why."""

import pytest
from domainkit.contrib.auth import AuthUnitOfWork
from domainkit.http import get_asgi_application
from domainkit.testing import AsyncTestClient

pytestmark = pytest.mark.anyio
PASSWORD = "correct-horse-battery"


async def make(username: str, *, verified: bool) -> None:
    """A user, their address verified or not."""
    async with AuthUnitOfWork() as uow:
        user = await uow.users.create_user(
            username, PASSWORD, email=f"{username}@example.com"
        )
        if verified:
            user.verify_email()
        await uow.commit()


async def bearer(client: AsyncTestClient, username: str) -> dict[str, str]:
    """The headers of ``username``'s requests, once logged in."""
    credentials = {"username": username, "password": PASSWORD}
    answer = await client.post("/auth/login", json=credentials)
    return {"Authorization": f"Bearer {answer.json()['access_token']}"}


async def test_only_verified_users_read_reports(db: None) -> None:
    """200 for ann, 403 with the rule's message for bob, 401 for nobody."""
    await make("ann", verified=True)
    await make("bob", verified=False)

    async with AsyncTestClient(get_asgi_application()) as client:
        ann = await client.get("/reports", headers=await bearer(client, "ann"))
        bob = await client.get("/reports", headers=await bearer(client, "bob"))
        nobody = await client.get("/reports")

    assert (ann.status_code, ann.json()) == (200, {"reports": ["monthly"]})
    assert (bob.status_code, bob.json()["detail"]) == (
        403,
        "Verify your email address first.",
    )
    assert nobody.status_code == 401
```

```bash
ruff check --fix .
ruff format --check .
pytest
```

A refusal by a rule of your own is recorded as any other denial is:
with `contrib.audit` installed, as `auth.access` with the outcome
`denied`, the route, and the rule's class, never the request's values. A
rule that refuses more than it should then shows in the audit trail.
