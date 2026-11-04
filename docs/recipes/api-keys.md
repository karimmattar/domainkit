# Service accounts with API keys

This recipe lets another service call your API with a key of its own,
sent in a header, and act as a user made for it.

## The problem

Another service (a billing system, a nightly job) calls your API. It
cannot log in with a password and a second factor as a person does. You
want it to send a key, as `X-API-Key: <key>`, and to act as a user made
for it, so that permissions, rate limits, and the audit trail treat it
like any other user. You also want the keys to stay out of the settings
and the database, so a leak of either names no key.

You need a project with `domainkit[sqlite,testing]` installed. Every step
here is run as written by `domainkit_tests/test_recipes.py`.

## The solution

### Step 1: make a project

Make a project, and the tables of its users:

```bash
domainkit init serviceAPI service
cd service
python manage.py makemigrations -m "auth"
python manage.py migrate
```

### Step 2: declare the setting that holds the keys

The keys are never stored. A setting of the project's own, `API_KEYS`,
holds each key's SHA-256 digest and the username it logs in as. A name
that the framework does not declare belongs to the project's settings
schema, which startup validates with the rest
([settings](../reference/settings.md#how-the-settings-are-validated)):

```python title="src/config/schema.py (new file)"
"""The project's own settings, validated with the framework's."""

from domainkit.conf import Settings


class ProjectSettings(Settings):
    """``API_KEYS`` beside every setting domainkit declares."""

    API_KEYS: dict[str, str] = {}  # noqa: RUF012 - pydantic copies it.
    """Each API key's SHA-256 digest, and the username it logs in as."""
```

Name the schema in the base settings, and read the pairs from the
environment, as `SERVICE_API_API_KEYS=<digest>=billing,...`:

```python title="src/config/settings/base.py"
SETTINGS_CLASS = "config.schema.ProjectSettings"
API_KEYS = dict(pair.split("=", 1) for pair in env.list("API_KEYS", []))
```

### Step 3: write the authentication class

An authentication class reads the header, finds the key's user, and
returns its credentials. It returns `None` when the header is missing, so
the other classes try:

```python title="src/config/api_keys.py (new file)"
"""Service accounts: ``X-API-Key: <key>`` logs in as the key's user."""

import hashlib

from domainkit.conf import settings
from domainkit.contrib.auth import (
    AuthenticationFailed,
    AuthUnitOfWork,
    BaseAuthentication,
    Credentials,
)
from starlette.requests import HTTPConnection


def digest(key: str) -> str:
    """What ``API_KEYS`` holds for ``key``: its SHA-256, in hex."""
    return hashlib.sha256(key.encode()).hexdigest()


class ApiKeyAuthentication(BaseAuthentication):
    """A request with ``X-API-Key`` is its key's user's, or refused."""

    scheme = "ApiKey"

    async def authenticate(self, connection: HTTPConnection) -> Credentials | None:
        """The key's user; None without the header, so other classes try."""
        key = connection.headers.get("x-api-key")
        if key is None:
            return None
        username = settings.API_KEYS.get(digest(key))
        if username is None:
            raise AuthenticationFailed("Unknown API key.")
        async with AuthUnitOfWork() as uow:
            user = await uow.users.get_by_natural_key(username)
        if user is None or not user.is_active:
            raise AuthenticationFailed("The API key's user is inactive or gone.")
        return Credentials(user, scheme="api_key")
```

### Step 4: list the class after the token class

The class that checks logins' tokens stays first:

```python title="src/config/settings/base.py"
AUTH: dict[str, object] = {
    "AUTHENTICATION_CLASSES": [
        "domainkit.contrib.auth.authentication.TokenAuthentication",
        "config.api_keys.ApiKeyAuthentication",
    ],
}
```

Make the service's user without a password (`createsuperuser` is for
people). It logs in with its key only, and holds the permissions you
grant it.

### Step 5: test the key

The test makes the user, sets `API_KEYS` to the key's digest, and calls
`GET /auth/me` with the key, then with a key that nobody was given:

```python title="tests/test_api_keys.py (new file)"
"""A service calls with its key; a key nobody was given is refused."""

from typing import Any

import pytest
from config.api_keys import digest
from domainkit.conf import override_settings
from domainkit.contrib.auth import AuthUnitOfWork
from domainkit.http import get_asgi_application
from domainkit.testing import AsyncTestClient

pytestmark = pytest.mark.anyio
KEY = "tests-only-key-of-the-billing-service"


async def me(key: str) -> tuple[int, Any]:
    """The status and body of ``GET /auth/me`` with ``key``."""
    async with AsyncTestClient(get_asgi_application()) as client:
        answer = await client.get("/auth/me", headers={"X-API-Key": key})
    return answer.status_code, answer.json()


async def test_a_service_calls_as_its_user(db: None) -> None:
    """The key's digest names the user; the key itself is stored nowhere."""
    async with AuthUnitOfWork() as uow:
        await uow.users.create_user("billing")
        await uow.commit()

    with override_settings(API_KEYS={digest(KEY): "billing"}):
        status, body = await me(KEY)

    assert (status, body["username"]) == (200, "billing")


async def test_a_key_nobody_was_given_is_refused(db: None) -> None:
    """401, as a wrong password is."""
    with override_settings(API_KEYS={digest(KEY): "billing"}):
        status, body = await me("a-guessed-key")

    assert (status, body["code"]) == (401, "authentication_failed")
```

Run the checks and the tests:

```bash
ruff check --fix .
ruff format --check .
pytest
```

### Step 6: give a service its key

1. Make a key: `python -c "import secrets; print(secrets.token_urlsafe(32))"`.
2. Hand the key to the service, by a channel that keeps it secret.
3. Deploy its digest in `API_KEYS`:
   `python -c "from config.api_keys import digest; print(digest('<key>'))"`.

To replace a key, deploy the new digest and remove the old one. A leaked
environment names no key, because it holds digests only.

## How it works

Every request goes through the classes of
`AUTH["AUTHENTICATION_CLASSES"]`, in order. Each class answers one of
three ways ([authentication](../auth/authentication.md#other-credentials)):

| The class | Means |
| --- | --- |
| returns `None` | "Not mine": the next class tries. A request without `X-API-Key` goes on to the token class. |
| returns `Credentials(user, scheme=...)` | The request is that user's. |
| raises `AuthenticationFailed` | The credentials were presented and do not hold: 401 `authentication_failed`. |

A key that does not hold answers 401, is logged as refused credentials,
and counts against the address's `RATE_LIMIT`. Past that limit, the
address's keys are refused unread for a while. Everything after
authentication (permissions, rate limits, the audit trail) sees an
ordinary user.

## See also

- [Authentication](../auth/authentication.md): every way requests prove
  who sent them.
- [Extension points](../reference/extension-points.md#users-logins-and-rules):
  `BaseAuthentication` and `SETTINGS_CLASS`.
- [A permission rule of your own](permission-rules.md): decide what the
  service may call.
