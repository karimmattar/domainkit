"""Settings of ``domainkit.contrib.keys``: ``KEYS = {...}``."""

from typing import Annotated, Final, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator

KEY_ID_PATTERN: Final = r"^[A-Za-z0-9_-]{1,32}$"
"""A data key's id: 1 to 32 letters, digits, ``_``, ``-`` (``2026-10``)."""
MAX_DATA_KEYS: Final = 16
REGION_PATTERN: Final = r"^[a-z]{2}(-[a-z]+)+-[0-9]{1,2}$"
"""An AWS region, such as ``eu-west-1`` or ``us-gov-west-1``."""
DEVELOPMENT_KEY: Final = "dev"
"""The id of the data key derived from ``SECRET_KEY`` when ``DATA_KEYS`` is empty."""
PROVIDER_PREFIXES: Final = {"local": "local:", "aws-kms": "aws-kms:", "vault": "vault:"}

type KeyId = Annotated[str, Field(pattern=KEY_ID_PATTERN)]


class AwsKmsSettings(BaseModel):
    """``KEYS["AWS_KMS"]``: the KMS key data keys are wrapped under."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    KEY_ID: str = Field(min_length=1, max_length=2048)
    """The key's ARN, id, or ``alias/<name>``."""
    REGION: str = Field(pattern=REGION_PATTERN)
    """Its region; KMS is called at ``https://kms.<REGION>.amazonaws.com``."""
    ENDPOINT: str | None = Field(default=None, pattern=r"^https://[^\s/?#]+/?$")
    """Another HTTPS endpoint: a VPC endpoint, or FIPS (``kms-fips``)."""


class VaultSettings(BaseModel):
    """``KEYS["VAULT"]``: the transit key data keys are wrapped under."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    URL: str = Field(pattern=r"^https?://[^\s/?#]+/?$")
    """Vault's address, such as ``https://vault.internal:8200``."""
    KEY: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.-]+$")
    """The transit key's name."""
    TOKEN: SecretStr = Field(min_length=1)
    """A token allowed ``update`` on ``datakey``, ``decrypt``, ``rewrap`` of the key."""
    MOUNT: str = Field(
        default="transit", pattern=r"^[A-Za-z0-9_.-]+(/[A-Za-z0-9_.-]+)*$"
    )
    """Where the transit engine is mounted."""
    NAMESPACE: str | None = Field(default=None, min_length=1, max_length=255)
    """Vault Enterprise's namespace, sent as ``X-Vault-Namespace``."""


class KeysSettings(BaseModel):
    """Where the key-encryption key is, and the data keys it wraps.

    ``PROVIDER`` names the service holding the KEK; ``DATA_KEYS`` maps ids
    to data keys it wrapped (``manage.py keys generate`` makes one), and
    ``CURRENT_KEY`` names the one new values are sealed under. The others
    are retired: they still open what they sealed, until
    ``manage.py keys reencrypt`` moves it to the current key.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)

    PROVIDER: Literal["local", "aws-kms", "vault"] = "local"
    """``local``: a KEK derived from ``SECRET_KEY`` (development and tests);
    ``aws-kms`` (``AWS_KMS``); ``vault`` (``VAULT``)."""
    ALLOW_LOCAL_KEK: bool = False
    """Let staging and production use the local KEK (``keys.E001`` otherwise)."""
    AWS_KMS: AwsKmsSettings | None = None
    VAULT: VaultSettings | None = None
    DATA_KEYS: dict[KeyId, str] = Field(default_factory=dict, max_length=MAX_DATA_KEYS)
    """Data keys by id, each wrapped by ``PROVIDER`` (at most 16). Empty with
    the local provider: one development key, ``dev``, derived from ``SECRET_KEY``."""
    CURRENT_KEY: KeyId | None = None
    """The data key new values are sealed under; may be left out with one key."""
    TIMEOUT: float = Field(default=5.0, gt=0, le=60)
    """Seconds each call to the key service may take, retries included."""

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        """Refuse a provider without its settings, and keys it did not wrap.

        Raises:
            ValueError: ``AWS_KMS`` or ``VAULT`` is missing for its provider,
                ``DATA_KEYS`` is empty for a key service, a data key is
                wrapped by another provider, or ``CURRENT_KEY`` names none.
        """
        if self.PROVIDER == "aws-kms" and self.AWS_KMS is None:
            raise ValueError('PROVIDER "aws-kms" needs AWS_KMS (KEY_ID, REGION).')
        if self.PROVIDER == "vault" and self.VAULT is None:
            raise ValueError('PROVIDER "vault" needs VAULT (URL, KEY, TOKEN).')
        if self.PROVIDER != "local" and not self.DATA_KEYS:
            raise ValueError(
                f"PROVIDER {self.PROVIDER!r} needs DATA_KEYS: make one with "
                "'manage.py keys generate'."
            )
        prefix = PROVIDER_PREFIXES[self.PROVIDER]
        if wrong := sorted(
            key_id
            for key_id, wrapped in self.DATA_KEYS.items()
            if not wrapped.startswith(prefix)
        ):
            raise ValueError(
                f"DATA_KEYS {', '.join(wrong)} were not wrapped by {self.PROVIDER!r} "
                f"(they do not start with {prefix!r})."
            )
        if self.CURRENT_KEY is not None and self.CURRENT_KEY not in self.DATA_KEYS:
            raise ValueError(f"CURRENT_KEY {self.CURRENT_KEY!r} is not in DATA_KEYS.")
        if self.CURRENT_KEY is None and len(self.DATA_KEYS) > 1:
            raise ValueError("CURRENT_KEY must name one of the DATA_KEYS.")
        return self

    @property
    def current(self) -> str:
        """The id of the data key new values are sealed under."""
        if self.CURRENT_KEY is not None:
            return self.CURRENT_KEY
        return next(iter(self.DATA_KEYS), DEVELOPMENT_KEY)

    @property
    def retired(self) -> tuple[str, ...]:
        """The ids of the data keys that only open values, sorted."""
        return tuple(sorted(set(self.DATA_KEYS) - {self.current}))
