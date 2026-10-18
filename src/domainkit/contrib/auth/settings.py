"""Settings of ``domainkit.contrib.auth``: ``AUTH = {...}``."""

import json
from datetime import timedelta
from typing import Any, Final, Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator

from domainkit.apps import registry
from domainkit.utils.rates import parse_rate

PBKDF2: str = "domainkit.contrib.auth.hashers.PBKDF2PasswordHasher"
VALIDATORS: str = "domainkit.contrib.auth.validators"
AUTHENTICATION: str = "domainkit.contrib.auth.authentication"
HMAC_ALGORITHMS: Final = frozenset({"HS256", "HS384", "HS512"})
"""JWT algorithms that sign and verify with one shared secret."""


class PasswordValidatorConfig(BaseModel):
    """One ``PASSWORD_VALIDATORS`` entry: a class and its options."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    NAME: str
    """Dotted path to the validator class."""
    OPTIONS: dict[str, Any] = Field(default_factory=dict)
    """Keyword arguments for the class, such as ``{"min_length": 12}``."""

    def __hash__(self) -> int:
        """Hash the name and options, so loaded validators can be cached."""
        return hash((self.NAME, json.dumps(self.OPTIONS, sort_keys=True, default=str)))


class JWTSettings(BaseModel):
    """``AUTH["JWT"]``: signed access tokens (``JWTAuthentication``)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    SIGNING_KEY: SecretStr | None = None
    """The HMAC secret (32 bytes or more), or the private key in PEM."""
    VERIFYING_KEY: str | None = None
    """The public key in PEM, for RS*, PS*, ES*, and EdDSA."""
    ALGORITHM: Literal[
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
    ] = "HS256"
    """The only algorithm accepted; the token's own header is never trusted."""
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


class AuthSettings(BaseModel):
    """Users, hashing, validation, and backends; every key is optional."""

    model_config = ConfigDict(frozen=True, extra="forbid")

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
    HASHING_THREADS: int = Field(default=1, ge=1)
    """Threads per process that hash passwords; each keeps a core busy.

    One, like a Django worker: with a process per core, more would take
    cores from the event loops. Raise it where cores sit idle.
    """
    HASHING_QUEUE: int = Field(default=8, ge=0)
    """Hashing calls that may wait for a thread; more get 503 at once."""
    PASSWORD_VALIDATORS: tuple[PasswordValidatorConfig, ...] = (
        PasswordValidatorConfig(NAME=f"{VALIDATORS}.UserAttributeSimilarityValidator"),
        PasswordValidatorConfig(NAME=f"{VALIDATORS}.MinimumLengthValidator"),
        PasswordValidatorConfig(NAME=f"{VALIDATORS}.CommonPasswordValidator"),
        PasswordValidatorConfig(NAME=f"{VALIDATORS}.NumericPasswordValidator"),
    )
    """Rules new passwords must follow; ``()`` checks nothing."""
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
    REQUIRE_ROUTE_RULES: bool = True
    """Every route must say who may call it (``Require``, or ``Public``)."""
    TOKEN_TTL: timedelta | None = Field(default=timedelta(days=14), gt=timedelta(0))
    """How long a token from logging in works (``TokenAuthentication``)."""
    TOKEN_PREFIX: str = Field(default="dkt_", pattern=r"^[A-Za-z][A-Za-z0-9]{1,15}_$")
    """Starts every opaque token, so secret scanners and people can spot one."""
    LOGIN_RATE: str | None = "10/minute"
    """Login attempts allowed per client address and per username."""
    JWT: JWTSettings = JWTSettings()
    """Signing and lifetimes of JSON Web Tokens (``JWTAuthentication``)."""

    @field_validator("LOGIN_RATE")
    @classmethod
    def _check_rate(cls, value: str | None) -> str | None:
        """Refuse rates ``parse_rate`` cannot read."""
        if value is not None:
            parse_rate(value)
        return value


def auth_settings() -> AuthSettings:
    """The validated ``AUTH`` settings."""
    return registry.settings(AuthSettings)
