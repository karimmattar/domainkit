"""Payouts at or under the threshold: sent once their maker confirms the act."""

from domainkit.testing import confirm_authorization

from tests.people import URL, Person, payout


def test_a_payout_is_sent_once_its_maker_confirms_it(maker: Person) -> None:
    """Asked at the twin, shown, confirmed with a code, spent by the payout once."""
    body = payout("2500.00")

    asked = maker.api.post(f"{URL}/authorization", json=body)
    confirmed = confirm_authorization(maker.api, asked.json()["id"], maker.app)
    sent = maker.api.post(URL, json=body, headers={"Action-Authorization": confirmed})
    again = maker.api.post(URL, json=body, headers={"Action-Authorization": confirmed})

    assert asked.json()["shown"] == {
        "title": "Send a payout",
        "details": [
            {"label": "Pay", "value": "2500.00 USD to Acme Supplies Ltd"},
            {"label": "IBAN", "value": "DE89370400440532013000"},
        ],
    }
    assert (sent.status_code, again.status_code) == (201, 403)
    assert again.json()["code"] == "action_not_authorized"
    assert [item["amount"] for item in maker.api.get(URL).json()["items"]] == [
        {"amount": "2500.00", "currency": "USD"}
    ]


def test_a_payout_is_sent_only_as_its_maker_confirmed_it(maker: Person) -> None:
    """Another account, or no authorization at all, sends nothing."""
    headers = maker.confirm(URL, payout("2500.00"))

    unconfirmed = maker.api.post(URL, json=payout("2500.00"))
    elsewhere = maker.api.post(
        URL, json=payout("2500.00", iban="GB82WEST12345698765432"), headers=headers
    )

    assert (unconfirmed.status_code, unconfirmed.json()["code"]) == (
        401,
        "authorization_required",
    )
    assert (elsewhere.status_code, elsewhere.json()["code"]) == (
        403,
        "action_not_authorized",
    )
    assert maker.api.get(URL).json()["items"] == []


def test_the_payout_route_refuses_a_large_payout(maker: Person) -> None:
    """Over the threshold, confirmed or not, it needs approval: nothing is sent."""
    body = payout("25000.00")

    refused = maker.api.post(URL, json=body, headers=maker.confirm(URL, body))

    assert (refused.status_code, refused.json()["code"]) == (403, "approval_required")
    assert maker.api.get(URL).json()["items"] == []
