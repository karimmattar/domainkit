"""SEC-15c-1: the PCI preset lets a JSON Web Token login idle past 15 minutes.

PCI DSS v4.0 8.2.8 (docs/auth/pci-dss.md): "auth.E010 refuses a timeout
that could end later". A JWT login's idleness is only checked at its next
refresh, so an access token works, unused, for ``AUTH.JWT.ACCESS_TOKEN_TTL``:
E010 does not look at it, and a project that follows the preset with a
one-hour access token starts, and serves a login idle for 30 minutes.
"""

from datetime import timedelta
from typing import Any

import pytest

from domainkit.checks import CheckFailed
from domainkit.clock import freeze_time
from domainkit_tests.conftest import Api, UseAuth
from domainkit_tests.test_auth_http import JWT, bearer, create_user, login

PCI = {"PASSWORD_PRESET": "pci-dss-4"}
LONG_JWT: dict[str, Any] = {
    **JWT,
    "JWT": {**JWT["JWT"], "ACCESS_TOKEN_TTL": timedelta(hours=1)},
}


def test_e010_refuses_jwt_access_tokens_that_outlive_the_idle_bound(
    use_auth: UseAuth,
) -> None:
    """An access token living an hour lets a login idle an hour: E010."""
    with pytest.raises(CheckFailed) as refused:
        use_auth(**PCI, **LONG_JWT)

    assert any(
        getattr(each, "id", None) == "auth.E010" for each in refused.value.messages
    )


def test_a_jwt_login_idle_for_30_minutes_is_refused_under_the_preset(
    identity: Api,
) -> None:
    """8.2.8: idle for more than 15 minutes, the user authenticates again."""
    with freeze_time("2027-01-04T10:00:00Z") as clock:
        try:
            client = identity(**PCI, **LONG_JWT)
        except CheckFailed:
            return  # The safe behaviour: the project does not start.
        create_user("alice")
        headers = bearer(login(client, "alice")["access_token"])
        assert client.get("/whoami", headers=headers).json()["user"] == "alice"
        clock.tick(timedelta(minutes=30))
        idle = client.get("/whoami", headers=headers)

    assert idle.status_code == 401, idle.json()
