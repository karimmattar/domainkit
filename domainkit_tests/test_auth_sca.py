"""Dynamic linking: an authorization holds for one amount, one payee, once."""

import asyncio
from collections.abc import Iterator
from datetime import timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, update

from domainkit.clock import FrozenClock, freeze_time
from domainkit.conf import override_settings, settings
from domainkit.contrib.auth import AuthUnitOfWork
from domainkit.contrib.auth.repositories import ScaChallenge
from domainkit.contrib.auth.sca import (
    InvalidTransaction,
    TransactionNotAuthorized,
    authentication_code,
    canonical,
    consume_authorization,
    request_authorization,
)
from domainkit.contrib.auth.tables import sca_challenges
from domainkit.db import connections
from domainkit.management import call_command
from domainkit.management.base import run_async
from domainkit.money import Money, register_currency
from domainkit_tests.conftest import Api
from domainkit_tests.test_auth_http import PASSWORD, audited, bearer, create_user, login
from domainkit_tests.test_auth_stepup import enrol, hourly_purge, now_code

START = "2027-01-04T10:00:00Z"
IBAN = "DE89370400440532013000"
OTHER_IBAN = "GB33BUKB20201555555555"
HUNDRED = {"amount": "100.00", "currency": "EUR"}
NEW_KEY = "new-0123456789-abcdefghijklmnopqrstuvwxyz-ABCDEFGHIJ"


@pytest.fixture
def frozen() -> Iterator[FrozenClock]:
    """One clock for codes and expiries."""
    with freeze_time(START) as clock:
        yield clock


class Alice:
    """Alice, logged in with a second factor, at the API."""

    def __init__(self, client: TestClient, clock: FrozenClock) -> None:
        """Log in and turn the second factor on."""
        self.client = client
        self.clock = clock
        self.headers = bearer(login(client, "alice")["access_token"])
        self.secret = enrol(client, self.headers, clock)

    def ask(self, amount: dict[str, str] = HUNDRED, payee: str = IBAN) -> str:
        """Ask to authorize a transfer; return its id."""
        response = self.client.post(
            "/transfers/authorize",
            json={"amount": amount, "payee": payee},
            headers=self.headers,
        )
        assert response.status_code == 200, response.json()
        challenge: str = response.json()["challenge"]
        return challenge

    def confirm(self, challenge: str, code: str | None = None, **body: str) -> Any:  # noqa: ANN401
        """Confirm it with a code (now's, unless given)."""
        return self.client.post(
            f"/auth/sca/{challenge}/confirm",
            json={"code": code or now_code(self.secret, self.clock), **body},
            headers=self.headers,
        )

    def authorized(self, **asked: Any) -> str:  # noqa: ANN401
        """A confirmed authorization of a transfer."""
        confirmed = self.confirm(self.ask(**asked))
        assert confirmed.status_code == 200, confirmed.json()
        authorization: str = confirmed.json()["authorization"]
        return authorization

    def transfer(
        self,
        authorization: str,
        amount: dict[str, str] = HUNDRED,
        payee: str = IBAN,
    ) -> Any:  # noqa: ANN401 - a response.
        """Make the transfer with ``authorization``."""
        return self.client.post(
            "/transfers",
            json={"amount": amount, "payee": payee},
            headers={**self.headers, "X-Authorization": authorization},
        )


@pytest.fixture
def alice(identity: Api, frozen: FrozenClock) -> Alice:
    """Alice and her API."""
    create_user("alice")
    return Alice(identity(), frozen)


def rows() -> list[dict[str, Any]]:
    """Every stored transaction."""

    async def read() -> list[dict[str, Any]]:
        async with connections.engine().connect() as connection:
            result = await connection.execute(select(sca_challenges))
            return [dict(row._mapping) for row in result]

    return run_async(read())


def refused(response: Any) -> bool:  # noqa: ANN401 - a response.
    """Whether ``response`` is the refusal of an unauthorized transaction."""
    return (response.status_code, response.json()["code"]) == (
        403,
        "transaction_not_authorized",
    )


def test_an_authorization_holds_for_its_transaction_once(alice: Alice) -> None:
    """Shown, confirmed, spent; the same authorization again is a replay."""
    challenge = alice.ask()
    shown = alice.client.get(f"/auth/sca/{challenge}", headers=alice.headers)
    confirmed = alice.confirm(challenge)
    authorization = confirmed.json()["authorization"]

    made = alice.transfer(authorization)
    replayed = alice.transfer(authorization)

    assert shown.json() == {
        "id": challenge,
        "amount": HUNDRED,
        "payee": IBAN,
        "purpose": "transfer",
        "expires_at": "2027-01-04T10:05:30Z",
        "confirmed": False,
    }
    assert confirmed.status_code == 200
    assert confirmed.headers["Cache-Control"] == "no-store"
    assert confirmed.json()["expires_at"] == "2027-01-04T10:05:30Z"
    assert authorization.startswith(f"{challenge}.")
    assert made.json() == {"amount": HUNDRED, "payee": IBAN, "challenge": challenge}
    assert refused(replayed)
    [row] = rows()
    assert (row["amr"], row["consumed_at"] is not None) == ("otp", True)
    assert [row["details"]["reason"] for row in audited("auth.sca")] == [
        "invalid_authorization"
    ]
    assert len(audited("auth.transaction_authorized")) == 1
    assert len(audited("auth.transaction_authorization_used")) == 1


@pytest.mark.parametrize(
    ("amount", "payee"),
    [
        ({"amount": "100.01", "currency": "EUR"}, IBAN),
        ({"amount": "100.00", "currency": "USD"}, IBAN),
        ({"amount": "1000.00", "currency": "EUR"}, IBAN),
        (HUNDRED, OTHER_IBAN),
        (HUNDRED, IBAN + " "),
        (HUNDRED, IBAN.lower()),
    ],
)
def test_any_change_of_amount_or_payee_voids_it(
    alice: Alice, amount: dict[str, str], payee: str
) -> None:
    """The substitution is refused, and the authorization is gone for good."""
    authorization = alice.authorized()

    substituted = alice.transfer(authorization, amount, payee)
    then = alice.transfer(authorization)

    assert refused(substituted)
    assert refused(then)
    assert [row["details"]["reason"] for row in audited("auth.sca")] == [
        "changed",
        "invalid_authorization",
    ]


def test_another_purpose_is_another_transaction(alice: Alice) -> None:
    """The purpose is bound as the amount and payee are."""
    authorization = alice.authorized()
    user_id = run_async(_user_id("alice"))

    with pytest.raises(TransactionNotAuthorized):
        run_async(
            consume_authorization(
                authorization,
                user_id=user_id,
                amount=Money.of("100", "EUR"),
                payee=IBAN,
                purpose="payout",
            )
        )


async def _user_id(username: str) -> UUID:
    """The id of ``username``."""
    async with AuthUnitOfWork() as uow:
        user = await uow.users.get_by_natural_key(username)
        assert user is not None
        return user.id


def test_another_users_authorization_holds_nothing(alice: Alice, identity: Api) -> None:
    """Bob sees no transaction of alice's, and cannot spend her authorization."""
    create_user("bob")
    bob = bearer(login(alice.client, "bob")["access_token"])
    challenge = alice.ask()

    shown = alice.client.get(f"/auth/sca/{challenge}", headers=bob)
    confirmed = alice.client.post(
        f"/auth/sca/{challenge}/confirm", json={"code": "123456"}, headers=bob
    )
    authorization = alice.confirm(challenge).json()["authorization"]
    spent_by_bob = alice.client.post(
        "/transfers",
        json={"amount": HUNDRED, "payee": IBAN},
        headers={**bob, "X-Authorization": authorization},
    )

    assert (shown.status_code, shown.json()["code"]) == (404, "transaction_not_found")
    assert confirmed.status_code == 404
    assert refused(spent_by_bob)
    assert alice.transfer(authorization).status_code == 200  # Not voided by bob.


@pytest.mark.parametrize(
    "forged",
    [
        "",
        "nonsense",
        "not-a-uuid.code",
        f"{UUID(int=7)}.code",
    ],
)
def test_forged_authorizations_hold_nothing(alice: Alice, forged: str) -> None:
    """Unknown ids, malformed ones, and wrong codes alike."""
    assert refused(alice.transfer(forged))


def test_a_code_made_up_for_a_real_transaction_holds_nothing(alice: Alice) -> None:
    """The code is an HMAC under the server's key, not a checksum."""
    challenge = alice.ask()
    alice.confirm(challenge)

    assert refused(alice.transfer(f"{challenge}.AAAA"))
    assert alice.transfer(f"{challenge}.AAAA").status_code == 403


def test_an_unconfirmed_transaction_holds_nothing(alice: Alice) -> None:
    """Its code is right, but no factor confirmed it."""
    challenge = alice.ask()
    stored = run_async(_stored(UUID(challenge)))

    assert refused(alice.transfer(f"{challenge}.{authentication_code(stored)}"))


async def _stored(challenge_id: UUID) -> ScaChallenge:
    """The stored transaction ``challenge_id``."""
    async with AuthUnitOfWork() as uow:
        found = await uow.sca.get(challenge_id)
    assert found is not None
    return found


def test_a_row_changed_after_its_confirmation_holds_nothing(alice: Alice) -> None:
    """Whoever edits the stored amount cannot spend the authorization on it."""
    authorization = alice.authorized()

    async def tamper() -> None:
        async with connections.engine().begin() as connection:
            await connection.execute(update(sca_challenges).values(amount=9999))

    run_async(tamper())

    assert refused(
        alice.transfer(authorization, {"amount": "9999.00", "currency": "EUR"})
    )
    assert refused(alice.transfer(authorization))


def test_authorizations_expire(alice: Alice) -> None:
    """Confirmed or not, after ``AUTH.SCA.TTL``; the code is not spent on one."""
    late = alice.ask()
    authorization = alice.authorized()
    alice.clock.tick(timedelta(minutes=5))

    confirmed = alice.confirm(late)
    spent = alice.transfer(authorization)

    assert (confirmed.status_code, confirmed.json()["code"]) == (
        409,
        "transaction_expired",
    )
    assert refused(spent)


def test_a_transaction_is_confirmed_once(alice: Alice) -> None:
    """The second confirmation is refused; so is a wrong code, audited."""
    challenge = alice.ask()
    wrong = alice.confirm(challenge, "000000")
    alice.confirm(challenge)

    again = alice.confirm(challenge)

    assert (wrong.status_code, wrong.json()["code"]) == (401, "invalid_code")
    assert (again.status_code, again.json()["code"]) == (409, "transaction_confirmed")
    assert [row["details"]["reason"] for row in audited("auth.sca")] == ["invalid_code"]


def test_a_confirmation_that_loses_the_race_is_refused(
    alice: Alice, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Confirmed elsewhere between the read and the write: 409, nothing returned."""
    challenge = alice.ask()

    async def lost(*args: object) -> bool:
        return False

    from domainkit.contrib.auth.repositories import ScaStore

    monkeypatch.setattr(ScaStore, "confirm", lost)
    raced = alice.confirm(challenge)

    assert (raced.status_code, raced.json()["code"]) == (409, "transaction_expired")
    assert audited("auth.transaction_authorized") == []


def test_the_password_too_where_the_project_asks(
    identity: Api, frozen: FrozenClock
) -> None:
    """``SCA.METHODS = ("pwd", "otp")``."""
    create_user("alice")
    alice = Alice(identity(SCA={"METHODS": ["otp", "pwd"]}), frozen)
    challenge = alice.ask()

    missing = alice.confirm(challenge)
    both = alice.confirm(challenge, password=PASSWORD)

    assert (missing.status_code, missing.json()["code"]) == (400, "password_required")
    assert both.status_code == 200
    assert rows()[0]["amr"] == "otp pwd"


def test_one_of_many_concurrent_uses_wins(alice: Alice) -> None:
    """Twenty at once spend it once."""
    authorization = alice.authorized()
    user_id = run_async(_user_id("alice"))

    async def spend() -> bool:
        try:
            await consume_authorization(
                authorization,
                user_id=user_id,
                amount=Money.of("100", "EUR"),
                payee=IBAN,
                purpose="transfer",
            )
        except TransactionNotAuthorized:
            return False
        return True

    async def race() -> list[bool]:
        return list(await asyncio.gather(*(spend() for _ in range(20))))

    assert sum(run_async(race())) == 1


def test_authorizations_outlive_a_key_rotation(alice: Alice) -> None:
    """Made under a key that became a fallback, it still holds."""
    authorization = alice.authorized()
    old = settings.SECRET_KEY

    with override_settings(SECRET_KEY=NEW_KEY, SECRET_KEY_FALLBACKS=[old]):
        assert alice.transfer(authorization).status_code == 200


def test_the_canonical_encoding_names_every_value() -> None:
    """One JSON array; the amount in fixed point with the currency's places."""
    challenge = ScaChallenge(
        id=UUID(int=1),
        user_id=UUID(int=2),
        amount=Money.of(Decimal("1E+3"), "JPY"),
        payee="محمد",
        purpose="transfer",
        created_at=None,  # type: ignore[arg-type]
        expires_at=None,  # type: ignore[arg-type]
    )

    assert (
        canonical(challenge)
        == (
            '["domainkit.sca.v1","00000000-0000-0000-0000-000000000001",'
            '"00000000-0000-0000-0000-000000000002","JPY","1000","محمد","transfer"]'
        ).encode()
    )
    assert repr(challenge).count("محمد") == 0


@pytest.mark.parametrize(
    ("amount", "payee", "purpose", "message"),
    [
        (Money.of("0", "EUR"), IBAN, "transfer", "more than zero"),
        (Money.of("-1", "EUR"), IBAN, "transfer", "more than zero"),
        (Money.of("1", "EUR"), "", "transfer", "printable"),
        (Money.of("1", "EUR"), "x" * 256, "transfer", "printable"),
        (Money.of("1", "EUR"), "a\nb", "transfer", "printable"),
        (Money.of("1", "EUR"), IBAN, "Transfer", "lower-case"),
        (Money.of("1", "EUR"), IBAN, "", "lower-case"),
        (Money.of("1", "EUR"), IBAN, "a" * 101, "lower-case"),
    ],
)
def test_what_cannot_be_authorized_is_refused(
    alice: Alice, amount: Money, payee: str, purpose: str, message: str
) -> None:
    """Before anything is stored."""
    with pytest.raises(InvalidTransaction, match=message):
        run_async(request_authorization(uuid4(), amount, payee, purpose=purpose))

    assert rows() == []


def test_amounts_of_five_places_are_refused(alice: Alice) -> None:
    """The table keeps four places; a project's currency of five does not fit."""
    register_currency("XPT5", minor_units=5)
    with pytest.raises(InvalidTransaction, match="4 places"):
        run_async(
            request_authorization(
                uuid4(), Money.of("1", "XPT5"), IBAN, purpose="transfer"
            )
        )


def test_expired_transactions_are_cleared(alice: Alice) -> None:
    """By the hourly task, and by ``cleartokens``."""
    alice.ask()
    alice.clock.tick(timedelta(minutes=5, seconds=31))
    run_async(hourly_purge())
    gone = rows()
    alice.ask()
    alice.clock.tick(timedelta(minutes=6))
    call_command("cleartokens", stdout=None)

    assert gone == []
    assert rows() == []


def test_transactions_go_with_the_users_credentials(alice: Alice) -> None:
    """``forget_credentials`` deletes them: the payees may be personal data."""
    alice.ask()
    user_id = run_async(_user_id("alice"))

    async def forget() -> None:
        async with AuthUnitOfWork() as uow:
            await uow.users.forget_credentials(user_id)
            await uow.commit()

    run_async(forget())

    assert rows() == []
