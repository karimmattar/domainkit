# Running behind a proxy

Behind a load balancer every connection comes from the proxy. The
application then takes each caller's address, scheme, and request id from
the headers the proxy sets, and only from the proxies `TRUSTED_PROXIES`
lists, so a caller cannot claim another's address or request id
([ADR 0072](../adr/0072-walk-proxy-headers-from-the-right-and-trust-unix-sockets.md),
[ADR 0099](../adr/0099-take-request-ids-from-trusted-proxies-only.md),
[operations.md](../operations.md#request-ids)).

You need a project with `domainkit[sqlite,testing]` installed. Every step
here is run as written by `domainkit_tests/test_recipes.py`.

## The proxy

TLS ends at the proxy, which sets the caller's address, the scheme, and a
request id of its own, over whatever the caller sent. With nginx:

```nginx
location / {
    proxy_pass http://app:8000;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-For $remote_addr;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_set_header X-Request-ID $request_id;
}
```

Run uvicorn with `--no-proxy-headers` (the image `init` writes does), so
the application alone decides whom to believe.

## The application

```bash
domainkit init edgeAPI edge
cd edge
```

The production settings `init` writes read `TRUSTED_PROXIES` from the
environment: the networks the proxies connect from, comma-separated
(`EDGE_API_TRUSTED_PROXIES=10.0.0.0/8`), or empty when callers connect
directly. `PROXY_SCHEME_HEADER` (`X-Forwarded-Proto`) and
`ACCEPT_REQUEST_ID` (`"proxies"`) keep their defaults. `check` says where
the scheme comes from:

```bash
EDGE_API_SETTINGS_MODULE=config.settings.production EDGE_API_ALLOWED_HOSTS=api.example.com EDGE_API_DATABASE_URL=postgresql+asyncpg://app@db/edge EDGE_API_REDIS_URL=rediss://:s3cret@cache:6380/0 EDGE_API_SECRET_KEY=a-production-secret-of-fifty-random-characters-or-more EDGE_API_TRUSTED_PROXIES=10.0.0.0/8 python manage.py check
```

It prints `Info: security.I001: HTTPS is not checked: ...`: the request's
scheme is taken from the `X-Forwarded-Proto` header of `TRUSTED_PROXIES`
only. The tests trust a proxy network too:

```python title="src/config/settings/test.py"
TRUSTED_PROXIES = ["10.0.0.0/8"]
```

## A test of whom the application believes

```python title="tests/test_proxies.py (new file)"
"""Request ids and addresses come from trusted proxies only."""

import logging
import re

import pytest
from domainkit.http import get_asgi_application
from domainkit.testing import AsyncTestClient

pytestmark = pytest.mark.anyio
PROXY = ("10.0.0.7", 51000)
CALLER = ("203.0.113.9", 51000)


async def answer_id(peer: tuple[str, int], request_id: str) -> str:
    """The request id the answer names, for a request from ``peer``."""
    async with AsyncTestClient(get_asgi_application(), client=peer) as client:
        answer = await client.get("/health", headers={"X-Request-ID": request_id})
    assert answer.status_code == 200
    return answer.headers["x-request-id"]


async def test_the_proxys_request_id_names_the_request(db: None) -> None:
    """Logs, error reports, and audit rows carry the proxy's id."""
    assert await answer_id(PROXY, "edge-7f3a") == "edge-7f3a"


async def test_a_callers_own_request_id_is_replaced(db: None) -> None:
    """Connecting directly, a caller cannot name its requests."""
    given = await answer_id(CALLER, "edge-7f3a")

    assert given != "edge-7f3a"
    assert re.fullmatch(r"[0-9a-f]{32}", given)


@pytest.mark.parametrize(
    ("peer", "recorded"), [(PROXY, "198.51.100.4"), (CALLER, "203.0.113.9")]
)
async def test_the_caller_is_whom_a_trusted_proxy_names(
    db: None,
    caplog: pytest.LogCaptureFixture,
    peer: tuple[str, int],
    recorded: str,
) -> None:
    """The proxy's X-Forwarded-For is believed; a caller's own is not."""
    caplog.set_level(logging.INFO, "domainkit.access")
    async with AsyncTestClient(get_asgi_application(), client=peer) as client:
        await client.get("/auth/me", headers={"X-Forwarded-For": "198.51.100.4"})

    [line] = [each for each in caplog.records if each.name == "domainkit.access"]
    assert line.client_ip == recorded
```

```bash
ruff check --fix .
ruff format --check .
pytest
```

Of several `X-Forwarded-For` addresses, the application walks from the
right and takes the first that is not a trusted proxy's: the nearest
caller the proxies saw. That address is what rate limits, login throttles,
the audit trail, and the logs record. A proxy that cannot set
`X-Request-ID` would pass a caller's on: set `ACCEPT_REQUEST_ID = "never"`
then, and every request gets an id of its own.
