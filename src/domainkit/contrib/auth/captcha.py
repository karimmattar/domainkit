"""A captcha on the routes bots try: login, signup, password reset.

::

    AUTH = {
        "CAPTCHA": {"PROVIDER": "turnstile", "SECRET": env.str("TURNSTILE_SECRET")},
    }

With ``AUTH.CAPTCHA`` set, each route ``ON`` names asks for the token the
page's widget gave, in the ``X-Captcha-Token`` header, and checks it with
the provider (Cloudflare Turnstile, Google reCAPTCHA, hCaptcha) before
anything else: a missing or refused token is 400 ``captcha_failed``. A
provider that cannot be reached refuses the request (503
``captcha_unavailable``): bots never pass while it is down. reCAPTCHA v3's
score must reach ``MIN_SCORE``. Only the token and the secret are sent:
never the client's address, which is personal data.
"""

from typing import Any, ClassVar, Final

from fastapi import Depends, Request, status

from domainkit.exceptions import UpstreamError
from domainkit.http.errors import APIError

from .settings import auth_settings

__all__ = ["CaptchaUnavailable", "captcha"]

HEADER: Final = "X-Captcha-Token"
MAX_TOKEN: Final = 4096
"""The longest token accepted: providers' are about 2,000 characters."""
VERIFY: Final = {
    "turnstile": "https://challenges.cloudflare.com/turnstile/v0/siteverify",
    "recaptcha": "https://www.google.com/recaptcha/api/siteverify",
    "hcaptcha": "https://api.hcaptcha.com/siteverify",
}
"""Where each provider checks a token."""


class CaptchaUnavailable(UpstreamError):
    """The captcha provider could not check the token: 503."""

    status_code: ClassVar[int] = 503
    code: ClassVar[str] = "captcha_unavailable"
    detail: ClassVar[str] = "The captcha cannot be checked now; try again later."


def captcha(action: str) -> Any:  # noqa: ANN401 - FastAPI's Depends.
    """A dependency that checks the captcha of ``action``, if it is ``ON``."""

    async def check(request: Request) -> None:
        config = auth_settings().CAPTCHA
        if config is None or action not in config.ON:
            return
        token = request.headers.get(HEADER, "")
        if not token or len(token) > MAX_TOKEN:
            raise _failed()
        from domainkit.http.client import HttpClient

        verifier = HttpClient(propagate=False)
        # The client's address is not sent: it is personal data, and optional.
        form = {"secret": config.SECRET.get_secret_value(), "response": token}
        try:
            response = await verifier.post(
                VERIFY[config.PROVIDER], data=form, retry=True
            )
            verdict = response.json()
        except Exception as exc:  # The network, a timeout, or no JSON.
            raise CaptchaUnavailable from exc
        finally:
            await verifier.aclose()
        if not isinstance(verdict, dict) or verdict.get("success") is not True:
            raise _failed()
        score = verdict.get("score")
        if isinstance(score, int | float) and score < config.MIN_SCORE:
            raise _failed()

    return Depends(check)


def _failed() -> APIError:
    """400 ``captcha_failed``: the client is told nothing more."""
    return APIError(
        status.HTTP_400_BAD_REQUEST,
        "The captcha was missing or not solved.",
        code="captcha_failed",
    )
