"""What a sign-in request may be refused, and what a poll may answer.

A poll's answers follow CIBA's token errors (``authorization_pending``,
``slow_down``, ``expired_token``, ``access_denied``, ``invalid_grant``):
the first-party routes answer them as any API error, by their ``code``
(``sign_in_unknown`` for ``invalid_grant``, a code another app uses), and
the OIDC token endpoint in OAuth's format, by their ``oauth_error``.
"""

from typing import ClassVar

from fastapi import status

from domainkit.http.errors import APIError

__all__ = [
    "AccessDenied",
    "AuthorizationPending",
    "DifferentNetwork",
    "OAuthError",
    "PhoneApprovalRequired",
    "PollRefused",
    "QrCodeExpired",
    "SignInExpired",
    "SignInNotFound",
    "SignInUnknown",
    "SlowDown",
    "WrongMatchCode",
    "invalid_client",
]


class PhoneApprovalRequired(APIError):
    """This login must be approved from the user's mobile app (403).

    ``CIBA.APPROVE_LOGINS``: start the sign-in at ``/app-sign-in/push`` or
    ``/app-sign-in/qr``, or log in from a bound device.
    """

    def __init__(self) -> None:
        """Use status 403 and the code ``phone_approval_required``."""
        super().__init__(
            status.HTTP_403_FORBIDDEN,
            "Approve this sign-in from your mobile app.",
            code="phone_approval_required",
        )


class SignInNotFound(APIError):
    """The user has no sign-in request with that id that waits for an answer (404)."""

    def __init__(self) -> None:
        """Use status 404 and the code ``sign_in_not_found``."""
        super().__init__(
            status.HTTP_404_NOT_FOUND,
            "No sign-in request of yours waits with that id.",
            code="sign_in_not_found",
        )


class QrCodeExpired(APIError):
    """The QR code is unknown, was claimed, or expired (422)."""

    def __init__(self) -> None:
        """Use status 422 and the code ``qr_code_expired``."""
        super().__init__(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "That code expired or was used; scan the new one on the page.",
            code="qr_code_expired",
        )


class DifferentNetwork(APIError):
    """The phone is not on the address of the page (403, ``QR_SAME_NETWORK``)."""

    def __init__(self) -> None:
        """Use status 403 and the code ``different_network``."""
        super().__init__(
            status.HTTP_403_FORBIDDEN,
            "Scan the code from a phone on the same network as the page.",
            code="different_network",
        )


class WrongMatchCode(APIError):
    """The number typed is not the page's: the sign-in is denied (422)."""

    def __init__(self) -> None:
        """Use status 422 and the code ``wrong_match_code``."""
        super().__init__(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "That is not the number on the sign-in page; the sign-in is denied.",
            code="wrong_match_code",
        )


class PollRefused(APIError):
    """A poll that gives no tokens (400); ``code`` says why.

    ``oauth_error`` is CIBA's name for it, which the token endpoint answers.
    """

    oauth_error: ClassVar[str]


class AuthorizationPending(PollRefused):
    """The phone has not answered yet: poll again after the interval."""

    oauth_error = "authorization_pending"

    def __init__(self) -> None:
        """Use status 400 and the code ``authorization_pending``."""
        super().__init__(
            status.HTTP_400_BAD_REQUEST,
            "The sign-in waits for the phone; ask again later.",
            code="authorization_pending",
        )


class SlowDown(PollRefused):
    """A poll came before the interval: wait 5 seconds more from now on."""

    oauth_error = "slow_down"

    def __init__(self) -> None:
        """Use status 400 and the code ``slow_down``."""
        super().__init__(
            status.HTTP_400_BAD_REQUEST,
            "Asked too often; wait longer between polls.",
            code="slow_down",
        )


class SignInExpired(PollRefused):
    """The request's time is up before the phone approved it."""

    oauth_error = "expired_token"

    def __init__(self) -> None:
        """Use status 400 and the code ``expired_token``."""
        super().__init__(
            status.HTTP_400_BAD_REQUEST,
            "The sign-in expired; start a new one.",
            code="expired_token",
        )


class AccessDenied(PollRefused):
    """The phone denied the sign-in."""

    oauth_error = "access_denied"

    def __init__(self) -> None:
        """Use status 400 and the code ``access_denied``."""
        super().__init__(
            status.HTTP_400_BAD_REQUEST,
            "The sign-in was denied.",
            code="access_denied",
        )


class SignInUnknown(PollRefused):
    """No request has that handle, or it was redeemed, or is another client's."""

    oauth_error = "invalid_grant"

    def __init__(self) -> None:
        """Use status 400 and the code ``sign_in_unknown`` (``invalid_grant``)."""
        super().__init__(
            status.HTTP_400_BAD_REQUEST,
            "No sign-in waits with that handle.",
            code="sign_in_unknown",
        )


class OAuthError(Exception):
    """An OAuth error the OpenID Provider routes answer in OAuth's format.

    ``{"error": error, "error_description": description}``, with ``status``
    (400, or 401 for ``invalid_client``) and ``Cache-Control: no-store``.
    """

    def __init__(
        self,
        error: str,
        description: str,
        *,
        status_code: int = status.HTTP_400_BAD_REQUEST,
        headers: dict[str, str] | None = None,
    ) -> None:
        """Keep what the answer carries."""
        super().__init__(description)
        self.error = error
        self.description = description
        self.status_code = status_code
        self.headers = headers or {}


def invalid_client(description: str, *, basic: bool = False) -> OAuthError:
    """``401 invalid_client``; a challenge where Basic authentication was tried."""
    return OAuthError(
        "invalid_client",
        description,
        status_code=status.HTTP_401_UNAUTHORIZED,
        headers={"WWW-Authenticate": 'Basic realm="ciba"'} if basic else None,
    )
