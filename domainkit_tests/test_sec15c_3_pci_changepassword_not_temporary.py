"""SEC-15c-3: a password an operator sets with ``changepassword`` is never changed.

PCI DSS v4.0 8.3.5 (docs/auth/pci-dss.md): a password set or reset for a
user is changed at its first use. ``changepassword alice`` is an operator
resetting another user's password, exactly as ``POST /admin/auth/users/
{id}/password`` is, but it stores the password as the user's own
(``temporary`` False): under the preset alice logs in with what the
operator typed, and keeps it, with no ``password_change_required``.
"""

import io

import pytest

from domainkit.contrib.auth.management.commands import changepassword
from domainkit.management import call_command
from domainkit_tests.conftest import Api
from domainkit_tests.test_auth_http import create_user

PCI = {"PASSWORD_PRESET": "pci-dss-4"}
OWN = "Kq7-zv93-Lmw2"
SET_BY_OPERATOR = "Pw4-tt81-Xcv9"


def test_a_password_an_operator_sets_is_changed_at_first_use(
    identity: Api, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The first login with it answers 403 password_change_required."""
    client = identity(**PCI)
    create_user("alice", OWN)
    monkeypatch.setattr(
        changepassword.Command, "ask_secret", lambda self, _: SET_BY_OPERATOR
    )
    call_command("changepassword", "alice", stdout=io.StringIO(), stderr=io.StringIO())

    first = client.post(
        "/auth/login", json={"username": "alice", "password": SET_BY_OPERATOR}
    )

    assert (first.status_code, first.json().get("code")) == (
        403,
        "password_change_required",
    ), first.json()
