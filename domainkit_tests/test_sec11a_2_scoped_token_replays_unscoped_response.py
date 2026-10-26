"""SEC-11a-2: a token limited to scopes is replayed responses its scopes refuse.

``SCOPED_CREDENTIALS = "refuse"`` (the default since 0.11, AUTH-01)
answers 403 to credentials limited to scopes on a route that checks no
scope. ``IdempotencyMiddleware`` runs before the route, and scopes keys
to the actor: the user, whatever credentials the request carries. A
personal token limited to ``shop.view_product`` (one handed to an
integration) that sends a key its user's own login used on such a route
is replayed that response, with its status and body, without the route
or its rules running: the refusal is skipped. A different body would tell
it the key is taken (``idempotency_key_reused``).
"""

from typing import Any, Final

from domainkit_tests.conftest import Api
from domainkit_tests.test_auth_http import bearer, create_user, grant, login

VIEW: Final = "shop.view_product"
KEY: Final = {"Idempotency-Key": "note-2026-11-02"}
"""A key the user's client derives from the day: predictable, as DATA-09 allows."""


def test_a_scoped_token_is_not_replayed_what_its_scopes_refuse(identity: Api) -> None:
    """Alice's login writes a note; her read-only token is refused one, and its replay.

    The fixed code refuses the scoped token's keyed request as it refuses
    the same request without the key; the branch replays Alice's 200.
    """
    create_user("alice")
    grant("alice", VIEW)
    client = identity()
    session = bearer(login(client, "alice")["access_token"])
    made = client.post(
        "/auth/tokens", json={"name": "integration", "scopes": [VIEW]}, headers=session
    )
    scoped = bearer(made.json()["token"])

    plain = client.post("/notes", headers=scoped)
    written = client.post("/notes", headers={**session, **KEY})
    replayed: Any = client.post("/notes", headers={**scoped, **KEY})

    assert made.status_code == 201
    assert plain.status_code == 403  # The route refuses the scoped token.
    assert written.status_code == 200
    assert replayed.headers.get("Idempotent-Replayed") is None, (
        "the token limited to scopes was replayed the response of a route "
        f"that refuses it: {replayed.status_code} {replayed.json()}"
    )
    assert replayed.status_code == 403
