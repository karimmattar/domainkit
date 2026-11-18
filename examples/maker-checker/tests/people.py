"""People who send payouts and approve them, each with an authenticator app."""

from typing import Any

from domainkit.contrib.auth.testing import create_user
from domainkit.testing import Authenticator, TestClient, authorize_act, enrol_totp

URL = "/api/v1/payouts"
REQUESTS = "/api/approvals/requests"
PASSWORD = "correct-horse-battery-42"
MAKER = (
    "payouts.add_payout",
    "payouts.view_payout",
    "approvals.add_approvalrequest",
    "approvals.view_approvalrequest",
)
APPROVER = ("payouts.approve_payout", "approvals.view_approvalrequest")


def payout(amount: str, iban: str = "DE89370400440532013000") -> dict[str, Any]:
    """A payout of ``amount`` US dollars to a supplier, as JSON."""
    return {
        "payee": "Acme Supplies Ltd",
        "iban": iban,
        "amount": {"amount": amount, "currency": "USD"},
    }


def submission(amount: str) -> dict[str, Any]:
    """A payout submitted to approvals, as JSON."""
    return {"action": "payouts.send_payout", "arguments": payout(amount)}


class Person:
    """A user granted ``permissions``, logged in, with an authenticator app.

    The clock must be frozen (the ``clock`` fixture): each code moves it to
    the next 30-second step, as an authenticator app's codes come.
    """

    def __init__(self, client: TestClient, *permissions: str) -> None:
        """Store the user and turn their authenticator app on."""
        self.user = client.run(create_user, *permissions, password=PASSWORD)
        self.api = client.authenticated_as(self.user)
        self.app: Authenticator = enrol_totp(self.api, PASSWORD)

    @property
    def id(self) -> str:
        """The user's id, as requests and the audit trail name them."""
        return str(self.user.id)

    def confirm(self, act: str, body: dict[str, Any]) -> dict[str, str]:
        """Ask for an authorization of the act at ``act``, confirm it with a code.

        Returns the header that sends it with the act.
        """
        authorization = authorize_act(self.api, act, self.app, json=body)
        return {"Action-Authorization": authorization}

    def submit(self, amount: str) -> dict[str, Any]:
        """Submit a payout of ``amount`` for approval, confirmed; the request."""
        body = submission(amount)
        submitted = self.api.post(
            REQUESTS, json=body, headers=self.confirm(REQUESTS, body)
        )
        assert submitted.status_code == 201, submitted.json()
        request: dict[str, Any] = submitted.json()
        return request

    def approve(self, request: dict[str, Any]) -> Any:
        """Approve ``request``, as shown to this approver, confirmed; the answer."""
        act = f"{REQUESTS}/{request['id']}/approve"
        body = {"fingerprint": request["fingerprint"]}
        return self.api.post(act, json=body, headers=self.confirm(act, body))
