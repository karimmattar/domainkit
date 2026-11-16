"""What happens to users and groups; the audit trail records each one.

Events carry ids, names, and permission keys, never passwords or hashes.
Usernames are personal data: the audit trail keeps them as pseudonyms, as
it keeps the address or number a change replaced (``EmailChanged``,
``MobileNumberChanged``). Each event names who caused it (``actor``).
"""

from typing import Annotated
from uuid import UUID

from domainkit.ddd import DomainEvent, Personal


class UserCreated(DomainEvent):
    """A user was created."""

    user_id: UUID
    username: Annotated[str, Personal(of="user_id")]


class PasswordChanged(DomainEvent):
    """A user's password was set, changed, or made unusable."""

    user_id: UUID


class UserLoggedIn(DomainEvent):
    """A user logged in and got tokens."""

    user_id: UUID


class UserLoggedOut(DomainEvent):
    """A user logged out: of this session, ``everywhere``, or of the ``others``."""

    user_id: UUID
    everywhere: bool
    others: bool = False
    """Every other login ended, and the one that asked stayed (ADR 0130)."""


class UserImpersonated(DomainEvent):
    """Staff began acting as a user, for a reason, until the token expires."""

    user_id: UUID
    impersonator_id: UUID
    reason: Annotated[str, Personal(of="user_id")]
    """Why: it may name the user's case, so it is kept as personal data."""


class RefreshTokenReused(DomainEvent):
    """A used refresh token came back; the login was ended, as it may be stolen."""

    user_id: UUID
    family: UUID


class TokenCreated(DomainEvent):
    """A long-lived token was made for a user (``createtoken``, ``/tokens``)."""

    user_id: UUID
    name: str
    scopes: tuple[str, ...] | None = None
    """The permission keys it is limited to; None: all of the user's."""


class MfaEnabled(DomainEvent):
    """The user confirmed an authenticator: logins now need its codes."""

    user_id: UUID


class MfaDisabled(DomainEvent):
    """The user's authenticator and recovery codes were removed."""

    user_id: UUID


class RecoveryCodesRegenerated(DomainEvent):
    """The user's recovery codes were replaced by new ones."""

    user_id: UUID


class RecoveryCodeUsed(DomainEvent):
    """A recovery code stood in for the authenticator, once."""

    user_id: UUID
    remaining: int


class TokenRevoked(DomainEvent):
    """A user revoked one of their own tokens."""

    user_id: UUID
    name: str


class PasswordResetRequested(DomainEvent):
    """A reset link was sent to the user's email address."""

    user_id: UUID


class PasswordReset(DomainEvent):
    """The user chose a new password through a reset link."""

    user_id: UUID


class EmailVerified(DomainEvent):
    """The user proved they read mail at their address."""

    user_id: UUID


class MobileNumberVerified(DomainEvent):
    """The user proved they receive texts at their mobile number."""

    user_id: UUID


class EmailReleased(DomainEvent):
    """The user lost an address they never proved: a signup proved it (ADR 0133)."""

    user_id: UUID


class MobileNumberReleased(DomainEvent):
    """The user lost a number they never proved: a signup proved it (ADR 0133)."""

    user_id: UUID


class UserSignedUp(DomainEvent):
    """Someone created their own account (``AUTH.SIGNUP_ENABLED``)."""

    user_id: UUID


class UserUpdated(DomainEvent):
    """Fields of a user changed (names only: values may be personal data).

    A new email address or mobile number also records ``EmailChanged`` or
    ``MobileNumberChanged``, which name the one it replaced.
    """

    user_id: UUID
    fields: tuple[str, ...]


class EmailChanged(DomainEvent):
    """A user's email address changed: tell the old one (ADR 0147).

    ``previous`` is personal data: the audit trail keeps a pseudonym of it,
    the outbox keeps it until its message ran, and erasure finds it there.
    The new address is the user's own, read from them.
    """

    user_id: UUID
    previous: Annotated[str, Personal(of="user_id")] | None
    """The address it replaced; None when the user had none."""
    previous_verified: bool
    """Whether the user had proved ``previous``: only a proved address is
    sure to be theirs (ADR 0141)."""


class MobileNumberChanged(DomainEvent):
    """A user's mobile number changed: tell the old one (ADR 0147).

    ``previous`` is personal data, kept as ``EmailChanged`` keeps its own.
    """

    user_id: UUID
    previous: Annotated[str, Personal(of="user_id")] | None
    """The number it replaced (E.164); None when the user had none."""
    previous_verified: bool
    """Whether the user had proved ``previous`` (ADR 0141)."""


class UserDeleted(DomainEvent):
    """A user was deleted."""

    user_id: UUID
    username: Annotated[str, Personal(of="user_id")]


class UserStatusChanged(DomainEvent):
    """``is_active``, ``is_staff``, or ``is_superuser`` changed."""

    user_id: UUID
    field: str
    value: bool


class UserJoinedGroup(DomainEvent):
    """A user became a member of a group."""

    user_id: UUID
    group_id: UUID
    group: str


class UserLeftGroup(DomainEvent):
    """A user stopped being a member of a group."""

    user_id: UUID
    group_id: UUID
    group: str


class UserPermissionGranted(DomainEvent):
    """A permission was given to a user directly."""

    user_id: UUID
    permission: str


class UserPermissionRevoked(DomainEvent):
    """A permission given to a user directly was taken back."""

    user_id: UUID
    permission: str


class GroupCreated(DomainEvent):
    """A group was created."""

    group_id: UUID
    name: str


class GroupDeleted(DomainEvent):
    """A group was deleted; its members lost its permissions."""

    group_id: UUID
    name: str


class GroupRenamed(DomainEvent):
    """A group got a new name."""

    group_id: UUID
    name: str


class GroupPermissionGranted(DomainEvent):
    """A permission was given to a group (and so to its members)."""

    group_id: UUID
    permission: str


class GroupPermissionRevoked(DomainEvent):
    """A permission was taken back from a group."""

    group_id: UUID
    permission: str


class SteppedUp(DomainEvent):
    """A user authenticated again, for acts that ask for it (``StepUp``)."""

    user_id: UUID
    methods: tuple[str, ...]
    """What they proved, RFC 8176's names: ``otp``, ``pwd``."""


class TransactionAuthorized(DomainEvent):
    """A user confirmed a transaction: one amount, to one payee, once."""

    user_id: UUID
    challenge_id: UUID
    purpose: str
    methods: tuple[str, ...]


class TransactionAuthorizationUsed(DomainEvent):
    """A transaction's authorization was spent on the transaction it names."""

    user_id: UUID
    challenge_id: UUID
    purpose: str


class ActionAuthorized(DomainEvent):
    """A user confirmed one act, with its arguments (``auth.actions``).

    The digest lets an auditor match the act to its authorization: neither
    the arguments nor what was shown is kept here, as either may be
    personal data.
    """

    user_id: UUID
    authorization_id: UUID
    purpose: str
    subject: str
    digest: str
    methods: tuple[str, ...]
    device_id: UUID | None = None
    """The bound device that confirmed it, if one did."""


class ActionAuthorizationUsed(DomainEvent):
    """An act's authorization was spent on that act, once."""

    user_id: UUID
    authorization_id: UUID
    purpose: str
    subject: str
    digest: str
    methods: tuple[str, ...]
    device_id: UUID | None = None
