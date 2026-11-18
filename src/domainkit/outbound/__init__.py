"""Calls to other services: the outgoing HTTP client, and its guard.

- ``domainkit.outbound.client``: ``HttpClient``, pooled, retried, behind
  a circuit breaker, and bounded by the deadline of the work it serves
  (``domainkit[httpclient]``).
- ``domainkit.outbound.guard``: which URLs a server may call, for a client
  made with ``guard=True`` and for webhooks.

Both sat in ``domainkit.http`` until 2.9, and still import from there,
the same objects (ADR 0195). The package sits with the adapters, which
call through it, below the serving package; importing it imports neither
module, so a process that calls no service needs no ``httpclient`` extra.
"""
