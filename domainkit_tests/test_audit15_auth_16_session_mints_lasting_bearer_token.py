"""AUTH-16: a browser session mints a bearer token that never expires, unasked.

``POST /auth/tokens`` needs only credentials without scopes: no password,
no step-up. A browser session's cookie is HttpOnly so that scripts never
read a credential (``cookies.py``), but any script on the page can send
``POST /auth/tokens`` with the CSRF token it reads from ``dk_csrf``, and
gets back, in the body, an unscoped opaque token with
``expires_in_days: null``: a credential that works until revoked, is not
a login (``IDLE_TIMEOUT``, ``MAX_LOGINS`` and logging out leave it), and
is read by the script. Under the PCI preset, a session left open long
enough to be used (8.2.8) turns into a credential that never idles.
"""

from domainkit_tests.conftest import Api
from domainkit_tests.test_auth_cookies import TRUSTED, browser, start
from domainkit_tests.test_auth_http import bearer, create_user


def test_a_browser_session_cannot_mint_a_lasting_token_unasked(
    identity: Api,
) -> None:
    """Minting needs the user to authenticate again, as other credential acts do."""
    create_user("alice")
    client = browser(identity, PASSWORD_PRESET="pci-dss-4")
    csrf = start(client)

    minted = client.post(
        "/auth/tokens",
        json={"name": "exfiltrated", "expires_in_days": None},
        headers={**TRUSTED, "X-CSRF-Token": csrf},
    )
    if minted.status_code == 201:
        token = minted.json()["token"]
        logged_out = client.delete(
            "/auth/session", headers={**TRUSTED, "X-CSRF-Token": csrf}
        )
        assert logged_out.status_code == 204
        after = client.get("/whoami", headers=bearer(token)).json()
        assert minted.json()["expires_at"] is None
        assert after == {"user": "alice", "scheme": "token"}
    assert minted.status_code in {401, 403}, (
        "a cookie session minted a never-expiring bearer token, without "
        "authenticating again, that outlived the session's logout"
    )
