# 0031. Operation ids from route names, for new projects

- Status: Accepted
- Date: 2026-10-19

## Context

Client generators (openapi-generator, orval, openapi-ts) name each method
after its OpenAPI operation id. FastAPI makes the id from the route's
name, its full path, and its first method:
`list_products_api_v1_catalog_products_get`. The id changes whenever a
router is mounted under another prefix, a path is reworded, or a method is
added, so every client must be regenerated and its callers renamed, though
the operation itself did not change. The ids are also long and repeat what
the path already says.

The options were to keep FastAPI's ids; to derive ids from route names
everywhere, which renames every method of every existing project's clients
on upgrade; to require explicit `operation_id=` on each route, which the
generated domains would have to repeat and nobody would keep unique by
hand; or to offer route names as a setting, the default for new projects
only.

## Decision

- `OPENAPI_OPERATION_IDS` chooses: `"fastapi"` keeps FastAPI's ids and is
  the default, so upgrading changes no existing project's schema;
  `"route_name"` makes the id the route's name, its function's name unless
  `name=` says otherwise. An explicit `operation_id=` wins either way.
- `init` writes `OPENAPI_OPERATION_IDS = "route_name"` in the base settings
  of new projects.
- Ids must be unique. Clashes between routes are application problems:
  `check` lists them, and staging and production refuse to start, since a
  schema with duplicate ids breaks generators. A route that answers several
  methods keeps FastAPI's single id for them, as FastAPI does (it warns).
- Versioned routers (`versioned_router("v1")`) prefix their routes' names
  with the version (`v1_list_products`) in both modes, so versions can
  reuse route names. FastAPI asks for ids as routers are included, which
  may happen before settings are loaded, so these ids cannot read the
  setting.

## Consequences

New projects get short ids that survive moving routers, and generated
clients keep their method names across refactors. Route names now matter:
two domains that both name a route `list` clash, and must rename one.
Existing projects opt in by setting the value, and must regenerate their
clients once when they do. `openapi --diff` compares operations by path
and method, not by id, so changing ids is not reported as breaking: it is
a change to clients' code, not to the HTTP contract.
