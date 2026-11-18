# 0192. Start a contrib router's route names with its app

- Status: Accepted
- Date: 2026-11-18
- Amends: [0031](0031-operation-ids-from-route-names.md)

## Context

With `OPENAPI_OPERATION_IDS = "route_name"`, the default since 1.0, an
operation id is its route's name, and two routes of one name fail
`check` (`openapi.E001`, ADR 0031); staging and production refuse to
start. The contrib routers named most of their routes as a project
would: a `ResourceRouter` names its own `list_<plural>` and
`read_<basename>`, and the plain routes took their functions' names. A
project that mounted the ledger's router beside a resource of its own
named `accounts` failed `check` on `list_accounts` and `read_account`.
It was found writing the tutorial's accounts (A28-DX-06). The same held
for approvals' requests (`approve_request`), limits (`list_limits`), the
webhooks admin's subscriptions (`list_subscriptions`), the flags admin's
settings (`list_settings`), the auth admin's users and groups
(`list_users`, `read_user`, `create_user`), and the audit trail's
history (`read_history`).

A name is also what `url_path_for` finds, and a resource builds its
`Location` and its links with it. Two routes of one name resolve to the
first mounted, so with `"fastapi"` ids, which hold the path and never
clash, a created account's `Location` could still name the project's
route instead of the ledger's.

A few contrib routes were already named after their app:
`accounting_trial_balance`, `approvals_submit`, `limits_usage`.

Others do it this way. The OpenAPI Specification requires an operation
id to be unique among all operations of the API [1]. Django gives an
included app's URL names a namespace (`app_name`) so that "different
applications [can] use the same URL names", calls namespaced URLs good
practice for third-party apps, and suggests a name "derived from the
application name" to avoid clashes [2].

The options weighed:

- **Leave the names**, and let a project rename its own routes. Every
  project with users, accounts, or subscriptions of its own would, and
  it could not rename a framework route.
- **An id function per contrib router** (`generate_unique_id_function`,
  as `versioned_router` has). It changes the ids only: the names still
  clash for `url_path_for`, and the function runs when the router is
  built, at import, before the settings may be loaded, so it could not
  follow `OPENAPI_OPERATION_IDS`.
- **An `operation_id=` on each route.** It ignores the setting too, and
  every new route must remember it.
- **Behind a setting, or with a deprecation.** A route's name is fixed
  when its module is imported, by its decorator or its resource, before
  the settings may be loaded, so a setting cannot choose it. A Starlette
  route has one name: keeping the 2.8 name as an alias means a second
  route of that name, which brings the clash back, for `check` and for
  `url_path_for` alike, and a deprecation needs the old name to keep
  resolving. A mode that renamed the ids alone would keep the clash in
  `url_path_for`, and so the wrong `Location` and links. 2.1 renamed the
  identity providers' `start_login` to `start_provider_login` the same
  way, with no setting.
- **Each route's name starts with its app** (chosen): `name_prefix=` on
  a contrib `ResourceRouter`, `name=` on a plain route. The ids follow
  the setting in both of its values, and `url_path_for` finds each
  app's own routes.

## Decision

- Every route of a contrib router that serves a project's records has a
  name that starts with its app's label: `accounting_`, `approvals_`,
  `limits_`, `webhooks_` (the admin's and the owner's), `flags_`. The
  auth admin's routes start with `auth_admin_`: auth serves several
  routers, and these act on other users. The audit trail's history, a
  staff route over a project's records as the other staff routers are,
  starts with `audit_`.
- The routes already named so keep their names.
- A test lists these routers and fails on a route whose name does not
  start with its app's, so a new route is named as its neighbours.

## Consequences

- A project's resource named as a contrib app's clashes with none of
  its routes, and its links and `Location` name its own routes.
- The operation ids of these routes change in 2.9, in both modes: a
  client generated from the OpenAPI document calls them by their new
  names once generated again, and code that calls `url_path_for` with
  an old name names the new one. The paths, the methods, and the
  answers are unchanged, and `openapi --diff` reports nothing, as ADR
  0031 says of ids. Each new name is the 2.8 name with its app's prefix,
  as [upgrading.md](../upgrading.md#28-to-29) says. FastAPI writes an
  operation's summary, and the title of a response schema declared
  inline, from the name, so those change with it.
- The routers a user reaches their own account through keep their
  names, and are left for a later release: auth's (`login`, `me`),
  devices', phones', ciba's, and identity's, whose names are as generic
  as `callback`, `redeem`, `remove`, `linked`, and `providers`. Renaming
  them renames the sign-in methods of every client, which this finding
  did not need. A project's route named as one of them still clashes,
  and `check` says so.

## References

1. OpenAPI Specification 3.1.0, *Operation Object*, `operationId`:
   <https://spec.openapis.org/oas/v3.1.0.html#operation-object>
2. Django documentation, *URL dispatcher*, "URL namespaces" and "Naming
   URL patterns": <https://docs.djangoproject.com/en/5.2/topics/http/urls/>
