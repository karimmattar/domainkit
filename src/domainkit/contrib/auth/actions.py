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

A ``@use_case`` is guarded as a copy (ADR 0161):
``requires_authorization(END_MEMBERSHIP)(end_membership)`` is the use case
whose ``run`` spends the authorization, for the route that asks for one;
``end_membership`` itself stays as it was, for the routes that run it
without an act. ``Action(command=end_membership)`` takes the use case's
command. A ``show(command, uow)`` reads what it shows in the unit of work
the act runs in, which the asker gives it: the route's, for a
``ResourceRouter``'s twin.

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

The record an act decides on (ADR 0174): ``Action(binds=...)`` reads it,
``binds(command, uow)``, in the act's unit of work, when the authorization
is asked for and again when the service opens its unit of work. The
digest covers what it read when asked, so the phone signs it too; another
value refuses the act (``409 action_record_changed``) and voids the
authorization. An act held for approval (approvals' ``approval_pending``)
keeps its confirmed authorization until the request expires, within
``AUTHORIZATIONS.HELD_TTL`` of its own expiry (``hold_authorizations``).
"""

import base64
import copy
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
from typing import TYPE_CHECKING, Any, Final, NoReturn, Self, cast, get_type_hints
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, Request, Response, status
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter
from pydantic_core import PydanticSerializationError, to_jsonable_python
from starlette.requests import HTTPConnection
from starlette.types import Message, Receive, Scope, Send

from domainkit.apps import registry
from domainkit.apps.hooks import HookPoint
from domainkit.conf import settings
from domainkit.db.routing import router as database_router
from domainkit.ddd import NotFound, PermissionDenied, UnitOfWork, UseCase, fingerprint
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
    "ActionRecordChanged",
    "AuthorizationChanged",
    "AuthorizationNeedsOtherFactors",
    "AuthorizationNotFound",
    "AuthorizationRequired",
    "Shown",
    "action_authorization",
    "action_for",
    "confirm_action_with",
    "confirmed_action_authorization",
    "hold_authorizations",
    "link_action_authorization",
    "linked_action_authorization",
    "login_digest",
    "on_action_confirmation",
    "on_held_act",
    "register_action",
    "registered_actions",
    "request_action_authorization",
    "requires_authorization",
    "router",
    "spend_authorization",
    "spent_in_code",
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


class ActionRecordChanged(APIError):
    """What the act decides on changed since its authorization was asked for (409).

    Its action ``binds`` the record (ADR 0174): what the act reads of it is
    not what was read when the user was shown it. The authorization is
    voided: ask for a new one, which shows the record as it is now.
    """

    def __init__(self) -> None:
        """Use status 409 and the code ``action_record_changed``."""
        super().__init__(
            status.HTTP_409_CONFLICT,
            "What this act decides on changed since you were shown it: ask for "
            "a new authorization.",
            code="action_record_changed",
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


type Show = Callable[..., Awaitable[Shown] | Shown]
"""What builds what the user is shown of a command: ``show(command)``, or
``show(command, uow)`` to read in the act's unit of work."""
type UnitOfWorkFactory = Callable[[], UnitOfWork]
"""What makes the unit of work a ``show(command, uow)`` reads in: a class."""
type Binds = Callable[[Any, Any], Awaitable[object] | object]
"""What reads the record an act decides on: ``binds(command, uow)``, any
value pydantic writes as JSON (an aggregate's ``version``, its fields)."""


@dataclass(frozen=True, slots=True, eq=False)
class Action:
    """An act that asks for its own authorization: its purpose, command, and words.

    ``purpose`` is a lower-case name, one action per purpose. ``command``
    is the type its service takes, or the ``@use_case`` whose command it
    is; the digest is computed from it, read back through pydantic, less
    the fields ``ignore`` names (a time, a fresh id). ``subject`` says what
    the act is on, ``"<type>:<id>"``, at most 200 characters: an internal
    id, kept for the trail. ``show`` builds what the user is shown
    (``Shown``), from the command and the stored state, in the request's
    language: ``show(command)``, or with a parameter named ``uow``,
    ``show(command, uow)``, given the unit of work the act runs in to read
    in (a ``ResourceRouter``'s twin gives its route's). ``ttl`` and
    ``methods`` take the place of ``AUTH.AUTHORIZATIONS.TTL`` and
    ``METHODS`` for this act; ``AUTHORIZATIONS.ACTIONS`` names its own, in
    place of both. ``binds(command, uow)`` reads the record the act decides
    on, in the act's unit of work, when it is asked for and when it runs:
    the authorization holds while it reads the same (ADR 0174).
    """

    purpose: str
    _: KW_ONLY
    command: type
    """The command's class (a use case given here is replaced by its command)."""
    subject: Callable[[Any], str]
    show: Show
    ignore: Collection[str] = ()
    ttl: timedelta | None = None
    methods: Collection[str] | None = None
    binds: Binds | None = None
    """What the act decides on, read again when it runs (ADR 0174)."""
    adapter: TypeAdapter[Any] = field(init=False, repr=False)
    reads_unit_of_work: bool = field(init=False, repr=False)
    """Whether ``show`` takes the unit of work the act runs in (``uow``)."""

    if TYPE_CHECKING:  # The constructor takes a use case too; the field is a class.

        def __init__(
            self,
            purpose: str,
            *,
            command: type | UseCase[Any],
            subject: Callable[[Any], str],
            show: Show,
            ignore: Collection[str] = (),
            ttl: timedelta | None = None,
            methods: Collection[str] | None = None,
            binds: Binds | None = None,
        ) -> None:
            """Declare the act; ``command`` may be a use case, for its command."""

    def __post_init__(self) -> None:
        """Check what the act declares; build its command's adapter.

        A use case given as ``command`` is replaced by its command.

        Raises:
            ImproperlyConfigured: The purpose is not a lower-case name of at
                most 100 characters; ``command`` is not a class or a use
                case; ``ignore`` names a field the command lacks; ``ttl`` is
                not more than zero and at most 30 minutes; or ``methods``
                names others than ``pwd``, ``otp``, and ``phone``, lacks
                ``otp`` and ``phone``, or repeats one; ``binds`` is not
                callable.
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
        given: object = self.command
        command = given.command if isinstance(given, UseCase) else given
        if not isinstance(command, type):
            raise ImproperlyConfigured(
                f"Action {self.purpose!r}: command is the class its service takes, "
                "or its @use_case."
            )
        object.__setattr__(self, "command", command)
        if unknown := sorted(set(self.ignore) - _fields_of(command)):
            raise ImproperlyConfigured(
                f"Action {self.purpose!r} ignores fields {command.__name__} "
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
        if self.binds is not None and not callable(self.binds):
            raise ImproperlyConfigured(
                f"Action {self.purpose!r}: binds is a function of the command and "
                "the unit of work."
            )
        object.__setattr__(self, "ignore", frozenset(self.ignore))
        object.__setattr__(self, "adapter", TypeAdapter(command))
        object.__setattr__(self, "reads_unit_of_work", _takes_uow(self.show))

    def required(self) -> frozenset[str]:
        """What confirming it must prove: its setting's, ``methods``, or ``METHODS``."""
        config = auth_settings().AUTHORIZATIONS
        own = config.ACTIONS.get(self.purpose)
        if own is not None and own.METHODS is not None:
            return frozenset(own.METHODS)
        if self.methods is not None:
            return frozenset(self.methods)
        return frozenset(config.METHODS)

    def lifetime(self) -> timedelta:
        """How long its authorizations work: its setting's, ``ttl``, or ``TTL``."""
        config = auth_settings().AUTHORIZATIONS
        own = config.ACTIONS.get(self.purpose)
        return (own and own.TTL) or self.ttl or config.TTL

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

    def digest_of(self, command: object, bound: str | None = None) -> str:
        """The act's digest: ``fingerprint(purpose, arguments)`` (64 hex digits).

        With ``bound``, the SHA-256 of what ``binds`` read of the record
        (ADR 0174), the SHA-256 of ``{"action", "arguments", "binds"}`` as
        canonical JSON: the digest covers the record as the user was shown
        it, and the phone signs that.
        """
        arguments = self.arguments(command)
        if bound is None:
            return fingerprint(self.purpose, arguments)
        return _sha256({"action": self.purpose, "arguments": arguments, "binds": bound})

    async def bound_in(self, command: object, uow: object) -> str:
        """The SHA-256 of what ``binds(command, uow)`` reads, as canonical JSON.

        ``uow`` is the act's unit of work, open: it is lent as one that
        cannot commit. Every set is read sorted, wherever it is (a model's
        field, aliased or not, a dataclass, a mapping, a list), so two
        processes agree whatever their hash seeds.

        Raises:
            ImproperlyConfigured: The action binds nothing, or ``binds``
                answered what JSON cannot hold: bytes that are not UTF-8,
                a lone surrogate, NaN or an infinity, an object pydantic
                cannot write.
        """
        if self.binds is None:
            raise ImproperlyConfigured(f"Action {self.purpose!r} binds nothing.")
        read = _Reading(uow, self.purpose, "binds(command, uow)")
        value = await _awaited(self.binds(command, read))
        try:
            return _sha256(to_jsonable_python(_canonical(value)), strict=True)
        except (PydanticSerializationError, ValueError):
            raise ImproperlyConfigured(
                f"Action {self.purpose!r}: binds(command, uow) must answer what "
                f"JSON holds, not this {type(value).__name__}."
            ) from None

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

    async def shown(
        self, command: object, unit_of_work: UnitOfWorkFactory | None = None
    ) -> Shown:
        """What the user is shown of ``command``: ``show``'s answer.

        A ``show(command, uow)`` reads in a unit of work ``unit_of_work``
        makes, the one the act runs in: it is opened for ``show``, lent to
        it as one that cannot commit, and left without a commit.

        Raises:
            ImproperlyConfigured: ``show`` returned no ``Shown``, takes a
                unit of work and none was given, or tried to commit.
        """
        shown, _ = await self.read(command, unit_of_work)
        return shown

    async def read(
        self, command: object, unit_of_work: UnitOfWorkFactory | None = None
    ) -> tuple[Shown, str | None]:
        """What the user is shown of ``command``, and what ``binds`` reads (ADR 0174).

        Both in one unit of work ``unit_of_work`` makes, opened once: what
        ``binds`` reads, then what ``show`` reads. The second value is the
        SHA-256 of what ``binds`` read, or None for an action that binds
        nothing.

        Raises:
            ImproperlyConfigured: ``show`` returned no ``Shown``; ``show``
                or ``binds`` reads a unit of work and none was given, or
                tried to commit; ``binds`` answered what JSON cannot hold.
        """
        bound = None
        if not self.reads_unit_of_work and self.binds is None:
            result = await _awaited(self.show(command))
        elif unit_of_work is None:
            reader = "show(command, uow)" if self.binds is None else "binds"
            raise ImproperlyConfigured(
                f"Action {self.purpose!r}: {reader} reads in the act's "
                "unit of work, and none was given: pass unit_of_work= to "
                "request_action_authorization (a ResourceRouter's twin gives "
                "its route's)."
            )
        else:
            async with unit_of_work() as uow:
                if self.binds is not None:
                    bound = await self.bound_in(command, uow)
                if self.reads_unit_of_work:
                    read = _Reading(uow, self.purpose, "show(command, uow)")
                    result = await _awaited(self.show(command, uow=read))
                else:
                    result = await _awaited(self.show(command))
        if not isinstance(result, Shown):
            raise ImproperlyConfigured(
                f"Action {self.purpose!r}: show(...) must return a Shown."
            )
        return result, bound

    async def __call__(
        self, command: object, unit_of_work: UnitOfWorkFactory | None = None
    ) -> Shown:
        """As ``shown``: the hook point an action registers on holds callables."""
        return await self.shown(command, unit_of_work)


class _Reading:
    """The act's unit of work, open, lent to ``show`` or ``binds``, which only read.

    A block ``async with uow:`` in ``show`` enters nothing: the unit of work
    is open already, and is left, without a commit, when ``show`` returns.
    """

    def __init__(self, uow: object, purpose: str, reader: str) -> None:
        """Lend ``uow``, open, to ``reader`` of ``purpose``'s action."""
        self._uow = uow
        self._purpose = purpose
        self._reader = reader

    def __getattr__(self, name: str) -> Any:  # noqa: ANN401 - the unit of work's.
        """The unit of work's repositories, and the rest of what it has."""
        return getattr(self._uow, name)

    async def __aenter__(self) -> "_Reading":
        """Enter nothing: the unit of work is open."""
        return self

    async def __aexit__(self, *exc: object) -> None:
        """Leave nothing: it is left when ``show`` returns."""

    async def commit(self) -> None:
        """Refuse: what is shown is read, and asking writes nothing of the act.

        Raises:
            ImproperlyConfigured: Always.
        """
        raise ImproperlyConfigured(
            f"Action {self._purpose!r}: {self._reader} only reads; it may not commit."
        )


async def _awaited(result: object) -> object:
    """``result``, awaited if it is awaitable: a ``show`` may be either."""
    return await result if inspect.isawaitable(result) else result


def _takes_uow(show: Callable[..., object]) -> bool:
    """Whether ``show`` takes a unit of work: a parameter ``uow``, given by name."""
    try:
        parameters = inspect.signature(show).parameters
    except (TypeError, ValueError):  # A built-in without one: the command alone.
        return False
    found = parameters.get("uow")
    return found is not None and found.kind in BY_NAME


BY_NAME: Final = frozenset(
    {inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY}
)
"""The kinds of parameter a unit of work can be given to by name."""


def _by_content(value: object, dumped: Any) -> Any:  # noqa: ANN401 - JSON values.
    """``dumped``, the JSON of ``value``, without the storage's name of an upload.

    Wherever the upload is: a field, a list, a set, a mapping (its keys
    whatever they dump to: an enum's value), a model or a dataclass
    inside. A set's items come in the order of their hashes, which hold
    the storage's name: they are sorted once the names are out, so the
    twin's and the act's agree. Every set is sorted, holding uploads or
    not: its order is its items' hashes', which differ from one process
    to the next. One upload without its SHA-256 has no content to be bound
    by, and keeps its name: it matches no other request. Where ``value``
    and ``dumped`` do not pair up item by item (two keys dumped to one),
    the names stay. The upload's class exists only once its module is
    loaded: a process that never loaded it holds none, and loads nothing
    here.
    """
    module = sys.modules.get("domainkit.storage.base")
    upload: type | None = getattr(module, "StoredUpload", None)
    if upload is not None and isinstance(value, upload) and isinstance(dumped, dict):
        if not getattr(value, "sha256", ""):
            return dumped
        return {key: item for key, item in dumped.items() if key != "name"}
    if isinstance(dumped, list) and isinstance(value, list | tuple | set | frozenset):
        if len(value) != len(dumped):
            return dumped
        items = [_by_content(*pair) for pair in zip(value, dumped, strict=True)]
        if isinstance(value, set | frozenset):
            return sorted(items, key=lambda item: json.dumps(item, sort_keys=True))
        return items
    if isinstance(dumped, dict):
        if isinstance(value, Mapping):
            if len(value) != len(dumped):
                return dumped
            return {  # In the order the model dumped them: the mapping's own.
                key: _by_content(item, inner)
                for item, (key, inner) in zip(
                    value.values(), dumped.items(), strict=True
                )
            }
        return {
            key: _by_content(getattr(value, key, None), item)
            for key, item in dumped.items()
        }
    return dumped


def _canonical(value: object) -> object:
    """``value`` with every set in it sorted, and models and dataclasses as dicts.

    A model or a dataclass is read field by field, by the fields' names,
    never their aliases; a set or a frozenset becomes a list, sorted by
    each item's canonical JSON. A set's order is its items' hashes', which
    differ from one process to the next.
    """
    if isinstance(value, BaseModel):
        return {
            name: _canonical(getattr(value, name)) for name in type(value).model_fields
        }
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {
            each.name: _canonical(getattr(value, each.name))
            for each in dataclasses.fields(value)
        }
    if isinstance(value, Mapping):
        return {key: _canonical(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_canonical(item) for item in value]
    if isinstance(value, set | frozenset):
        items = [_canonical(item) for item in value]
        return sorted(
            items, key=lambda item: json.dumps(to_jsonable_python(item), sort_keys=True)
        )
    return value


def _sha256(value: object, *, strict: bool = False) -> str:
    """The SHA-256 of ``value`` as canonical JSON, keys sorted: 64 hex digits.

    ``strict`` refuses NaN and the infinities, which JSON has no words for.

    Raises:
        ValueError: ``strict``, and ``value`` holds NaN or an infinity; or
            text that UTF-8 cannot write (a lone surrogate).
    """
    text = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=not strict,
    )
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


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
    methods: list[str]
    """What confirming it must prove (``otp``, ``pwd``, ``phone``): which
    ways may confirm it, as ``GET /authorizations/{id}`` answers."""

    @classmethod
    def of(cls, authorization: ActionAuthorization) -> "ActionAuthorizationOut":
        """What a client is told of ``authorization``."""
        return cls(
            id=authorization.id,
            purpose=authorization.purpose,
            shown=_shown_out(authorization.shown),
            expires_at=authorization.expires_at,
            methods=sorted(_required(authorization)),
        )


def _shown_out(shown: Shown) -> ShownOut:
    """``shown``, as a client reads it."""
    return ShownOut(
        title=shown.title,
        details=[DetailOut(label=label, value=value) for label, value in shown.details],
    )


async def request_action_authorization(
    request: HTTPConnection,
    action: Action,
    command: object,
    *,
    unit_of_work: UnitOfWorkFactory | None = None,
) -> ActionAuthorization:
    """Store an authorization of ``command`` for the request's user and login.

    The server builds each part: the subject (``action.subject``), the
    digest of the arguments, what is shown (``action.show``, in the
    request's language), and the login's digest. It works for the
    action's TTL. A login holds at most ten unconfirmed authorizations:
    an eleventh ends the oldest. ``unit_of_work`` makes the unit of work
    the act runs in (its class), for a ``show(command, uow)`` to read in: a
    new one, opened for it. An action that ``binds`` reads the record in
    it too, first, and the digest covers what it read (ADR 0174).

    Raises:
        NotAuthenticated: The request names no user.
        ActionNotAuthorized: Staff act as the user: the act is the user's.
        APIError: 400 ``step_up_unsupported``: the credentials are limited
            to scopes, or cannot be told apart from the user's others.
        NoSecondFactor: The user has no way to confirm what the act asks.
        TypeError: ``command`` is not the action's command.
        ImproperlyConfigured: ``show`` or ``binds`` reads a unit of work,
            and ``unit_of_work`` is None.
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
    shown, bound = await action.read(command, unit_of_work)
    if bound is not None:
        digest = action.digest_of(command, bound)
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
        bound=bound,
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
    consumed: bool = False
    """Whether a transaction spent it, committed or not: it is held no more."""
    held: tuple[datetime, str] | None = None
    """Until when its act waits for approval, and on what (``hold_authorizations``)."""
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
    ``purpose``, confirmed, unspent, live (or held for approval), and its
    code covers its row.
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
        or not await _live(found)
        or not _matches(code, found)
    ):
        await audit_refusal(ACTION, "invalid_authorization")
        return None
    verdict.authorization = found
    _kept_on(connection).accepted[purpose] = _Accepted(
        found, code, _route_of(connection)
    )
    return found


async def _live(authorization: ActionAuthorization) -> bool:
    """Whether it works now: before its expiry, or held for approval.

    One held for an act that waits on something (``held_for``) works only
    while that still waits, as the ``on_held_act`` hooks answer: rejected,
    cancelled, expired, or carried out, the hold is over, and the
    authorization with it, its own expiry or not. Past its expiry, it is
    held until ``held_until``, and never past ``AUTHORIZATIONS.HELD_TTL``
    after its expiry, which its code covers; with ``HELD_TTL`` None, a hold
    counts no more.
    """
    if authorization.held_for is not None and not await _still_waits(
        authorization.held_for
    ):
        return False
    moment = utc_now()
    if not authorization.expired(moment):
        return True
    held = authorization.held_until
    longest = auth_settings().AUTHORIZATIONS.HELD_TTL
    return (
        held is not None
        and longest is not None
        and moment < min(held, authorization.expires_at + longest)
    )


on_held_act: Final[HookPoint[Callable[[str], Awaitable[bool | None]]]] = HookPoint(
    "held act", arguments=1, awaited=True
)
"""Say whether what a held authorization's act waits on still waits.

A hook takes the reference ``hold_authorizations`` was given
(``approvals.request:<id>``), and answers True while it waits, or is
approved and not yet carried out; False once it is over; None for a
reference of another app's. A hold that no hook answers True for, or one
answers False for, is over. Approvals registers one for its requests.
Hooks are asked when the route's rule reads the authorization.
"""


async def _still_waits(held_for: str) -> bool:
    """Whether a hook says ``held_for`` still waits, and none says it is over."""
    answers = {await hook.call(held_for) for hook in on_held_act}
    return True in answers and False not in answers


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
    """Whether something spends ``purpose``: a decorated service, or code that said so.

    A service decorated ``requires_authorization`` says so itself; code that
    calls ``spend_authorization`` says so with ``spent_in_code``.
    """
    return purpose in _spending_purposes


def spent_in_code(action: Action) -> Action:
    """Say that code spends ``action``'s authorizations, with ``spend_authorization``.

    ``check`` refuses a route that asks for an act nothing spends
    (``auth.E110``). A decorated service says so itself; code that is not
    a service, and calls ``spend_authorization``, says so with this once,
    where it declares the action. Returns ``action``.
    """
    _spending_purposes.add(action.purpose)
    return action


def requires_authorization[F: Service | UseCase[Any]](
    action: Action,
) -> Callable[[F], F]:
    """Spend the request's authorization of ``action`` on the service's command.

    The service is ``service(command, uow, ...)``. Called with no
    authorization of the purpose kept by a rule (from another route, a
    batch, a task), it is refused: the act is the user's own. With the
    act when ``uow`` is on auth's database (its first commit spends it);
    otherwise just before the act, in a transaction of its own.

    Given a ``@use_case``, it returns a copy whose ``run``, and so its
    call, spends the authorization: give the copy to the route that asks
    for one (``ResourceRouter.use_case``). The use case itself stays as it
    was, for the routes, tasks, and tests that run it without an act
    (ADR 0161).

    An action that ``binds`` reads the record again when the service opens
    its unit of work (``async with uow``), before the service reads: the
    act runs on the record the user was shown, or not at all (ADR 0174).

    Raises (when the service is called):
        AuthorizationRequired: No rule kept an authorization of the purpose.
        ActionNotAuthorized: It is for other arguments or another subject
            (it is voided), or was spent meanwhile.
        ActionRecordChanged: The action binds the record, and it reads
            otherwise now (it is voided).
        ImproperlyConfigured: The action binds the record, and the service
            takes no unit of work, or never opens it.
    """

    def decorate(service: F) -> F:
        if isinstance(service, UseCase):
            return cast(F, _guarded_use_case(action, service))
        return cast(F, _spending(action, service))

    return decorate


def _spending(action: Action, service: Service) -> Service:
    """``service``, spending the request's authorization of ``action`` first."""

    @functools.wraps(service)
    async def spending(
        command: object,
        uow: object,
        *args: Any,  # noqa: ANN401 - the service's own.
        **kwargs: Any,  # noqa: ANN401 - the service's own.
    ) -> Any:  # noqa: ANN401 - what the service returns.
        accepted = await _take(action, command)
        held = _unit_of_work_of(uow)
        check = _binding(action, command, held, accepted)
        try:
            if held is not None and _on_auth_database(held):
                joined = _Spending(uow, held, accepted, check)
                return joined.opened(await service(command, joined, *args, **kwargs))
            await _spend_alone(accepted)
        except _Spent:  # The transaction that found it spent is over.
            await audit_refusal(ACTION, "used")
            raise
        except _Changed as changed:  # The act's transaction is over.
            await _refuse_changed(accepted, changed)
        if check is None:
            return await service(command, uow, *args, **kwargs)
        bound = _Acting(uow, accepted, check)
        try:
            return bound.opened(await service(command, bound, *args, **kwargs))
        except _Changed as changed:
            await _refuse_changed(accepted, changed)

    setattr(spending, SPENDS, action.purpose)
    _spending_purposes.add(action.purpose)
    return spending


def _guarded_use_case(action: Action, use_case: UseCase[Any]) -> UseCase[Any]:
    """A copy of ``use_case`` whose ``run`` spends the authorization first.

    ``UseCase.__call__`` runs ``run``, and routes wire ``run``: guarding it
    guards every way into the copy. The original stays as it was.

    Raises:
        ImproperlyConfigured: The use case runs another command than the
            action's (a subclass of it too: the digest would leave fields out).
    """
    if use_case.command is not action.command:
        raise ImproperlyConfigured(
            f"Action {action.purpose!r} takes a {action.command.__name__}, and "
            f"the use case {use_case.name} runs a {use_case.command.__name__}: "
            f"give the action command={use_case.name}."
        )
    guarded = copy.copy(use_case)
    guarded.run = _spending(action, use_case.run)  # type: ignore[method-assign,assignment]
    setattr(guarded, SPENDS, action.purpose)
    return guarded


async def spend_authorization(
    uow: object, action: Action, command: object
) -> ActionAuthorization:
    """Spend the request's authorization of ``action`` on ``command``, in code.

    For code that is not a decorated service: on auth's database, in the
    open transaction of ``uow`` (it commits with ``uow``'s next commit, or
    not at all); otherwise now, in a transaction of its own. Found spent in
    ``uow``'s transaction, the trail hears of it at the end of the request.
    An action that ``binds`` reads the record in ``uow``, open, first: call
    it before the act changes what ``binds`` reads.

    Raises:
        AuthorizationRequired: No rule kept an authorization of the purpose.
        ActionNotAuthorized: It is for other arguments or another subject
            (it is voided), or was spent meanwhile.
        ActionRecordChanged: The action binds the record, and it reads
            otherwise now (it is voided).
        ImproperlyConfigured: The action binds the record, and ``uow`` is
            no unit of work.
        RuntimeError: ``uow`` is on auth's database, and not open.
    """
    accepted = await _take(action, command)
    held = _unit_of_work_of(uow)
    check = _binding(action, command, held, accepted)
    if check is not None:
        try:
            await check(uow)
        except _Changed as changed:
            await _refuse_changed(accepted, changed)
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


async def check_authorization(action: Action, command: object) -> None:
    """Refuse the request's authorization of ``action`` unless it binds ``command``.

    What ``spend_authorization`` checks before it spends, spending nothing:
    for code that checks the act before a step it cannot take back, then
    spends it (the admin API, before the ``on_admin_change`` checks, which
    may spend an approval). The record an action ``binds`` is read when it
    is spent.

    Raises:
        AuthorizationRequired: No rule kept an authorization of the purpose.
        ActionNotAuthorized: It is for other arguments or another subject
            (it is voided).
    """
    await _take(action, command)


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
        digest=action.digest_of(command, accepted.authorization.bound),
        subject=action.subject_of(command),
    )
    if not _matches(accepted.code, asked):
        async with AuthUnitOfWork() as uow:
            await uow.authorizations.void(accepted.authorization.id, utc_now())
            await uow.commit()
        await audit_refusal(ACTION, "changed")
        raise ActionNotAuthorized
    return accepted


type Check = Callable[[object], Awaitable[None]]
"""What reads the record an act binds in its unit of work, and refuses another."""


def _binding(
    action: Action, command: object, held: UnitOfWork | None, accepted: _Accepted
) -> Check | None:
    """What checks the record ``action`` binds, in the act's unit of work; None: none.

    Raises:
        ImproperlyConfigured: The action binds the record, and the act has
            no unit of work to read it in.
    """
    if action.binds is None:
        return None
    if held is None:
        raise ImproperlyConfigured(
            f"Action {action.purpose!r} binds the record it decides on, read in "
            "the act's unit of work: its service takes none."
        )
    stored = accepted.authorization.bound or ""

    async def check(uow: object) -> None:
        try:
            bound = await action.bound_in(command, uow)
        except NotFound as gone:  # It will never read as it did: void it.
            raise _Changed(gone) from gone
        if not hmac.compare_digest(bound.encode(), stored.encode()):
            raise _Changed

    return check


class _Changed(ActionRecordChanged):
    """What the act binds reads otherwise than when its authorization was asked for.

    Or no longer exists (``binds`` raised ``NotFound``): ``answer`` is what
    the act is refused with, once the authorization is voided.
    """

    def __init__(self, answer: Exception | None = None) -> None:
        """Answer ``answer``, or ``409 action_record_changed``."""
        super().__init__()
        self.answer = answer or ActionRecordChanged()


async def _refuse_changed(accepted: _Accepted, changed: _Changed) -> NoReturn:
    """Void ``accepted``, in a transaction of its own; refuse the act.

    Raises:
        Exception: What ``changed`` answers: ``ActionRecordChanged``, or
            the record's ``NotFound``.
    """
    async with AuthUnitOfWork() as uow:
        await uow.authorizations.void(accepted.authorization.id, utc_now())
        await uow.commit()
    await audit_refusal(ACTION, "record_changed")
    raise changed.answer


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
    accepted.consumed = True
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


class _Acting:
    """A service's unit of work, in which an act that binds decides in one transaction.

    For an action that ``binds`` (ADR 0174): each block the service opens
    (``async with uow``) opens its own transaction, as everywhere, and
    reads what the action binds first, refusing the act if it reads
    otherwise, until a block has committed. A block that ended in a
    rollback (a run ``uow.run`` retries, a failure the service caught) did
    not commit: the next one is read again. The first commit lands the
    act, whatever it wrote; a block opened after it, or a second commit,
    is a mistake. So is a block opened while another of the act's is
    open, inside it or by another task: the act then commits nothing. A
    commit made with no block open reads it first. An action that binds
    nothing is wrapped as before: each block and commit is the unit of
    work's own.
    """

    def __init__(self, uow: object, accepted: _Accepted, check: Check | None) -> None:
        """Wrap ``uow`` (a unit of work, or a wrapper lending it)."""
        self._uow = uow
        self._accepted = accepted
        self._check = check
        self.checked = check is None
        """Whether the record was read once at least, if the act binds one."""
        self._open = False
        """Whether a block of the act's is open (an act that binds)."""
        self._landed = False
        """Whether a commit made the act (an act that binds)."""
        self._misused: str | None = None
        """Why the act may commit nothing more: a block opened beside another."""

    def __getattr__(self, name: str) -> Any:  # noqa: ANN401 - the unit of work's.
        """The wrapped unit of work's attribute (its repositories, ``run``, ...)."""
        return getattr(self._uow, name)

    def _refuse(self, why: str) -> NoReturn:
        """Refuse a block or a commit an act that binds does not make.

        Raises:
            ImproperlyConfigured: Always.
        """
        raise ImproperlyConfigured(
            f"Action {self._accepted.authorization.purpose!r} binds the record it "
            f"decides on, and decides in one transaction: {why} Read and decide "
            "in one block; keep work that must persist apart (an audit, a "
            "notice) to a unit of work of its own, or for after the act."
        )

    async def __aenter__(self) -> Self:
        """Open a transaction and read what the act binds.

        Raises:
            ImproperlyConfigured: The act binds, and a block of its is open
                (nested, or another task's), or it committed already: nothing
                is entered.
            _Changed: It reads otherwise: the unit of work is left.
        """
        if self._check is not None:
            if self._open:
                self._misused = (
                    "a block was opened while another was open, inside it or "
                    "in another task."
                )
                self._refuse(self._misused)
            if self._landed:
                self._refuse("a block was opened after the act committed.")
            self._open = True
        try:
            await self._uow.__aenter__()  # type: ignore[attr-defined]
        except BaseException:
            self._open = False
            raise
        try:
            await self._bound()
        except BaseException as exc:
            self._open = False
            await self._uow.__aexit__(type(exc), exc, exc.__traceback__)  # type: ignore[attr-defined]
            raise
        return self

    async def __aexit__(self, *exc: object) -> None:
        """Leave the unit of work as it leaves; the next block opens a new one."""
        try:
            await self._uow.__aexit__(*exc)  # type: ignore[attr-defined]
        finally:
            self._open = False

    async def _bound(self) -> None:
        """Read what the act binds, in the unit of work's transaction now.

        Raises:
            _Changed: It reads otherwise.
        """
        if self._check is None:
            return
        self.checked = True
        await self._check(self._uow)

    async def _before_commit(self) -> None:
        """Refuse a commit the act does not make; read the record if no block did.

        Raises:
            ImproperlyConfigured: The act binds, and committed already, or a
                block was opened beside another.
            _Changed: It reads otherwise.
        """
        if self._check is None:
            return
        if self._misused is not None:
            self._refuse(self._misused)
        if self._landed:
            self._refuse("it committed a second time.")
        if not self._open:
            await self._bound()

    def _committed(self) -> None:
        """A commit succeeded: an act that binds has landed."""
        self._landed = self._check is not None

    async def commit(self) -> None:
        """Commit, having read what the act binds.

        Raises:
            ImproperlyConfigured: See ``_before_commit``.
            _Changed: It reads otherwise: nothing commits.
        """
        await self._before_commit()
        await self._uow.commit()  # type: ignore[attr-defined]
        self._committed()

    def opened(self, result: object) -> Any:  # noqa: ANN401 - what the service returns.
        """``result``, if the record the act binds was read.

        Raises:
            ImproperlyConfigured: The service never opened its unit of work.
        """
        if not self.checked:
            raise ImproperlyConfigured(
                f"Action {self._accepted.authorization.purpose!r} binds the record "
                "it decides on, read when the service opens its unit of work, "
                "and the service never opened it."
            )
        return result


class _Spending(_Acting):
    """A service's unit of work on auth's database, which spends with its commit.

    Its first commit spends the authorization in the same transaction,
    with ``ActionAuthorizationUsed`` among the act's events: spent if and
    only if the act commits. Everything else is ``_Acting``'s.
    """

    def __init__(
        self, uow: object, held: UnitOfWork, accepted: _Accepted, check: Check | None
    ) -> None:
        """Wrap ``uow`` (the unit of work ``held``, or a wrapper lending it)."""
        super().__init__(uow, accepted, check)
        self._held = held
        self.spent = False
        """Whether a commit spent the authorization."""

    async def commit(self) -> None:
        """Commit, spending the authorization with the first commit.

        Raises:
            ImproperlyConfigured: See ``_before_commit``.
            _Spent: It was spent, voided, or expired meanwhile: nothing
                commits.
            _Changed: What the act binds reads otherwise: nothing commits.
        """
        await self._before_commit()
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
        self._committed()


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
    a forgotten decorator shows. What an act held for approval took is
    kept (``hold_authorizations``), whatever the response.
    """
    for accepted in kept.accepted.values():
        if accepted.unheard is not None:
            await audit_refusal(ACTION, accepted.unheard)
            accepted.unheard = None
    await _hold(kept)
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


def hold_authorizations(until: datetime, *, held_for: str) -> None:
    """Keep the authorization of the request's act, which now waits for approval.

    For the app that holds the act (approvals, for a guard ``by_maker``,
    whose maker's own call makes it once approved): each authorization of
    the request that an act took (``requires_authorization``,
    ``check_authorization``) and did not spend is kept for ``held_for``,
    what the act waits on (``approvals.request:<id>``), until ``until``,
    its expiry, and within ``AUTH.AUTHORIZATIONS.HELD_TTL`` of its own
    (ADR 0174). It works only while an ``on_held_act`` hook says
    ``held_for`` still waits, and is never held for another. Written as
    the response starts, whatever it is; nothing while ``HELD_TTL`` is
    None, the default, nor outside a request.

    Raises:
        ValueError: ``held_for`` is empty, or longer than 200 characters.
    """
    if not 0 < len(held_for) <= SUBJECT_MAX_LENGTH:
        raise ValueError(
            f"held_for names what the act waits on in 1 to {SUBJECT_MAX_LENGTH} "
            f"characters, such as approvals.request:<id>: {held_for[:40]!r}..."
        )
    kept = _kept.get()
    if kept is None:
        return
    for accepted in kept.accepted.values():
        if accepted.taken and not accepted.consumed:
            accepted.held = (until, held_for)


async def _hold(kept: Kept) -> None:
    """Write what ``hold_authorizations`` kept: each until then, within ``HELD_TTL``."""
    longest = auth_settings().AUTHORIZATIONS.HELD_TTL
    now = utc_now()
    for accepted in kept.accepted.values():
        held, accepted.held = accepted.held, None
        if held is None or longest is None:
            continue
        authorization = accepted.authorization
        until = min(held[0], authorization.expires_at + longest)
        async with AuthUnitOfWork() as uow:
            await uow.authorizations.hold(
                authorization.id, until, now, held_for=held[1]
            )
            await uow.commit()


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
    uow: AuthUnitOfWork | None = None,
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
    With ``uow`` (since 2.5), the confirmation is made in it and its
    caller commits: with a change of its own, in one transaction (ciba
    approves the phone's request with it).

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
    if uow is None:
        async with AuthUnitOfWork() as own:
            await _confirm_in(own, authorization, proved, device)
            await own.commit()
    else:
        await _confirm_in(uow, authorization, proved, device)
    return _authorization(authorization)


async def _confirm_in(
    uow: AuthUnitOfWork,
    authorization: ActionAuthorization,
    proved: frozenset[str],
    device: dict[str, Any],
) -> None:
    """Confirm ``authorization`` in ``uow``, its evidence deferred to the commit.

    Raises:
        APIError: 409 ``authorization_expired``: it can no more.
    """
    methods = tuple(sorted(proved))
    if authorization.expired() or not await _record_confirmation(
        uow, authorization, methods, **device
    ):
        raise _expired()
    if device["signature"] is not None:
        _defer_evidence(uow, authorization, methods, **device)


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
