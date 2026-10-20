# 0033. Keep feature flags and dynamic settings in the database, cached whole

- Status: Accepted
- Date: 2026-10-19

## Context

Turning a feature on for some users, or changing a limit, should not need a
deployment, and who changed it should be on record. Hosted services
(LaunchDarkly, Unleash) do this well but add a dependency, a network call
or a sidecar per decision, and a second place where personal data
(user ids) goes. Settings modules need a deployment. Django projects
mostly use django-waffle and django-constance: rows in the database, read
through a cache.

Checking a flag must be cheap, since it happens in requests, possibly
several times. A rollout must keep each user in or out as long as the
percentage stays, in every process, and keep them in as it grows.

## Decision

- `domainkit.contrib.flags` stores flags (`domainkit_flags`) and the values
  of dynamic settings (`domainkit_dynamic_settings`) in its routing
  target, which must be one database. A flag is on for everyone, for no
  one, or for listed users, members of listed groups, and a percentage of
  the rest, by SHA-256 of the flag's name and the user's id into 100
  buckets. Anonymous callers get a flag only when it is on for everyone.
- Dynamic settings are declared in code with a type and a default, and
  validated by pydantic on every write and read; a stored value the type
  refuses reads as the default, with a warning.
- Every flag and stored value is read in one query and cached as one JSON
  entry in `FLAGS.CACHE` for `FLAGS.CACHE_TTL` (30 seconds). Flags and
  settings are aggregates changed through `FlagsUnitOfWork`, so each
  change is a domain event: the audit trail records it with its actor, and
  an in-process handler drops the cached entry once it commits.
- `RequireFlag` answers 404 while a flag is off for the caller, so a hidden
  feature looks absent; it is not an access rule, and does not satisfy
  deny by default.

## Consequences

A flag check costs one cache read, and nothing else is needed to run them.
With a Redis cache every process sees a change at once; with memory caches,
other processes see it within the TTL, which is the price of not
broadcasting changes. The one entry grows with the number of flags and
settings, which stays small in practice. Values of dynamic settings are
audited in clear, so they must never hold secrets. Rollouts are by user:
without a user there is no stable bucket, so anonymous rollouts would need
a key of their own, which this does not offer.
