"""What an act declares, what a route asks, and what ``check`` refuses (user report A6).

``Action``, ``Shown``, ``StepUp(for_=...)``, ``AUTH.AUTHORIZATIONS``, the
digest, the code, and the checks ``auth.E108`` to ``auth.E111``, without a
database (ADR 0144). A ``@use_case`` as the act, guarded as a copy, and a
``show`` that reads in the act's unit of work (ADR 0161, user report A13).
"""

from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi import Depends, FastAPI, WebSocket
from pydantic import ValidationError
from starlette.requests import HTTPConnection

from domainkit.conf import override_settings
from domainkit.contrib.approvals import fingerprint as approvals_fingerprint
from domainkit.contrib.auth import Credentials, PasswordHash, StepUp, User, UserId
from domainkit.contrib.auth.access import ActionStepUp
from domainkit.contrib.auth.actions import (
    SPENDS,
    Action,
    ActionAuthorization,
    ActionConfirmation,
    ActionNotAuthorized,
    AuthorizationRequired,
    Shown,
    authorization_code,
    canonical,
    login_digest,
    on_action_confirmation,
    register_action,
    registered_actions,
    request_action_authorization,
    requires_authorization,
    spent_by_a_service,
)
from domainkit.contrib.auth.apps import action_problems
from domainkit.contrib.auth.settings import AuthorizationsSettings
from domainkit.ddd import Command, EventBus, UnitOfWork, UseCase, fingerprint, use_case
from domainkit.exceptions import ImproperlyConfigured
from domainkit.http.access import AsksAuthorization, Require
from domainkit.http.errors import APIError
from domainkit.http.sse import EventSourceResponse, ServerSentEvent
from domainkit_tests.conftest import UseAuth
from domainkit_tests.sample_action_routes import RENAME, Rename

pytestmark = pytest.mark.anyio

ALICE = User(id=UserId(UUID(int=1)), password=PasswordHash.unusable(), username="alice")


class Close(Command):
    """Close an account."""

    account_id: UUID
    reason: str


def closing(**options: Any) -> Action:  # noqa: ANN401 - Action's options.
    """An action that closes an account, with ``options``."""
    return Action(
        options.pop("purpose", "accounts.close"),
        command=Close,
        subject=lambda command: f"accounts.account:{command.account_id}",
        show=lambda command: Shown("Close an account", {"Reason": command.reason}),
        **options,
    )


def connection(credentials: object = None, **scope: Any) -> HTTPConnection:  # noqa: ANN401
    """A request of alice's whose credentials are ``credentials``."""
    return HTTPConnection(
        {"type": "http", "headers": [], "user": ALICE, "auth": credentials, **scope}
    )


def test_a_rule_asks_for_a_step_up_or_one_acts_authorization_never_both(
    use_auth: UseAuth,
) -> None:
    """``max_age`` or ``for_``; ``for_`` takes no methods, and a purpose's name."""
    use_auth()
    for wrong in (
        {},
        {"max_age": timedelta(minutes=5), "for_": "accounts.close"},
        {"for_": "accounts.close", "methods": ["otp"]},
        {"for_": "Not a purpose"},
        {"for_": object()},
    ):
        with pytest.raises(ImproperlyConfigured, match="StepUp"):
            StepUp(**wrong)

    asked = StepUp(for_=closing())
    named = StepUp(for_="accounts.close")
    window = StepUp(timedelta(minutes=5))

    assert isinstance(asked, ActionStepUp)
    assert isinstance(asked, StepUp)
    assert (asked.purpose, named.purpose, named.action) == (
        "accounts.close",
        "accounts.close",
        None,
    )
    assert isinstance(asked, AsksAuthorization)
    assert not isinstance(window, AsksAuthorization)  # No twin for a window.
    assert asked.unprovable() == frozenset()
    assert asked.required() == {"otp"}


async def test_the_rule_refuses_without_a_header_and_never_says_why_after(
    use_auth: UseAuth,
) -> None:
    """No header: 401 with the purpose; a malformed one: 403; staff, a socket: no."""
    use_auth()
    rule = StepUp(for_="accounts.close")
    credentials = Credentials(ALICE, scheme="token", token_id="a" * 64)
    without = connection(credentials)
    malformed = connection(
        credentials, headers=[(b"action-authorization", b"not-an-id.code")]
    )
    staff = connection(replace(credentials, impersonator=uuid4()))
    socket = connection(credentials, type="websocket")

    assert await rule.has_permission(without, ALICE) is False
    assert isinstance(await rule.refusal(without, ALICE), AuthorizationRequired)
    assert await rule.has_permission(malformed, ALICE) is False
    assert await rule.has_object_permission(malformed, ALICE, object()) is False
    assert isinstance(await rule.refusal(malformed, ALICE), ActionNotAuthorized)
    assert await rule.has_permission(staff, ALICE) is False
    assert await rule.has_permission(socket, ALICE) is False


async def test_asking_needs_credentials_without_scopes_that_can_be_told_apart(
    use_auth: UseAuth,
) -> None:
    """Scoped, or keeping no login: 400 ``step_up_unsupported``; staff: 403."""
    use_auth()
    scoped = Credentials(
        ALICE, scheme="token", token_id="a" * 64, scopes=frozenset({"a.b"})
    )
    untold = Credentials(ALICE, scheme="certificate")
    staff = Credentials(ALICE, scheme="token", token_id="a" * 64, impersonator=uuid4())
    command = Close(account_id=uuid4(), reason="moving")

    for credentials in (scoped, untold):
        with pytest.raises(APIError) as refused:
            await request_action_authorization(
                connection(credentials), closing(), command
            )
        assert refused.value.code == "step_up_unsupported"
    with pytest.raises(ActionNotAuthorized):
        await request_action_authorization(connection(staff), closing(), command)


def test_an_action_declares_a_purpose_its_fields_a_ttl_and_methods(
    use_auth: UseAuth,
) -> None:
    """Each mistake is refused where the action is declared."""
    use_auth()
    for wrong, message in (
        ({"purpose": "Accounts Close"}, "purpose"),
        ({"purpose": "a" * 101}, "purpose"),
        ({"ignore": ["missing"]}, "ignores fields Close lacks: missing"),
        ({"ttl": timedelta(0)}, "ttl"),
        ({"ttl": timedelta(minutes=30, seconds=1)}, "ttl"),
        ({"methods": ["pwd"]}, '"otp" or "phone"'),
        ({"methods": ["otp", "otp"]}, "once"),
        ({"methods": ["hwk"]}, "unknown methods hwk"),
    ):
        with pytest.raises(ImproperlyConfigured, match=message):
            closing(**wrong)

    phone = closing(methods=["phone"], ttl=timedelta(minutes=30))

    assert phone.required() == {"phone"}
    assert phone.lifetime() == timedelta(minutes=30)
    assert closing().required() == {"otp"}
    assert closing().lifetime() == timedelta(minutes=5)


def test_the_settings_take_the_phone_alone_and_thirty_minutes_at_most() -> None:
    """``METHODS`` holds ``otp`` or ``phone``; ``TTL`` is 30 minutes at most."""
    assert AuthorizationsSettings(METHODS=("phone",)).METHODS == ("phone",)
    assert timedelta(minutes=5) == AuthorizationsSettings(METHODS=("pwd", "otp")).TTL
    for wrong in (
        {"METHODS": ("pwd",)},
        {"METHODS": ("otp", "otp")},
        {"METHODS": ("hwk",)},
        {"TTL": timedelta(minutes=31)},
        {"TTL": timedelta(0)},
    ):
        with pytest.raises(ValidationError):
            AuthorizationsSettings(**wrong)


def test_what_is_shown_fits_a_phones_screen() -> None:
    """A title of 64 printable characters, six details of 64, each label once."""
    shown = Shown("Pay", [("Amount", "250.00 EUR"), ("Payee", "")])

    assert shown.details == (("Amount", "250.00 EUR"), ("Payee", ""))
    assert Shown.from_json(shown.as_json()) == shown
    for title, details in (
        ("", {}),
        ("x" * 65, {}),
        ("Pay\n", {}),
        ("Pay", {str(number): "x" for number in range(7)}),
        ("Pay", [("Amount", "1"), ("Amount", "2")]),
        ("Pay", {"": "x"}),
        ("Pay", {"Amount": "x" * 65}),
        ("Pay", {"Amount": "1\u202e0"}),
    ):
        with pytest.raises(ValueError, match=r"once|most|printable"):
            Shown(title, details)


def test_the_digest_is_the_fingerprint_approvals_computes(use_auth: UseAuth) -> None:
    """One function, in the core: the purpose and the arguments, less ``ignore``."""
    use_auth()
    command = Rename(user_id=UUID(int=1), first_name="Al", asked_at="10:00")
    arguments = {"user_id": str(UUID(int=1)), "first_name": "Al"}

    assert approvals_fingerprint is fingerprint
    assert RENAME.arguments(command) == arguments
    assert RENAME.digest_of(command) == fingerprint("accounts.rename", arguments)
    assert RENAME.digest_of(replace(command, asked_at="10:01")) == RENAME.digest_of(
        command
    )
    assert RENAME.subject_of(command) == f"auth.user:{UUID(int=1)}"
    with pytest.raises(TypeError, match="takes a Rename"):
        RENAME.digest_of(Close(account_id=uuid4(), reason="x"))


def test_the_code_covers_every_value_of_the_row(use_auth: UseAuth) -> None:
    """Encoded by name and version; any value changed makes another code."""
    use_auth()
    expires = datetime(2027, 1, 4, 10, 5, tzinfo=UTC)
    authorization = ActionAuthorization(
        id=UUID(int=9),
        user_id=UUID(int=1),
        login="b" * 64,
        purpose="accounts.close",
        subject="accounts.account:7",
        digest="c" * 64,
        shown=Shown("Close an account", {"Reason": "moving"}),
        created_at=expires - timedelta(minutes=5),
        expires_at=expires,
    )
    code = authorization_code(authorization)

    assert canonical(authorization).decode() == (
        f'["domainkit.authorization.v1","{UUID(int=9)}","{UUID(int=1)}",'
        f'"{"b" * 64}","accounts.close","accounts.account:7","{"c" * 64}",'
        f'"{authorization.shown.digest()}","2027-01-04T10:05:00.000000+00:00"]'
    )
    for changed in (
        {"user_id": UUID(int=2)},
        {"login": "d" * 64},
        {"purpose": "accounts.open"},
        {"subject": "accounts.account:8"},
        {"digest": "e" * 64},
        {"shown": Shown("Close an account", {"Reason": "moved"})},
        {"expires_at": expires + timedelta(microseconds=1)},
    ):
        assert authorization_code(replace(authorization, **changed)) != code
    assert "Shown" not in repr(authorization)
    assert "b" * 64 not in repr(authorization)


def test_the_logins_digest_is_the_one_the_phone_keeps() -> None:
    """The SHA-256 of the scheme and the token, or a JSON Web Token's login."""
    login = uuid4()
    token = Credentials(ALICE, scheme="token", token_id="a" * 64)
    jwt = Credentials(ALICE, scheme="jwt", login=login)

    assert (
        login_digest(token)
        == __import__("hashlib").sha256(f"token:{'a' * 64}".encode()).hexdigest()
    )
    assert login_digest(jwt) != login_digest(Credentials(ALICE, scheme="jwt"))


def test_one_purpose_is_one_action(use_auth: UseAuth) -> None:
    """Two actions registered with one purpose are refused."""
    use_auth()
    register_action(closing(), domain="alpha")
    register_action(closing(), domain="alpha")

    with pytest.raises(ImproperlyConfigured, match="Two actions"):
        registered_actions()


async def _events() -> AsyncIterator[ServerSentEvent]:
    """No events."""
    for _ in ():
        yield ServerSentEvent.json({})


def application(**routes: Any) -> FastAPI:  # noqa: ANN401 - each route's rule.
    """An application with a POST route per rule ``routes`` names."""
    app = FastAPI()
    for name, rule in routes.items():

        async def endpoint() -> dict[str, str]:
            return {}

        app.add_api_route(
            f"/{name}",
            endpoint,
            methods=["POST"],
            dependencies=[Depends(Require(rule))],
        )
    return app


def test_check_refuses_a_purpose_no_action_has_and_one_no_service_spends(
    use_auth: UseAuth,
) -> None:
    """``auth.E109`` for an unknown purpose; ``auth.E110`` with no decorated service."""
    use_auth()
    register_action(RENAME, domain="alpha")
    register_action(closing(purpose="accounts.unspent"), domain="alpha")
    app = application(
        renamed=StepUp(for_="accounts.rename"),
        unknown=StepUp(for_="accounts.nowhere"),
        unspent=StepUp(for_="accounts.unspent") & StepUp(timedelta(minutes=5)),
    )

    found = action_problems(app)

    assert [message.id for message in found] == ["auth.E109", "auth.E110"]
    assert found[0].startswith(
        "POST /unknown asks for an authorization of 'accounts.nowhere'"
    )
    assert found[1].startswith("POST /unspent asks")


def test_check_refuses_an_act_on_a_websocket_or_an_event_stream(
    use_auth: UseAuth,
) -> None:
    """``auth.E111``: a connection is asked again and again, and acts on nothing."""
    use_auth()
    register_action(RENAME, domain="alpha")
    app = FastAPI()
    rule = Depends(Require(StepUp(for_="accounts.rename")))

    @app.websocket("/live", dependencies=[rule])
    async def live(websocket: WebSocket) -> None:
        await websocket.close()

    @app.get("/events", dependencies=[rule])
    async def events() -> EventSourceResponse:
        return EventSourceResponse(_events())

    found = action_problems(app)

    assert [message.id for message in found] == ["auth.E111", "auth.E111"]
    assert {message.split(" asks")[0] for message in found} == {
        "WEBSOCKET /live",
        "GET /events",
    }


def test_check_refuses_an_act_nothing_installed_can_confirm(
    use_auth: UseAuth,
) -> None:
    """``auth.E108``: the phone, asked alone, until an app proves it."""
    use_auth()
    register_action(closing(methods=["phone"]), domain="alpha")
    register_action(
        closing(purpose="accounts.with_password", methods=["pwd", "otp"]),
        domain="alpha",
    )

    before = [message.id for message in action_problems(FastAPI())]

    async def anyone(user_id: UUID) -> bool:
        return True

    on_action_confirmation(
        ActionConfirmation("phone", frozenset({"hwk", "phone"}), anyone),
        domain="alpha",
    )
    after = action_problems(FastAPI())

    assert before == ["auth.E108"]  # A code with the password proves pwd and otp.
    assert after == []


def test_actions_and_ways_to_confirm_register_as_hooks_do(use_auth: UseAuth) -> None:
    """In ``ready()``, or for an installed domain; auth registers the code itself."""
    use_auth()
    confirmations = [hook.call.name for hook in on_action_confirmation]

    with pytest.raises(ImproperlyConfigured, match="ready"):
        register_action(closing())
    assert confirmations == ["code"]


def test_overriding_methods_in_settings_changes_what_an_action_asks(
    use_auth: UseAuth,
) -> None:
    """An action without ``methods`` asks ``AUTH.AUTHORIZATIONS.METHODS``."""
    use_auth()
    with override_settings(AUTH={"AUTHORIZATIONS": {"METHODS": ["otp", "phone"]}}):
        assert closing().required() == {"otp", "phone"}
        assert StepUp(for_=closing()).required() == {"otp", "phone"}


# A use case as the act (ADR 0161, user report A13) ------------------------------

ACCOUNT = UUID("00000000-0000-4000-8000-000000000007")


class Ledger(UnitOfWork):
    """Accounts in memory: what a use case changes, and a ``show`` reads."""

    def __init__(self) -> None:
        """No account closed, none opened yet."""
        super().__init__(events=EventBus())
        self.names = {ACCOUNT: "Savings"}
        self.closed: list[UUID] = []
        self.opened = 0

    async def __aenter__(self) -> "Ledger":
        """Count each time it is opened."""
        self.opened += 1
        return await super().__aenter__()

    async def _commit(self) -> None:
        """Nothing to write."""

    async def _rollback(self) -> None:
        """Nothing to undo."""


@use_case
async def close_account(uow: Ledger, account_id: UUID, reason: str) -> None:
    """Close an account: some routes run it without an act."""
    uow.closed.append(account_id)


async def show_closing(command: Any, uow: Ledger) -> Shown:  # noqa: ANN401 - the use case's command.
    """The account's stored name, read in ``uow``, which is open."""
    assert uow.opened == 1
    return Shown("Close an account", {"Account": uow.names[command.account_id]})


def closing_by_use_case(**options: Any) -> Action:  # noqa: ANN401 - Action's options.
    """An action whose command is ``close_account``'s, shown from the ledger."""
    return Action(
        "accounts.close_by_use_case",
        command=close_account,
        subject=lambda command: f"accounts.account:{command.account_id}",
        show=options.pop("show", show_closing),
        **options,
    )


def test_an_action_takes_a_use_case_for_its_command() -> None:
    """``command=`` a ``@use_case`` is its command; anything else is refused."""
    action = closing_by_use_case(ignore=["reason"])
    one = close_account.command(account_id=ACCOUNT, reason="moving")
    other = close_account.command(account_id=ACCOUNT, reason="fees")

    assert action.command is close_account.command
    assert action.digest_of(one) == action.digest_of(other)
    with pytest.raises(TypeError, match="takes a CloseAccount, not a Close"):
        action.arguments(Close(account_id=ACCOUNT, reason="moving"))
    with pytest.raises(ImproperlyConfigured, match="or its @use_case"):
        Action(
            "accounts.close",
            command=close_account.run,  # type: ignore[arg-type]
            subject=str,
            show=show_closing,
        )


async def test_requires_authorization_guards_a_copy_of_a_use_case(
    use_auth: UseAuth,
) -> None:
    """The copy's ``run`` and call spend; the use case itself stays as it was.

    So a route that runs the use case without an act (the phone's) is
    left alone, and the route that asks runs the copy.
    """
    use_auth()
    guarded = requires_authorization(closing_by_use_case())(close_account)
    uow = Ledger()
    command = close_account.command(account_id=ACCOUNT, reason="moving")

    await close_account.run(command, uow)
    await close_account(uow, account_id=ACCOUNT, reason="moving")
    with pytest.raises(AuthorizationRequired):
        await guarded.run(command, uow)
    with pytest.raises(AuthorizationRequired):
        await guarded(uow, account_id=ACCOUNT, reason="moving")

    assert isinstance(guarded, UseCase)
    assert guarded is not close_account
    assert (guarded.command, guarded.name, guarded.fields) == (
        close_account.command,
        "close_account",
        ("account_id", "reason"),
    )
    assert "run" not in vars(close_account)  # The class's own run, unguarded.
    assert not hasattr(close_account, SPENDS)
    assert getattr(guarded, SPENDS) == getattr(guarded.run, SPENDS)
    assert spent_by_a_service("accounts.close_by_use_case")
    assert uow.closed == [ACCOUNT, ACCOUNT]


def test_a_use_case_of_another_command_is_refused() -> None:
    """The action must take the use case's command: refused when guarded."""
    with pytest.raises(ImproperlyConfigured, match="runs a CloseAccount"):
        requires_authorization(closing())(close_account)


async def test_show_reads_in_the_unit_of_work_it_is_given() -> None:
    """``show(command, uow)`` is given the act's unit of work, opened, not committed.

    Without one it is refused: the asker must lend it. A ``show`` of the
    command alone, or without a signature, takes none.
    """
    action = closing_by_use_case()
    uow = Ledger()
    command = close_account.command(account_id=ACCOUNT, reason="moving")

    shown = await action.shown(command, uow)

    assert shown == Shown("Close an account", {"Account": "Savings"})
    assert (uow.opened, uow.committed) == (1, 0)
    assert action.reads_unit_of_work
    with pytest.raises(ImproperlyConfigured, match="pass uow= to request_action"):
        await action.shown(command)
    with pytest.raises(ImproperlyConfigured, match="must return a Shown"):
        await closing_by_use_case(show=lambda command, uow: "words").shown(
            command, Ledger()
        )
    assert await action(command, Ledger()) == shown  # As the hook point calls it.
    assert not closing().reads_unit_of_work
    assert not closing_by_use_case(show=vars).reads_unit_of_work
