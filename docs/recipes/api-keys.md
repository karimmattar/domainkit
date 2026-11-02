# Service accounts with API keys

Another service calls the API with a key in a header of its own, as
`X-API-Key: <key>`, and acts as a user made for it. An authentication
class of your own reads the header; everything after it (permissions,
rate limits, the audit trail) sees an ordinary user
([authentication](../auth/authentication.md#other-credentials)).

You need a project with `domainkit[sqlite,testing]` installed. Every step
here is run as written by `domainkit_tests/test_recipes.py`.

## The class

```bash
domainkit init serviceAPI service
cd service
python manage.py makemigrations -m "auth"
python manage.py migrate
```

The keys are never stored: the environment holds each key's SHA-256
digest and the user it logs in as, `API_KEYS=<digest>=<username>,...`.
A leaked environment then names no key, and replacing a key is a
redeployment with a new digest.

```python title="src/config/api_keys.py (new file)"
"""Service accounts: ``X-API-Key: <key>`` logs in as the key's user."""

import hashlib
import os

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


def keys() -> dict[str, str]:
    """``API_KEYS``: each key's digest, and the username it logs in as."""
    pairs = (each.split("=", 1) for each in os.environ.get("API_KEYS", "").split(","))
    return {pair[0]: pair[1] for pair in pairs if len(pair) == 2}


class ApiKeyAuthentication(BaseAuthentication):
    """A request with ``X-API-Key`` is its key's user's, or refused."""

    scheme = "ApiKey"

    async def authenticate(self, connection: HTTPConnection) -> Credentials | None:
        """The key's user; None without the header, so other classes try."""
        key = connection.headers.get("x-api-key")
        if key is None:
            return None
        username = keys().get(digest(key))
        if username is None:
            raise AuthenticationFailed("Unknown API key.")
        async with AuthUnitOfWork() as uow:
            user = await uow.users.get_by_natural_key(username)
        if user is None or not user.is_active:
            raise AuthenticationFailed("The API key's user is inactive or gone.")
        return Credentials(user, scheme="api_key")
```

Name it after the class that issues logins' tokens, which stays first:

```python title="src/config/settings/base.py"
AUTH: dict[str, object] = {
    "AUTHENTICATION_CLASSES": [
        "domainkit.contrib.auth.authentication.TokenAuthentication",
        "config.api_keys.ApiKeyAuthentication",
    ],
}
```

A key that does not hold answers 401, is logged as refused credentials,
and counts against the address's `RATE_LIMIT`, past which the address's
keys are refused unread for a while; a request without the header goes
on to the other classes. Make the service's user without a
password (`createsuperuser` is for people): it logs in with its key only,
and holds the permissions you grant it.

## A test of the key

```python title="tests/test_api_keys.py (new file)"
"""A service calls with its key; a key nobody was given is refused."""

from typing import Any

import pytest
from config.api_keys import digest
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


async def test_a_service_calls_as_its_user(
    db: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The key's digest names the user; the key itself is stored nowhere."""
    monkeypatch.setenv("API_KEYS", f"{digest(KEY)}=billing")
    async with AuthUnitOfWork() as uow:
        await uow.users.create_user("billing")
        await uow.commit()

    status, body = await me(KEY)

    assert (status, body["username"]) == (200, "billing")


async def test_a_key_nobody_was_given_is_refused(
    db: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """401, as a wrong password is."""
    monkeypatch.setenv("API_KEYS", f"{digest(KEY)}=billing")

    status, body = await me("a-guessed-key")

    assert (status, body["code"]) == (401, "authentication_failed")
```

```bash
ruff check --fix .
ruff format --check .
pytest
```

To give a service a key, make one
(`python -c "import secrets; print(secrets.token_urlsafe(32))"`), hand
it to the service, and deploy its digest
(`python -c "from config.api_keys import digest; print(digest('<key>'))"`)
in `API_KEYS`.
