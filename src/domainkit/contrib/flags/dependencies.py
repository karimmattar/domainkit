"""``RequireFlag``: serve a route only while a flag is on for the caller."""

from fastapi import status
from starlette.requests import HTTPConnection

from domainkit.http.errors import APIError

from .store import flag_enabled


class RequireFlag:
    """A FastAPI dependency: 404 unless flag ``name`` is on for the caller.

    The caller is the user ``AuthenticationMiddleware`` found, if installed.
    A route behind an off flag looks like no route at all. It is not an
    access rule: the route still needs ``Require(...)`` or ``Public``::

        @router.get("/beta", dependencies=[Depends(RequireFlag("beta")), Public])
    """

    def __init__(self, name: str) -> None:
        """Check flag ``name`` on every request."""
        self.name = name

    async def __call__(self, connection: HTTPConnection) -> None:
        """Refuse the request while the flag is off for its user."""
        if not await flag_enabled(self.name, connection.scope.get("user")):
            raise APIError(status.HTTP_404_NOT_FOUND, "Not found.", code="not_found")
