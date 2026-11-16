"""Authorize one act, with its arguments, once (ADR 0144).

A step-up (``StepUp(max_age=...)``) opens every guarded route for a while;
an action authorization opens one act, with the arguments the user was
shown, once. A domain declares the act::

    from domainkit.contrib.auth.actions import Action, Shown, requires_authorization


    async def show(command: EndMembership) -> Shown:
        membership = await memberships.get(command.membership_id)
        return Shown("End a membership", {"Member": membership.display_name})


    END_MEMBERSHIP = Action(
        "organizations.end_membership",
        command=EndMembership,
        subject=lambda command: f"organizations.membership:{command.membership_id}",
        show=show,
    )


    @requires_authorization(END_MEMBERSHIP)
    async def end_membership(command: EndMembership, uow: OrganizationsUnitOfWork):
        ...

registers it in ``ready()`` (``register_action(END_MEMBERSHIP)``), and puts
``StepUp(for_="organizations.end_membership")`` on the route. A request
without an ``Action-Authorization`` header is refused with ``401
authorization_required``; the client asks for one (``POST <path>/authorization``
on a ``ResourceRouter``, or ``request_action_authorization`` in a route of
its own), the user confirms it (a code at ``POST
/authorizations/{id}/confirm``, or another app's way: the phone, a passkey),
and the client sends the request again with ``Action-Authorization:
<id>.<code>``. The rule checks it and keeps it on the request; the
decorated service spends it.

How it is bound: the authorization is a stored row (the user, the login
that asked, the purpose, the subject, the act's digest, what was shown,
and the expiry). Confirming it returns ``<id>.<code>``, the code an
HMAC-SHA256, under a key derived from ``SECRET_KEY`` for
``auth.authorization``, of those values. It is never stored: a row changed
after its confirmation fails it. The service computes the digest and the
subject again from the command it is given, and spends the row in one
statement: other arguments void it, and of two acts at once one wins.

When it is spent: with the act, in its transaction, when the service's unit
of work is on auth's database (an act refused on the way can be sent
again); otherwise just before the act, in a transaction of its own (at
most once). An authorization a rule accepted that no service spent is
voided at the end of a request that succeeded, and logged
(``auth.authorization_unspent``); under ``DEBUG`` and in tests, that
request fails.
"""

import base64
import dataclasses
import functools
import hashlib
import hmac
import inspect
import json
import logging
import re
import sys
from collections.abc import (
    Awaitable,
    Callable,
    Collection,
    Iterable,
    Iterator,
    Mapping,
)
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import KW_ONLY, dataclass, field, replace
from datetime import UTC, datetime, timedelta
from typing import Any, Final, cast, get_type_hints
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, Request, Response, status
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter
from starlette.requests import HTTPConnection
from starlette.types import Message, Receive, Scope, Send

from domainkit.apps import registry
from domainkit.apps.hooks import HookPoint
from domainkit.conf import settings
from domainkit.db.routing import router as database_router
from domainkit.ddd import NotFound, PermissionDenied, UnitOfWork, fingerprint
from domainkit.exceptions import ImproperlyConfigured
from domainkit.http.access import IsUnscoped, NotAuthenticated, Require
from domainkit.http.errors import APIError, FieldError, error_response, error_responses
from domainkit.log import SECURITY_LOGGER
from domainkit.signing import derive_keys

from ._http import PASSWORD_MAX_LENGTH, no_store
from .access import INSUFFICIENT, PURPOSE, AuthenticatedUser, impersonated, proven
from .authentication import (
    JWT_SCHEME,
    STEPPING_SCHEMES,
    Credentials,
    get_authenticators,
)
from .events import ActionAuthorizationUsed, ActionAuthorized
from .models import AbstractBaseUser, UserId
from .repositories import (
    ActionAuthorization,
    AuthorizationStore,
    Shown,
    UserRepository,
)
from .settings import MAX_AUTHORIZATION_TTL, auth_settings, with_a_second_factor
from .tables import PURPOSE_MAX_LENGTH, SUBJECT_MAX_LENGTH
from .throttling import audit_refusal
from .tokens import utc_now
from .uow import AuthUnitOfWork

__all__ = [
    "AUTHORIZATION_HEADER",
    "Action",
    "ActionAuthorization",
    "ActionAuthorizationOut",
    "ActionAuthorizationUsed",
    "ActionAuthorized",
    "ActionConfirmation",
    "ActionNotAuthorized",
    "AuthorizationChanged",
    "AuthorizationNeedsOtherFactors",
    "AuthorizationNotFound",
    "AuthorizationRequired",
    "Shown",
    "action_authorization",
    "action_for",
    "confirm_action_with",
    "confirmed_action_authorization",
    "link_action_authorization",
    "linked_action_authorization",
    "login_digest",
    "on_action_confirmation",
    "register_action",
    "registered_actions",
    "request_action_authorization",
    "requires_authorization",
    "router",
    "spend_authorization",
]

AUTHORIZATION_HEADER: Final = "Action-Authorization"
"""The header a request sends its act's authorization in: ``<id>.<code>``."""
KEY_PURPOSE: Final = "auth.authorization"
"""What the key of authorization codes is derived for."""
ENCODING: Final = "domainkit.authorization.v1"
"""Starts every canonical encoding: a later one never matches it."""
ACTION: Final = "auth.authorization"
"""The audit trail's action for refused authorizations."""
SUBJECT: Final = re.compile(r"[A-Za-z0-9_.-]+:[A-Za-z0-9_.:/-]+")
"""A subject: ``<type>:<id>``, an internal id (ADR 0018)."""
METHOD_NAMES: Final = frozenset({"pwd", "otp", "phone"})
"""What an action may ask confirming it to prove."""
MAX_UNCONFIRMED: Final = 10
"""Unconfirmed authorizations a login holds; asking for one more ends the oldest."""
KEPT: Final = "domainkit.auth.authorizations"
"""Where a request keeps the authorizations its rules accepted."""
VERDICTS: Final = "domainkit.auth.authorization_verdicts"
"""Where a request keeps what each purpose's rule found, asked once."""
SPENDS: Final = "__action_authorization__"
"""The attribute ``requires_authorization`` sets on the service it decorates."""

security_logger = logging.getLogger(SECURITY_LOGGER)
router = APIRouter(tags=["auth"])
UNSCOPED: Final = [Depends(Require(IsUnscoped()))]


class ActionNotAuthorized(PermissionDenied):
    """No authorization holds for this act: ask for one, then try again (403).

    It is unknown, another user's or another login's, for another purpose,
    unconfirmed, spent, expired, for other arguments, or its code does not
    cover the row; which one is never said.
    """

    code = "action_not_authorized"

    def __init__(self) -> None:
        """Say what to do, and nothing of why."""
        super().__init__(
            "This act is not authorized: ask for an authorization of it, confirm "
            "it, then try again."
        )


class AuthorizationRequired(APIError):
    """The act needs an authorization of its own, and none came with it (401).

    ``WWW-Authenticate`` says so as RFC 9470 does
    (``error="insufficient_user_authentication"``), with the purpose; the
    body names it too, in ``errors``, at the ``Action-Authorization`` header.
    """

    def __init__(self, purpose: str) -> None:
        """Use status 401 and the code ``authorization_required``."""
        challenge = get_authenticators()[0].challenge(INSUFFICIENT)
        super().__init__(
            status.HTTP_401_UNAUTHORIZED,
            "Authorize this act first: ask for an authorization of it, confirm "
            "it, and send it in the Action-Authorization header.",
            code="authorization_required",
            headers={"WWW-Authenticate": f'{challenge}, purpose="{purpose}"'},
            errors=[
                FieldError(
                    loc=["header", AUTHORIZATION_HEADER],
                    msg=purpose,
                    type="authorization_required",
                )
            ],
        )
        self.purpose = purpose


class AuthorizationNotFound(NotFound):
    """No authorization of the user's, or of the login's, has that id (404)."""

    code = "authorization_not_found"


class AuthorizationNeedsOtherFactors(APIError):
    """The act asks confirming it to prove what this way does not (409)."""

    def __init__(self) -> None:
        """Use status 409 and the code ``authorization_needs_other_factors``."""
        super().__init__(
            status.HTTP_409_CONFLICT,
            "This act must be confirmed another way.",
            code="authorization_needs_other_factors",
        )


class AuthorizationChanged(APIError):
    """What the device was shown is not what the authorization holds (409)."""

    def __init__(self) -> None:
        """Use status 409 and the code ``authorization_changed``."""
        super().__init__(
            status.HTTP_409_CONFLICT,
            "This act is not the one you were shown.",
            code="authorization_changed",
        )


def _closed() -> APIError:
    """409 ``authorization_confirmed``: confirmed, spent, or ended already."""
    return APIError(
        status.HTTP_409_CONFLICT,
        "This authorization was confirmed already, or ended.",
        code="authorization_confirmed",
    )


def _expired() -> APIError:
    """409 ``authorization_expired``: confirm it no more; ask again."""
    return APIError(
        status.HTTP_409_CONFLICT,
        "This authorization can no longer be confirmed; ask for a new one.",
        code="authorization_expired",
    )


# The act ------------------------------------------------------------------------


type Show = Callable[[Any], Awaitable[Shown] | Shown]
"""What builds what the user is shown of a command."""


@dataclass(frozen=True, slots=True, eq=False)
class Action:
    """An act that asks for its own authorization: its purpose, command, and words.

    ``purpose`` is a lower-case name, one action per purpose. ``command``
    is the type its service takes; the digest is computed from it, read
    back through pydantic, less the fields ``ignore`` names (a time, a
    fresh id). ``subject`` says what the act is on, ``"<type>:<id>"``, at
    most 200 characters: an internal id, kept for the trail. ``show``
    builds what the user is shown (``Shown``), from the command and the
    stored state, in the request's language. ``ttl`` and ``methods`` take
    the place of ``AUTH.AUTHORIZATIONS.TTL`` and ``METHODS`` for this act.
    """

    purpose: str
    _: KW_ONLY
    command: type
    subject: Callable[[Any], str]
    show: Show
    ignore: Collection[str] = ()
    ttl: timedelta | None = None
    methods: Collection[str] | None = None
    adapter: TypeAdapter[Any] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        """Check what the act declares; build its command's adapter.

        Raises:
            ImproperlyConfigured: The purpose is not a lower-case name of at
                most 100 characters; ``command`` is not a class; ``ignore``
                names a field the command lacks; ``ttl`` is not more than
                zero and at most 30 minutes; or ``methods`` names others than
                ``pwd``, ``otp``, and ``phone``, lacks ``otp`` and ``phone``,
                or repeats one.
        """
        if (
            not isinstance(self.purpose, str)
            or len(self.purpose) > PURPOSE_MAX_LENGTH
            or not PURPOSE.fullmatch(self.purpose)
        ):
            raise ImproperlyConfigured(
                "An action's purpose is a lower-case name of at most "
                f"{PURPOSE_MAX_LENGTH} characters, such as "
                f"organizations.end_membership: {self.purpose!r}."
            )
        if not isinstance(self.command, type):
            raise ImproperlyConfigured(
                f"Action {self.purpose!r}: command is the class its service takes."
            )
        if unknown := sorted(set(self.ignore) - _fields_of(self.command)):
            raise ImproperlyConfigured(
                f"Action {self.purpose!r} ignores fields {self.command.__name__} "
                f"lacks: {', '.join(unknown)}."
            )
        if self.ttl is not None and not timedelta(0) < self.ttl <= (
            MAX_AUTHORIZATION_TTL
        ):
            raise ImproperlyConfigured(
                f"Action {self.purpose!r}: ttl must be more than 0 and 30 minutes "
                "at most."
            )
        if self.methods is not None:
            methods = tuple(self.methods)
            try:
                if unknown := sorted(set(methods) - METHOD_NAMES):
                    raise ValueError(f"unknown methods {', '.join(unknown)}")
                with_a_second_factor(methods)  # type: ignore[arg-type]
            except ValueError as exc:
                raise ImproperlyConfigured(
                    f"Action {self.purpose!r}: methods: {exc}."
                ) from None
            object.__setattr__(self, "methods", frozenset(methods))
        object.__setattr__(self, "ignore", frozenset(self.ignore))
        object.__setattr__(self, "adapter", TypeAdapter(self.command))

    def required(self) -> frozenset[str]:
        """What confirming it must prove: ``methods``, or the setting's."""
        if self.methods is not None:
            return frozenset(self.methods)
        return frozenset(auth_settings().AUTHORIZATIONS.METHODS)

    def lifetime(self) -> timedelta:
        """How long its authorizations work: ``ttl``, or the setting's."""
        return self.ttl or auth_settings().AUTHORIZATIONS.TTL

    def arguments(self, command: object) -> dict[str, Any]:
        """``command`` read back as JSON, less the fields ``ignore`` names.

        An uploaded file (``StoredUpload``), wherever it is in the command,
        is bound by its content: its type, size, file name and SHA-256,
        never the name the storage gave it, which is new on every request.
        One without a SHA-256 is bound by its name (ADR 0167).

        Raises:
            TypeError: ``command`` is not the action's command.
        """
        if not isinstance(command, self.command):
            raise TypeError(
                f"Action {self.purpose!r} takes a {self.command.__name__}, not a "
                f"{type(command).__name__}."
            )
        dumped: dict[str, Any] = self.adapter.dump_python(command, mode="json")
        return {
            key: _by_content(getattr(command, key, None), value)
            for key, value in dumped.items()
            if key not in self.ignore
        }

    def digest_of(self, command: object) -> str:
        """The act's digest: ``fingerprint(purpose, arguments)`` (64 hex digits)."""
        return fingerprint(self.purpose, self.arguments(command))

    def subject_of(self, command: object) -> str:
        """What the act is on: ``subject(command)``, checked.

        Raises:
            ImproperlyConfigured: It is not ``"<type>:<id>"`` of at most 200
                characters (letters, digits, and ``_ . : / -``).
        """
        subject = self.subject(command)
        if (
            not isinstance(subject, str)
            or len(subject) > SUBJECT_MAX_LENGTH
            or not SUBJECT.fullmatch(subject)
        ):
            raise ImproperlyConfigured(
                f"Action {self.purpose!r}: subject(...) must return '<type>:<id>', "
                f"an internal id of at most {SUBJECT_MAX_LENGTH} characters "
                "(letters, digits, and _ . : / -)."
            )
        return subject

    async def shown(self, command: object) -> Shown:
        """What the user is shown of ``command``: ``show``'s answer.

        Raises:
            ImproperlyConfigured: ``show`` returned no ``Shown``.
        """
        result = self.show(command)
        if inspect.isawaitable(result):
            result = await result
        if not isinstance(result, Shown):
            raise ImproperlyConfigured(
                f"Action {self.purpose!r}: show(...) must return a Shown."
            )
        return result

    async def __call__(self, command: object) -> Shown:
        """As ``shown``: the hook point an action registers on holds callables."""
        return await self.shown(command)


def _by_content(value: object, dumped: Any) -> Any:  # noqa: ANN401 - JSON values.
    """``dumped``, the JSON of ``value``, without the storage's name of an upload.

    Wherever the upload is: a field, a list, a mapping, a model or a
    dataclass inside. One without its SHA-256 has no content to be bound
    by, and keeps its name: it matches no other request. The upload's
    class exists only once its module is loaded: a process that never
    loaded it holds none, and loads nothing here.
    """
    module = sys.modules.get("domainkit.storage.base")
    upload: type | None = getattr(module, "StoredUpload", None)
    if upload is None:
        return dumped
    if isinstance(value, upload) and isinstance(dumped, dict):
        if not getattr(value, "sha256", ""):
            return dumped
        return {key: item for key, item in dumped.items() if key != "name"}
    if isinstance(dumped, list) and isinstance(value, list | tuple):
        return [_by_content(*pair) for pair in zip(value, dumped, strict=True)]
    if isinstance(dumped, dict):
        if isinstance(value, Mapping):
            inside = {str(key): item for key, item in value.items()}
            return {
                key: _by_content(inside.get(key), item) for key, item in dumped.items()
            }
        return {
            key: _by_content(getattr(value, key, None), item)
            for key, item in dumped.items()
        }
    return dumped


def _fields_of(command: type) -> set[str]:
    """The names of the fields of ``command``: a dataclass, a model, or a class."""
    if dataclasses.is_dataclass(command):
        return {each.name for each in dataclasses.fields(command)}
    model_fields = getattr(command, "model_fields", None)
    if isinstance(model_fields, dict):
        return set(model_fields)
    return set(get_type_hints(command))


register_action: Final[HookPoint[Action]] = HookPoint(
    "action", arguments=1, awaited=True
)
"""Register an ``Action`` (``register_action(END_MEMBERSHIP)`` in ``ready()``)."""


def registered_actions() -> dict[str, Action]:
    """Every registered action, by purpose.

    Raises:
        ImproperlyConfigured: Two actions are registered with one purpose.
    """
    found: dict[str, Action] = {}
    for hook in register_action.hooks():
        if hook.call.purpose in found and found[hook.call.purpose] is not hook.call:
            raise ImproperlyConfigured(
                f"Two actions are registered as {hook.call.purpose!r}."
            )
        found[hook.call.purpose] = hook.call
    return found


def action_for(purpose: str) -> Action | None:
    """The registered action of ``purpose``, if there is one."""
    return registered_actions().get(purpose)


# Ways to confirm -----------------------------------------------------------------


@dataclass(frozen=True, slots=True, eq=False)
class ActionConfirmation:
    """A way to confirm action authorizations: what it proves, and who has it.

    ``methods`` is what a confirmation this way proves (RFC 8176's names,
    and ``phone``): a code ``otp`` (``pwd`` with the password), the phone
    ``hwk`` and ``phone``, a passkey ``hwk`` and ``mfa``. ``available``
    says whether a user can confirm this way now. Register one with
    ``on_action_confirmation``: asking for an authorization needs a way the
    user has, and ``check`` refuses an action no way proves (``auth.E108``).
    """

    name: str
    methods: frozenset[str]
    available: Callable[[UUID], Awaitable[bool]]

    async def __call__(self, user_id: UUID) -> bool:
        """Whether the user ``user_id`` can confirm this way now."""
        return await self.available(user_id)


on_action_confirmation: Final[HookPoint[ActionConfirmation]] = HookPoint(
    "action confirmation", arguments=1, awaited=True
)
"""Register a way to confirm action authorizations (an ``ActionConfirmation``).

Auth registers the code of the authenticator app; an app that confirms
another way (the phone, a passkey) registers its own in ``ready()``, then
calls ``confirm_action_with``.
"""


async def _has_authenticator(user_id: UUID) -> bool:
    """Whether the user has a confirmed authenticator app."""
    async with AuthUnitOfWork() as uow:
        return await uow.mfa.enabled(user_id)


CODE_CONFIRMATION: Final = ActionConfirmation(
    "code", frozenset({"otp", "pwd"}), _has_authenticator
)
"""A code of the authenticator app, and the password where the act asks it
(``POST /authorizations/{id}/confirm``)."""


def provable(required: frozenset[str]) -> bool:
    """Whether some registered way to confirm proves ``required``."""
    return any(required <= proven(hook.call.methods) for hook in on_action_confirmation)


async def _can_confirm(user_id: UUID, required: frozenset[str]) -> bool:
    """Whether the user has a way to confirm that proves ``required``."""
    for hook in on_action_confirmation:
        if required <= proven(hook.call.methods) and await hook.call(user_id):
            return True
    return False


# The binding ---------------------------------------------------------------------


def login_digest(credentials: Credentials) -> str:
    """The login's digest: the SHA-256 of its scheme and token, as the phone keeps it.

    For a login token or a browser session, its token's digest; for a JSON
    Web Token, its login. Every access token of one login has the same.
    """
    held = credentials.token_id or str(credentials.login)
    return hashlib.sha256(f"{credentials.scheme}:{held}".encode()).hexdigest()


def _login_of(connection: HTTPConnection) -> str | None:
    """The digest of the connection's login, if its credentials can be told apart."""
    credentials = connection.scope.get("auth")
    if not isinstance(credentials, Credentials):
        return None
    if credentials.scheme == JWT_SCHEME and credentials.login is not None:
        return login_digest(credentials)
    if credentials.scheme in STEPPING_SCHEMES and credentials.token_id is not None:
        return login_digest(credentials)
    return None


def canonical(authorization: ActionAuthorization) -> bytes:
    """The bytes an authorization's code covers: one encoding per act.

    A JSON array of the encoding's name, the id (the nonce), the user, the
    login's digest, the purpose, the subject, the act's digest, the
    SHA-256 of what was shown, and the expiry in ISO 8601, in UTC.
    """
    return json.dumps(
        [
            ENCODING,
            str(authorization.id),
            str(authorization.user_id),
            authorization.login,
            authorization.purpose,
            authorization.subject,
            authorization.digest,
            authorization.shown.digest(),
            _utc(authorization.expires_at),
        ],
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode()


def _utc(moment: datetime) -> str:
    """``moment`` in UTC, ISO 8601, to the microsecond."""
    return moment.astimezone(UTC).isoformat(timespec="microseconds")


def authorization_code(
    authorization: ActionAuthorization, key: bytes | None = None
) -> str:
    """The code that binds an authorization to its row's values.

    HMAC-SHA256 of ``canonical(authorization)`` under a key derived from
    ``SECRET_KEY`` (or ``key``), in base64url without padding.
    """
    secret = derive_keys(KEY_PURPOSE)[0] if key is None else key
    digest = hmac.new(secret, canonical(authorization), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


def _matches(code: str, authorization: ActionAuthorization) -> bool:
    """Whether ``code`` is the authorization's code, under any key."""
    return any(
        hmac.compare_digest(
            code.encode(), authorization_code(authorization, key).encode()
        )
        for key in derive_keys(KEY_PURPOSE)
    )


def _authorization(authorization: ActionAuthorization) -> str:
    """``<id>.<code>``: what spends ``authorization``, once."""
    return f"{authorization.id}.{authorization_code(authorization)}"


def _parsed(header: str) -> tuple[UUID, str]:
    """The id and the code of ``<id>.<code>``.

    A malformed id is the nil UUID, which no authorization has (``uuid4``).
    """
    head, _, code = header.partition(".")
    try:
        return UUID(head), code
    except ValueError:
        return UUID(int=0), code


# Asking ----------------------------------------------------------------------------


class DetailOut(BaseModel):
    """One labelled value the user is shown."""

    label: str
    value: str


class ShownOut(BaseModel):
    """What the user is shown of the act: kept with it, never sent by a page."""

    title: str
    details: list[DetailOut]


class ActionAuthorizationOut(BaseModel):
    """An authorization asked for: confirm it, then send it with the act."""

    id: UUID
    purpose: str
    shown: ShownOut
    expires_at: datetime

    @classmethod
    def of(cls, authorization: ActionAuthorization) -> "ActionAuthorizationOut":
        """What a client is told of ``authorization``."""
        return cls(
            id=authorization.id,
            purpose=authorization.purpose,
            shown=_shown_out(authorization.shown),
            expires_at=authorization.expires_at,
        )


def _shown_out(shown: Shown) -> ShownOut:
    """``shown``, as a client reads it."""
    return ShownOut(
        title=shown.title,
        details=[DetailOut(label=label, value=value) for label, value in shown.details],
    )


async def request_action_authorization(
    request: HTTPConnection, action: Action, command: object
) -> ActionAuthorization:
    """Store an authorization of ``command`` for the request's user and login.

    The server builds each part: the subject (``action.subject``), the
    digest of the arguments, what is shown (``action.show``, in the
    request's language), and the login's digest. It works for the
    action's TTL. A login holds at most ten unconfirmed authorizations:
    an eleventh ends the oldest.

    Raises:
        NotAuthenticated: The request names no user.
        ActionNotAuthorized: Staff act as the user: the act is the user's.
        APIError: 400 ``step_up_unsupported``: the credentials are limited
            to scopes, or cannot be told apart from the user's others.
        NoSecondFactor: The user has no way to confirm what the act asks.
        TypeError: ``command`` is not the action's command.
    """
    from .stepup import NoSecondFactor

    user = request.scope.get("user")
    if not isinstance(user, AbstractBaseUser):
        raise NotAuthenticated(get_authenticators()[0].challenge())
    if impersonated(request):
        raise ActionNotAuthorized
    login = _login_of(request)
    credentials = request.scope.get("auth")
    if login is None or getattr(credentials, "scopes", None) is not None:
        raise APIError(
            status.HTTP_400_BAD_REQUEST,
            "These credentials cannot ask for an authorization: log in, then ask.",
            code="step_up_unsupported",
        )
    subject = action.subject_of(command)
    digest = action.digest_of(command)
    if not await _can_confirm(user.id, action.required()):
        raise NoSecondFactor
    shown = await action.shown(command)
    now = utc_now()
    authorization = ActionAuthorization(
        id=uuid4(),
        user_id=user.id,
        login=login,
        purpose=action.purpose,
        subject=subject,
        digest=digest,
        shown=shown,
        created_at=now,
        expires_at=now + action.lifetime(),
    )
    async with AuthUnitOfWork() as uow:
        await uow.authorizations.add(authorization)
        await uow.authorizations.end_unconfirmed(login, keep=MAX_UNCONFIRMED, now=now)
        await uow.commit()
    return authorization


# The rule's side: checked once, kept for the act ---------------------------------


@dataclass(slots=True)
class _Accepted:
    """An authorization a rule accepted for the request's act."""

    authorization: ActionAuthorization
    code: str
    route: str
    taken: bool = False
    """Whether a decorated service took it: spent, or tried to."""
    unheard: str | None = None
    """A refusal the trail is told of at the end of the request: one made in
    a transaction still open, where telling it then could wait on itself."""


class Kept:
    """The authorizations a request's rules accepted, by purpose, for its act."""

    __slots__ = ("accepted",)

    def __init__(self) -> None:
        """None yet."""
        self.accepted: dict[str, _Accepted] = {}


_kept: ContextVar[Kept | None] = ContextVar("domainkit_auth_kept", default=None)
"""The request's kept authorizations, for the service that spends one."""


@dataclass(slots=True)
class _Verdict:
    """What a purpose's rule found on a request."""

    sent: bool
    """Whether the request sent an ``Action-Authorization`` header."""
    authorization: ActionAuthorization | None


def _kept_on(connection: HTTPConnection) -> Kept:
    """The connection's kept authorizations: the middleware's, or new ones."""
    kept: Kept | None = connection.scope.get(KEPT)
    if kept is None:
        kept = connection.scope[KEPT] = Kept()
        _kept.set(kept)
    return kept


async def accepted_authorization(
    connection: HTTPConnection, user: AbstractBaseUser, purpose: str
) -> ActionAuthorization | None:
    """The authorization of ``purpose`` the request holds, kept for its act.

    It holds when it is the user's, of the request's login, for
    ``purpose``, confirmed, unspent, live, and its code covers its row.
    Read once a request, by id; nothing is written. A header that does not
    hold is told to the ``on_refusal`` hooks once (``invalid_authorization``).
    """
    verdicts: dict[str, _Verdict] = connection.scope.setdefault(VERDICTS, {})
    if purpose in verdicts:
        return verdicts[purpose].authorization
    header = connection.headers.get(AUTHORIZATION_HEADER)
    verdict = verdicts[purpose] = _Verdict(sent=header is not None, authorization=None)
    if header is None:
        return None
    authorization_id, code = _parsed(header)
    login = _login_of(connection)
    found = None
    if login is not None and authorization_id.int:  # The nil UUID: malformed.
        async with AuthUnitOfWork() as uow:
            found = await uow.authorizations.get(authorization_id)
    if (
        found is None
        or found.user_id != user.id
        or found.login != login
        or found.purpose != purpose
        or found.confirmed_at is None
        or found.consumed_at is not None
        or found.expired()
        or not _matches(code, found)
    ):
        await audit_refusal(ACTION, "invalid_authorization")
        return None
    verdict.authorization = found
    _kept_on(connection).accepted[purpose] = _Accepted(
        found, code, _route_of(connection)
    )
    return found


def authorization_refusal(connection: HTTPConnection, purpose: str) -> Exception:
    """Why ``purpose``'s rule refused: no header (401), or one that does not hold."""
    verdict: _Verdict | None = connection.scope.get(VERDICTS, {}).get(purpose)
    if verdict is None or not verdict.sent:
        return AuthorizationRequired(purpose)
    return ActionNotAuthorized()


def _route_of(connection: HTTPConnection) -> str:
    """``METHOD /path`` of the route the connection reached, for the log."""
    route = connection.scope.get("route")
    path = getattr(route, "path", None) or connection.url.path
    return f"{connection.scope.get('method', 'GET')} {path}"


# The act's side: spent once ------------------------------------------------------


type Service = Callable[..., Awaitable[Any]]
"""An application service: ``service(command, uow)``."""

_spending_purposes: set[str] = set()
"""The purposes of the services ``requires_authorization`` decorated."""


def spent_by_a_service(purpose: str) -> bool:
    """Whether a service decorated ``requires_authorization`` spends ``purpose``."""
    return purpose in _spending_purposes


def requires_authorization[F: Service](action: Action) -> Callable[[F], F]:
    """Spend the request's authorization of ``action`` on the service's command.

    The service is ``service(command, uow, ...)``. Called with no
    authorization of the purpose kept by a rule (from another route, a
    batch, a task), it is refused: the act is the user's own. With the
    act when ``uow`` is on auth's database (its first commit spends it);
    otherwise just before the act, in a transaction of its own.

    Raises (when the service is called):
        AuthorizationRequired: No rule kept an authorization of the purpose.
        ActionNotAuthorized: It is for other arguments or another subject
            (it is voided), or was spent meanwhile.
    """

    def decorate(service: F) -> F:
        @functools.wraps(service)
        async def spending(
            command: object,
            uow: object,
            *args: Any,  # noqa: ANN401 - the service's own.
            **kwargs: Any,  # noqa: ANN401 - the service's own.
        ) -> Any:  # noqa: ANN401 - what the service returns.
            accepted = await _take(action, command)
            held = _unit_of_work_of(uow)
            try:
                if held is not None and _on_auth_database(held):
                    joined = _Spending(uow, held, accepted)
                    return await service(command, joined, *args, **kwargs)
                await _spend_alone(accepted)
            except _Spent:  # The transaction that found it spent is over.
                await audit_refusal(ACTION, "used")
                raise
            return await service(command, uow, *args, **kwargs)

        setattr(spending, SPENDS, action.purpose)
        _spending_purposes.add(action.purpose)
        return cast(F, spending)

    return decorate


async def spend_authorization(
    uow: object, action: Action, command: object
) -> ActionAuthorization:
    """Spend the request's authorization of ``action`` on ``command``, in code.

    For code that is not a decorated service: on auth's database, in the
    open transaction of ``uow`` (it commits with ``uow``'s next commit, or
    not at all); otherwise now, in a transaction of its own. Found spent in
    ``uow``'s transaction, the trail hears of it at the end of the request.

    Raises:
        AuthorizationRequired: No rule kept an authorization of the purpose.
        ActionNotAuthorized: It is for other arguments or another subject
            (it is voided), or was spent meanwhile.
        RuntimeError: ``uow`` is on auth's database, and not open.
    """
    accepted = await _take(action, command)
    held = _unit_of_work_of(uow)
    if held is not None and _on_auth_database(held):
        session = getattr(held, "session", None)
        if session is None:
            raise RuntimeError("Spend an authorization in the unit of work's block.")
        users = UserRepository(session)
        setattr(held, USERS, users)
        try:
            await _consume(held, users, accepted)
        except _Spent:
            accepted.unheard = "used"
            raise
        return accepted.authorization
    try:
        await _spend_alone(accepted)
    except _Spent:
        await audit_refusal(ACTION, "used")
        raise
    return accepted.authorization


USERS: Final = "action_authorization_users"
"""The repository a unit of work gets for the user whose authorization it spends."""


async def _take(action: Action, command: object) -> _Accepted:
    """The kept authorization of ``action``, if it binds ``command``; it is taken.

    Raises:
        AuthorizationRequired: None is kept.
        ActionNotAuthorized: It binds other arguments or another subject:
            it is voided, in a transaction of its own.
    """
    kept = _kept.get()
    accepted = None if kept is None else kept.accepted.get(action.purpose)
    if accepted is None:
        raise AuthorizationRequired(action.purpose)
    accepted.taken = True
    asked = replace(
        accepted.authorization,
        digest=action.digest_of(command),
        subject=action.subject_of(command),
    )
    if not _matches(accepted.code, asked):
        async with AuthUnitOfWork() as uow:
            await uow.authorizations.void(accepted.authorization.id, utc_now())
            await uow.commit()
        await audit_refusal(ACTION, "changed")
        raise ActionNotAuthorized
    return accepted


def _unit_of_work_of(uow: object) -> UnitOfWork | None:
    """``uow``, or the unit of work a wrapper of it lends (a route's ``Joined``)."""
    for _ in range(4):  # A wrapper of a wrapper, at most a few deep.
        if isinstance(uow, UnitOfWork):
            return uow
        lent = {
            id(each): each
            for each in getattr(uow, "__dict__", {}).values()
            if isinstance(each, UnitOfWork)
        }
        if len(lent) != 1:
            return None
        [uow] = lent.values()
    return None


def _on_auth_database(uow: UnitOfWork) -> bool:
    """Whether ``uow`` writes to auth's database: the act's spend joins it."""
    alias = getattr(uow, "alias", None)
    return (
        alias is not None
        and not uow.readonly
        and alias
        == database_router.require_standalone(
            database_router.target_for("auth"), "DATABASE_ROUTING['auth']"
        )
    )


async def _consume(uow: UnitOfWork, users: UserRepository, accepted: _Accepted) -> None:
    """Spend ``accepted`` in ``uow``'s transaction, and say so with its events.

    The row is written before the act's own changes are flushed: of two
    acts at once, the second waits on it, then finds it spent. The caller
    tells the trail once the transaction is over.

    Raises:
        _Spent: It was spent, voided, or expired meanwhile.
    """
    authorization = accepted.authorization
    session = getattr(uow, "session")  # noqa: B009 - set once the block opens.
    with session.no_autoflush:
        spent = await AuthorizationStore(session).consume(authorization.id, utc_now())
    if not spent:
        raise _Spent
    stored = await users.get(UserId(authorization.user_id))
    stored.record(_used(authorization))


class _Spent(ActionNotAuthorized):
    """The authorization was spent, voided, or expired since the rule read it."""

    code = "action_not_authorized"


async def _spend_alone(accepted: _Accepted) -> None:
    """Spend ``accepted`` now, in a transaction of its own: at most once.

    Raises:
        _Spent: It was spent, voided, or expired meanwhile.
    """
    async with AuthUnitOfWork() as uow:
        await _consume(uow, uow.users, accepted)
        await uow.commit()


def _used(authorization: ActionAuthorization) -> ActionAuthorizationUsed:
    """The event of ``authorization`` spent."""
    return ActionAuthorizationUsed(
        user_id=authorization.user_id,
        authorization_id=authorization.id,
        purpose=authorization.purpose,
        subject=authorization.subject,
        digest=authorization.digest,
        methods=tuple(sorted(authorization.methods)),
        device_id=authorization.device_id,
    )


class _Spending:
    """A service's unit of work on auth's database, which spends with its commit.

    Its first commit spends the authorization in the same transaction,
    with ``ActionAuthorizationUsed`` among the act's events: spent if and
    only if the act commits. Everything else is the unit of work's own.
    """

    def __init__(self, uow: object, held: UnitOfWork, accepted: _Accepted) -> None:
        """Wrap ``uow`` (the unit of work ``held``, or a wrapper lending it)."""
        self._uow = uow
        self._held = held
        self._accepted = accepted
        self.spent = False
        """Whether a commit spent the authorization."""

    def __getattr__(self, name: str) -> Any:  # noqa: ANN401 - the unit of work's.
        """The wrapped unit of work's attribute (its repositories, ``run``, ...)."""
        return getattr(self._uow, name)

    async def __aenter__(self) -> "_Spending":
        """Enter the unit of work as it enters."""
        await self._uow.__aenter__()  # type: ignore[attr-defined]
        return self

    async def __aexit__(self, *exc: object) -> None:
        """Leave the unit of work as it leaves."""
        await self._uow.__aexit__(*exc)  # type: ignore[attr-defined]

    async def commit(self) -> None:
        """Commit, spending the authorization with the first commit.

        Raises:
            _Spent: It was spent, voided, or expired meanwhile: nothing
                commits.
        """
        if self.spent:
            await self._uow.commit()  # type: ignore[attr-defined]
            return
        users = UserRepository(getattr(self._held, "session"))  # noqa: B009
        await _consume(self._held, users, self._accepted)
        setattr(self._held, USERS, users)
        try:
            await self._uow.commit()  # type: ignore[attr-defined]
        finally:
            delattr(self._held, USERS)
        self.spent = True


# The end of a request ------------------------------------------------------------


@contextmanager
def keeping_authorizations(
    scope: Scope, receive: Receive, send: Send
) -> Iterator[Send]:
    """Keep what the request's rules accept; settle it as the response starts.

    ``AuthenticationMiddleware`` wraps each HTTP request of a user in it,
    and sends through what it yields.
    """
    settling = _Settling(scope, receive, send)
    token = _kept.set(settling.kept)
    try:
        yield settling
    finally:
        _kept.reset(token)


class _Settling:
    """A response's ``send``, which settles the request's authorizations first."""

    def __init__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Keep the request's authorizations in its scope."""
        self.kept = scope[KEPT] = Kept()
        self.scope = scope
        self.receive = receive
        self.send = send
        self.started = False
        self.replaced = False

    async def __call__(self, message: Message) -> None:
        """Send ``message``; before the response starts, settle what was kept."""
        if self.replaced:
            return
        if message["type"] == "http.response.start" and not self.started:
            self.started = True
            if self.kept.accepted and await settle(self.kept, message["status"]):
                self.replaced = True
                response = error_response(
                    status.HTTP_500_INTERNAL_SERVER_ERROR,
                    "A rule accepted an authorization that no service spent: "
                    "decorate the act's service with requires_authorization.",
                    code="authorization_unspent",
                )
                await response(self.scope, self.receive, self.send)
                return
        await self.send(message)


async def settle(kept: Kept, status_code: int) -> bool:
    """Settle what the rules accepted; whether the request must fail.

    The trail hears of the refusals made in transactions that were open.
    Then, of a response that succeeded, what no service took is voided:
    the act may have happened without spending its authorization (a
    service left undecorated). Each is logged
    (``auth.authorization_unspent``) and told to the ``on_refusal`` hooks
    (``unspent``). Under ``DEBUG`` and in tests, the request then fails, so
    a forgotten decorator shows.
    """
    for accepted in kept.accepted.values():
        if accepted.unheard is not None:
            await audit_refusal(ACTION, accepted.unheard)
            accepted.unheard = None
    unspent = [each for each in kept.accepted.values() if not each.taken]
    if not unspent or status_code >= status.HTTP_400_BAD_REQUEST:
        return False
    for accepted in unspent:
        async with AuthUnitOfWork() as uow:
            await uow.authorizations.void(accepted.authorization.id, utc_now())
            await uow.commit()
        security_logger.warning(
            "auth.authorization_unspent: %s accepted an authorization of %s that "
            "no service spent; it is void.",
            accepted.route,
            accepted.authorization.purpose,
        )
        await audit_refusal(ACTION, "unspent")
    return bool(settings.DEBUG) or settings.ENVIRONMENT == "test"


# Confirming ------------------------------------------------------------------------


async def action_authorization(
    authorization_id: UUID, user_id: UUID, *, login: str | None = None
) -> ActionAuthorization:
    """The user's authorization ``authorization_id``, to show it, in any state.

    Its purpose, the kept title and details (``shown``), the digest, the
    expiry, the user, and the login's digest. With ``login``, only that
    login's.

    Raises:
        AuthorizationNotFound: There is none, or it is another user's (or
            another login's).
    """
    async with AuthUnitOfWork() as uow:
        found = await uow.authorizations.get(authorization_id)
    if (
        found is None
        or found.user_id != user_id
        or (login is not None and found.login != login)
    ):
        raise AuthorizationNotFound("No such authorization.")
    return found


async def linked_action_authorization(
    sign_in_request_id: UUID, user_id: UUID, *, login: str | None = None
) -> ActionAuthorization:
    """The user's authorization the phone's request ``sign_in_request_id`` confirms.

    The one ``link_action_authorization`` linked to it, in any state. With
    ``login``, only that login's.

    Raises:
        AuthorizationNotFound: None of the user's (or the login's) is linked
            to it.
    """
    async with AuthUnitOfWork() as uow:
        found = await uow.authorizations.linked(user_id, sign_in_request_id)
    if found is None or (login is not None and found.login != login):
        raise AuthorizationNotFound("No such authorization.")
    return found


async def confirmed_action_authorization(
    authorization_id: UUID, user_id: UUID, *, login: str | None = None
) -> str:
    """``<id>.<code>`` of the user's confirmed authorization, for the confirming app.

    For an app that confirms in one request (``confirm_action_with``) and
    hands the authorization over in another: the phone approves, then the
    page that asked it redeems the answer. Only a confirmed authorization,
    unspent and live, has one. With ``login``, only that login's.

    Raises:
        AuthorizationNotFound: None of the user's (or the login's) has that
            id, or it is unconfirmed.
        APIError: 409 ``authorization_confirmed``: spent, or ended; 409
            ``authorization_expired``.
    """
    authorization = await action_authorization(authorization_id, user_id, login=login)
    if authorization.confirmed_at is None:
        raise AuthorizationNotFound("No such authorization.")
    if authorization.consumed_at is not None:
        raise _closed()
    if authorization.expired():
        raise _expired()
    return _authorization(authorization)


async def link_action_authorization(
    authorization_id: UUID,
    *,
    user_id: UUID,
    sign_in_request_id: UUID | None = None,
    passkey_challenge: str | None = None,
) -> ActionAuthorization:
    """Link what will confirm the authorization: the phone's request, or a passkey's.

    ``sign_in_request_id`` is the phone's sign-in request
    (``domainkit.contrib.ciba``); ``passkey_challenge`` the challenge made
    for it (``domainkit.contrib.devices``). A link not given stays.

    Raises:
        AuthorizationNotFound: None of the user's has that id.
        APIError: 409 ``authorization_confirmed``: confirmed, spent, or
            ended already; 409 ``authorization_expired``.
    """
    authorization = await action_authorization(authorization_id, user_id)
    _waiting(authorization)
    async with AuthUnitOfWork() as uow:
        linked = await uow.authorizations.link(
            authorization_id,
            utc_now(),
            sign_in_request_id=sign_in_request_id,
            passkey_challenge=passkey_challenge,
        )
        await uow.commit()
    if not linked:
        raise _expired()
    return replace(
        authorization,
        sign_in_request_id=sign_in_request_id or authorization.sign_in_request_id,
        passkey_challenge=passkey_challenge or authorization.passkey_challenge,
    )


async def confirm_action_with(
    authorization_id: UUID,
    methods: Iterable[str],
    *,
    user_id: UUID,
    login: str | None = None,
    digest: str | None = None,
    device_id: UUID | None = None,
    algorithm: int | None = None,
    public_key: bytes | None = None,
    signature: bytes | None = None,
    signed: bytes | None = None,
    signed_at: datetime | None = None,
) -> str:
    """Confirm the authorization, which ``methods`` proved; return ``<id>.<code>``.

    For an app that proves a factor auth does not check itself: the phone's
    approval (``hwk``, ``phone``), the app's own act, a passkey (``hwk``,
    ``mfa``). ``methods`` must prove what the act asks, with what each
    stands for (``STANDS_FOR``). With ``login``, only that login's
    authorization; with ``digest``, only if it is the act's (what the
    device was shown and signed). The device's id, its key's algorithm and
    public key, what it signed, its signature, and when are kept with the
    row; with ``domainkit.contrib.evidence`` installed, a signed
    confirmation goes into the evidence ledger too, which outlives the row.

    Raises:
        AuthorizationNotFound: None of the user's (or the login's) has that id.
        AuthorizationChanged: ``digest`` is not the act's.
        AuthorizationNeedsOtherFactors: ``methods`` do not prove what it asks.
        APIError: 409 ``authorization_confirmed``: confirmed, spent, or
            ended already; 409 ``authorization_expired``.
    """
    authorization = await action_authorization(authorization_id, user_id, login=login)
    if digest is not None and not hmac.compare_digest(
        digest.encode(), authorization.digest.encode()
    ):
        raise AuthorizationChanged
    _waiting(authorization)
    proved = frozenset(methods)
    if not _required(authorization) <= proven(proved):
        raise AuthorizationNeedsOtherFactors
    device = {
        "device_id": device_id,
        "algorithm": algorithm,
        "public_key": public_key,
        "signature": signature,
        "signed": signed,
        "signed_at": signed_at,
    }
    async with AuthUnitOfWork() as uow:
        confirmed = not authorization.expired() and await _record_confirmation(
            uow, authorization, tuple(sorted(proved)), **device
        )
        if confirmed and signature is not None:
            _defer_evidence(uow, authorization, tuple(sorted(proved)), **device)
        if confirmed:
            await uow.commit()
    if not confirmed:
        raise _expired()
    return _authorization(authorization)


def _waiting(authorization: ActionAuthorization) -> None:
    """Refuse an authorization that waits no more.

    Raises:
        APIError: 409 ``authorization_confirmed``; 409 ``authorization_expired``.
    """
    if authorization.confirmed_at is not None or authorization.consumed_at is not None:
        raise _closed()
    if authorization.expired():
        raise _expired()


def _required(authorization: ActionAuthorization) -> frozenset[str]:
    """What confirming ``authorization`` must prove: its action's, or the setting's."""
    action = action_for(authorization.purpose)
    if action is not None:
        return action.required()
    return frozenset(auth_settings().AUTHORIZATIONS.METHODS)


async def _record_confirmation(
    uow: AuthUnitOfWork,
    authorization: ActionAuthorization,
    methods: tuple[str, ...],
    **device: Any,  # noqa: ANN401 - the device's proof, as the store takes it.
) -> bool:
    """Confirm ``authorization`` by ``methods`` in ``uow``; False if it can no more."""
    if not await uow.authorizations.confirm(
        authorization.id, utc_now(), methods, **device
    ):
        return False
    stored = await uow.users.get(UserId(authorization.user_id))
    stored.record(
        ActionAuthorized(
            user_id=authorization.user_id,
            authorization_id=authorization.id,
            purpose=authorization.purpose,
            subject=authorization.subject,
            digest=authorization.digest,
            methods=methods,
            device_id=device.get("device_id"),
        )
    )
    return True


EVIDENCE: Final = "domainkit.contrib.evidence"
"""Where a signed confirmation is kept too, when installed."""


def _defer_evidence(
    uow: AuthUnitOfWork,
    authorization: ActionAuthorization,
    methods: tuple[str, ...],
    **device: Any,  # noqa: ANN401 - the device's proof.
) -> None:
    """Record the device's signed confirmation on the evidence ledger, once committed.

    The authorization, its purpose, subject, and digest, the methods, and
    the device's id, key, what it signed, its signature, and when: enough to
    check the approval again once the device and the row are gone.
    """
    if not registry.is_installed(EVIDENCE):
        return
    from domainkit.contrib.evidence import Evidence
    from domainkit.contrib.evidence.tasks import defer_evidence

    def encoded(value: bytes | None) -> str | None:
        return None if value is None else base64.urlsafe_b64encode(value).decode()

    signed_at: datetime | None = device.get("signed_at")
    defer_evidence(
        uow,
        Evidence(
            kind="auth.action_authorized",
            subject=f"auth.user:{authorization.user_id}",
            facts={
                "authorization_id": str(authorization.id),
                "purpose": authorization.purpose,
                "subject": authorization.subject,
                "digest": authorization.digest,
                "methods": list(methods),
                "device_id": None
                if device.get("device_id") is None
                else str(device["device_id"]),
                "algorithm": device.get("algorithm"),
                "public_key": encoded(device.get("public_key")),
                "signed": encoded(device.get("signed")),
                "signature": encoded(device.get("signature")),
                "signed_at": None if signed_at is None else _utc(signed_at),
            },
        ),
    )


class ConfirmIn(BaseModel):
    """A code of the authenticator app, and the password where the act asks it."""

    model_config = ConfigDict(extra="forbid")

    code: str = Field(min_length=1, max_length=64, repr=False)
    password: str | None = Field(
        default=None, min_length=1, max_length=PASSWORD_MAX_LENGTH, repr=False
    )
    """Needed where the act asks ``pwd``; ignored otherwise."""


class AuthorizationCodeOut(BaseModel):
    """The authorization: send it with the act, in ``Action-Authorization``, once."""

    authorization: str = Field(repr=False)
    expires_at: datetime


class ActionAuthorizationState(ActionAuthorizationOut):
    """An authorization, as the session that asked sees it."""

    confirmed: bool
    methods: list[str]
    """What confirming it must prove."""


@router.get(
    "/authorizations/{authorization_id}",
    dependencies=UNSCOPED,
    responses=error_responses(status.HTTP_401_UNAUTHORIZED, status.HTTP_404_NOT_FOUND),
)
async def show_authorization(
    authorization_id: UUID,
    request: Request,
    response: Response,
    user: AuthenticatedUser,
) -> ActionAuthorizationState:
    """What one of the session's authorizations shows, and whether it is confirmed."""
    no_store(response)
    authorization = await _own(request, authorization_id, user)
    return ActionAuthorizationState(
        **ActionAuthorizationOut.of(authorization).model_dump(),
        confirmed=authorization.confirmed_at is not None,
        methods=sorted(_required(authorization)),
    )


@router.post(
    "/authorizations/{authorization_id}/confirm",
    dependencies=UNSCOPED,
    responses=error_responses(
        status.HTTP_400_BAD_REQUEST,
        status.HTTP_401_UNAUTHORIZED,
        status.HTTP_404_NOT_FOUND,
        status.HTTP_409_CONFLICT,
        status.HTTP_429_TOO_MANY_REQUESTS,
    ),
)
async def confirm_authorization(
    authorization_id: UUID,
    body: ConfirmIn,
    request: Request,
    response: Response,
    user: AuthenticatedUser,
) -> AuthorizationCodeOut:
    """Confirm one of the session's authorizations with a code; return it."""
    from .stepup import verify_factors

    no_store(response)
    authorization = await _own(request, authorization_id, user)
    _waiting(authorization)
    required = _required(authorization)
    methods = tuple(sorted(required & {"pwd", "otp"} | {"otp"}))
    if not required <= proven(frozenset(methods)):
        raise AuthorizationNeedsOtherFactors

    async def confirm(uow: AuthUnitOfWork) -> bool:
        return await _record_confirmation(uow, authorization, methods)

    if authorization.expired() or not await verify_factors(
        request,
        user,
        methods,
        code=body.code,
        password=body.password,
        action=ACTION,
        then=confirm,
    ):
        raise _expired()
    return AuthorizationCodeOut(
        authorization=_authorization(authorization),
        expires_at=authorization.expires_at,
    )


async def _own(
    request: Request, authorization_id: UUID, user: AbstractBaseUser
) -> ActionAuthorization:
    """The authorization ``authorization_id`` of the request's own login.

    Raises:
        AuthorizationNotFound: None of the login's has that id.
    """
    login = _login_of(request)
    if login is None:
        raise AuthorizationNotFound("No such authorization.")
    return await action_authorization(authorization_id, user.id, login=login)
