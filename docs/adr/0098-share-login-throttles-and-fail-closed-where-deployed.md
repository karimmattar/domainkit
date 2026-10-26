# 0098. Share the attempt throttles, and fail closed, where deployed

- Status: Accepted
- Date: 2026-10-26
- Amends: [0013](0013-shared-rate-limits-in-redis.md), [0068](0068-count-failed-attempts-and-hold-back-only-guesses.md)

## Context

Wrong passwords, one-time codes, and requests that send mail are counted
before they are checked, in `RATE_LIMIT_BACKEND`
([0068](0068-count-failed-attempts-and-hold-back-only-guesses.md)). The
default backend, `MemoryBackend`, counts in each process; `RedisBackend`
counts once for every process, and lets requests through, uncounted,
while Redis is down ([0013](0013-shared-rate-limits-in-redis.md)). 0.9.1
made `check` warn about the first in staging and production (`auth.W004`)
and let a project refuse the second (`AUTH.THROTTLE_FAIL_CLOSED`), off by
default (AUTH-09 of the 0.9.0 audit). Both still held by default:

- Two applications with the default backend, `LOGIN_RATE = "2/minute"`,
  wrong passwords for one account from one address: each answered `401,
  401, 429`, so four passwords were checked where the rate says two. A
  deployment of N processes checks N times the rate. With `RedisBackend`,
  the same requests answered `401, 401` and `429, 429`.
- With `RedisBackend` and Redis down, in production settings: four
  attempts at a rate of five a minute, all let through and checked. A
  brute force timed with an outage, or one that causes it, is not
  counted at all.

A warning does not refuse: `check` passes, the service starts, and a
project can silence it. 0.10 settled how a missing safeguard becomes a
refusal without breaking development (`audit.E001`, `idempotency.E002`):
an error where deployed, which `SILENCED_CHECKS` cannot name, with a
setting that turns it back into the warning.

Options considered:

- **Require a shared backend everywhere.** Development and tests would
  need Redis, for one process.
- **A backend for attempts of its own** (`AUTH.THROTTLE_BACKEND`), so the
  request limit could stay in each process. Another setting and another
  client of the same Redis; `RATE_LIMIT_BACKEND` already names the shared
  store, and `auth.W004` has told projects to set it since 0.9.1.
- **Require it where deployed, as an error, with an explicit way back.**
  Chosen. Any backend but a `MemoryBackend` counts: a project's own
  backend is taken to be shared.
- **Fail closed where deployed.** The local fallback of 0.10 (WEB-14)
  counts each caller in one process at its share of the rate, and marks
  its decisions not counted, so failing closed refuses them too.

## Decision

- In staging and production, with an attempt rate set (`LOGIN_RATE`,
  `EMAIL_RATE`, `MFA.RATE`, `MFA.ADDRESS_RATE`), a `MemoryBackend`
  (or subclass) as `RATE_LIMIT_BACKEND` is the error `auth.E105`: the
  application refuses to start and `check` fails; workers and commands
  start. `AUTH.REQUIRE_SHARED_THROTTLES = False` makes it 0.10's warning,
  `auth.W004`, for a deployment of one process.
- `AUTH.THROTTLE_FAIL_CLOSED` defaults to None, which follows
  `ENVIRONMENT`: True in staging and production, False elsewhere. True or
  False set explicitly decides anywhere.
- Development and tests keep counting in memory, without Redis.
- The `init` template's production settings count in Redis
  (`RedisBackend` on a `shared` cache from `<PREFIX>REDIS_URL`, required),
  and its projects depend on the `redis` extra.

## Consequences

- Where deployed, a guesser gets each rate once, however many processes
  serve it, and nothing while Redis cannot count.
- While Redis is down, every login, second-factor code, password change,
  and request that sends mail is refused with `503 throttle_unavailable`
  and `Retry-After`; other requests are served (the request limit still
  fails open, as 0013 decided). Redis becomes a dependency of logging in,
  as the database is. `docs/runbook.md` says what an operator sees and
  does.
- With `RATE_LIMIT_FALLBACK_REPLICAS`, the refused attempts still use the
  fallback's share of the rate: once it is spent, the fallback answers
  `429` itself until it refills. Refused either way; nothing is checked.
- A deployment of one process sets `AUTH.REQUIRE_SHARED_THROTTLES =
  False` and silences `auth.W004`; `MemoryBackend` always counts, so
  failing closed changes nothing for it.
- Projects upgrading from 0.10 with the default backend fail `check` in
  staging and production until they set `RATE_LIMIT_BACKEND` or the
  setting above: a breaking change, listed in `docs/upgrading.md`.
