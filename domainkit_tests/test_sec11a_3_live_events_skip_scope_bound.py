"""SEC-11a-3: live events hand a scoped credential's user to the event rule.

``SCOPED_CREDENTIALS = "refuse"`` (the default since 0.11, AUTH-01) holds
credentials limited to scopes to routes that declare a rule that checks
scopes, wherever the principal is read: ``Require``, ``CurrentUser``,
``AuthenticatedUser``, and ``current_user``, which a provider's own
dependencies call (SEC-10a-4). ``live_events`` reads it a way of its own:
each event's rule is asked about ``scope["user"]`` as it is, without the
check. On a route anyone may open (``Public``), whose events each go to
the user they are about (the module's ``IsCustomer``, the tests'
``OwnEvents``), a token limited to ``shop.view_product`` that
``current_user`` refuses on the same route is streamed its user's events.
"""

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any, ClassVar, Final
from uuid import UUID, uuid4

import pytest
from fastapi.routing import APIWebSocketRoute
from starlette.requests import HTTPConnection
from starlette.websockets import WebSocket

from domainkit.contrib.auth.events import UserPermissionGranted
from domainkit.ddd import DomainEvent, EventBus, PermissionDenied
from domainkit.http.access import Public, current_user
from domainkit.http.live_events import LiveEvents
from domainkit_tests.sample_live_routes import OwnEvents

SCOPES: Final = frozenset({"shop.view_product"})
"""What the integration's token may do: view products, nothing about the user."""


@dataclass(frozen=True, slots=True)
class Alice:
    """The token's user, as the access rules see one."""

    id: UUID
    is_authenticated: ClassVar[bool] = True
    is_active: ClassVar[bool] = True


@dataclass(frozen=True, slots=True)
class ScopedToken:
    """Credentials limited to ``SCOPES``, as a personal token's are."""

    scopes: frozenset[str] | None = SCOPES


async def own_events(websocket: WebSocket) -> None:
    """A route anyone may open: each event goes to the user it is about."""


ROUTE: Final = APIWebSocketRoute("/orders/live", own_events, dependencies=[Public])


def scoped_connection(user: Alice) -> HTTPConnection:
    """A connection to ``ROUTE`` with the scoped token, as the middleware leaves it."""
    return HTTPConnection(
        {
            "type": "websocket",
            "headers": [],
            "route": ROUTE,
            "user": user,
            "auth": ScopedToken(),
        }
    )


async def first_delivered(events: AsyncIterator[DomainEvent]) -> DomainEvent | None:
    """The event the subscription hands out, once the loop has run its checks."""
    pending = asyncio.ensure_future(anext(events))
    for _ in range(20):  # The rule awaits nothing slow: a few turns suffice.
        await asyncio.sleep(0)
    if not pending.done():
        pending.cancel()
        return None
    return pending.result()


@pytest.mark.anyio
async def test_live_events_bound_scoped_credentials_as_current_user_does() -> None:
    """``current_user`` refuses the token on the route; the event stream must too.

    The fixed code refuses the subscription, or hands out nothing; the
    branch streams Alice's own events to the token limited to scopes.
    """
    alice = Alice(uuid4())
    with pytest.raises(PermissionDenied):
        current_user(scoped_connection(alice))  # The promise of AUTH-01.

    bus = EventBus()
    hub = LiveEvents(bus)
    delivered: Any = None
    try:
        async with hub.subscribe(
            scoped_connection(alice), UserPermissionGranted, rule=OwnEvents()
        ) as events:
            await bus.publish(
                UserPermissionGranted(user_id=alice.id, permission="shop.add_order")
            )
            delivered = await first_delivered(events)
    except PermissionDenied:
        delivered = None

    assert delivered is None, (
        "a credential limited to scopes, refused by current_user on this route, "
        f"was streamed its user's event: {delivered!r}"
    )
