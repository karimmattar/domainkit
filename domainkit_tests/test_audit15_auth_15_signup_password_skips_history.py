"""AUTH-15: the password chosen at signup is never kept in the password history.

``signup.confirm_signup_code`` hashes the password and adds the user itself
(``uow.users.add``), not through ``create_user`` or ``set_password``, so
``PasswordHistoryStore.record`` never sees it. With ``PASSWORD_HISTORY``
(4 under the PCI preset, 8.3.7: "not the same as any of the last four"),
a user who signed up with P1 and changed to P2 can change back to P1 at
once: the history holds P2 alone, and the current password is P2.
"""

from domainkit_tests.conftest import Api
from domainkit_tests.support.auth import bearer, login
from domainkit_tests.test_auth_recovery import run_worker
from domainkit_tests.test_auth_signup import code_to, confirm, signup

FIRST = "Sign-up-pass-77"
SECOND = "Second-pass-88x"


def test_the_signup_password_counts_among_the_last_ones(identity: Api) -> None:
    """Going back to the signup password is refused as a reuse."""
    client = identity(SIGNUP_ENABLED=True, PASSWORD_HISTORY=4)
    assert signup(client).status_code == 202
    run_worker()
    assert confirm(client, code_to("alice@example.com"), FIRST).status_code == 204
    headers = bearer(login(client, "alice", FIRST)["access_token"])
    changed = client.post(
        "/auth/password",
        json={"old_password": FIRST, "new_password": SECOND},
        headers=headers,
    )
    assert changed.status_code == 204

    back = client.post(
        "/auth/password",
        json={"old_password": SECOND, "new_password": FIRST},
        headers=headers,
    )

    assert back.status_code == 422, "the signup password was taken again"
    assert back.json()["code"] == "invalid_password"
