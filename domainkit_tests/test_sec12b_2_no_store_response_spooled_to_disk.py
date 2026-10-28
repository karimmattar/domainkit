"""SEC-12b-2: a ``Cache-Control: no-store`` response is written to the disk.

The middleware never keeps a response marked ``no-store`` (new tokens, an
export of someone's data): ``_no_store`` is read from its start. But every
part of a keyed response goes into the response spool before that is
asked, so past ``SPOOL_BYTES`` it lands in a temporary file. ADR 0105
spools "a response being kept"; this one never is, and ``no-store`` asks
that it not reach non-volatile storage. A project that raised
``MAX_RESPONSE_BYTES`` to keep keyed exports (the ADR's case), and left
``SPOOL_BYTES`` at its 1 MiB, writes each such response to ``TMPDIR``.
"""

from typing import IO, Any

from starlette.types import Receive, Scope, Send

from domainkit_tests.test_idempotency_spool import (  # noqa: F401 - fixtures.
    MIB,
    PART,
    files,
    keyed,
    spooling,
)
from domainkit_tests.test_ratelimit_and_idempotency import (  # noqa: F401 - fixture.
    idempotent,
)


def secret(size: int) -> Any:  # noqa: ANN401 - an ASGI app.
    """An app that reads its body, then streams ``size`` bytes marked no-store."""

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        while (await receive()).get("more_body"):
            pass
        headers = [(b"cache-control", b"no-store"), (b"content-type", b"text/csv")]
        await send({"type": "http.response.start", "status": 200, "headers": headers})
        for _ in range(size // PART):
            part = b"ann@example.com,4111111111111111\n".ljust(PART, b"-")
            await send({"type": "http.response.body", "body": part, "more_body": True})
        await send({"type": "http.response.body", "body": b""})

    return app


def test_a_no_store_response_never_reaches_a_temporary_file(
    spooling: list[IO[bytes]],
) -> None:
    """4 MiB marked no-store, never kept: nothing of it is written to the disk."""
    status, _, _ = keyed(secret(4 * MIB), PART)

    assert status == 200
    assert spooling == [], "a response marked no-store was written to a temporary file"
