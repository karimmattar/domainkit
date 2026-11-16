"""A fake of Have I Been Pwned's range API, for tests of breached passwords."""

import hashlib
from collections.abc import Callable

import httpx2

LEAKED = "Tr0ub4dor&3-leaked"
"""A password the fake knows from a breach."""
BREACHED_ON = {"BREACHED_PASSWORDS": {"ENABLED": True}}
"""``AUTH`` keys that turn the breached-password check on."""
PADDING = 3
"""Rows of zero count the fake adds to every answer, as ``Add-Padding`` asks."""


def sha1(password: str) -> str:
    """The password's SHA-1, in upper-case hex, as the API writes it."""
    return hashlib.sha1(password.encode(), usedforsecurity=False).hexdigest().upper()


def pwned(
    *breached: str, count: int = 7, seen: list[httpx2.Request] | None = None
) -> Callable[[httpx2.Request], httpx2.Response]:
    """A range API that knows ``breached``, each seen ``count`` times.

    It answers the suffixes under the prefix asked for, and pads with rows
    that count 0; each request it gets is added to ``seen``.
    """
    known = {sha1(password): count for password in breached}

    def answer(request: httpx2.Request) -> httpx2.Response:
        if seen is not None:
            seen.append(request)
        prefix = request.url.path.rsplit("/", 1)[-1]
        rows = [
            f"{digest[5:]}:{n}" for digest, n in known.items() if digest[:5] == prefix
        ]
        rows += [f"{index:035X}:0" for index in range(PADDING)]
        return httpx2.Response(200, text="\r\n".join(rows))

    return answer
