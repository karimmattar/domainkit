"""The rungs: each a request on one stack, from a bare ASGI app upwards.

The first rungs need no database. The rest are the paths a real API serves
most, behind the stack ``domainkit init`` writes, against PostgreSQL.
"""

from dataclasses import dataclass
from typing import Final, Literal

POSTGRES_VARIABLE: Final = "DOMAINKIT_BENCH_POSTGRES_URL"
"""The database the database rungs make their schemas in; never on a command line."""
UNLIMITED: Final = "1000000000/minute"
"""A rate limit that counts every request and refuses none."""
IDEMPOTENCY_MIDDLEWARE: Final = (
    "domainkit.contrib.idempotency.middleware.IdempotencyMiddleware"
)

type App = Literal["asgi", "fastapi", "domainkit"]
type Stack = Literal["none", "default", "template", "template+idempotency"]


@dataclass(frozen=True, slots=True)
class Rung:
    """One scenario: an application, its middleware, and the request sent."""

    name: str
    title: str
    app: App = "domainkit"
    stack: Stack = "none"
    """``default``: ``DEFAULT_MIDDLEWARE``; ``template``: what ``init`` writes."""
    path: str = "/hello"
    """``{item}`` stands for the id of an item made with the database."""
    method: str = "GET"
    token: bool = False
    """Send the opaque token of a user who may view and add items."""
    json_log: bool = False
    """Write the access log as JSON lines (to ``/dev/null``)."""
    idempotency_key: bool = False
    """Send a new ``Idempotency-Key`` with each request."""
    rate_limit: str = UNLIMITED
    status: int = 200
    """The status every measured request must get."""
    database: bool = False
    """Needs PostgreSQL."""


LADDER: Final = (
    Rung("asgi", "Bare ASGI app", app="asgi"),
    Rung("fastapi", "Plain FastAPI", app="fastapi"),
    Rung("bare", "domainkit, no middleware"),
    Rung("nested", "... route two routers deep", path="/a/b/hello"),
    Rung("default", "Default MIDDLEWARE", stack="default"),
    Rung("default-json", "... plus JSON access log", stack="default", json_log=True),
    Rung("template", "init template stack", stack="template"),
    Rung("template-json", "... plus JSON access log", stack="template", json_log=True),
    Rung(
        "me",
        "GET /me, opaque token",
        stack="template",
        path="/me",
        token=True,
        database=True,
    ),
    Rung(
        "read",
        "Permission-guarded read, unit of work",
        stack="template",
        path="/items/{item}",
        token=True,
        database=True,
    ),
    Rung(
        "write",
        "Write, unit of work",
        stack="template",
        path="/items",
        method="POST",
        token=True,
        status=201,
        database=True,
    ),
    Rung(
        "keyed-write",
        "Keyed write (Idempotency-Key)",
        stack="template+idempotency",
        path="/items",
        method="POST",
        token=True,
        idempotency_key=True,
        status=201,
        database=True,
    ),
    Rung(
        "throttled",
        "429 for a valid token",
        stack="template",
        path="/me",
        token=True,
        rate_limit="1/hour",
        status=429,
        database=True,
    ),
)
"""Every rung, in the order the ladder climbs."""
RUNGS: Final = {rung.name: rung for rung in LADDER}
