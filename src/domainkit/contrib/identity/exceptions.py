"""What an identity provider, or a login through one, may refuse."""

from typing import ClassVar

from domainkit.ddd import BusinessRuleViolation, Conflict, NotFound
from domainkit.exceptions import UpstreamError

__all__ = [
    "EmailTaken",
    "IdentityTaken",
    "LastWayIn",
    "NoAccount",
    "ProviderError",
    "UnknownProvider",
]


class ProviderError(UpstreamError):
    """The provider refused, could not be reached, or answered what does not hold (502).

    The message, logged, says which; the client is told only that the
    login failed.
    """

    status_code: ClassVar[int] = 502
    code: ClassVar[str] = "identity_provider_error"
    detail: ClassVar[str] = "The identity provider could not log you in; try again."


class UnknownProvider(NotFound):
    """No identity provider has that name (404)."""

    code = "unknown_identity_provider"


class NoAccount(BusinessRuleViolation):
    """The identity is linked to no account, and none may be made for it."""

    code = "identity_not_linked"


class EmailTaken(Conflict):
    """An account has the identity's email: log in to it, then link the identity."""

    code = "identity_email_taken"


class IdentityTaken(Conflict):
    """The identity is linked to another account already."""

    code = "identity_taken"


class LastWayIn(BusinessRuleViolation):
    """Unlinking would leave the user no way to log in."""

    code = "identity_last_way_in"
