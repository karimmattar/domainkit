# 0003. Layer rules enforced by `check`

- Status: Accepted; amended by [0037](0037-one-clock-frozen-per-context.md), [0166](0166-let-a-domain-layer-mark-its-texts-extract-the-projects-own-and-add-a-translation-alone.md), [0167](0167-check-an-upload-whole-and-let-a-resource-take-one.md), [0182](0182-let-a-gateway-raise-a-routes-problems-and-name-users-by-the-fields-a-project-chooses.md), [0188](0188-write-down-the-cores-layering-and-hold-the-boundaries-it-draws.md), [0195](0195-move-the-outgoing-client-below-the-adapters-and-hold-module-state-to-its-resets.md), [0199](0199-let-a-projects-module-be-a-built-in-layer.md), and 2026-10-25, 2026-11-15, 2026-11-16, 2026-11-17 and 2026-11-18 (at the end)
- Date: 2026-10-14

## Context

Domain-driven layering (domain, application, infrastructure, api) erodes
quietly: one convenient import of SQLAlchemy into the model, or of another
domain's repository, and the boundaries are gone. Code review alone does not
catch it reliably.

## Decision

`domainkit.architecture` encodes the rules:

- Dependencies point inward: `api → infrastructure → application → domain`.
- The domain layer imports only the standard library and `domainkit.ddd`.
- Another domain is reachable only through its `application` layer or its
  `domain.events`.

`manage.py check` and the test suite parse every module's imports and fail on
a violation.

## Consequences

Violations fail CI, not review. Some code needs an extra port or event where
a direct import would have been shorter; that cost is intended. Framework
packages are exempt from the cross-domain rule.

## Amendment (2026-10-25, 0.9.2)

- The domain layer may also import `domainkit.clock`, as the code has
  allowed since the clock was added ([ADR 0037](0037-one-clock-frozen-per-context.md)):
  reading the time through it keeps a domain free of I/O and testable.
- The rules are the defaults of `domainkit.architecture.LayerRules`, and a
  project extends them with the `ARCHITECTURE` setting: more packages its
  domain layers may import (a shared kernel), more libraries or layers a
  layer must not use, layers of its own (`ports`), and more of a domain
  other domains may import. A setting adds and never removes, so every rule
  above still holds in every project; each entry is validated and bounded,
  and an unknown layer or a rule on the domain layer's allowlist is refused
  when the settings load. Before, a project could change the rules only by
  patching the module before `check` ran.

## Amendment (2026-11-15, 2.4)

- The infrastructure layer may import `domainkit.http.client`, the
  outgoing HTTP client ([ADR 0050](0050-outgoing-http-client.md)).
  "No web libraries" keeps the serving side out of adapters (FastAPI,
  Starlette, and `domainkit.http`'s routing and errors). The rule forbids
  `domainkit.http` by prefix, so it also forbade the client that ADR 0050
  put in that package, though calling another service is what an adapter
  is for. A project had added a layer of its own only to hold its HTTP
  calls. Every other `domainkit.http` module stays forbidden in
  infrastructure, and the whole package in the application layer.
- The exception is `domainkit.architecture.ALLOWED_MODULES`. It lifts
  only a forbidden entry less specific than itself, so a project that
  names `domainkit.http.client` in `FORBIDDEN_LIBRARIES` still forbids
  it. `ARCHITECTURE` adds no exceptions: a setting adds rules only.

## Amendment (2026-11-16, 2.5)

- The infrastructure layer may import `domainkit.http.uploads` too
  ([0167](0167-check-an-upload-whole-and-let-a-resource-take-one.md)): a file gateway checks and stores the bytes its route hands
  it, and `save_upload` takes bytes or a stream since 2.5, so the
  gateway needs no web type. `domainkit.http.errors` and Starlette stay
  forbidden there: a gateway raises domain errors, which the HTTP layer
  maps, and the route turns the request into bytes. A project had added
  a layer of its own only to hold its file gateways.

## Amendment (2026-11-17, 2.8)

- The infrastructure layer may import `domainkit.http.problems` too
  ([0182](0182-let-a-gateway-raise-a-routes-problems-and-name-users-by-the-fields-a-project-chooses.md)): `APIError` and `FieldError`, moved there from
  `domainkit.http.errors`, which re-exports them. A file gateway refuses
  a file with the status, code, and field error a route would. Only the
  names its `__all__` lists may be imported from it, not what it imports
  itself (Starlette's `HTTPException`). The rest of
  `domainkit.http.errors`, which reads requests and builds responses,
  stays forbidden there, and a business refusal stays a domain error.

## Amendment (2026-11-18, 2.9)

- `check` warns of a name the infrastructure layer takes from
  `domainkit.http.client` or `domainkit.http.uploads` that their
  `__all__` does not list (Starlette's `UploadFile`, `StreamingResponse`),
  and of `uploads.serve`, which answers a request
  ([0188](0188-write-down-the-cores-layering-and-hold-the-boundaries-it-draws.md)): `architecture.W001`, which `check --deploy` fails on. 3.0
  refuses them, as `domainkit.http.problems` is refused today. Every
  other name of the two modules' `__all__` stays allowed.

- The outgoing client moved to `domainkit.outbound` ([0195](0195-move-the-outgoing-client-below-the-adapters-and-hold-module-state-to-its-resets.md)), which no
  rule names in the infrastructure layer: an adapter imports it with no
  exception. The application layer may not import it, as it could not
  import `domainkit.http.client`. The exception for
  `domainkit.http.client`, the client's 2.8 path, stays.
