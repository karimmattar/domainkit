# A permission rule of your own

This recipe writes a rule of your own that decides who may call a route:
here, only users who proved their email address.

## The problem

A [rule](../learning.md#words) decides who may call a route. The built-in
rules ask whether the caller logged in, holds a permission, or owns the
object ([routes](../auth/routes.md#protecting-routes)). You need to ask
something else about the caller: here, whether they proved that they
read mail at their address. A caller who did not log in must still get
401, and a logged-in caller who is refused must get 403 with a message
that says why.

You need a project with `domainkit[sqlite,testing]` installed. Every step
here is run as written by `domainkit_tests/test_recipes.py`.

## The solution

### Step 1: make a project

```bash
domainkit init reportsAPI reports
cd reports
python manage.py makemigrations -m "auth"
python manage.py migrate
```

### Step 2: write the rule and its route

A rule subclasses `BasePermission`, and answers `has_permission`. A
refusal is a 403 with the rule's `message`. `Require` runs the rules in
order, and `IsAuthenticated` first makes a caller who did not log in a
401, not a 403:

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

### Step 3: mount the route

Mount it with the others:

```python title="src/config/urls.py"
from config.reports import router as reports_router

router.include_router(reports_router, prefix="/reports")
```

### Step 4: test the rule

The test makes two users, one verified and one not, and calls the route
as each of them, and as nobody:

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
    credentials = {"identifier": f"{username}@example.com", "password": PASSWORD}
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

Run the checks and the tests:

```bash
ruff check --fix .
ruff format --check .
pytest
```

## How it works

`Depends(Require(IsAuthenticated, EmailVerified))` runs the rules in
order, and stops at the first refusal:

| Caller | `IsAuthenticated` | `EmailVerified` | Answer |
| --- | --- | --- | --- |
| nobody | refuses | not asked | 401 |
| `bob`, not verified | passes | refuses | 403, `"Verify your email address first."` |
| `ann`, verified | passes | passes | 200 |

More you can do with rules:

- **Rules about one object.** `has_object_permission` is the hook for a
  rule about one object (the caller's own order, say). `ResourceRouter`
  asks it of the object a route reads or changes.
- **Rules combine.** `AnyOf(A, B)`, `AllOf(A, B)`, and `Not(A)` make one
  rule of several.
- **Refusals are recorded.** With `contrib.audit` installed, a refusal by
  a rule of your own is recorded as any other denial is: as
  `auth.access`, with the outcome `denied`, the route, and the rule's
  class, never the request's values. A rule that refuses more than it
  should then shows in the audit trail.

## See also

- [Protecting routes](../auth/routes.md#protecting-routes): the built-in
  rules.
- [Extension points](../reference/extension-points.md#users-logins-and-rules):
  `BasePermission`.
- [Service accounts with API keys](api-keys.md): callers that are
  services.
