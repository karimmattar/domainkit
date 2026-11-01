"""SEC-16a-3: ``live.W002`` misses live routes guarded by ``AuthenticatedUser``.

WEB-24's fix warns (``check``, ``live.W002``) about a route that keeps
live connections open without re-checking who holds them:
``LiveLimits(reauth_interval=None)`` on a route with an access rule.
``unchecked_routes`` counts a route as guarded only if it depends on
``Require``. A route that declares its rule the other way the framework
accepts (``open_routes``, DSGN-17: "a dependency marked ``reads_principal``
that refuses anonymous callers (``AuthenticatedUser``)") is passed over,
though it is the usual WebSocket signature (``user: AuthenticatedUser``)
and its connection keeps a revoked token or a deactivated user for its
whole lifetime just the same.
"""

from fastapi import Depends, FastAPI, WebSocket

from domainkit.contrib.auth.access import AuthenticatedUser
from domainkit.http.live import LiveLimits, unchecked_routes


def test_a_route_read_by_authenticated_user_is_warned_without_rechecks() -> None:
    """``user: AuthenticatedUser`` is a rule: turning re-checks off is named."""
    app = FastAPI()

    @app.websocket("/feed", dependencies=[Depends(LiveLimits(reauth_interval=None))])
    async def feed(websocket: WebSocket, user: AuthenticatedUser) -> None:
        """Nothing."""

    assert [each.split(" turns")[0] for each in unchecked_routes(app.routes)] == [
        "WEBSOCKET /feed"
    ]
