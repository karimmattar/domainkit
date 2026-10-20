"""Entity tags and conditional requests: ``ETag``, ``If-None-Match``, ``If-Match``.

A response for one aggregate carries a weak tag of its version and of the
representation sent: ``ETag: W/"3-5d41402abc4b2a76"``. The version lets a
write say which version it changes (``If-Match``); the digest tells two
representations of one version apart, since what a caller sees depends on
who asks (field policies) and in which language.

- ``If-None-Match`` on a read answers ``304 Not Modified`` while the tag
  still matches, compared weakly, as ``@cache_page`` compares its tags.
- ``If-Match`` on a write names the versions it may change; a write on
  another version is ``412 precondition_failed``. Tags are compared by
  the version they carry, so ``"3"``, built from a body's ``version``,
  works as well as the ``ETag`` itself. ``*`` matches any version.
- A resource may require ``If-Match`` on its writes: without one they
  are ``428 precondition_required``.
"""

import hashlib
import re
from typing import Final

from fastapi import status

from domainkit.http.errors import APIError
from domainkit.utils.headers import EntityTag, parse_tags

_VERSION: Final = re.compile(r"([0-9]{1,18})(?:-[0-9a-f]{1,64})?")
_DIGEST_LENGTH: Final = 16


class PreconditionRequired(APIError):
    """The resource changes only with ``If-Match``, and the request sent none."""

    def __init__(self) -> None:
        """Answer with 428 and the ``precondition_required`` code."""
        super().__init__(
            status.HTTP_428_PRECONDITION_REQUIRED,
            "Send If-Match with the ETag of the version you are changing.",
            code="precondition_required",
        )


def entity_tag(version: int, representation: bytes) -> str:
    """The weak tag of one aggregate at ``version``, sent as ``representation``."""
    digest = hashlib.sha256(representation).hexdigest()[:_DIGEST_LENGTH]
    return str(EntityTag(f"{version}-{digest}", weak=True))


def expected_versions(header: str) -> frozenset[int] | None:
    """The versions an ``If-Match`` value accepts; None for ``*`` (any).

    A tag that carries no version (one this API did not make) accepts
    none, so the write is refused (412), never applied unchecked.
    """
    tags = parse_tags(header)
    if tags == []:
        return None
    versions = set()
    for tag in tags or ():
        match = _VERSION.fullmatch(tag.value)
        if match is not None:
            versions.add(int(match[1]))
    return frozenset(versions)
