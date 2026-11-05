# Replacing the key that signs JSON Web Tokens

This recipe replaces the key that signs your JSON Web Tokens, without
logging out any client.

## The problem

A signing key should not live for ever. You replace it on a schedule, or
when a person who knew it leaves. But every client holds a token that the
old key signed. If the service stopped trusting the old key at once,
every client would be refused at once.

domainkit avoids that. Every JSON Web Token names the key that signed it
(`kid`). So a new key can sign, while the old key still verifies what it
signed, for one token lifetime after the rollout ends
([ADR 0097](../adr/0097-name-the-key-of-every-json-web-token-and-retire-keys-within-a-token-lifetime.md),
[authentication](../auth/authentication.md#replacing-a-key)).

You need a project with `domainkit[jwt,sqlite,testing]` installed. Every
step here is run as written by `domainkit_tests/test_recipes.py`.

## The solution

### Step 1: make a project

```bash
domainkit init keysAPI keys
cd keys
python manage.py makemigrations -m "auth"
python manage.py migrate
```

### Step 2: read the keys from the environment

In production, the signing key comes from the environment. So do the
keys it replaced, with the time the rollout of the new key ended:

```python title="src/config/settings/production.py"
AUTH = {
    "AUTHENTICATION_CLASSES": [
        "domainkit.contrib.auth.authentication.JWTAuthentication",
        "domainkit.contrib.auth.authentication.TokenAuthentication",
    ],
    "JWT": {
        "SIGNING_KEY": env.str("JWT_SIGNING_KEY"),
        "PREVIOUS_KEYS": [
            {"KEY": key, "RETIRED_AT": env.str("JWT_KEY_RETIRED_AT")}
            for key in env.list("JWT_PREVIOUS_KEYS", [])
        ],
    },
}
```

The tests sign with a key of their own:

```python title="src/config/settings/test.py"
AUTH = {
    **AUTH,
    "AUTHENTICATION_CLASSES": [
        "domainkit.contrib.auth.authentication.JWTAuthentication",
        "domainkit.contrib.auth.authentication.TokenAuthentication",
    ],
    "JWT": {"SIGNING_KEY": "tests-only-jwt-signing-key-of-32-bytes"},
}
```

An HMAC key (`HS256`, the default) has at least 32 bytes. Make one with
`python -c "import secrets; print(secrets.token_urlsafe(32))"`.

### Step 3: check the production settings

`check` reads the production settings as a deployment would set them:

```bash
KEYS_API_SETTINGS_MODULE=config.settings.production KEYS_API_ALLOWED_HOSTS=api.example.com KEYS_API_DATABASE_URL=postgresql+asyncpg://app@db/keys KEYS_API_REDIS_URL=rediss://:s3cret@cache:6380/0 KEYS_API_SECRET_KEY=a-production-secret-of-fifty-random-characters-or-more KEYS_API_TRUSTED_PROXIES=10.0.0.0/8 KEYS_API_JWT_SIGNING_KEY=the-new-key-of-thirty-two-bytes-or-more KEYS_API_JWT_PREVIOUS_KEYS=the-old-key-of-thirty-two-bytes-or-more KEYS_API_JWT_KEY_RETIRED_AT=2026-10-01T09:30:00Z python manage.py check
```

It prints `System check identified no issues`.

### Step 4: test the rollout

The test logs in while the old key signs, then serves the application
with the new key and the old one retired. It checks that the old token
works through the rollout, and is refused after it:

```python title="tests/test_jwt_keys.py (new file)"
"""A token of the old key works through the rollout, then is refused."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any

import jwt
import pytest
from domainkit.conf import override_settings, settings
from domainkit.contrib.auth import AuthUnitOfWork
from domainkit.http import get_asgi_application
from domainkit.testing import AsyncTestClient, FreezeTime

pytestmark = pytest.mark.anyio
OLD_KEY = "tests-only-the-old-jwt-key-of-32-bytes"
ROLLOUT_ENDED = datetime(2026, 10, 1, 9, 30, tzinfo=UTC)
PASSWORD = "correct-horse-battery"


@asynccontextmanager
async def serving(keys: dict[str, Any]) -> AsyncIterator[AsyncTestClient]:
    """The application as processes started with these keys serve it."""
    with override_settings(AUTH={**settings.AUTH, "JWT": keys}):
        async with AsyncTestClient(get_asgi_application()) as client:
            yield client


async def login(client: AsyncTestClient) -> str:
    """Log in as ann; the access token."""
    credentials = {"identifier": "ann@example.com", "password": PASSWORD}
    answer = await client.post("/auth/login", json=credentials)
    assert answer.status_code == 200, answer.json()
    token: str = answer.json()["access_token"]
    return token


async def me(client: AsyncTestClient, token: str) -> int:
    """The status of ``GET /auth/me`` with ``token``."""
    answer = await client.get("/auth/me", headers={"Authorization": f"Bearer {token}"})
    return answer.status_code


async def test_the_old_keys_tokens_work_through_the_rollout(
    db: None, freeze_time: FreezeTime
) -> None:
    """Signed with the old key, accepted after the new one signs, then refused.

    The old key's tokens lasted an hour: retiring it refuses them unexpired.
    """
    async with AuthUnitOfWork() as uow:
        await uow.users.create_user("ann", PASSWORD, email="ann@example.com")
        await uow.commit()
    new = settings.AUTH["JWT"]
    retired = {"KEY": OLD_KEY, "RETIRED_AT": ROLLOUT_ENDED}
    clock = freeze_time(ROLLOUT_ENDED - timedelta(minutes=1))
    hour = timedelta(hours=1)
    async with serving({"SIGNING_KEY": OLD_KEY, "ACCESS_TOKEN_TTL": hour}) as before:
        old = await login(before)

    clock.tick(timedelta(minutes=2))  # The new key signs everywhere now.
    async with serving({**new, "PREVIOUS_KEYS": [retired]}) as after:
        assert await me(after, old) == 200
        fresh = await login(after)
        kids = {jwt.get_unverified_header(token)["kid"] for token in (old, fresh)}
        assert len(kids) == 2  # Each names the key that signed it.
        clock.move_to(ROLLOUT_ENDED + timedelta(minutes=5, seconds=30))
        # Past RETIRED_AT and the new settings' token lifetime: the old key
        # verifies nothing now, though its token has most of an hour left.
        assert await me(after, old) == 401
        assert await me(after, fresh) == 200
```

Run the checks and the tests:

```bash
ruff check --fix .
ruff format --check .
pytest
```

## The rollout

When you replace the key in production, follow these steps:

1. Make a new key. Deploy it as `JWT_SIGNING_KEY`, with the old one in
   `JWT_PREVIOUS_KEYS`, and `JWT_KEY_RETIRED_AT` set to when the rollout
   ends: when the last process stops signing with the old key. That time
   is at most a day ahead (`auth.E009` refuses a later one).
2. Tokens that the old key signed keep working until `JWT_KEY_RETIRED_AT`
   plus `ACCESS_TOKEN_TTL` (5 minutes) and `LEEWAY`. Then they are
   refused. Refresh tokens are not signed: a client that is refused
   refreshes, and gets a token of the new key.
3. After that time, remove `JWT_PREVIOUS_KEYS`, and destroy the old key.

!!! warning
    A key that leaked is never listed in `JWT_PREVIOUS_KEYS`. Replace it
    alone: every client then refreshes once.

## How it works

- Each token's header names its key (`kid`), so the service knows which
  key to verify it with.
- `AUTH.JWT.SIGNING_KEY` signs every new token. `PREVIOUS_KEYS` only
  verify, and each one stops at its `RETIRED_AT` plus the token lifetime.
  So a token signed with an old key cannot outlive the rollout by more
  than one lifetime, even when the token asked for longer.
- `check` refuses a previous key that is the signing key, or that is
  listed twice (`auth.E008`), and one that retires more than a day ahead
  (`auth.E009`).

## See also

- [Authentication](../auth/authentication.md#replacing-a-key): JSON Web
  Tokens and their settings.
- [Checks](../reference/checks.md#errors-of-domainkitcontribauth):
  `auth.E008` and `auth.E009`.
- [Signing](../reference/signing.md#replacing-secret_key): replacing
  `SECRET_KEY`, which signs other values.
