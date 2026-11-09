"""Settings of ``domainkit.contrib.auth``: ``AUTH = {...}``."""

import json
import re
from collections.abc import Mapping
from datetime import timedelta
from typing import Any, Final, Literal, Self

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    IPvAnyNetwork,
    SecretStr,
    field_validator,
    model_validator,
)

from domainkit.apps import registry
from domainkit.utils.rates import parse_rate

PBKDF2: str = "domainkit.contrib.auth.hashers.PBKDF2PasswordHasher"
VALIDATORS: str = "domainkit.contrib.auth.validators"
AUTHENTICATION: str = "domainkit.contrib.auth.authentication"
HEADER_NAME: Final = r"^[A-Za-z0-9-]{1,64}$"
COOKIE_NAME: Final = r"^[A-Za-z0-9_.-]{1,64}$"
"""Cookie names: letters, digits, ``_ . -`` (``__Host-`` included)."""
ORIGIN: Final = re.compile(r"https?://(\*\.)?[a-z0-9.-]+(:[0-9]{1,5})?")
"""An origin: a scheme and a host, and a port; ``*.`` matches subdomains."""
HMAC_ALGORITHMS: Final = frozenset({"HS256", "HS384", "HS512"})
"""JWT algorithms that sign and verify with one shared secret."""
MAX_PREVIOUS_KEYS: Final = 3
"""The most retired keys ``AUTH.JWT.PREVIOUS_KEYS`` may keep."""
MAX_RETIREMENT_AHEAD: Final = timedelta(days=1)
"""How far ahead of startup a previous key's ``RETIRED_AT`` may be: a rollout."""
PRINCIPAL_TTL: Final = timedelta(seconds=5)
"""How long a cached principal is used by default (``PRINCIPAL_CACHE.TTL``).

Also the longest ``check`` accepts without ``STAMPS`` where deployed."""
MAX_PRINCIPAL_TTL: Final = timedelta(minutes=5)
"""The longest ``PRINCIPAL_CACHE.TTL`` may be: a JWT's default lifetime."""
ROUTE_RULES: Final = "REQUIRE_ROUTE_RULES"
"""``AUTH.REQUIRE_ROUTE_RULES``: the core's since 0.10, refused here since 0.11."""
LINK_KEYS: Final = (
    "VERIFICATION",
    "PASSWORD_RESET_TIMEOUT",
    "PASSWORD_RESET_URL",
    "EMAIL_VERIFICATION_TIMEOUT",
    "EMAIL_VERIFICATION_URL",
    "SIGNUP_URL",
)
"""The keys of 1.x's links, removed in 2.0 (ADR 0126): codes are the only way."""
PCI_DSS_4: Final = "pci-dss-4"
"""``AUTH.PRESET`` for PCI DSS v4.0 (requirements 8.2.8 and 8.3)."""
PCI_MIN_LENGTH: Final = 12
"""The fewest characters PCI DSS v4.0 takes in a password (8.3.6)."""
PCI_HISTORY: Final = 4
"""The passwords PCI DSS v4.0 forbids using again: the last four (8.3.7)."""
PCI_ATTEMPTS: Final = 10
"""The most wrong passwords PCI DSS v4.0 lets through before refusing (8.3.4)."""
PCI_LOCKOUT: Final = timedelta(minutes=30)
"""The shortest refusal PCI DSS v4.0 asks after them (8.3.4)."""
PCI_IDLE: Final = timedelta(minutes=15)
"""How long PCI DSS v4.0 lets a session idle before it asks for a login (8.2.8)."""
PCI_PRESET: Final[Mapping[str, object]] = {
    "PASSWORD_VALIDATORS": (
        {"NAME": f"{VALIDATORS}.UserAttributeSimilarityValidator"},
        {
            "NAME": f"{VALIDATORS}.MinimumLengthValidator",
            "OPTIONS": {"min_length": PCI_MIN_LENGTH},
        },
        {"NAME": f"{VALIDATORS}.CommonPasswordValidator"},
        {"NAME": f"{VALIDATORS}.LettersAndDigitsValidator"},
    ),
    "PASSWORD_HISTORY": PCI_HISTORY,
    "PASSWORD_CHANGE_ON_FIRST_USE": True,
    "LOGIN_RATE": "10/hour",
    "PASSWORD_CHECKS_COUNT_AS_LOGINS": True,
    # A login ends between IDLE_TIMEOUT and a tenth more after its last use
    # (tokens.idle_since): 13 minutes ends it by 14.3, within 8.2.8's 15.
    "IDLE_TIMEOUT": timedelta(minutes=13),
}
"""What ``AUTH.PRESET = "pci-dss-4"`` sets, for each key left unset."""
PRESETS: Final[Mapping[str, Mapping[str, object]]] = {PCI_DSS_4: PCI_PRESET}
"""Each ``AUTH.PRESET``, and the settings it sets."""
MAX_STEP_UP_AGE: Final = timedelta(days=1)
"""The longest ``StepUp(max_age=...)`` may be; older step-ups are deleted."""

type Factor = Literal["pwd", "otp"]
"""What a step-up or a transaction authorization asks for (RFC 8176's names):
the password (``pwd``), or a code of the authenticator app (``otp``)."""


class PasswordValidatorConfig(BaseModel):
    """One ``PASSWORD_VALIDATORS`` entry: a class and its options."""

    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)

    NAME: str
    """Dotted path to the validator class."""
    OPTIONS: dict[str, Any] = Field(default_factory=dict)
    """Keyword arguments for the class, such as ``{"min_length": 12}``."""

    def __hash__(self) -> int:
        """Hash the name and options, so loaded validators can be cached."""
        return hash((self.NAME, json.dumps(self.OPTIONS, sort_keys=True, default=str)))


type JWTAlgorithm = Literal[
    "HS256",
    "HS384",
    "HS512",
    "RS256",
    "RS384",
    "RS512",
    "PS256",
    "PS384",
    "PS512",
    "ES256",
    "ES384",
    "ES512",
    "EdDSA",
]
"""The algorithms a JSON Web Token may be signed with."""


class PreviousJWTKey(BaseModel):
    """One ``AUTH["JWT"]["PREVIOUS_KEYS"]`` entry: a key that signed, kept to verify.

    Tokens whose ``kid`` names it are verified with it, and its algorithm
    alone, until ``RETIRED_AT`` plus ``ACCESS_TOKEN_TTL`` and ``LEEWAY``:
    then they are refused, unexpired or not (ADR 0097).
    """

    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)

    KEY: SecretStr
    """The old HMAC secret, or the old public key in PEM (never a private key)."""
    ALGORITHM: JWTAlgorithm | None = None
    """The algorithm it signed with; None: ``AUTH.JWT.ALGORITHM``."""
    RETIRED_AT: AwareDatetime
    """When the last process stopped signing with it: the end of the rollout
    of the new key, at most a day after startup."""


class JWTSettings(BaseModel):
    """``AUTH["JWT"]``: signed access tokens (``JWTAuthentication``)."""

    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)

    SIGNING_KEY: SecretStr | None = None
    """The HMAC secret (32 bytes or more), or the private key in PEM."""
    VERIFYING_KEY: str | None = None
    """The public key in PEM, for RS*, PS*, ES*, and EdDSA."""
    ALGORITHM: JWTAlgorithm = "HS256"
    """The algorithm the signing key signs with; a token's own header never
    chooses one."""
    PREVIOUS_KEYS: tuple[PreviousJWTKey, ...] = Field(
        default=(), max_length=MAX_PREVIOUS_KEYS
    )
    """Keys that signed before ``SIGNING_KEY``, kept to verify what they
    signed for one token lifetime after they retired; at most 3."""
    ACCESS_TOKEN_TTL: timedelta = Field(default=timedelta(minutes=5), gt=timedelta(0))
    """How long an access token works; it cannot be revoked before."""
    REFRESH_TOKEN_TTL: timedelta = Field(default=timedelta(days=14), gt=timedelta(0))
    """How long a refresh token works; each use replaces it."""
    ISSUER: str | None = None
    """``iss``: set in every token and required in every token checked."""
    AUDIENCE: str | None = None
    """``aud``: set in every token and required in every token checked."""
    LEEWAY: timedelta = Field(default=timedelta(0), ge=timedelta(0))
    """Clock difference tolerated when checking ``exp`` and ``iat``."""


class CookieSettings(BaseModel):
    """``AUTH["COOKIE"]``: browser sessions (``CookieAuthentication``)."""

    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)

    NAME: str | None = Field(default=None, pattern=COOKIE_NAME)
    """The session cookie: HttpOnly, so scripts never read it. None:
    ``__Host-dk_session`` where browsers take the prefix (``SECURE``, no
    ``DOMAIN``, the ``PATH`` ``/``), ``dk_session`` elsewhere."""
    CSRF_NAME: str | None = Field(default=None, pattern=COOKIE_NAME)
    """The cookie that hands scripts the CSRF token. None: ``__Host-dk_csrf``
    or ``dk_csrf``, as ``NAME``."""
    CSRF_HEADER: str = Field(default="X-CSRF-Token", pattern=r"^[A-Za-z0-9-]{1,64}$")
    """The header scripts send the CSRF token back in."""
    SAMESITE: Literal["lax", "strict", "none"] = "lax"
    SECURE: bool | None = None
    """Send the cookies over HTTPS only; None: unless ``DEBUG``."""
    DOMAIN: str | None = Field(default=None, pattern=r"^[A-Za-z0-9.-]{1,253}$")
    """None: this host only (not its subdomains)."""
    PATH: str = Field(default="/", pattern=r"^/[A-Za-z0-9._~/-]*$")
    TTL: timedelta = Field(default=timedelta(days=14), gt=timedelta(0))
    """How long a browser session works."""
    END_PREVIOUS_SESSION: bool = True
    """A login from a browser ends the session its cookie held: the browser
    replaces the cookie, so a copy of the old one would otherwise work on,
    unseen, for ``TTL`` (AUTH-05). False keeps it, as before 0.10."""

    @model_validator(mode="after")
    def check_prefixes(self) -> Self:
        """Keep the rules browsers apply to ``__Host-`` and ``SameSite=None``."""
        if self.SAMESITE == "none" and self.SECURE is False:
            raise ValueError('SAMESITE "none" needs SECURE cookies')
        for name in (self.NAME or "", self.CSRF_NAME or ""):
            if name.startswith("__Host-") and (
                self.SECURE is False or self.DOMAIN is not None or self.PATH != "/"
            ):
                raise ValueError(
                    "__Host- cookies need SECURE, no DOMAIN, and the PATH /"
                )
        return self


class MfaSettings(BaseModel):
    """``AUTH["MFA"]``: logins with a second factor (``auth.mfa``)."""

    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)

    ISSUER: str | None = Field(default=None, min_length=1, max_length=100)
    """The name authenticator apps show; None: ``PROJECT_NAME``."""
    CHALLENGE_TTL: timedelta = Field(
        default=timedelta(minutes=5), gt=timedelta(0), le=timedelta(hours=1)
    )
    """How long the step between the password and the code may take."""
    RECOVERY_CODES: int = Field(default=10, ge=1, le=50)
    """Recovery codes each user gets, each good once."""
    WINDOW: int = Field(default=1, ge=0, le=10)
    """30-second steps either side of now whose codes still count (clock drift)."""
    RATE: str | None = "10/hour"
    """Wrong codes per user, wherever they come from."""
    ADDRESS_RATE: str | None = "60/hour"
    """Wrong codes per client address at ``/mfa/login``, with a challenge
    that holds: an address may be an office or a carrier's, of many users."""

    @field_validator("RATE", "ADDRESS_RATE")
    @classmethod
    def _check_rate(cls, value: str | None) -> str | None:
        """Refuse rates ``parse_rate`` cannot read."""
        if value is not None:
            parse_rate(value)
        return value


def with_a_factor(value: tuple[Factor, ...]) -> tuple[Factor, ...]:
    """Refuse a list of methods without the second factor, or with repeats."""
    if "otp" not in value:
        raise ValueError(
            'METHODS must hold "otp": a password alone is no second factor'
        )
    if len(set(value)) != len(value):
        raise ValueError("METHODS lists each method once")
    return value


class StepUpSettings(BaseModel):
    """``AUTH["STEP_UP"]``: authenticating again for one act (``auth.stepup``)."""

    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)

    METHODS: tuple[Factor, ...] = ("otp",)
    """What a step-up proves: a code of the authenticator app (``otp``), or
    the password with it (``("pwd", "otp")``). ``StepUp`` asks for them all."""

    _check_methods = field_validator("METHODS")(with_a_factor)


class ScaSettings(BaseModel):
    """``AUTH["SCA"]``: transactions authorized for one amount and payee."""

    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)

    METHODS: tuple[Factor, ...] = ("otp",)
    """What confirming a transaction proves, as ``STEP_UP.METHODS``."""
    TTL: timedelta = Field(
        default=timedelta(minutes=5), gt=timedelta(0), le=timedelta(minutes=30)
    )
    """How long a transaction's authorization request works, from when it
    is made, confirming and using it included; 30 minutes at most."""

    _check_methods = field_validator("METHODS")(with_a_factor)


class ClientCertSettings(BaseModel):
    """``AUTH["CLIENT_CERT"]``: mutual TLS (``ClientCertificateAuthentication``)."""

    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)

    MATCH: Literal["fingerprint", "subject"] = "fingerprint"
    """Find the user by the certificate's SHA-256 (``addcertificate``), or by
    its subject's common name as the username."""
    HEADER: str | None = Field(default=None, pattern=HEADER_NAME)
    """The header a proxy of ``PROXIES`` puts the URL-encoded PEM in; None:
    only certificates the server itself checked (the ASGI TLS extension)."""
    VERIFY_HEADER: str | None = Field(default=None, pattern=HEADER_NAME)
    """The header in which the proxy says ``SUCCESS`` if it checked the
    certificate (nginx's ``$ssl_client_verify``)."""
    PROXIES: tuple[IPvAnyNetwork, ...] = ()
    """The addresses of the proxies that check certificates and set
    ``HEADER``: only connections from them may send it. Not
    ``TRUSTED_PROXIES``, which may hold whole networks: staging and
    production take single addresses here (``/32``, ``/128``)."""


class PrincipalCacheSettings(BaseModel):
    """``AUTH["PRINCIPAL_CACHE"]``: who credentials name, kept between requests.

    See ``domainkit.contrib.auth.principals`` and ADR 0058.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)

    ENABLED: bool = True
    """Keep principals between requests; False reads them every time."""
    TTL: timedelta = Field(default=PRINCIPAL_TTL, gt=timedelta(0), le=MAX_PRINCIPAL_TTL)
    """How long a principal is used before it is read again, at most 5 minutes.

    The longest a change made by another process takes to reach this one,
    unless ``STAMPS`` names a shared cache.
    """
    MAX_ENTRIES: int = Field(default=10_000, ge=1, le=100_000)
    """Principals kept per process; the least recently used go first."""
    STAMPS: str | None = Field(default=None, min_length=1, max_length=100)
    """A ``CACHES`` alias of a Redis or Valkey cache every process shares.

    Changes stamp what they affect there, and each request checks the
    stamps of its principal in one round trip: a change made anywhere then
    reaches every process at once. None: the ``TTL`` is the bound.
    """


class CaptchaSettings(BaseModel):
    """``AUTH["CAPTCHA"]``: the captcha the routes bots try ask for."""

    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)

    PROVIDER: Literal["turnstile", "recaptcha", "hcaptcha"]
    """Cloudflare Turnstile, Google reCAPTCHA, or hCaptcha."""
    SECRET: SecretStr
    """The provider's secret key for the site."""
    ON: frozenset[Literal["login", "signup", "password_reset"]] = frozenset(
        {"login", "signup", "password_reset"}
    )
    """The routes that ask for a token."""
    MIN_SCORE: float = Field(default=0.5, ge=0, le=1)
    """reCAPTCHA v3's lowest score let through."""


class AuthSettings(BaseModel):
    """Users, hashing, validation, and backends; every key is optional."""

    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)

    @model_validator(mode="before")
    @classmethod
    def _refuse_removed(cls, data: object) -> object:
        """Name what replaced a removed key (DSGN-02; links, in 2.0).

        A ``PRESET`` then sets each key of its own left unset.
        """
        return _with_preset(_removed_keys_refused(data))

    USER_MODEL: str = "domainkit.contrib.auth.models.User"
    """Dotted path to the user class (``AUTH_USER_MODEL``)."""
    USER_TABLE: str | None = None
    """Dotted path to a custom user's ``Table``; required with ``USER_MODEL``."""
    PASSWORD_HASHERS: tuple[str, ...] = Field(
        default=(
            PBKDF2,
            "domainkit.contrib.auth.hashers.PBKDF2SHA1PasswordHasher",
            "domainkit.contrib.auth.hashers.Argon2PasswordHasher",
            "domainkit.contrib.auth.hashers.ScryptPasswordHasher",
        ),
        min_length=1,
    )
    """Hasher classes; the first hashes new passwords, all check old ones."""
    HASHING_THREADS: int = Field(default=1, ge=1, le=1024)
    """Threads per process that hash passwords; each keeps a core busy.

    One, like a Django worker: with a process per core, more would take
    cores from the event loops. Raise it where cores sit idle.
    """
    HASHING_QUEUE: int = Field(default=8, ge=0, le=10_000)
    """Hashing calls that may wait for a thread; more get 503 at once."""
    HASHING_RESERVED: int = Field(default=1, ge=0)
    """Places of the queue kept for signed-in users changing their password.

    Logins and other checks may fill the rest. More than ``HASHING_QUEUE``
    counts as ``HASHING_QUEUE``.
    """
    PASSWORD_VALIDATORS: tuple[PasswordValidatorConfig, ...] = (
        PasswordValidatorConfig(NAME=f"{VALIDATORS}.UserAttributeSimilarityValidator"),
        PasswordValidatorConfig(NAME=f"{VALIDATORS}.MinimumLengthValidator"),
        PasswordValidatorConfig(NAME=f"{VALIDATORS}.CommonPasswordValidator"),
        PasswordValidatorConfig(NAME=f"{VALIDATORS}.NumericPasswordValidator"),
    )
    """Rules new passwords must follow; ``()`` checks nothing."""
    PRESET: Literal["pci-dss-4"] | None = None
    """A set of settings a standard asks for: ``"pci-dss-4"`` sets
    ``PASSWORD_VALIDATORS``, ``PASSWORD_HISTORY``,
    ``PASSWORD_CHANGE_ON_FIRST_USE``, ``LOGIN_RATE``,
    ``PASSWORD_CHECKS_COUNT_AS_LOGINS``, and ``IDLE_TIMEOUT``
    where they are left unset, and refuses to start where one set
    contradicts it (``auth.E010``). None: no preset."""
    PASSWORD_HISTORY: int = Field(default=0, ge=0, le=24)
    """Passwords of a user's own, the current one among them, that a new one
    may not repeat. 0: none is kept."""
    PASSWORD_CHANGE_ON_FIRST_USE: bool = False
    """A password set by staff (the admin API) logs in only once it is
    changed: ``/login`` and ``/session`` answer ``403
    password_change_required`` until the login sends ``new_password``."""
    CREDENTIAL_CHANGE_DEVICE_LOGINS: Literal["end", "keep", "ask"] = "end"
    """What the user's password change (``/password``), reset
    (``/password-reset/code``), or turning the second factor on
    (``/mfa/totp/confirm``) does to the logins of their bound devices
    (ADR 0131, 0132): ``"end"`` ends them with the other logins; ``"keep"``
    keeps them; ``"ask"`` does as the request's ``keep_device_logins`` says,
    true by default. Every other login ends in each case."""
    BACKENDS: tuple[str, ...] = Field(
        default=("domainkit.contrib.auth.backends.ModelBackend",), min_length=1
    )
    """Authentication and permission backends, asked in order."""
    AUTHENTICATION_CLASSES: tuple[str, ...] = Field(
        default=(f"{AUTHENTICATION}.TokenAuthentication",), min_length=1
    )
    """How requests prove who sent them, tried in order; the first logs in."""
    AUTHENTICATION_EXEMPT_PATHS: tuple[str, ...] = ("/health", "/ready", "/metrics")
    """Paths served without looking at credentials (probes, ``METRICS_TOKEN``)."""
    TOKEN_TTL: timedelta | None = Field(default=timedelta(days=14), gt=timedelta(0))
    """How long a token from logging in works (``TokenAuthentication``)."""
    TOKEN_PREFIX: str = Field(default="dkt_", pattern=r"^[A-Za-z][A-Za-z0-9]{1,15}_$")
    """Starts every opaque token, so secret scanners and people can spot one."""
    MAX_LOGINS: int = Field(default=100, ge=1, le=10_000)
    """Logins a user holds at once: login tokens, browser sessions, and JSON
    Web Token logins. A new one past it ends the oldest, so a user is never
    refused for their own history (AUTH-08); personal tokens do not count."""
    IDLE_TIMEOUT: timedelta | None = Field(
        default=None, ge=timedelta(minutes=5), le=timedelta(days=30)
    )
    """End a login unused for this long: a login token or a browser session
    that authenticated nothing, or a JSON Web Token login not refreshed.
    None: they last their whole lifetime, as before 0.10. Personal tokens
    and certificates have none. Last uses are written a tenth as often, at
    most every 5 minutes, so a login ends within a tenth of this after it."""
    MAX_LIVE_TOKENS: int = Field(default=100, ge=1, le=10_000)
    """Live access tokens (logins and personal tokens) a user may hold and
    still make a personal token (``POST /auth/tokens``): past it, ``409
    too_many_tokens``. It bounds what one user can make the server keep."""
    PERSONAL_TOKEN_MAX_AGE: timedelta | None = Field(
        default=timedelta(days=90), ge=timedelta(days=1), le=timedelta(days=3650)
    )
    """The longest a personal token made at ``POST /auth/tokens`` may work,
    from a day to 3,650 days; tokens asked without ``expires_in_days`` get
    the shorter of 90 days and this. None lets a token work until revoked
    (``expires_in_days: null``), as before 1.0 (AUTH-16)."""
    LOGIN_RATE: str | None = "10/minute"
    """Wrong passwords allowed per client address and per username.

    Also counts reset, verification, signup, and second-factor links that
    do not hold, per client address (one-time codes count against
    ``MFA.RATE``). Checks that succeed are not counted.
    """
    ACCOUNT_LOGIN_RATE: str | None = "100/day"
    """Wrong passwords allowed per username, from anywhere, over a longer
    period: ``LOGIN_RATE`` bounds bursts, this the day's total, as NIST SP
    800-63B 5.2.2 asks (100 failed attempts at most). Clients with a device
    proof for the account have a count of their own at this rate, which
    strangers cannot use up (ADR 0087). None: ``LOGIN_RATE`` alone, as
    before 1.0 (AUTH-19)."""
    PASSWORD_CHECKS_COUNT_AS_LOGINS: bool = False
    """Count wrong passwords of signed-in users (``/password``, a step-up,
    the second factor's changes) against the account's count of wrong
    logins too, so ``LOGIN_RATE`` bounds every guess at the account's
    password. False, the default, counts them apart, so others' wrong
    logins never keep a signed-in user from changing the password."""
    DEVICE_TTL: timedelta | None = Field(
        default=timedelta(days=30),
        ge=timedelta(hours=1),
        le=timedelta(days=400),
    )
    """How long a client that logged in keeps a count of its own (a cookie).

    A login that works hands out a signed, HttpOnly device proof for its
    account; logins that send it count against ``LOGIN_RATE`` per device,
    not per account or address, so others' wrong passwords never keep the
    user out on it. None: no proofs, as before 0.10 (``auth.devices``).
    """
    EMAIL_RATE: str | None = "5/hour"
    """Requests that send mail (resets, verifications, signups) per client
    address and per email address."""
    THROTTLE_FAIL_CLOSED: bool | None = None
    """Refuse attempts with 503 while ``RATE_LIMIT_BACKEND`` cannot count them.

    ``RedisBackend`` lets requests through while Redis fails, so a brute
    force timed with an outage goes uncounted. True refuses password and
    code checks, and requests that send mail, instead, until it counts
    again; other requests are not affected. None, the default, is True
    in staging and production (since 0.11) and False elsewhere.
    """
    REQUIRE_SHARED_THROTTLES: bool = True
    """Staging and production refuse to start while attempts are counted in
    each process (a ``MemoryBackend``), since 0.11 (``auth.E105``); False
    only warns (``auth.W004``), as 0.10 did."""
    JWT: JWTSettings = JWTSettings()
    """Signing and lifetimes of JSON Web Tokens (``JWTAuthentication``)."""
    IDENTIFIERS: tuple[str, ...] | None = Field(default=None, min_length=1)
    """The user fields that identify a user, in the order a login tries
    them, such as ``("email", "mobile_number")`` (ADR 0126). None: the user
    model's ``USERNAME_FIELD`` alone, as before 1.8."""
    CODE_TTL: timedelta = Field(
        default=timedelta(minutes=10),
        ge=timedelta(minutes=1),
        le=timedelta(hours=1),
    )
    """How long a code works, from 1 minute to 1 hour."""
    CODE_ATTEMPTS: int = Field(default=5, ge=1, le=10)
    """Wrong tries that spend a code, from 1 to 10."""
    SIGNUP_ENABLED: bool = False
    """Let anyone create an account at ``/signup`` (``auth.signup.router``)."""
    COOKIE: CookieSettings = CookieSettings()
    """Browser sessions in cookies (``CookieAuthentication``)."""
    MFA: MfaSettings = MfaSettings()
    """Second factors: authenticator apps and recovery codes."""
    STEP_UP: StepUpSettings = StepUpSettings()
    """Authenticating again for one act (``StepUp``)."""
    SCA: ScaSettings = ScaSettings()
    """Transactions authorized for one amount and payee (``auth.sca``)."""
    CLIENT_CERT: ClientCertSettings = ClientCertSettings()
    """Client certificates (mutual TLS)."""
    PRINCIPAL_CACHE: PrincipalCacheSettings = PrincipalCacheSettings()
    """Principals kept between requests, so most cost no query."""
    CAPTCHA: CaptchaSettings | None = None
    """A captcha on login, signup, and password reset (``auth.captcha``)."""
    IMPERSONATION_TTL: timedelta | None = Field(
        default=None, gt=timedelta(0), le=timedelta(hours=8)
    )
    """How long a support login as another user works (up to 8 hours); None: off."""
    CSRF_TRUSTED_ORIGINS: tuple[str, ...] = ()
    """Origins of pages that may send cookie requests besides ``ALLOWED_HOSTS``'."""

    @field_validator("LOGIN_RATE", "ACCOUNT_LOGIN_RATE", "EMAIL_RATE")
    @classmethod
    def _check_rate(cls, value: str | None) -> str | None:
        """Refuse rates ``parse_rate`` cannot read."""
        if value is not None:
            parse_rate(value)
        return value

    @field_validator("CSRF_TRUSTED_ORIGINS")
    @classmethod
    def _check_origins(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        """Origins are a scheme and a host: ``https://app.example.com``."""
        for origin in value:
            if not ORIGIN.fullmatch(origin):
                raise ValueError(
                    "origins are a scheme and a host, such as "
                    "https://app.example.com or https://*.example.com"
                )
        return value


def _removed_keys_refused(data: object) -> object:
    """Return ``AUTH``'s raw ``data``, unless it sets a removed key.

    Raises:
        ValueError: It sets ``REQUIRE_ROUTE_RULES`` (removed in 0.11), or a
            key of 1.x's links (``LINK_KEYS``, removed in 2.0): the message
            says what to do instead.
    """
    if not isinstance(data, Mapping):
        return data
    if ROUTE_RULES in data:
        raise ValueError(
            f"AUTH.{ROUTE_RULES} was removed in 0.11: set {ROUTE_RULES}, "
            "a setting of its own, outside AUTH"
        )
    removed = [key for key in LINK_KEYS if key in data]
    if removed:
        raise ValueError(
            f"AUTH.{removed[0]} was removed in 2.0 with the links it set up: "
            "codes are the only way (AUTH.CODE_TTL, AUTH.CODE_ATTEMPTS). "
            f"Delete {', '.join(f'AUTH.{key}' for key in removed)}."
        )
    return data


def _with_preset(data: object) -> object:
    """``AUTH``'s raw ``data``, with its preset's keys where it leaves them unset."""
    if not isinstance(data, Mapping):
        return data
    preset = PRESETS.get(str(data.get("PRESET")), {})
    return {**preset, **data}


def auth_settings() -> AuthSettings:
    """The validated ``AUTH`` settings."""
    return registry.settings(AuthSettings)


def identifier_fields(model: type[Any]) -> tuple[str, ...]:
    """``AUTH.IDENTIFIERS``, or the ``USERNAME_FIELD`` of ``model`` without it."""
    configured = auth_settings().IDENTIFIERS
    return configured if configured is not None else (model.USERNAME_FIELD,)
