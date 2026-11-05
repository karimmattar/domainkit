"""What a sign-in request may be refused, and what a poll may answer.

A poll's answers follow CIBA's token errors (``authorization_pending``,
``slow_down``, ``expired_token``, ``access_denied``, ``invalid_grant``):
the first-party routes answer them as any API error, the OIDC token
endpoint in OAuth's format, by their ``code``.
"""

from fastapi import status

from domainkit.http.errors import APIError

__all__ = [
    "AccessDenied",
    "ApprovalRequired",
    "AuthorizationPending",
    "DifferentNetwork",
    "PollRefused",
    "QrCodeExpired",
    "SignInExpired",
    "SignInNotFound",
    "SignInUnknown",
    "SlowDown",
    "WrongMatchCode",
]


class ApprovalRequired(APIError):
    """This login must be approved from the user's mobile app (403).

    ``CIBA.APPROVE_LOGINS``: start the sign-in at ``/app-sign-in/push`` or
    ``/app-sign-in/qr``, or log in from a bound device.
    """

    def __init__(self) -> None:
        """Use status 403 and the code ``approval_required``."""
        super().__init__(
            status.HTTP_403_FORBIDDEN,
            "Approve this sign-in from your mobile app.",
            code="approval_required",
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
    """A poll that gives no tokens (400); ``code`` says why, as CIBA names it."""


class AuthorizationPending(PollRefused):
    """The phone has not answered yet: poll again after the interval."""

    def __init__(self) -> None:
        """Use status 400 and the code ``authorization_pending``."""
        super().__init__(
            status.HTTP_400_BAD_REQUEST,
            "The sign-in waits for the phone; ask again later.",
            code="authorization_pending",
        )


class SlowDown(PollRefused):
    """A poll came before the interval: wait 5 seconds more from now on."""

    def __init__(self) -> None:
        """Use status 400 and the code ``slow_down``."""
        super().__init__(
            status.HTTP_400_BAD_REQUEST,
            "Asked too often; wait longer between polls.",
            code="slow_down",
        )


class SignInExpired(PollRefused):
    """The request's time is up before the phone approved it."""

    def __init__(self) -> None:
        """Use status 400 and the code ``expired_token``."""
        super().__init__(
            status.HTTP_400_BAD_REQUEST,
            "The sign-in expired; start a new one.",
            code="expired_token",
        )


class AccessDenied(PollRefused):
    """The phone denied the sign-in."""

    def __init__(self) -> None:
        """Use status 400 and the code ``access_denied``."""
        super().__init__(
            status.HTTP_400_BAD_REQUEST,
            "The sign-in was denied.",
            code="access_denied",
        )


class SignInUnknown(PollRefused):
    """No request has that handle, or it was redeemed, or is another client's."""

    def __init__(self) -> None:
        """Use status 400 and the code ``invalid_grant``."""
        super().__init__(
            status.HTTP_400_BAD_REQUEST,
            "No sign-in waits with that handle.",
            code="invalid_grant",
        )
