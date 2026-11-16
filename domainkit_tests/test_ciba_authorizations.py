"""The phone confirms one act, in the server's words, signed (ADR 0144, user report A6).

A signed-in session asks the user's phone to confirm one of its action
authorizations (``POST /app-sign-in/authorizations/{id}``). The phone
lists the act as auth keeps it, and approves it with the act's digest, in
a request its device signs. The session then redeems ``<id>.<code>``
(``POST /app-sign-in/authorizations/token``) and sends it with the act,
which spends it once. The free-text step-up, the page's words and a
window, is deprecated.
"""

import base64
import hashlib
import json
import warnings
from collections.abc import Iterator
from datetime import timedelta
from typing import Any
from uuid import UUID

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.requests import Request

from domainkit import clock
from domainkit.conf import override_settings
from domainkit.contrib.auth.actions import (
    Action,
    on_action_confirmation,
    register_action,
)
from domainkit.contrib.auth.apps import action_problems
from domainkit.contrib.auth.integration import auth_settings
from domainkit.contrib.ciba.api import FREE_TEXT_STEP_UP_WARNING
from domainkit.contrib.ciba.api import _warn_free_text_step_up as warn_once
from domainkit.contrib.ciba.hooks import on_approve
from domainkit.ddd import PermissionDenied
from domainkit.deprecation import DomainkitDeprecationWarning
from domainkit_tests import sample_action_routes as acts
from domainkit_tests.conftest import UseAuth
from domainkit_tests.support.auth import audited, create_user
from domainkit_tests.test_action_authorisations import rows
from domainkit_tests.test_ciba import PHONE, App, Phone, app, sign_in_pushes
from domainkit_tests.test_ciba import rows as sign_in_rows
from domainkit_tests.test_ciba_step_up import ask, redeem, web_of

__all__ = ["app"]  # The fixture the tests take.

ROUTES = "domainkit_tests.sample_ciba_action_routes.router"
DEPRECATION_DOCS = (
    "https://github.com/karimmattar/domainkit/blob/main/docs/auth/ciba.md"
    "#the-pages-words-and-a-window-deprecated"
)


@pytest.fixture
def acting(app: App) -> Iterator[App]:
    """``app``, serving an act that asks an authorization: renaming oneself."""
    acts.FAILURES.clear()
    with override_settings(ROOT_ROUTER=ROUTES):
        register_action(acts.RENAME, domain="shop")
        yield app


class Session:
    """A signed-in web session of a user's, which renames them."""

    def __init__(self, client: TestClient, username: str = "alice") -> None:
        """Log ``username`` in from ``client``: a session of its own."""
        self.client = client
        self.headers = web_of(client, username)

    def ask(self, name: str = "Al") -> Any:  # noqa: ANN401 - a response.
        """Ask for an authorization of renaming oneself ``name``."""
        return self.client.post(
            "/me/name/authorization", json={"first_name": name}, headers=self.headers
        )

    def asked(self, name: str = "Al") -> dict[str, Any]:
        """An authorization of renaming oneself ``name``: what asking answered."""
        answer = self.ask(name)
        assert answer.status_code == 201, answer.json()
        asked: dict[str, Any] = answer.json()
        return asked

    def ask_phone(self, authorization_id: str) -> Any:  # noqa: ANN401 - a response.
        """Ask the phone to confirm the authorization ``authorization_id``."""
        return self.client.post(
            f"/auth/app-sign-in/authorizations/{authorization_id}",
            headers=self.headers,
        )

    def redeem(self, handle: str) -> Any:  # noqa: ANN401 - a response.
        """Poll for the authorization the phone confirmed."""
        return self.client.post(
            "/auth/app-sign-in/authorizations/token",
            json={"request": handle},
            headers=self.headers,
        )

    def rename(self, name: str = "Al", authorization: str | None = None) -> Any:  # noqa: ANN401
        """Rename oneself ``name``, sending ``authorization`` if given."""
        headers = dict(self.headers)
        if authorization is not None:
            headers["Action-Authorization"] = authorization
        return self.client.post("/me/name", json={"first_name": name}, headers=headers)


def code_of(response: Any) -> tuple[int, str]:  # noqa: ANN401 - a response.
    """The status and the error code of ``response``."""
    return response.status_code, response.json().get("code")


def asked_of_the_phone(session: Session, phone: Phone) -> tuple[dict[str, Any], ...]:
    """An authorization asked, the phone asked, and what the phone lists of it."""
    asked = session.asked()
    started = session.ask_phone(asked["id"])
    assert started.status_code == 200, started.json()
    [listed] = phone.waiting()
    return asked, started.json(), listed


def approve(phone: Phone, listed: dict[str, Any], started: dict[str, Any]) -> Any:  # noqa: ANN401
    """Approve the act the phone lists, with the page's number and its digest."""
    return phone.approve(
        listed["id"],
        match_code=started["match_code"],
        digest=listed["authorization"]["digest"],
    )


def later(seconds: int = 6) -> Any:  # noqa: ANN401 - a context manager.
    """The clock ``seconds`` on: a poll after the interval."""
    return clock.freeze_time(clock.now() + timedelta(seconds=seconds))


def test_the_phone_confirms_one_act_in_the_servers_words_and_it_is_spent_once(
    acting: App,
) -> None:
    """Asked, listed as auth keeps it, approved with its digest, redeemed, spent once.

    The push names the request and nothing of the act; the phone reads the
    act from auth's record; the authorization proves ``hwk`` and ``phone``.
    """
    phone = Phone(acting(PHONE))
    session = Session(acting())
    before = session.rename()
    asked = session.asked()

    started = session.ask_phone(asked["id"])
    pending = session.redeem(started.json()["request"])
    [push] = sign_in_pushes()
    [listed] = phone.waiting()
    approved = approve(phone, listed, started.json())
    with later():
        answered = session.redeem(started.json()["request"])
    authorization = answered.json()["authorization"]
    renamed = session.rename("Al", authorization)
    replayed = session.rename("Al", authorization)
    with later(12):
        again = session.redeem(started.json()["request"])

    assert code_of(before) == (401, "authorization_required")
    assert started.status_code == 200, started.json()
    assert started.headers["Cache-Control"] == "no-store"
    assert set(started.json()) == {"request", "expires_in", "interval", "match_code"}
    assert pending.json()["code"] == "authorization_pending"
    assert push["data"]["request_id"] == listed["id"]
    assert push["title"] == "Approve sign-in?"
    assert "Change your first name" not in json.dumps(push)
    [row] = rows()
    assert listed["kind"] == "authorization"
    assert (listed["binding_message"], listed["transaction"]) == (None, None)
    assert listed["authorization"] == {
        "id": asked["id"],
        "purpose": "accounts.rename",
        "shown": {
            "title": "Change your first name",
            "details": [{"label": "New first name", "value": "Al"}],
        },
        "expires_at": asked["expires_at"],
        "methods": ["otp"],
        "digest": row["digest"],
    }
    assert approved.status_code == 204, approved.json()
    assert answered.status_code == 200, answered.json()
    assert answered.headers["Cache-Control"] == "no-store"
    assert authorization.startswith(f"{asked['id']}.")
    assert answered.json()["expires_at"] == asked["expires_at"]
    assert renamed.status_code == 200, renamed.json()
    assert code_of(replayed) == (403, "action_not_authorized")
    assert again.json()["code"] == "sign_in_unknown"  # Redeemed once.
    assert row["amr"] == "hwk phone"
    assert row["sign_in_request_id"] == UUID(listed["id"])
    [authorized] = audited("auth.action_authorized")
    assert authorized["details"]["device_id"] == phone.device.id
    assert len(audited("ciba.sign_in_approved")) == 1


def test_the_row_keeps_the_devices_proof_which_verifies_against_the_kept_key(
    acting: App,
) -> None:
    """The key, the bytes it signed, its signature, and when: checkable once unbound.

    The bytes are the approval as the device signed it: its method, path,
    time, and the digest of a body that holds the act's digest.
    """
    phone = Phone(acting(PHONE))
    session = Session(acting())
    _, started, listed = asked_of_the_phone(session, phone)
    digest = listed["authorization"]["digest"]

    approved = approve(phone, listed, started)
    unbound = phone.send("DELETE", f"/auth/devices/{phone.device.id}")
    [row] = rows()

    version, method, target, moment, body_digest = row["signed"].split(b"\n")
    body = json.dumps({"match_code": started["match_code"], "digest": digest})
    sent = base64.urlsafe_b64encode(hashlib.sha256(body.encode()).digest())
    key = serialization.load_der_public_key(row["public_key"])
    assert isinstance(key, ec.EllipticCurvePublicKey)
    assert approved.status_code == 204, approved.json()
    assert unbound.status_code == 204, unbound.json()  # The proof stays.
    assert row["device_id"] == UUID(phone.device.id)
    assert row["algorithm"] == -7  # ES256.
    assert row["public_key"] == phone.device.key.public_key().public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    assert (version, method) == (b"domainkit-device-v1", b"POST")
    assert target == f"/auth/app-sign-in/requests/{listed['id']}/approve".encode()
    assert body_digest == sent.rstrip(b"=")
    assert round(row["signed_at"].timestamp() * 1000) == int(moment)
    key.verify(row["signature"], row["signed"], ec.ECDSA(hashes.SHA256()))


def test_another_digest_denies_the_request_and_the_act_is_refused(
    acting: App,
) -> None:
    """``409 authorization_changed``: denied, unconfirmed; none sent: ``422``."""
    phone = Phone(acting(PHONE))
    session = Session(acting())
    asked, started, listed = asked_of_the_phone(session, phone)

    unsent = phone.approve(listed["id"], match_code=started["match_code"])
    still = phone.waiting()
    changed = phone.approve(
        listed["id"], match_code=started["match_code"], digest="0" * 64
    )
    polled = session.redeem(started["request"])
    late = approve(phone, listed, started)
    renamed = session.rename()
    forged = session.rename("Al", f"{asked['id']}.forged")

    assert code_of(unsent) == (422, "digest_required")
    assert [each["id"] for each in still] == [listed["id"]]  # It waits still.
    assert code_of(changed) == (409, "authorization_changed")
    assert polled.json()["code"] == "access_denied"
    assert code_of(late) == (404, "sign_in_not_found")
    assert code_of(renamed) == (401, "authorization_required")
    assert code_of(forged) == (403, "action_not_authorized")
    [row] = rows()
    assert (row["confirmed_at"], row["signature"]) == (None, None)
    assert sign_in_rows()[0]["denied_at"] is not None
    assert len(audited("ciba.sign_in_denied")) == 1


async def refuse_every_act(request: Request, user: Any, sign_in: Any) -> None:  # noqa: ANN401
    """A project's rule: no act is approved from the phone."""
    if sign_in.kind == "authorization":
        raise PermissionDenied("Acts are not approved from the phone here.")


def test_an_act_a_hook_refuses_is_neither_confirmed_nor_approved(acting: App) -> None:
    """``on_approve`` runs before auth confirms the act: nothing is kept."""
    phone = Phone(acting(PHONE))
    session = Session(acting())
    on_approve(refuse_every_act, domain="ciba")
    _, started, listed = asked_of_the_phone(session, phone)

    refused = approve(phone, listed, started)
    with later():
        polled = session.redeem(started["request"])

    assert refused.status_code == 403, refused.json()
    assert refused.json()["detail"] == "Acts are not approved from the phone here."
    assert polled.json()["code"] == "authorization_pending"
    assert [each["id"] for each in phone.waiting()] == [listed["id"]]
    [row] = rows()
    assert (row["confirmed_at"], row["signature"]) == (None, None)


def test_another_session_can_neither_ask_the_phone_nor_redeem_the_answer(
    acting: App,
) -> None:
    """Another login of the same user: 404, then ``sign_in_unknown``; the asker's holds.

    The handle redeems as an authorization only: never a step-up, nor a login.
    """
    phone = Phone(acting(PHONE))
    web = acting()
    session = Session(web)
    other = Session(web)
    asked = session.asked()

    asked_elsewhere = other.ask_phone(asked["id"])
    started = session.ask_phone(asked["id"]).json()
    [listed] = phone.waiting()
    approve(phone, listed, started)
    redeemed_elsewhere = other.redeem(started["request"])
    as_step_up = redeem(web, session.headers, started["request"])
    as_login = web.post("/auth/app-sign-in/token", json={"request": started["request"]})
    own = session.redeem(started["request"])

    assert code_of(asked_elsewhere) == (404, "authorization_not_found")
    for refused in (redeemed_elsewhere, as_step_up, as_login):
        assert refused.json()["code"] == "sign_in_unknown"
    assert own.status_code == 200, own.json()  # None of those spent it.


def test_with_number_matching_the_phone_types_the_pages_number(acting: App) -> None:
    """A wrong number denies the request and confirms nothing; off, none is asked."""
    phone = Phone(acting(PHONE))
    session = Session(acting())
    _, started, listed = asked_of_the_phone(session, phone)
    wrong = f"{(int(started['match_code']) + 1) % 100:02d}"

    mistyped = phone.approve(
        listed["id"], match_code=wrong, digest=listed["authorization"]["digest"]
    )
    polled = session.redeem(started["request"])
    unconfirmed = rows()[0]["confirmed_at"]
    acting(NUMBER_MATCHING=False)
    _, unmatched, listed = asked_of_the_phone(session, phone)
    approved = phone.approve(listed["id"], digest=listed["authorization"]["digest"])

    assert len(started["match_code"]) == 2
    assert started["match_code"].isdigit()
    assert code_of(mistyped) == (422, "wrong_match_code")
    assert polled.json()["code"] == "access_denied"
    assert unconfirmed is None
    assert "match_code" not in unmatched
    assert listed["match_required"] is False
    assert approved.status_code == 204, approved.json()


def test_an_act_only_the_phone_may_confirm_is_refused_a_code(acting: App) -> None:
    """``METHODS = ["phone"]``: a code is ``409``; the phone confirms, the act runs."""
    phone = Phone(acting(PHONE))
    session = Session(acting())
    only_the_phone = {
        **auth_settings().model_dump(exclude_unset=True),
        "AUTHORIZATIONS": {"METHODS": ["phone"]},
    }
    with override_settings(AUTH=only_the_phone):
        asked, started, listed = asked_of_the_phone(session, phone)
        shown = session.client.get(
            f"/auth/authorizations/{asked['id']}", headers=session.headers
        )
        by_code = session.client.post(
            f"/auth/authorizations/{asked['id']}/confirm",
            json={"code": "123456"},
            headers=session.headers,
        )
        approved = approve(phone, listed, started)
        with later():
            authorization = session.redeem(started["request"]).json()["authorization"]
        renamed = session.rename("Al", authorization)

    assert shown.json()["methods"] == ["phone"]
    assert code_of(by_code) == (409, "authorization_needs_other_factors")
    assert approved.status_code == 204, approved.json()
    assert renamed.status_code == 200, renamed.json()
    assert rows()[0]["amr"] == "hwk phone"


def test_an_act_that_asks_the_password_is_not_the_phones_to_confirm(
    acting: App,
) -> None:
    """``METHODS = ["pwd", "otp"]``: the phone proves no password, and is not asked."""
    phone = Phone(acting(PHONE))
    session = Session(acting())
    asked = session.asked()
    with_password = {
        **auth_settings().model_dump(exclude_unset=True),
        "AUTHORIZATIONS": {"METHODS": ["pwd", "otp"]},
    }

    with override_settings(AUTH=with_password):
        refused = session.ask_phone(asked["id"])

    assert code_of(refused) == (409, "authorization_needs_other_factors")
    assert phone.waiting() == []


def test_the_phone_is_asked_as_a_step_up_is_counted_and_bounded(acting: App) -> None:
    """``MAX_PENDING`` waiting: ``409 phone_unavailable``; past ``START_RATE``: 429."""
    Phone(acting(PHONE))
    session = Session(acting(MAX_PENDING=1, START_RATE="2/minute"))
    first, second = session.asked("Al"), session.asked("Bo")

    asked = session.ask_phone(first["id"])
    unavailable = session.ask_phone(second["id"])
    throttled = session.ask_phone(second["id"])

    assert asked.status_code == 200, asked.json()
    assert code_of(unavailable) == (409, "phone_unavailable")
    assert code_of(throttled) == (429, "rate_limited")


def test_the_phone_counts_as_a_way_to_confirm_for_a_user_with_a_bound_device(
    acting: App,
) -> None:
    """No device and no authenticator: ``409 no_second_factor``; a bound phone: 201."""
    Phone(acting(PHONE))
    create_user("bob")
    web = acting()

    unconfirmable = Session(web, "bob").ask()
    confirmable = Session(web, "alice").ask()

    assert code_of(unconfirmable) == (409, "no_second_factor")
    assert confirmable.status_code == 201, confirmable.json()


def test_a_request_auth_no_longer_links_lists_no_act(acting: App) -> None:
    """Asked twice, the authorization names the newer request; the older shows none.

    Approving the older is ``404 authorization_not_found``, and confirms nothing.
    """
    phone = Phone(acting(PHONE))
    session = Session(acting(NUMBER_MATCHING=False))
    asked = session.asked()
    session.ask_phone(asked["id"])
    session.ask_phone(asked["id"])

    listed = phone.waiting()
    [older] = [each for each in listed if each["authorization"] is None]
    [newer] = [each for each in listed if each["authorization"] is not None]
    refused = phone.approve(older["id"], digest=newer["authorization"]["digest"])

    assert newer["authorization"]["id"] == asked["id"]
    assert code_of(refused) == (404, "authorization_not_found")
    assert rows()[0]["confirmed_at"] is None


PHONE_ONLY = Action(
    "accounts.rename_by_phone",
    command=acts.Rename,
    subject=lambda command: f"auth.user:{command.user_id}",
    show=acts.show_rename,
    methods=["phone"],
)


def test_with_ciba_installed_the_phone_proves_what_an_act_asks(acting: App) -> None:
    """Ciba registers the phone (``hwk``, ``phone``): ``auth.E108`` holds no more."""
    register_action(PHONE_ONLY, domain="shop")

    ways = {hook.call.name: hook.call.methods for hook in on_action_confirmation}

    assert ways["phone"] == {"hwk", "phone"}
    assert [message.id for message in action_problems(FastAPI())] == []


def test_without_ciba_an_act_only_the_phone_confirms_is_refused(
    use_auth: UseAuth,
) -> None:
    """``auth.E108``: nothing installed proves ``phone``."""
    use_auth()
    register_action(PHONE_ONLY, domain="alpha")

    found = action_problems(FastAPI())

    assert [message.id for message in found] == ["auth.E108"]


def test_the_free_text_step_up_is_deprecated_with_a_header_and_a_warning_once(
    app: App,
) -> None:
    """``Deprecation`` (RFC 9745) and ``Link`` on each answer; one warning a process."""
    warn_once.cache_clear()
    phone = Phone(app(PHONE))
    web = app(NUMBER_MATCHING=False)
    headers = web_of(web)

    with pytest.warns(DomainkitDeprecationWarning) as warned:
        first = ask(web, headers)
    with warnings.catch_warnings(record=True) as warned_again:
        warnings.simplefilter("always")
        second = ask(web, headers)
    for listed in phone.waiting():
        approved = phone.approve(listed["id"])
        assert approved.status_code == 204, approved.json()
    waited = web.post(
        "/auth/app-sign-in/step-up/wait",
        json={"request": first.json()["request"]},
        headers=headers,
    )
    stepped = redeem(web, headers, first.json()["request"])

    said = [str(each.message) for each in warned]
    assert said == [FREE_TEXT_STEP_UP_WARNING]
    assert warned_again == []  # Once a process.
    assert "removed in domainkit 3.0" in said[0]
    for answer in (first, second, waited, stepped):
        assert answer.status_code == 200, answer.json()
        assert answer.headers["Deprecation"] == "@1794700800"  # 2026-11-15.
        assert answer.headers["Link"] == f'<{DEPRECATION_DOCS}>; rel="deprecation"'
