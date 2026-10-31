"""SEC-15c-2: under the PCI preset, an open live connection keeps an idle login alive.

docs/auth/pci-dss.md (8.2.8): "After 13 minutes without a request, a login
token or a browser session ends." An open WebSocket or event stream is
re-authorized every ``LIVE_REAUTH_INTERVAL`` (``AuthenticationMiddleware.
reauthenticate``), and that re-check authenticates the token as a request
does, writing its last use. A tab left open on a live page, with nobody at
the keyboard, so keeps the login from ever idling: an hour later the
token still authenticates a plain request.
"""

from datetime import timedelta
from typing import Any

from starlette.requests import HTTPConnection

from domainkit.clock import freeze_time
from domainkit.contrib.auth.authentication import get_authenticators
from domainkit.contrib.auth.middleware import AuthenticationMiddleware
from domainkit.management.base import run_async
from domainkit_tests.conftest import Api
from domainkit_tests.test_auth_http import bearer, create_user, login
from domainkit_tests.test_auth_principals import scope

PCI = {"PASSWORD_PRESET": "pci-dss-4"}


async def _credentials(headers: dict[str, str]) -> Any:  # noqa: ANN401
    """What the configured classes make of a request with ``headers``."""
    connection = HTTPConnection(scope(headers))
    for authenticator in get_authenticators():
        found = await authenticator.authenticate(connection)
        if found is not None:
            return found
    return None


def test_live_rechecks_do_not_keep_an_idle_login_alive(identity: Api) -> None:
    """No request for an hour: the login ends, whatever a socket re-checks."""
    with freeze_time("2027-01-04T10:00:00Z") as clock:
        client = identity(**PCI)
        create_user("alice")
        headers = bearer(login(client, "alice")["access_token"])
        assert client.get("/whoami", headers=headers).json()["user"] == "alice"

        middleware = AuthenticationMiddleware(app=lambda *args: None)  # type: ignore[arg-type]
        socket = {**scope(headers), "type": "websocket"}
        socket["auth"] = run_async(_credentials(headers))
        for _ in range(15):  # An hour of re-checks, four minutes apart.
            clock.tick(timedelta(minutes=4))
            run_async(middleware.reauthenticate(socket))

        idle = client.get("/whoami", headers=headers)

    assert idle.status_code == 401, idle.json()
