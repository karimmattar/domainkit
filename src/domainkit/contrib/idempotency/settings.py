"""Settings of ``domainkit.contrib.idempotency``: ``IDEMPOTENCY = {...}``."""

from typing import Annotated, Final, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from domainkit.conf import DEFAULT_DB_ALIAS

PER_ADDRESS: Final = "per-address"
REFUSE: Final = "refuse"
type CookieName = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_.-]{1,64}$")]


class IdempotencySettings(BaseModel):
    """How long keys live and which requests may carry one."""

    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)

    DATABASE: str = DEFAULT_DB_ALIAS
    TTL_SECONDS: float = Field(default=24 * 3600.0, gt=0, le=30 * 24 * 3600)
    """How long a stored response is replayed for the same key; at least
    ``LEASE_SECONDS``."""
    TOMBSTONE_SECONDS: float = Field(default=7 * 24 * 3600.0, ge=0, le=90 * 24 * 3600)
    """How long a key whose request may have committed is kept once
    ``TTL_SECONDS`` has passed, without its response (a tombstone): a retry
    gets 409 ``idempotency_response_unavailable`` instead of running again.

    A week covers the retry windows clients plausibly have past a day: a
    queue replayed after an outage over a long weekend, a broker's default
    retention (Kafka keeps 7 days, SQS 4 by default), messaging's own 7
    days of finished messages. 0 forgets such a key with its response, and
    a retry after ``TTL_SECONDS`` runs again, as 0.10 did (FIN-84).
    """
    LEASE_SECONDS: float = Field(default=60.0, gt=0, le=24 * 3600)
    """How long a request holds its key before a retry may take it over.

    It must be above ``REQUEST_TIMEOUT`` (checked at startup), and above the
    ``RequestTimeout`` of keyed routes that set their own (``check``).
    """
    REQUIRE_DEADLINE: bool = True
    """Staging and production refuse to start while keyed requests have no
    deadline (no ``REQUEST_TIMEOUT``, or no ``DeadlineMiddleware``), since
    0.10; False only warns, as 0.9 did."""
    METHODS: tuple[str, ...] = ("POST", "PATCH")
    MAX_RESPONSE_BYTES: int = Field(default=1_048_576, ge=1)
    """Larger responses are not stored: a retry runs again if nothing committed."""
    SPOOL_BYTES: int = Field(default=1_048_576, ge=0, le=1_073_741_824)
    """Bytes of a keyed request's body, and of a response being kept, held in
    memory; past them, the rest goes to a temporary file, unlinked from the
    start and closed when the request ends (PERF-11). The default is that of
    ``MAX_REQUEST_BODY_SIZE`` and ``MAX_RESPONSE_BYTES``: with both left
    alone, nothing reaches the disk."""
    CREDENTIAL_HEADERS: tuple[str, ...] = (
        "authorization",
        "proxy-authorization",
        "cookie",
        "x-api-key",
    )
    """Headers that name a caller without an actor: their keys are apart.

    Not once an authentication middleware found no one in the request (its
    principal in ``scope["user"]`` is not authenticated): the header then
    authenticated nobody, as ``Authorization: Bearer null`` (SEC-11a-1).
    """
    ANONYMOUS: Literal["per-address", "refuse"] = REFUSE
    """Keys sent with no actor and none of ``CREDENTIAL_HEADERS``.

    ``refuse`` (the default since 0.11) answers them 400
    ``idempotency_key_needs_caller``, and the request does not run.
    ``per-address`` (0.10's default) scopes them to the client's address, so
    callers behind one address (a NAT, a proxy) share them: one can be
    replayed another's response (DATA-09).
    For ``refuse``, a ``Cookie`` names a caller only if it carries one of
    ``SESSION_COOKIES``: a consent or locale cookie, which many callers
    send alike, does not (SEC-10b-7).
    """
    SESSION_COOKIES: tuple[CookieName, ...] = Field(default=(), max_length=16)
    """Cookies that name a caller the actor does not (a session another
    layer reads), for ``ANONYMOUS = "refuse"``. Empty: no cookie does;
    auth's own sessions set the actor."""

    @model_validator(mode="after")
    def check_lease(self) -> Self:
        """Keep a key until its lease runs out.

        A key that expired while its request still held it could be claimed
        again: a retry would run the request a second time.
        """
        if self.TTL_SECONDS < self.LEASE_SECONDS:
            raise ValueError(
                "TTL_SECONDS must be at least LEASE_SECONDS: a key that expired "
                "while its request still held it could be claimed again, and "
                "the request run twice"
            )
        return self
