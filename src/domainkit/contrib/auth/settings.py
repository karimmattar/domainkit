"""Settings of ``domainkit.contrib.auth``: ``AUTH = {...}``."""

import json
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from domainkit.apps import registry

PBKDF2: str = "domainkit.contrib.auth.hashers.PBKDF2PasswordHasher"
VALIDATORS: str = "domainkit.contrib.auth.validators"


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


def auth_settings() -> AuthSettings:
    """The validated ``AUTH`` settings."""
    return registry.settings(AuthSettings)
