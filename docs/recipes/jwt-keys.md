# Replacing the key that signs JSON Web Tokens

Every JSON Web Token names the key that signed it (`kid`). A new key can
then sign while the old one still verifies what it signed, for one token
lifetime after the rollout ends, and no client is logged out
([ADR 0097](../adr/0097-name-the-key-of-every-json-web-token-and-retire-keys-within-a-token-lifetime.md),
[authentication](../auth/authentication.md#replacing-a-key)).

You need a project with `domainkit[jwt,sqlite,testing]` installed. Every
step here is run as written by `domainkit_tests/test_recipes.py`.

## Tokens signed with a key from the environment

```bash
domainkit init keysAPI keys
cd keys
python manage.py makemigrations -m "auth"
python manage.py migrate
```

In production the signing key comes from the environment, and so do the
keys it replaced, with the time the rollout of the new one ended:

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

An HMAC key (`HS256`, the default) has at least 32 bytes:
`python -c "import secrets; print(secrets.token_urlsafe(32))"`.

## The rollout

1. Make a new key. Deploy it as `JWT_SIGNING_KEY`, with the old one in
   `JWT_PREVIOUS_KEYS`, and `JWT_KEY_RETIRED_AT` set to when the rollout
   ends: when the last process stops signing with the old key. That is at
   most a day ahead (`auth.E009` refuses later).
2. Tokens the old key signed keep working until `JWT_KEY_RETIRED_AT` plus
   `ACCESS_TOKEN_TTL` (5 minutes) and `LEEWAY`; then they are refused.
   Refresh tokens are not signed: a client that is refused refreshes, and
   gets a token of the new key.
3. After that time, remove `JWT_PREVIOUS_KEYS` and destroy the old key.

A key that leaked is not listed: replace it alone, and every client
refreshes once.

`check` reads the production settings as a deployment would set them:

```bash
KEYS_API_SETTINGS_MODULE=config.settings.production KEYS_API_ALLOWED_HOSTS=api.example.com KEYS_API_DATABASE_URL=postgresql+asyncpg://app@db/keys KEYS_API_REDIS_URL=rediss://:s3cret@cache:6380/0 KEYS_API_SECRET_KEY=a-production-secret-of-fifty-random-characters-or-more KEYS_API_TRUSTED_PROXIES=10.0.0.0/8 KEYS_API_JWT_SIGNING_KEY=the-new-key-of-thirty-two-bytes-or-more KEYS_API_JWT_PREVIOUS_KEYS=the-old-key-of-thirty-two-bytes-or-more KEYS_API_JWT_KEY_RETIRED_AT=2026-10-01T09:30:00Z python manage.py check
```

## A test of the rollout

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
    credentials = {"username": "ann", "password": PASSWORD}
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
        await uow.users.create_user("ann", PASSWORD)
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

```bash
ruff check --fix .
ruff format --check .
pytest
```
