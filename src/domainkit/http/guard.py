"""Which URLs a server may call: ``domainkit.outbound.guard``, under its 2.8 path.

The guard moved to ``domainkit.outbound`` in 2.9, with the outgoing HTTP
client it serves (ADR 0195). This module is a view of that one: each name
is read from it, the same object, with one resolver and its threads. A
name set or deleted here (a 2.8 test's ``monkeypatch.setattr``) is set or
deleted there, where the guard reads it. Like the module it names, this
one is internal (``docs/versioning.md``); ``domainkit.outbound.guard``
publishes the checks since 2.9.
"""

from typing import TYPE_CHECKING, Any

from domainkit.http._moved import keep_path
from domainkit.outbound import guard as _new

if TYPE_CHECKING:
    from domainkit.outbound.guard import FAILED_LOOKUP_SECONDS as FAILED_LOOKUP_SECONDS
    from domainkit.outbound.guard import FAILED_LOOKUPS as FAILED_LOOKUPS
    from domainkit.outbound.guard import HUNG_LOOKUP_BACKOFF as HUNG_LOOKUP_BACKOFF
    from domainkit.outbound.guard import HUNG_LOOKUP_SECONDS as HUNG_LOOKUP_SECONDS
    from domainkit.outbound.guard import LOOKUP_THREADS as LOOKUP_THREADS
    from domainkit.outbound.guard import LOOKUPS as LOOKUPS
    from domainkit.outbound.guard import LOOKUPS_PER_HOST as LOOKUPS_PER_HOST
    from domainkit.outbound.guard import MAX_URL_LENGTH as MAX_URL_LENGTH
    from domainkit.outbound.guard import METADATA_ADDRESSES as METADATA_ADDRESSES
    from domainkit.outbound.guard import METADATA_HOSTS as METADATA_HOSTS
    from domainkit.outbound.guard import NAT64 as NAT64
    from domainkit.outbound.guard import OS_TRUST_STORES as OS_TRUST_STORES
    from domainkit.outbound.guard import OWNER_SHARE as OWNER_SHARE
    from domainkit.outbound.guard import SLOW_ANSWERS as SLOW_ANSWERS
    from domainkit.outbound.guard import SUBJECT as SUBJECT
    from domainkit.outbound.guard import SYSTEM_BUNDLES as SYSTEM_BUNDLES
    from domainkit.outbound.guard import Address as Address
    from domainkit.outbound.guard import GuardResolver as GuardResolver
    from domainkit.outbound.guard import Network as Network
    from domainkit.outbound.guard import Resolver as Resolver
    from domainkit.outbound.guard import UnsafeURL as UnsafeURL
    from domainkit.outbound.guard import checked_address as checked_address
    from domainkit.outbound.guard import checked_host as checked_host
    from domainkit.outbound.guard import checked_url as checked_url
    from domainkit.outbound.guard import judged as judged
    from domainkit.outbound.guard import lookup as lookup
    from domainkit.outbound.guard import lookup_owner as lookup_owner
    from domainkit.outbound.guard import lookups_for as lookups_for
    from domainkit.outbound.guard import refusal as refusal
    from domainkit.outbound.guard import resolver as resolver
    from domainkit.outbound.guard import system_trust as system_trust
    from domainkit.outbound.guard import tls_context as tls_context


def __getattr__(name: str) -> Any:  # noqa: ANN401 - whichever name 2.8 had.
    """A name of ``domainkit.outbound.guard``, the same object."""
    return getattr(_new, name)


keep_path(__name__, _new)
