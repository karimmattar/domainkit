"""A second factor in tests: authenticator codes, step-up, SCA, acts, approvals.

A project that moves money tests flows a person completes with an
authenticator app: enrolling it, stepping up before a sensitive act,
confirming a transaction (SCA) or one act, and another person approving
it. These do each step through the application's own routes, with a sync
``TestClient``, as a person would (TEST-21)::

    from domainkit.testing import approve_request, confirm_sca, enrol_totp, step_up

    def test_a_large_transfer(client_for, freeze_time):
        freeze_time("2027-01-04T10:00:00Z")  # Codes need a clock that moves on.
        maker = client_for("payments.add_transfer")
        app = enrol_totp(maker, "correct-horse-battery-42")
        step_up(maker, app)
        challenge = maker.post("/api/transfers/authorize", json=...).json()
        authorization = confirm_sca(maker, challenge["challenge"], app)
        ...
        approve_request(approver, "/api/approvals/requests/<id>")

Codes follow RFC 6238 as ``domainkit.contrib.auth`` checks them: HMAC-SHA1
of the 30-second step, six digits. The service refuses a step it has seen,
so ``Authenticator.code()`` never gives one twice: when the clock's step
was given already, it moves a frozen clock to the next step's start. The
routes are where ``domainkit init`` mounts them (``/auth``); ``prefix``
names another place. A refused step raises ``UnexpectedResponse``.

An act that asks for its own authorization (``StepUp(for_=...)``) is
authorized with ``authorize_act``: its twin asks, and a code of the app
confirms. Without an app, the installed domain that keeps authorizations
confirms it in the test, as the user's phone or passkey would
(``AuthorizationConfirmer``), for a project whose acts ask the phone.
Asking is not skipped: the user needs a way to confirm the act all the
same (an authenticator app, a bound device), or the twin answers ``409
no_second_factor``.
"""

import base64
import hashlib
import hmac
import struct
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from http import HTTPStatus
from typing import Any, Final, Protocol, runtime_checkable

from starlette.testclient import TestClient

from domainkit import clock
from domainkit.apps import registry
from domainkit.clock import Moment, moment
from domainkit.exceptions import DomainkitError, ImproperlyConfigured

STEP: Final = 30
"""Seconds each code works (RFC 6238's default, and domainkit's)."""
DIGITS: Final = 6
AUTH_PREFIX: Final = "/auth"
"""Where ``domainkit init`` mounts the auth, MFA, step-up and SCA routes."""


class UnexpectedResponse(AssertionError, DomainkitError):
    """A step of a flow was answered with another status than it expects."""

    def __init__(self, what: str, response: Any) -> None:  # noqa: ANN401 - a response.
        """Name the step, the status, and the body the application answered."""
        self.response = response
        super().__init__(f"{what}: {response.status_code} {response.text}")


def totp_code(secret: bytes | str, at: Moment | None = None) -> str:
    """The authenticator app's code for ``secret`` at ``at`` (default: the clock's now).

    ``secret`` is the raw key, or its base-32 text as the enrolment route
    answers it (with or without ``=`` padding).
    """
    return _hotp(_key(secret), int(moment(at).timestamp() // STEP))


@dataclass
class Authenticator:
    """One person's authenticator app: codes of its secret, never one step twice."""

    secret: bytes
    """The key the app and the service share."""
    _last: int | None = field(default=None, repr=False)

    def code(self) -> str:
        """A code of a step the service has not seen from this app.

        The clock's current step, or, when that was given already, the next
        one: a frozen clock (``freeze_time``) moves to that step's start.

        Raises:
            ImproperlyConfigured: The step was given already and the clock
                is not frozen, so the next code would wait up to 30 seconds.
        """
        step = int(clock.now().timestamp() // STEP)
        if self._last is not None and step <= self._last:
            frozen = clock.frozen()
            if frozen is None:
                raise ImproperlyConfigured(
                    "This step's code was given already; freeze the clock "
                    "(freeze_time) so the next code needs no wait."
                )
            step = self._last + 1
            frozen.move_to(datetime.fromtimestamp(step * STEP, UTC))
        self._last = step
        return _hotp(self.secret, step)


def enrol_totp(
    client: TestClient, password: str, *, prefix: str = AUTH_PREFIX
) -> Authenticator:
    """Turn on an authenticator app for ``client``'s user; return the app.

    Starts enrolment with the user's password and confirms it with a first
    code, as the user would. ``client`` signs in as the user (``client_for``,
    ``authenticated_as``).

    Raises:
        UnexpectedResponse: A route refused.
    """
    started = client.post(f"{prefix}/mfa/totp", json={"password": password})
    if started.status_code != HTTPStatus.OK:
        raise UnexpectedResponse("Starting TOTP enrolment", started)
    app = Authenticator(_key(started.json()["secret"]))
    confirmed = client.post(f"{prefix}/mfa/totp/confirm", json={"code": app.code()})
    if confirmed.status_code != HTTPStatus.OK:
        raise UnexpectedResponse("Confirming TOTP enrolment", confirmed)
    return app


def step_up(
    client: TestClient, app: Authenticator, *, prefix: str = AUTH_PREFIX
) -> None:
    """Step ``client``'s user up with a code of ``app``.

    Raises:
        UnexpectedResponse: The step-up route refused.
    """
    stepped = client.post(f"{prefix}/step-up", json={"code": app.code()})
    if stepped.status_code != HTTPStatus.OK:
        raise UnexpectedResponse("Stepping up", stepped)


def confirm_sca(
    client: TestClient,
    challenge: str,
    app: Authenticator,
    *,
    prefix: str = AUTH_PREFIX,
) -> str:
    """Confirm SCA ``challenge`` with a code of ``app``; return the authorization.

    The challenge is what the application's own route answered when the
    transaction asked for one (``request_authorization``).

    Raises:
        UnexpectedResponse: The confirm route refused.
    """
    confirmed = client.post(
        f"{prefix}/sca/{challenge}/confirm", json={"code": app.code()}
    )
    if confirmed.status_code != HTTPStatus.OK:
        raise UnexpectedResponse("Confirming the SCA challenge", confirmed)
    authorization: str = confirmed.json()["authorization"]
    return authorization


def authorize_act(
    client: TestClient,
    url: str,
    app: Authenticator | None = None,
    *,
    json: Any = None,  # noqa: ANN401 - the act's body.
    password: str | None = None,
    prefix: str = AUTH_PREFIX,
) -> str:
    """Ask for an authorization of the act at ``url``, and confirm it; return it.

    ``POST <url>/authorization`` with ``json``, the act's body (a
    ``ResourceRouter``'s twin), then ``confirm_authorization``: a code of
    ``app`` (and ``password`` where the act asks it), or, without ``app``,
    what the act asks, proved in the test. Send the act with the answer in
    ``Action-Authorization``. The user needs a way to confirm the act, as
    to ask for it outside tests.

    Raises:
        UnexpectedResponse: The twin or the confirm route refused (``409
            no_second_factor`` for a user with no way to confirm it).
    """
    asked = client.post(f"{url}/authorization", json=json)
    if asked.status_code != HTTPStatus.CREATED:
        raise UnexpectedResponse("Asking for an authorization of the act", asked)
    found: dict[str, Any] = asked.json()
    if app is None:
        return _confirmed_in_test(client, found["id"], found["methods"])
    return _confirmed(client, found["id"], app, password, prefix)


def confirm_authorization(
    client: TestClient,
    authorization_id: str,
    app: Authenticator | None = None,
    *,
    password: str | None = None,
    prefix: str = AUTH_PREFIX,
) -> str:
    """Confirm the action authorization ``authorization_id``; return ``<id>.<code>``.

    With ``app``, a code of it at ``POST <prefix>/authorizations/{id}/confirm``
    (and ``password`` where the act asks it), as the user would. Without,
    it is confirmed in the test as proving what the act asks (``GET
    <prefix>/authorizations/{id}`` names it): what the phone or a passkey
    proves, for an act a code cannot confirm. The installed domain that
    keeps authorizations does it (``AuthorizationConfirmer``:
    ``domainkit.contrib.auth``, under ``ENVIRONMENT = "test"`` only), in
    the client's event loop: enter the client first.

    Raises:
        UnexpectedResponse: The confirm route, or the read, refused.
    """
    if app is not None:
        return _confirmed(client, authorization_id, app, password, prefix)
    read = client.get(f"{prefix}/authorizations/{authorization_id}")
    if read.status_code != HTTPStatus.OK:
        raise UnexpectedResponse("Reading the authorization", read)
    return _confirmed_in_test(client, authorization_id, read.json()["methods"])


def _confirmed(
    client: TestClient,
    authorization_id: str,
    app: Authenticator,
    password: str | None,
    prefix: str,
) -> str:
    """``<id>.<code>``: the authorization confirmed with a code of ``app``."""
    body = {"code": app.code()}
    if password is not None:
        body["password"] = password
    confirmed = client.post(
        f"{prefix}/authorizations/{authorization_id}/confirm", json=body
    )
    if confirmed.status_code != HTTPStatus.OK:
        raise UnexpectedResponse("Confirming the authorization", confirmed)
    authorization: str = confirmed.json()["authorization"]
    return authorization


@runtime_checkable
class AuthorizationConfirmer(Protocol):
    """An installed domain that confirms action authorizations in tests.

    ``domainkit.contrib.auth`` is one; ``authorize_act`` and
    ``confirm_authorization`` ask it when no app is given.
    """

    async def confirm_test_authorization(
        self,
        authorization_id: Any,  # noqa: ANN401 - a UUID, or its text.
        methods: Sequence[str],
    ) -> str:
        """Confirm it as proved by ``methods``; ``<id>.<code>``."""
        ...


def _confirmed_in_test(
    client: TestClient, authorization_id: str, methods: Sequence[str]
) -> str:
    """``<id>.<code>``: confirmed by the installed ``AuthorizationConfirmer``.

    Raises:
        RuntimeError: The client is not entered.
    """
    if client.portal is None:
        raise RuntimeError("Enter the client first: with TestClient(app) as ...")
    confirmed: str = client.portal.call(_confirm_in_test, authorization_id, methods)
    return confirmed


async def _confirm_in_test(authorization_id: str, methods: Sequence[str]) -> str:
    """Ask the first installed ``AuthorizationConfirmer`` to confirm it.

    Raises:
        ImproperlyConfigured: No installed domain confirms authorizations.
    """
    for config in registry:
        if isinstance(config, AuthorizationConfirmer):
            return await config.confirm_test_authorization(authorization_id, methods)
    raise ImproperlyConfigured(
        "Confirming an act in a test needs an installed domain that keeps action "
        "authorizations, such as domainkit.contrib.auth."
    )


def approve_request(client: TestClient, url: str) -> dict[str, Any]:
    """Approve the approval request at ``url`` as ``client``'s user; return it.

    Reads the request for its fingerprint, then approves that version, as a
    person approves what they read. ``url`` is where the project mounts the
    request (``/api/approvals/requests/<id>``). A rule that asks for a
    step-up needs ``step_up`` first.

    Raises:
        UnexpectedResponse: Reading or approving the request was refused.
    """
    read = client.get(url)
    if read.status_code != HTTPStatus.OK:
        raise UnexpectedResponse("Reading the approval request", read)
    approved = client.post(
        f"{url}/approve", json={"fingerprint": read.json()["fingerprint"]}
    )
    if approved.status_code != HTTPStatus.OK:
        raise UnexpectedResponse("Approving the request", approved)
    answer: dict[str, Any] = approved.json()
    return answer


def _key(secret: bytes | str) -> bytes:
    """The raw key of ``secret``: bytes as they are, base-32 text decoded."""
    if isinstance(secret, bytes):
        return secret
    text = secret.strip().upper()
    return base64.b32decode(text + "=" * (-len(text) % 8))


def _hotp(key: bytes, counter: int) -> str:
    """RFC 4226's code for ``counter``: HMAC-SHA1, dynamically truncated."""
    digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    (number,) = struct.unpack(">I", digest[offset : offset + 4])
    return str((number & 0x7FFFFFFF) % 10**DIGITS).zfill(DIGITS)
