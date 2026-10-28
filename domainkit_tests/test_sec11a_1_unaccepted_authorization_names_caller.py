"""SEC-11a-1: an ``Authorization`` header that authenticates nobody names a caller.

``IDEMPOTENCY.ANONYMOUS = "refuse"`` (the default since 0.11, DATA-09)
answers 400 to a key that names no caller, and since SEC-10b-7 a cookie
many callers send alike (consent, locale) names nobody. A credential
header is still taken to name a caller whatever it holds, even when
auth's ``AuthenticationMiddleware`` read it and found no one.

Browser applications commonly send ``Authorization: Bearer null`` (or
``undefined``) while logged out. No authentication class recognizes it,
so the request is anonymous, with no actor; yet the key is kept, scoped
to the client address and that shared header. Two logged-out people
behind one NAT are then one caller again: the second is replayed the
first's response, as in DATA-09's proof.
"""

from typing import Any, Final

from domainkit_tests import sample_auth_routes
from domainkit_tests.conftest import Api

NEEDS_CALLER: Final = "idempotency_key_needs_caller"
OFFICE: Final = "203.0.113.7"
"""One public address, shared by everyone behind the office's NAT."""


def post(client: Any, body: dict[str, str]) -> Any:  # noqa: ANN401 - a response.
    """POST ``/count`` with a predictable key, as a logged-out SPA sends it."""
    return client.post(
        "/count",
        json=body,
        headers={"Idempotency-Key": "order-1", "Authorization": "Bearer null"},
    )


def test_a_header_that_authenticates_nobody_names_no_caller(api: Api) -> None:
    """Ann and Bob, both logged out, behind one address: neither is named.

    The fixed code answers both 400 ``idempotency_key_needs_caller``, as it
    answers the same requests without the header; the branch serves Ann
    and replays her response to Bob.
    """
    sample_auth_routes.counted.clear()
    ann = api(client=(OFFICE, 40001))
    bob = api(client=(OFFICE, 40002))
    who = ann.get("/whoami", headers={"Authorization": "Bearer null"}).json()

    first = post(ann, {"item": "ann's order"})
    second = post(bob, {"item": "ann's order"})

    assert who["actor"] is None  # Anonymous: no class accepted the header.
    assert second.headers.get("Idempotent-Replayed") is None, (
        "Bob was replayed Ann's response: an Authorization header that "
        "authenticated nobody named them both"
    )
    for refused in (first, second):
        assert refused.status_code == 400
        assert refused.json()["code"] == NEEDS_CALLER
    assert sample_auth_routes.counted == []
