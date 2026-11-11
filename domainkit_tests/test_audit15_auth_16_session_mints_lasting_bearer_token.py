"""AUTH-16: a browser session mints a bearer token that never expires, unasked.

``POST /auth/tokens`` needed only credentials without scopes: no password,
no step-up. A browser session's cookie is HttpOnly so that scripts never
read a credential (``cookies.py``), but any script on the page could send
``POST /auth/tokens`` with the CSRF token it reads from ``dk_csrf``, and
get back, in the body, an unscoped opaque token with
``expires_in_days: null``: a credential that works until revoked, is not
a login (``IDLE_TIMEOUT``, ``MAX_LOGINS`` and logging out leave it), and
is read by the script. Under the PCI preset, a session left open long
enough to be used (8.2.8) turned into a credential that never idles.

The decision (PLAN.md): minting needs the user to authenticate again (a
step-up with a second factor, the password without one), and a token
works ``AUTH.PERSONAL_TOKEN_MAX_AGE`` (90 days) at most by default. The
first version asserted a 401 or 403; a user without a second factor is
asked for the password, ``400 password_required``, as ``/step-up`` asks.
"""

from datetime import timedelta

from domainkit.clock import now
from domainkit_tests.conftest import Api
from domainkit_tests.support.auth import PASSWORD, create_user
from domainkit_tests.test_auth_cookies import TRUSTED, browser, start


def test_a_browser_session_cannot_mint_a_lasting_token_unasked(
    identity: Api,
) -> None:
    """Minting needs the user to authenticate again, and the token ends."""
    create_user("alice")
    client = browser(identity, PRESET="pci-dss-4")
    csrf = start(client)
    headers = {**TRUSTED, "X-CSRF-Token": csrf}

    unasked = client.post("/auth/tokens", json={"name": "exfiltrated"}, headers=headers)
    lasting = client.post(
        "/auth/tokens",
        json={"name": "lasting", "expires_in_days": None, "password": PASSWORD},
        headers=headers,
    )
    asked = client.post(
        "/auth/tokens", json={"name": "ci", "password": PASSWORD}, headers=headers
    )

    assert (unasked.status_code, unasked.json()["code"]) == (400, "password_required")
    assert (lasting.status_code, lasting.json()["code"]) == (
        422,
        "token_lifetime_too_long",
    )
    assert asked.status_code == 201
    expires = asked.json()["expires_at"]
    assert expires is not None
    assert expires <= (now() + timedelta(days=90, seconds=5)).isoformat()
