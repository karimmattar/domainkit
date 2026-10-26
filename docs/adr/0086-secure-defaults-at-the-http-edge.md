# 0086. Make the HTTP edge's defaults the safe ones

- Status: Accepted; amended 2026-10-26 (at the end)
- Date: 2026-10-26

## Context

The 0.9.0 audit left five design gaps at the HTTP edge for 0.10:

- Every response went out without a Content-Security-Policy, and `/docs`
  and `/redoc` ran Swagger UI and ReDoc from jsdelivr, with Google Fonts
  and Redocly's logo, in the API's origin. A CDN compromise would run in
  the origin that holds the callers' cookies and tokens (WEB-08). And
  `API_DOCS` was on unless a deployment turned it off.
- The access log, error reports, and the request context recorded the path
  as sent: ids, tokens, and emails in paths reached every log line
  (WEB-10).
- One body limit held for every route, so an upload route raised it for
  the login route too, and the whole upload was read before its route's
  `max_size` refused it (WEB-05).
- `crud=True` wrote every plain field unless told otherwise (WEB-09).

The options, for the documentation pages:

- Keep the CDN and add Subresource Integrity hashes. The pages would still
  depend on jsdelivr's availability and Google's fonts, and FastAPI's page
  starts Swagger UI from an inline script, which a strict policy refuses.
- Ship the assets in the wheel, as Django ships its admin's. 2.9 MB of
  files (the wheel grows from 1.01 MB to 1.82 MB), an upgrade by hand, and
  pages of our own instead of FastAPI's.
- A `domainkit[docs]` extra with the assets. One more extra to install for
  a page every project has.

Measured in headless Chromium, against a small application: with
the assets local and no inline script, Swagger UI renders and runs "Try it
out" under `default-src 'none'; script-src 'self'; style-src 'self';
img-src 'self' data:; connect-src 'self'` with no violation. ReDoc does
not render without `style-src 'unsafe-inline'`, and its search returns
nothing without `worker-src blob:`. Neither needs `unsafe-eval`. Under the
API's own policy (`default-src 'none'`) neither page renders.

For the paths in logs: the route's template is known only once the router
has matched, after the request context is bound, so the context reads it
from the scope when asked.

## Decision

- `SecurityHeadersMiddleware` adds `Content-Security-Policy: default-src
  'none'; frame-ancestors 'none'`, a `Permissions-Policy` that turns
  powerful browser features off, and `Cross-Origin-Opener-Policy:
  same-origin` to every response that sets none, from settings that may
  change or drop each.
- `/docs` and `/redoc` are domainkit's own pages over copies of Swagger UI
  and ReDoc in the package, unchanged from their npm releases and pinned
  by hash in the tests, each with a policy of its own: the least each page
  was measured to need. Swagger UI starts from a file that reads the
  document's URL from the page.
- `API_DOCS` defaults to `None`, which follows `DEBUG`: on in development,
  off in a deployment unless set.
- Logs, error reports, and the request context show the route's template,
  or `unmatched`; `ACCESS_LOG_PATH = "raw"` shows the path as sent. Answers
  to the caller (problem details' `instance`) keep the path as sent.
- A route's own body limit marks its endpoint (`body_limit`), so it works
  for any route class and survives flattening (ADR 0060);
  `RequestBodyLimitMiddleware` reads it from the matched route as the body
  arrives.
- `crud=True` takes the fields `writable=` names; without them it warns in
  0.10 and is refused in 0.11.

## Consequences

- A deployment serves no documentation and no answer a browser could run
  script in, unless the project says so. A project whose route answers
  HTML sets that route's policy, or `SECURE_CONTENT_SECURITY_POLICY`.
- Upgrading Swagger UI or ReDoc is a change to domainkit: replace the files
  with a newer release's, and the versions and hashes the tests pin. A
  FastAPI option of its documentation pages (`swagger_ui_parameters`, the
  OAuth2 redirect page) is not offered; a project that needs one serves
  its own page with `API_DOCS = False`.
- Log lines name routes, not resources: an investigation that needs the
  id finds the request by its request id, in the audit trail or the
  application's own records. Requests refused before routing (a 429, a
  body refused by its length) show `unmatched`, and their status says why.
- What would change this: a FastAPI release whose documentation pages
  start without inline script and load from the origin, or a browser
  feature that makes CDN integrity enough.

## Amendment (2026-10-26, 0.11): writable fields refused, anonymous keys refused

- `crud=True` without `writable=`, or without both `create=` and
  `update=`, raises `TypeError` when the router is built, as planned
  (WEB-09). It is a construction error, like a `writable=` that names a
  field no write sets: the router never exists, so no route can take a
  field nobody named. The message names the three ways out:
  `writable=[...]`, `writable=ALL_FIELDS` (0.10's implicit behaviour, now
  said), or both schemas.
- `IDEMPOTENCY.ANONYMOUS` defaults to `"refuse"` (DATA-09): a keyed
  request that names no caller (no actor, none of `CREDENTIAL_HEADERS`)
  gets 400 `idempotency_key_needs_caller` before its body is read, and
  does not run. `"per-address"` stays, as 0.10's default kept by choice.
  `idempotency.W002`, which warned deployments that left the setting
  unset, is removed rather than turned on `"per-address"`: 0.10 told
  projects to set `"per-address"` to keep its behaviour, and a warning on
  that value would fail their `check --deploy` for having done so. The
  other opt-outs that keep an earlier default (`REQUIRE_ROUTE_RULES =
  False`, `SCOPED_CREDENTIALS = "warn"`) do not warn either. The flip
  failed 59 of the suite's tests and errored 10 through one fixture, each
  on a keyed request that named no caller (0.10 had counted 63 and 8):
  those about per-address keys now set `"per-address"`, and the others,
  about what keys do, name a caller with a credential header, as a
  project's own authentication would.
