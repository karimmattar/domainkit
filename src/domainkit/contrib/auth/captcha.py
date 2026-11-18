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
score must reach ``MIN_SCORE``; with ``HOSTNAMES``, the token must have
been solved on one of them, and with ``CHECK_ACTION``, for the route's
action. Only the token and the secret are sent: never the client's
address, which is personal data.
"""

from functools import cache
from typing import TYPE_CHECKING, Any, ClassVar, Final, get_args

from fastapi import Depends, Request, status

from domainkit.exceptions import ImproperlyConfigured, UpstreamError
from domainkit.http.errors import APIError

from .settings import CaptchaAction, auth_settings

if TYPE_CHECKING:
    from domainkit.outbound.client import HttpClient

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
ACTIONS: Final = frozenset(get_args(CaptchaAction.__value__))
"""The actions a captcha may guard: those ``AUTH.CAPTCHA.ON`` can name."""


class CaptchaUnavailable(UpstreamError):
    """The captcha provider could not check the token: 503."""

    status_code: ClassVar[int] = 503
    code: ClassVar[str] = "captcha_unavailable"
    detail: ClassVar[str] = "The captcha cannot be checked now; try again later."


def captcha(action: str) -> Any:  # noqa: ANN401 - FastAPI's Depends.
    """A dependency that checks the captcha of ``action``, if it is ``ON``.

    Raises:
        ImproperlyConfigured: ``action`` is none ``AUTH.CAPTCHA.ON`` could
            name: the route would never ask for a token, whatever the
            settings say.
    """
    if action not in ACTIONS:
        raise ImproperlyConfigured(
            f"captcha({action!r}) guards nothing: AUTH.CAPTCHA.ON names "
            f"{', '.join(sorted(ACTIONS))}."
        )

    async def check(request: Request) -> None:
        config = auth_settings().CAPTCHA
        if config is None or action not in config.ON:
            return
        token = request.headers.get(HEADER, "")
        if not token or len(token) > MAX_TOKEN:
            raise _failed()
        # The client's address is not sent: it is personal data, and optional.
        form = {"secret": config.SECRET.get_secret_value(), "response": token}
        try:
            response = await _verifier().post(
                VERIFY[config.PROVIDER], data=form, retry=True
            )
            verdict = response.json()
        except Exception as exc:  # The network, a timeout, or no JSON.
            raise CaptchaUnavailable from exc
        if not isinstance(verdict, dict) or verdict.get("success") is not True:
            raise _failed()
        score = verdict.get("score")
        if isinstance(score, int | float) and score < config.MIN_SCORE:
            raise _failed()
        if config.HOSTNAMES and verdict.get("hostname") not in config.HOSTNAMES:
            raise _failed()  # Solved on a page of another site.
        if config.CHECK_ACTION and verdict.get("action") != action:
            raise _failed()  # Solved for another form.

    return Depends(check)


@cache
def _verifier() -> "HttpClient":
    """The client that checks tokens, shared by every request.

    Its pool is kept, and a provider that is down opens its circuit: the
    requests after are refused at once, 503 as before (audit A28-ARCH-02).
    """
    from domainkit.outbound.client import HttpClient

    return HttpClient(propagate=False, name="captcha")


def _failed() -> APIError:
    """400 ``captcha_failed``: the client is told nothing more."""
    return APIError(
        status.HTTP_400_BAD_REQUEST,
        "The captcha was missing or not solved.",
        code="captcha_failed",
    )
