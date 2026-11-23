# Upgrading

How to move a project to a new version, and what each one asks of it.
From 1.0, a minor version adds and fixes, and may deprecate, but breaks
nothing [versioning.md](versioning.md) calls public: a deprecated name
keeps working, with a warning, until the next major version removes it.
Patch releases change nothing a project relies on: take them as they come.
Before 1.0, each minor version could change a default or break a
behaviour; the sections from 0.9 to 1.0 below list each, with the setting
that keeps the old one where there is one.

## How to upgrade

Within a major version, upgrade to the newest minor, from any release of
it. From 1.x to 2.0, reach 1.8 first, with its deprecations as errors
(step 2 below), then follow [1.8 to 2.0](#18-to-20). From 0.12,
follow [0.12 to 1.0](#012-to-10): 0.13, 0.14 and 0.15 were never
published, so 0.12.1 is the release 1.0 follows. From an older release,
reach 0.12.1 first, one minor at a time, through the published releases
0.9.3, 0.10.1, 0.11.0, and 0.12.1 (0.9.2, 0.10.0, 0.11.1 and 0.11.2 were
never published), with each section below.

1. Read the section for the version you move to.
2. On the version you have, run the tests with domainkit's deprecations as
   errors, `pytest -W error::domainkit.deprecation.DomainkitDeprecationWarning`,
   so each deprecated name the project still uses fails, naming its
   replacement. Replace them all: they are removed in the next major
   version (before 1.0, the next minor).
3. Raise the version in the project's `pyproject.toml`, and install.
4. `python manage.py makemigrations`: a contrib app's tables may have
   changed. Review the revision, then `migrate` it. The test databases are
   built from the project's revisions, so the tests need it.
5. Run the tests again, deprecations as errors, for the names this version
   deprecates.
6. `python manage.py check` with the production settings and variables,
   for the deployment checks this version adds.
7. Deploy as usual: [operations.md](operations.md#migrations-in-a-rollout).

## 2.9 to 2.10

2.10 closes a project's notes on 2.9. Each item of this list says what a
project that relied on 2.9's behaviour changes, and what to run.

- **Making staff holds what the user comes back with** (security,
  [ADR 0172](adr/0172-hold-admin-changes-by-what-they-grant-and-check-before-the-hold.md)).
  Where `APPROVALS.ADMIN_APPROVALS` holds `auth.set_staff`, its request
  holds, as `auth.reactivate`'s does, whether the user is a superuser,
  each of their groups, their own permissions, and their grants on
  objects; its approver reads them ("Superuser", "Grants", "Groups",
  "Also holds"). A request stored by 2.9, pending or approved and not
  yet made, holds none of it, so its approval is never spent: the staff
  member's call submits a new request (`403 approval_pending`), which
  is approved again. With `AUTH.AUTHORIZATIONS.HELD_TTL` set, a call
  whose confirmation was kept for such a request is refused (`403
  action_not_authorized`): confirm the change again. Anything granted
  to the user while a request waits makes another request, as for a
  return. Code that reads the request's arguments finds the new keys.
  With `AUTH.AUTHORIZATIONS.WHOLE` and `APPROVE_ACTION` on, a user in
  many groups made staff may no longer fit the approver's screen, and
  is refused at the twin (`422 too_much_to_show`): take some groups
  away first, make them staff, then add the groups back, each step
  held. A guard of your own that refused making staff a user who holds
  a named key (`422`) can go, or read `named_keys_held(user)`.
- **The admin API's act shows what a user comes back with.** With
  `AUTH.ADMIN_ACTION` on, the screen of a `PATCH /users/{id}` that
  makes a user staff, or active again, shows "Superuser" and "Staff"
  where they are so, set by the change or not, and "Grants", the keys
  `NAMED_PERMISSIONS` names that they hold, directly or through a
  group. The act's digest is unchanged; under `WHOLE`, a change that
  filled the screen may no longer fit: make it in smaller steps. A
  client or a test that matched the screen reads the new lines.

## 2.8 to 2.9

2.9 follows up the audit of 2.8. Each item of this list says what a
project that relied on 2.8's behaviour changes, and what to run. With
`domainkit.contrib.approvals` installed, make and apply its index's
migration ([below](#make-and-apply-approvals-index)) before the new code
serves. On PostgreSQL, `migrate` makes indexes on the user table, with
no migration to make ([below](#migrate-makes-the-user-lookups-indexes-on-postgresql)).
One setting is new, `PROBE_PATHS`, which `RATE_LIMIT_EXEMPT_PATHS`,
`CONCURRENCY_EXEMPT_PATHS`, `ACCESS_LOG_QUIET_PATHS`, and
`AUTH.AUTHENTICATION_EXEMPT_PATHS` now add to, each defaulting to `()`
(below). A project on 2.8.0 or 2.8.1 first reads the patch releases'
sections below, [2.8.0 to 2.8.1](#280-to-281) and
[2.8.1 to 2.8.2](#281-to-282), which this list does not repeat.

- **`diffsettings` shows each domain's settings.** `AUTH`, `APPROVALS`,
  `LIMITS`, and every other installed domain's setting print as the
  domain's schema validated them, with its defaults, where 2.8 printed
  `'**********'`. A script that reads the command's output sees these
  lines, and `--all` lists a domain's setting left at its defaults with
  `###`. What stays hidden: values typed `SecretStr` or bytes, names that
  suggest a secret, URL passwords, the values of `password=` and
  `password:` pairs, and all text in a mapping, a value typed `Any`, or a
  value that is not a model, such as a dataclass. A project whose own
  domains declare settings types each secret among them as `SecretStr`,
  as the framework's schemas do: a secret in a field typed `str`, under a
  name such as `ENDPOINT`, would print
  ([diffsettings](reference/commands.md#diffsettings-show-the-settings)).
- **`diffsettings` hides more of the text it prints**, in every setting,
  the project's own too: the value after a name like a secret's and `:`
  or `=`, spaced or not, the name quoted or not. `password = ...` in a
  libpq connection string, `"private_key": "..."` in JSON, and
  `Authorization: Bearer ...` print with `**********` for their values,
  where 2.8 printed them: it hid only `password=...`. A script that reads
  such a value from the command's output reads it from the settings
  instead.
- **The application's problems are logged one to a record.** In
  development and tests, each problem found as the application is built
  or starts (`security.E101`, a route without an access rule, say) is a
  warning of its own: `The application has problems: security.E101: GET
  /reports declares no access rule: ...`. 2.8 logged one record for them
  all. Change a log filter or alert that matched the record of 2.8.
  Staging and production still refuse to start, with the same
  `CheckFailed`.
- **`manage.py help <command>` prints the command's options**, as
  `<command> --help` does, where 2.8 printed the list of commands.
  `manage.py help` alone still prints the list.
- **`createsuperuser`'s note is reworded**: "Note: no verification code
  can be sent to the superuser (to reset its password or enroll a phone)
  until its email address is verified: run ...". A script that matched
  2.8's wording matches this one instead.
- **The worker's heartbeat no longer ages while a dependency is away.**
  `runworker --heartbeat-file` is rewritten while the worker's event
  loop runs, whatever the broker, the databases, or the caches do, so
  the documented health check no longer restarts workers during their
  outage. A worker that relied on such a restart keeps running, and
  logs each failing check. To keep the restart, run the worker with
  `--ready-file /tmp/worker-ready`, which is removed while a check
  fails, and point the health check at it
  ([The worker](operations.md#the-worker)). The ready file goes after
  one failed round of checks, where 2.8's heartbeat had to age past the
  health check's 60 seconds: set the tolerance with the check's retries
  (`HEALTHCHECK --retries`, or a probe's `failureThreshold`).
- **`heartbeat.beat()` always touches its path**, then runs the checks,
  and takes the ready file as `ready`. Code that called it to touch a
  file only while the checks hold passes that file as `ready`, and
  `None` as the path.
- **Webhook deliveries whose task ended are marked failed.** The first
  run of the new hourly task `domainkit.contrib.webhooks.fail_abandoned`
  marks failed every delivery that 2.8 left `pending` with no task to
  run it (the worker killed during the last attempt, the attempt past
  its time limit, or its outcome not written), and logs how many. Send
  them again with `webhooks replay --failed`. Retrying such a delivery's
  dead task with `messages retry` sends nothing now, and logs so:
  replay the delivery.
- **Text messages, search, identity, and captcha open their circuits.**
  After `HTTP_CLIENT.BREAKER_FAILURES` failures in a row (5), a provider
  is not called for `BREAKER_COOLDOWN` seconds (30): text messages fail
  with `... could not be reached (CircuitOpen).` and are retried, a
  search raises `SearchError` at once, a login through an identity
  provider fails at once, and a captcha check answers 503
  `captcha_unavailable` at once, as it did after waiting. Their calls
  and circuits are counted under the client names `notifications` (text
  messages and push), `identity`, `captcha`, and `mail`, where they were
  `other`: change a dashboard or an alert on `client="other"`. The test
  plugin closes every client's circuits before each test; a test suite
  that does not use it calls `domainkit.outbound.client.reset_circuits()`
  between tests, or a circuit one test opens refuses the next one's
  calls.
- **The worker's log lines about a failing message name its id**, after
  its target: `Dead-lettered task shop.flaky (message 0f8e...) after 2
  attempts`. Change a log rule or an alert that matches the whole line.
- **Contrib routes are renamed, and so are their operation ids.** Each
  new name is the 2.8 name with its app's prefix, for every route
  renamed: `accounting_` on the ledger's, `approvals_` on approvals',
  `limits_` on limits', `webhooks_` on the webhooks admin's and owner's,
  `flags_` on the flags admin's, `auth_admin_` on the auth admin's, and
  `audit_` on the audit trail's history (`list_accounts` is
  `accounting_list_accounts`, `read_history` is `audit_read_history`).
  The routes named so already keep their names: `accounting_trial_balance`,
  `accounting_general_ledger`, `accounting_import_statement`,
  `accounting_reconcile`, `accounting_breaks`, `approvals_submit`,
  `approvals_submit_authorization`, and `limits_usage`. The paths, the
  methods, and the answers are unchanged, so a client that calls the
  URLs changes nothing, and `openapi --diff` reports nothing. Code that
  builds a URL with `url_path_for` and a 2.8 name uses the new name, and
  a client generated from the OpenAPI document calls each method by its
  new name once you generate it again. With `OPENAPI_OPERATION_IDS =
  "fastapi"`, FastAPI's ids start with the new name too. FastAPI writes
  an operation's summary, and the title of a response schema declared
  inline, from the name: 50 summaries ("Delete User" is "Auth Admin
  Delete User") and 7 titles ("Response List Flags" is "Response Flags
  List Flags") change with them. A project's resource named `accounts`,
  `users`, or `subscriptions` failed `check` beside the ledger, the auth
  admin, or the webhooks admin (`openapi.E001`), and its `Location` and
  links could name the contrib route; it clashes with none now
  ([Operation ids](reference/responses.md#operation-ids), ADR 0192).
- **A memory ledger keeps what it holds when the ledgers close.** In
  tests, evidence recorded before `outbox.run_queued()`, a command, or a
  client's shutdown is still there after it. A test that relied on the
  close to empty a `memory://` ledger calls `ledgers.reset()`; the test
  plugin calls it before each test, as before.
- **An approval screen shows money as its amount and currency.** A
  guard without `show` lists the command's arguments, and an argument
  that is `Money` now reads `1500.00 USD`, where 2.8 showed
  `{"amount":"1500.00","currency":"USD"}`. A test that compared a
  screen's details compares the new text.
- **A failed approval request keeps a seal that did not hold**
  (security). Failing makes the seal again only if it held, so a request
  changed around the application, then failed, still fails its seal.
  Code that reads `sealed()` on failed requests, to report them, finds
  these.
- **A request that committed is answered as committed**
  ([ADR 0186](adr/0186-answer-a-commit-as-committed-a-lost-database-as-unavailable-start-loudly-and-name-the-probes-once.md)).
  Once a unit of work commits, in the request's task or in one it
  started, the request's deadline is lifted: its in-process handlers all
  run, and it answers what it committed, past `REQUEST_TIMEOUT` if they
  are slow. It no longer answers `503 deadline_exceeded` for a change
  that was made. A request that commits, then calls out or commits again,
  is no longer cut off either: bound such calls by their own timeouts.
  Nothing to change; a handler that must not hold the answer belongs in
  the outbox (`durable=True`).
- **After-commit hooks get `EVENT_HANDLER_TIMEOUT` each.** A domain's
  `on_committed` or `on_transaction_committed` hook was bounded by the
  request's deadline, which the commit now lifts. One still running after
  `EVENT_HANDLER_TIMEOUT` (10 seconds unless set) is cut off, logged, and
  reported as `HandlerTimedOut`. A hook that needs longer belongs in the
  outbox.
- **A worker's task keeps the time it had when it committed**, and what
  follows each commit (handlers and hooks, up to `EVENT_HANDLER_TIMEOUT`
  each) adds to its run: `TASK_TIMEOUT` no longer bounds a run's whole
  time. A commit renews the claim; keep `VISIBILITY_TIMEOUT` above
  `TASK_TIMEOUT`, and above what follows a commit, or another worker may
  claim a run still in its handlers and run it again.
- **A 503 for a commit in doubt has no `Retry-After`.** When the deadline
  passes, or the connection is lost, as a request's `COMMIT` is sent, the
  change may have been made: the request answers `503 deadline_exceeded`
  or `503 database_unavailable` without `Retry-After`, with a detail that
  says to check before sending it again. A client that retries every 503
  should look the change up first, or send an `Idempotency-Key`.
- **`HandlerCancelled` in the error reports.** It names in-process
  handlers that a cancellation of their work (a shutdown, a cancelled
  task) cut off or kept from running. 2.8 lost them without a word.
- **A database out of reach answers `503 database_unavailable`**, with
  `Retry-After: 5`, where 2.8 answered `500 internal_error`. It is logged
  once per reason every 10 seconds, as a warning, and not sent to
  `ERROR_REPORTERS`. Move alerts on 500s or on error reports for a
  database outage to `/ready` and to this code
  ([Runbook](runbook.md#the-database-is-unavailable)). It is told from
  asyncpg's errors and SQLAlchemy's: through psycopg, a connection that
  cannot be opened may still answer 500.
- **A failed upload whose commit is in doubt keeps its file.** The
  warning `Kept the upload <name> on the <storage> storage` names it:
  delete it by hand if no row names it.
- **The probes are named once, in `PROBE_PATHS`.** `RATE_LIMIT_EXEMPT_PATHS`,
  `CONCURRENCY_EXEMPT_PATHS`, `ACCESS_LOG_QUIET_PATHS`, and
  `AUTH.AUTHENTICATION_EXEMPT_PATHS` default to `()`, and add to it. The
  paths a list you set names stay exempt, and every probe is added to it.
  - **A list that left a probe out on purpose no longer does.** With
    `RATE_LIMIT_EXEMPT_PATHS = ("/health", "/ready")`, 2.8 counted
    `/metrics`; 2.9 does not. To keep the old behaviour, take the probe
    out of `PROBE_PATHS`, and add it to each list that should still
    exempt it:

    ```python
    PROBE_PATHS = ("/health", "/ready")  # /metrics is counted again.
    CONCURRENCY_EXEMPT_PATHS = ("/metrics",)
    ACCESS_LOG_QUIET_PATHS = ("/metrics",)
    AUTH = {..., "AUTHENTICATION_EXEMPT_PATHS": ["/metrics"]}  # Its token.
    ```

    `check` warns of such a list (`probes.W001`); with 2.8's default of
    `ACCESS_LOG_QUIET_PATHS` set by hand, it says `/metrics` is now
    logged at DEBUG.
  - With `TENANCY`, `/ready` and `/metrics` need no tenant any more: a
    `TENANCY.EXEMPT_PATHS` that lists them for that can drop them.
  - Successful scrapes of `/metrics` are logged at DEBUG.
  - Code that reads one of the four lists gets `()` by default: read
    `PROBE_PATHS` too.
  - Probes mounted elsewhere: set `PROBE_PATHS` to their paths. A probe
    you want counted, authenticated, or given a tenant: leave it out of
    `PROBE_PATHS`.
- **A lifespan that raises stops the server.** `runserver` and `serve`
  run uvicorn with `lifespan="on"`, and `serve` exits 3. Under 2.8,
  uvicorn logged the error at INFO and served every request a 500. If a
  release no longer starts, fix the error it prints.
- **`check` refuses an optional app's middleware without the app**
  (`apps.E003`), and staging and production refuse to start with one.
  Install the app, or remove the middleware.
- **`check` and `runserver` say which migrations a database lacks**
  (`migrations.W001`), outside staging and production. `check` connects
  to each database there to ask, and waits 2 seconds at most for each to
  answer: a host that never answers adds that to `check`. Run `migrate`,
  or list the id in `SILENCED_CHECKS`.
- **`AUTH.IDENTIFIERS` names fields that the user table has a column
  for.** Startup refuses `groups` and `user_permissions`, which link
  tables keep (`auth.E011`). 2.8 started with them, and failed later, at
  a login, a search, or a list of users that read the field: name the
  fields the table stores instead.
- **A wider fixture's factory objects start at 0.** The test plugin
  resets every factory before a test's fixtures, as well as before the
  test's own: objects a module-scoped or session-scoped fixture builds are
  numbered from 0, whichever test runs first, where 2.8 went on from the
  test before. A test that asserted the numbers such a fixture's objects
  happened to get asserts the ones from 0 (`Product 0`, `Product 1`).
- **`check` warns of web names a gateway takes through an allowed
  module** (`architecture.W001`). The infrastructure layer may import
  `domainkit.http.client` and `domainkit.http.uploads`, but from 2.9
  only the names their `__all__` lists, and not `uploads.serve`, without
  a warning; 3.0 refuses the rest. `check` passes, and `check --deploy`
  fails until each import changes. `SILENCED_CHECKS` can silence the
  warning, but only as a whole: there is no way to allow one import, so
  silencing it also hides the next one. Change the imports instead. Type
  a route's file as `domainkit.http.uploads.Upload`, not Starlette's
  `UploadFile`. Import `StoredUpload` from `domainkit.storage`,
  `UpstreamError` from `domainkit.exceptions`, and `httpx2` itself, not
  through `domainkit.http.client`. Serve a file from its route, with
  `serve` there ([Layer rules](reference/settings.md#layer-rules)).
- **`domainkit.contrib.objectpermissions` needs `domainkit.contrib.auth`
  in `INSTALLED_DOMAINS`.** It always did, at its first grant; now
  `setup()` says so (`apps.E001`).
- **The ledger's triggers are checked as the audit trail's are.** Where
  deployed, a trigger on the entries or postings given arguments fails
  the startup check (`accounting.E001`), as it did on the trail. `migrate`
  makes the triggers again.
- **AWS's signing and credentials are defined in `domainkit.cloud.aws`.**
  `AwsCredentials`, `sign`, and the credential functions still import
  from `domainkit.keys.aws`, the same objects; what prints a class by its
  module prints the new one. A test that patches one of them in
  `domainkit.keys.aws` no longer reaches the code that calls it:
  `default_credentials` calls `environment_credentials` from
  `domainkit.cloud.aws`, so patch it there
  (`monkeypatch.setattr("domainkit.cloud.aws.environment_credentials", ...)`).
- **The outgoing client is defined in `domainkit.outbound.client`, and its
  guard in `domainkit.outbound.guard`.** Every name still imports from
  `domainkit.http.client` and `domainkit.http.guard`, the same objects,
  and neither path warns in 2.9; a later minor release may deprecate the
  old ones. What prints a class by its module prints the new one
  (`domainkit.outbound.client.CircuitOpen`, in a trace's
  `exception.type` or an admins' report). A test that patches a name of
  an old path still reaches the code, which reads it from the new one;
  write new patches against `domainkit.outbound.client` and
  `domainkit.outbound.guard`. The calls still log as
  `domainkit.http.client`. The layer rules let an infrastructure layer
  import `domainkit.outbound` with no exception, and keep it out of the
  application layer, as the old path was; a rule of the project's that
  names `domainkit.http`, `domainkit.http.client`, or
  `domainkit.http.guard` covers the new path too. Webhooks no longer
  imports the guard's lookup: a test that faked DNS by patching `lookup`
  in `domainkit.contrib.webhooks.guard` patches it in
  `domainkit.outbound.guard` instead, the module that calls it. `lifecycle.register("guard
  lookups", ...)` raises `ValueError`: shutdown closes the guard's lookup
  threads itself, under that name.
- **The plugin forgets the push services' tokens before each test**
  (`domainkit.contrib.notifications.push.reset_tokens()`). A test that
  counted on a token an earlier test's stub answered gets asked for one
  again: answer the token request in its own stub.
- **Approvals asks maker and approver rules on an `OfflineContext`.** A
  guard's `maker_rule`, when the approved command runs, and a policy's
  `approver_rule`, in `approvers_of`, are asked on a new `OfflineContext`
  each time: a call of its own, as since 2.8.2. The query, the headers,
  and the cookies are empty, `request.user` is the user asked about,
  `request.auth` is None, and `request.state` starts empty. A rule that
  raises an error of its own refuses, is logged as a warning by its class
  and its error's, and is reported to the error reporters: the request
  fails with `maker_not_allowed` at once, where 2.8 ran it again until it
  was dead-lettered, and `approvers_of` leaves that rule's users out,
  where 2.8 raised. For a maker rule, a `DomainError` still fails the request
  with its own code; for an approver rule, it leaves the user out, as
  any other error of the rule's. A transient error (a connection, a
  cache, ledger, or broker out of reach, a timeout, an outgoing call that
  may succeed later, a database busy or out of reach) is still raised,
  so the run is tried again; another `OSError`, such as a file not found,
  is the rule's own
  ([Rules asked outside a request](auth/routes.md#rules-asked-outside-a-request)).
- **`RATE_LIMIT_BACKEND` is checked before it counts.** A value that is
  neither a dotted path (`module.Class`, `module:Class`) nor a name a
  package offers is refused with the settings. One that does not import,
  or names a class without `hit(key, limit, period)`, is refused by
  `check` and as the application starts where something counts with it
  (`backends.E001`): `RATE_LIMIT`, a route's `Throttle`, or auth's attempt
  throttles. Staging and production do not start, and elsewhere the
  first request that counts is refused, with the reason. 2.8 refused
  these only where a request was counted, with an error of the call.
  Where nothing counts with it, `check` warns (`backends.W001`), and
  `check --deploy` stops on the warning: remove the setting or name a
  backend, or list `backends.W001` in `SILENCED_CHECKS`. The worker's
  task rates count with it too, and fail where they first count.
- **`domainkit.http.ratelimit.load_backend` is gone.** It was not public.
  Call `domainkit.http.ratelimit.rate_limit_backend.create()` for a new
  backend of `RATE_LIMIT_BACKEND`'s class, or
  `rate_limit_backend.backend_class()` for the class.
- **A password rule needs `validate` and `help_text`.** A
  `PASSWORD_VALIDATORS` class without both is refused at startup
  (`ImproperlyConfigured`), where 2.8 failed when the rules were listed.
  A `NAME` may still name a function that makes the rule, as in 2.8;
  what it makes is checked the same way.
- **Auth's classes are made once per settings object.** The backends,
  hashers, authentication classes, and password rules of `AUTH` are made
  again when `override_settings` (or the `settings` fixture) changes any
  setting, and kept while the settings stay the same, where 2.8 kept one
  set per value for the process. A test that emptied the loaders' caches
  (`load_authenticators.cache_clear()`) no longer needs to: the loaders
  `load_backends`, `load_hashers`, `load_authenticators`, and
  `load_validators`, which were not public, are gone; use
  `get_backends()` and the other getters. A class among them that cannot
  be made with its options raises `ImproperlyConfigured` naming its
  setting, as 2.8 did for password rules only.

### `migrate` makes the user lookups' indexes, on PostgreSQL

Logins by address, signups, the checks for a taken address or username,
and the admin API's list of users read the whole users table in 2.8. On
PostgreSQL, `migrate` now makes, on the user table, a project's own too
(`AUTH.USER_TABLE`), in the table's schema where it names one, those of
these indexes that are missing:

- `ix_<table>_<EMAIL_FIELD>_lower`, on `lower()` of the `EMAIL_FIELD`
  (`ix_domainkit_users_email_lower` on the default table);
- `ix_<table>_<USERNAME_FIELD>_lower`, on `lower()` of the
  `USERNAME_FIELD` (`ix_domainkit_users_username_lower`);
- `ix_<table>_<field>_id`, on the field the admin API lists users by,
  then the id: the first of `AUTH.IDENTIFIERS` the table does not
  encrypt, else the `USERNAME_FIELD` (`ix_domainkit_users_email_id` where
  addresses come first). None where that column is unique, as its own
  index serves the order, or the table has no `id`.

A column kept as anything but text, such as bytes of a type of your
own, gets no `lower()` index; an encrypted one gets none. No
`makemigrations`: they are not declared on the table, and revisions
leave them alone, by name. SQLite gets none, as it cannot reflect an
index on `lower()` ([ADR 0185](adr/0185-bound-keyset-pages-index-the-user-lookups-and-read-fewer-grants.md)).

`migrate` reads the table's indexes from the catalog first, and makes
only those missing: with none to make, it takes no lock on the table.
Building one holds writes to the table while it runs. On a large users
table, make them before you deploy, without holding writes, under the
same names; `migrate` then finds them and builds none. For the default
table, with addresses first in `AUTH.IDENTIFIERS`:

```sql
CREATE INDEX CONCURRENTLY ix_domainkit_users_email_lower ON domainkit_users (lower(email));
CREATE INDEX CONCURRENTLY ix_domainkit_users_username_lower ON domainkit_users (lower(username));
CREATE INDEX CONCURRENTLY ix_domainkit_users_email_id ON domainkit_users (email, id);
```

A concurrent build that fails leaves an invalid index of that name, which
`migrate` takes as made: drop it, and build it again.

### A repository that overrides `count` counts every match in an export

An export now asks its repository's `count` for at most `max_export + 1`
matches: `count(criteria, include_deleted=..., at_most=...)`. A
repository of the project's that overrides `count` keeps working: the
export asks it as 2.8 did, without `at_most`, and it counts every match.
The export does not read the override's signature, since a decorator's
need not tell the truth.

To count less, leave `count` alone and override the hooks under it.
`_count(criteria)` counts every match. `_count_at_most(criteria, limit)`
counts no more than `limit`, and by default answers the smaller of
`_count` and `limit`. A repository that overrides only `_count` needs
nothing more: a count that stops is asked of it, and its answer stops
at `at_most`.

### Keyset pages, admin changes, and exports read less

Nothing to change; plans and costs change.

- **A keyset page starts at its position.** What follows a position
  begins with a bound on the ordering's first field, so an index whose
  first column is that field is read from the position. On a field that
  may be missing, a page whose position's group is followed by the other
  group (past a value ascending, past a missing one descending) reads
  the rest of its group, then the other group in a second statement if
  it is not full yet. A test that counts that page's statements
  (`assert_num_queries`) counts one more statement for the page, plus
  one more for each child relationship it loads ([Pages](queries.md#pages)).
- **The admin API reads only the grants on objects that count.** A
  backend that reads its grants from a table can answer
  `object_grants_beyond(holder, permissions)` to leave out, in its query,
  the grants of the permissions staff hold on whole models
  ([Backends](auth/users.md#backends)). One that does not keeps working:
  its `object_grants` is filtered.
- **An export counts at most `max_export + 1` matches**
  (`count(at_most=...)`), and flags' `GET /settings` reads its values in
  one statement.

### Make and apply approvals' index

With `domainkit.contrib.approvals` installed, the requests table
(`domainkit_approval_requests`) gains an index,
`ix_domainkit_approval_requests_scope`, of `(tenant_id, action, policy,
submitted_at)`, which an approver's list reads through. Run
`python manage.py makemigrations`, review the revision, then `migrate`
it. The index is of 2.8.1's `tenant_id` column: from 2.8.0, the same
`makemigrations` makes both
([Make and apply approvals' migration](#make-and-apply-approvals-migration)).

## 2.8.1 to 2.8.2

2.8.2 is a security release, with no migration and no setting: raise the
version in the project's `pyproject.toml`, install, and deploy. Each item
says what a project may notice.

- **A unit of work cut off by a cancellation gives its connection back**
  as it closes, with asyncpg and aiosqlite. A close that is itself cut
  off discards its connection, and the pool opens another in its place
  ([Request deadlines](runtime.md#request-deadlines)).
- **A discarded PostgreSQL connection closes in the background.** With
  asyncpg, a connection the pool discards, such as one cut off under an
  anyio cancel scope, gives its pool slot back at once and closes in a
  task of its own within 2 seconds, while the pool may open another. For
  that time the server can hold more connections than the pool: a pool
  sized to `max_connections` exactly needs room for them. The close
  cancels a statement cut off on the connection, so the server logs
  `canceling statement due to user request` for it, as for a statement
  a deadline cuts.
- **A maker or approver rule starts afresh on each call.** When an
  approved command runs, a maker rule or approver rule no longer sees
  what it kept on the request (`request.state`) in an earlier call, and
  `request.app.state` is an empty `State` of its own.

## 2.8.0 to 2.8.1

2.8.1 is a security release. Each item of this list says what a
project that relied on 2.8.0's behaviour notices, and what to change.
With `domainkit.contrib.approvals` installed, make and apply its
migration ([below](#make-and-apply-approvals-migration)) before the new
code serves.

- **Staff acting as a user get answers kept for that user alone.** Under
  an impersonation, a page cached with `vary_on_user=True` and a
  response kept for an `Idempotency-Key` are kept for the staff member
  acting as that user, where 2.8 kept them for the staff member, shared
  across every user they acted as. An entry or a key made while acting
  as a user before the upgrade misses once; a retry of a request sent
  just before the deploy, while acting as a user, runs again. Rate
  limits, throttles, and live connections count the staff member, as
  before.
- **An impersonation needs the right to change users held everywhere.**
  Each of its requests checks the staff member's `change` permission on
  the user model as they hold it everywhere. With `MembershipBackend`, a
  staff member who holds it only through a role in a tenant can no
  longer keep an impersonation going in requests served as that tenant:
  they get `401`. Grant the permission everywhere to staff who
  impersonate.
- **The SAML ACS refuses a response past its bounds.** A response whose
  XML makes more than 50,000 nodes (each `<` counts twice, each `=`
  once), that has an element deeper than 32 levels, with more than 32
  namespace declarations on its path (a prefix declared again counts
  again), one of a URI past 128 characters, or more than 32 attributes,
  or a `SignedInfo` of more than 32 elements or 16 KiB, or that is not
  in UTF-8 (in UTF-16, or declaring Latin-1 with bytes past ASCII), gets
  `?error=saml_refused`, where 2.8 read it. A response of a thousand
  group values makes some 7,000 nodes; identity providers write UTF-8,
  about five declarations on a path, namespace URIs under 60 characters,
  fewer than ten levels, up to some seven attributes on an element, and
  a `SignedInfo` under 2 KiB. Responses are parsed in a
  thread of their own, at most 4 at once on an event loop; one that
  waits more than 5 seconds for its turn is refused. A response that
  lxml or signxml cannot read gets `saml_refused` where 2.8 answered 500.
- **With `TYPED_LINKS = "refused"`, more typed text is refused.** A name
  spaced out (`example . com`) or bracketed (`example (dot) com`), or
  spelled with `dot`, or a dot and white space, before `com` (`example
  dot com`, `example. com`, `example .com`), answers `422
  link_not_shown`, and so do `the dot com era`, `fine . me too` and `the
  key (dot) is broken`. Under `"shown"`, the default, nothing changes.
- **A CSV cell that starts with an invisible character and a formula's
  sign gets an apostrophe.** A directional mark, a soft hyphen, a control
  or any other character that shows nothing, before `=`, `+`, `-` or
  `@`, now counts as the spaces before them did: a phone number written
  with a left-to-right mark before its `+`, as right-to-left pages write
  it, is exported with an apostrophe first, as `+1 555 0100` already
  was. Numbers stay numbers.
- **Live events are each tenant's own** (security,
  [ADR 0183](adr/0183-keep-live-events-and-approval-requests-to-their-tenant-and-rows-of-no-tenant-to-keys-held-everywhere.md)).
  With `TENANCY`, an event reaches only the connections served as the
  tenant it was committed in, and one committed as no tenant only those
  served as none. A route only staff reach that streams every tenant's
  events on purpose, such as a back office's, passes `per_tenant=False`
  to `stream`, `forward`, or `subscribe`: its rule never sees an event's
  tenant ([tenancy](tenancy.md#live-events-and-approvals)). With
  `LIVE_FANOUT`, processes of 2.8.0 send their events without a tenant,
  so connections served as a tenant miss them until every process is
  upgraded. Without `TENANCY`, nothing changes.
- **Approval requests are each tenant's own** (security). With
  `TENANCY`, a request is listed, read, approved, rejected, and
  cancelled where the tenant it was submitted in is served alone, and
  its command runs in that tenant. Approvers who decided for a tenant
  while served as another, or as none, are served as the request's
  tenant now; code that decides across tenants does it inside
  `all_tenants()`
  ([Requests and tenants](contrib/approvals.md#requests-and-tenants)).
  Requests stored by 2.8.0 have no tenant: they are read where no tenant
  is served. Do not set their `tenant_id` by hand, which breaks their
  seal: decide or cancel them where no tenant is served, or let them
  expire. Before you deploy, let the worker run the requests already
  approved: once deployed, such a request runs as no tenant.
  `approvals.view_every_approvalrequest` shows the requests of a guard
  `everywhere`, such as approvals' own guards of postings, superusers,
  and admin changes, to those who hold it everywhere: grant it so to the
  staff who read them. Without `TENANCY`, nothing changes.
- **Limits and the ledger count the keys held everywhere** (security).
  A key granted in the tenant served, by `MembershipBackend` or any
  backend that grants by `current_tenant()`, does not count on the routes
  of `domainkit.contrib.limits` or `domainkit.contrib.accounting`
  (`403`), nor for the makers and approvers of approvals' guards of
  postings, captures, reversals, making superusers, and admin changes.
  Grant those keys to the staff who use them, directly or through a
  group ([Rows of no tenant](tenancy.md#rows-of-no-tenant)). Routes of
  your own over rows every tenant shares say `everywhere=True`, which
  asks the permissions as where no tenant is served: it relies on a
  backend that grants per tenant reading `current_tenant()`.
- **Staff APIs count the keys held everywhere** (security). The admin
  API (users, groups, permissions), the staff APIs of flags and
  webhooks, the audit trail's history, and the admin site's models that
  are not `TenantOwned` count a staff member's model permission, and
  `audit.view_history`, as they hold it everywhere: a role in the tenant
  served opens none of them (`403`). Staff who used them through a role
  are granted the keys everywhere, directly or through a group
  ([Rows of no tenant](tenancy.md#rows-of-no-tenant)).
- **`check` names the apps whose rows every tenant shares**
  (`tenancy.I001`). It is an info: nothing to change, and
  `check --deploy` never refuses it.

### Make and apply approvals' migration

With `domainkit.contrib.approvals` installed, the requests table
(`domainkit_approval_requests`) gains a nullable column, `tenant_id`,
the tenant a request was submitted in
([ADR 0183](adr/0183-keep-live-events-and-approval-requests-to-their-tenant-and-rows-of-no-tenant-to-keys-held-everywhere.md)).
Run `python manage.py makemigrations`, review the revision, then
`migrate` it before the new code serves. Until then every read of the
requests fails with a database error, so their routes and the worker's
runs answer `500`. Requests stored before have no tenant.

## 2.7 to 2.8

2.8 follows up 2.7. Each item of this list says what a project that
relied on 2.7's behaviour changes, and what to run.

- **`APIError` and `FieldError` name `domainkit.http.problems`.** They
  moved there from `domainkit.http.errors`, which re-exports them, so
  their imports work and the classes are the same. What prints a class
  by its module now prints the new one: tracebacks, the body of the mail
  reporter's report (`domainkit.http.problems.APIError was raised at
  ...`), and tracing's `error.type`. Change a log filter, an alert, or an
  error tracker's rule that matches `APIError` under the old module,
  `domainkit.http.errors`, to match `domainkit.http.problems.APIError`.
- **`GET /users/names` strips the names it joins.** A first or last name
  of white space alone gives no name, where 2.7 answered the spaces, and
  a padded one loses its padding.
- **Office documents that load from elsewhere in more ways are refused.**
  `DOCX`, `XLSX` and `PPTX` answer 422 `unsafe_file` for a Word field
  that pulls content in or runs a program when its name follows a
  backslash, a quote or an invisible mark (`\INCLUDEPICTURE`,
  `"INCLUDETEXT"`), and for a `HYPERLINK` so written to a file or a
  share; for an SVG picture that loads anything but a fragment of itself
  or a `data:` URL (an `href`, CSS's `url()` or `@import`), however it is
  named or typed; and for a mail merge that names its data source or a
  connection to one. Nothing to change: a document refused is saved again
  without the field, the picture's outside source, or the mail merge's
  data source (in Word, Mailings, Start Mail Merge, Normal Word Document)
  ([Checking documents](uploads.md#checking-documents)).
- **The admin API's act binds each group by id and grants** (security,
  [ADR 0179](adr/0179-bind-the-admin-acts-groups-by-id-and-keep-a-held-confirmation-to-its-request.md)).
  A change of a user's groups binds each group it adds as approvals'
  `auth.set_groups` holds it, and each it takes away by id; its screen
  shows each group added in a few words ("auditors (…9abc) 3 perms").
  An authorization of such a change asked for under 2.7, and not yet
  spent (one held for approval with `HELD_TTL` included), is refused
  once (`403 action_not_authorized`): the staff member asks again. Every
  other act's digest is unchanged. With `WHOLE` on, fewer groups fit one
  change: add them in steps. A client or a test that matched "Adds:
  auditors" reads the new words.
- **A held authorization serves its own request alone.** With
  `HELD_TTL` set, a call made again whose command changed since (a
  group renamed, a user put in a group while their return waits) is
  refused (`403 action_not_authorized`, the trail's `held_elsewhere`)
  where it submitted a new request; confirm the change again.
- **Making staff and a user's return hold what else the call sets.**
  `auth.set_staff` and `auth.reactivate` requests hold `values`: the
  fields and flags the `PATCH` changes beside the grant (a value the
  user holds already is left out, and a text staff typed is checked as
  typed text). A pending request of 2.7 is still found and spent by the
  same call when it changes nothing else; one that changes fields is
  submitted again, and approved again, holding them.
- **A username, an address, or a number another user has is refused
  first**, before your `on_admin_change` checks and at the twin (`409`),
  where the commit refused it after them. A check of yours that expected
  to see such a change sees it no more.
- **Named keys.** With `TENANCY.ALL_TENANTS_PERMISSION` set, approvals'
  screens of `auth.set_groups` and `auth.reactivate`, and the admin
  API's act of a change of groups, show that key by name ("Grants") on
  a line of their own when the change grants it: under `WHOLE`, a
  change that filled the screen is refused; make it in steps. Set
  `AUTH = {"AUTHORIZATIONS": {"NAMED_PERMISSIONS": []}}` to name none,
  or list the keys to name.
- **A twin reads `If-Match`** ([ADR 0178](adr/0178-bind-an-act-to-the-version-its-page-read.md)).
  On a `ResourceRouter`'s act on one aggregate, a twin asked with
  `If-Match` answers `412 precondition_failed` when the aggregate is at
  none of the versions it names, where 2.7 ignored the header, and the
  authorization then holds only for an act that sends the same
  `If-Match`: another, `*`, or none is `403 action_not_authorized`. A
  client that sent `If-Match` to a twin by habit sends the act the same
  one, or none to the twin. A twin asked without it, an `If-Match` sent
  to the act alone, and the authorizations stored before are as in
  2.7. A twin takes one tag: a list of several is `422`. To have every
  client send it, declare the act with
  `Action(..., require_if_match=True)`
  ([The version the page read](auth/actions.md#the-version-the-page-read)).
- **Twins' OpenAPI gains `If-Match`.** Each twin of an act on one
  aggregate documents an optional `If-Match` header and a `412` (and
  `422`) answer: a snapshot test of the schema sees them. Approvals'
  approve, reject, and cancel twins pass the header on, as
  `StepUp(for_=...)` does.
- **Deploy every instance before clients send `If-Match` to twins.** A
  2.7 instance computes the digest without it, so it refuses (`403`, and
  voids) an act whose authorization a 2.8 instance stored with
  `If-Match`.
- **A route of your own that passes `if_match=`** to
  `request_action_authorization` holds the unit of work it lends to the
  version, and its act's unit of work too (`uow.expect`): a twin whose
  `show` and `binds` checked nothing, or an act held to no version, is
  refused (`500`, `ImproperlyConfigured`).
- **With `WHOLE` on, a rejection's or a cancellation's reason fits a
  line.** The reject and cancel twins answer `422 too_much_to_show` for a
  reason longer than 64 characters as a screen shows it (a new line
  counts two), as the admin API's twin does for an impersonation's
  reason. Clients send a shorter one. Off, the reason is cut on the
  phone, as in 2.7
  ([ADR 0180](adr/0180-refuse-links-a-person-typed-for-a-screen-and-a-reason-past-a-line.md)).
- **To refuse links in what people type**, set
  `AUTH = {"AUTHORIZATIONS": {"TYPED_LINKS": "refused"}}`. An
  impersonation's reason, a group's new name, a user's names, and a
  rejection's or a cancellation's reason that hold a link, an email
  address, or a number to call are then refused at their twins (`422
  link_not_shown`), and a group's new name at the admin API too. Mark
  what a guard's `show` takes from its maker `Typed`; a project's own
  checks of such text register on `on_typed_text`, in place of twin
  dependencies that read the body again ([Text a person
  typed](auth/actions.md#text-a-person-typed)).
- **A Word document's XML is bounded before it is parsed, and what
  `fill_docx` writes in it** (security). `placeholders` and `fill_docx`
  refuse, with `DocumentError`, a document whose content types and
  filled parts pass 12 MiB of XML, or 1,000,000 elements, texts,
  attributes, and placeholders, in all; `fill_docx` counts the values it
  writes too, their bytes and three nodes for each line break or tab. Text
  with little formatting reaches the bounds at some 2,100 to 2,900
  pages; formatted text (bold, italics, links, tables, lists) at some 270
  to 330. 2.7 parsed what `ArchiveLimits` let inflate, 256 MiB, in the
  process that called it, at up to 90 times its size in memory
  ([The XML parsed](documents.md#the-xml-parsed)). A project that fills
  larger templates passes larger bounds, and has the memory for them,
  up to 340 bytes a node:
  `fill_docx(template, values, limits=ArchiveLimits(max_xml_bytes=24 * 1024 * 1024, max_nodes=2_000_000))`,
  and the same to `placeholders`. A project that checked limits of its
  own at upload, in the check's process, may keep doing so, with these
  bounds: a template refused there is never stored.

## 2.6 to 2.7

2.7 follows up 2.6. Each item of this list says what a project that
relied on 2.6's behaviour changes, and what to run.

- **The framework's acts word their screens for people.** The admin
  API's act names flags and fields as a person reads them ("Staff: no",
  "Email", "Display name"), and approvals' screens show a list's items,
  over as many lines as it takes ("added", "added (2)"), where 2.6 showed
  `is_staff: false` and a list as JSON cut at 64 characters. A user or a
  group shows by name and the end of its id ("ann (…123456789abc)"), the
  name cut past 48 characters; an empty list shows as nothing, where 2.6 wrote `[]`; an
  approval's empty "Takes away" is left out; a backslash shows doubled.
  The digest of each act is unchanged; a client or a test that matched
  the old labels or values reads the new ones
  ([What a screen shows](auth/actions.md#what-a-screen-shows)).
- **To show acts whole, or refuse them**, set
  `AUTH = {"AUTHORIZATIONS": {"WHOLE": True}}`. The admin API's twins,
  and approvals' submit and approve twins, then answer `422
  too_much_to_show` for a change their screen cannot show whole: a value
  longer than 64 characters (a long impersonation reason), more than six
  lines (a change padded with fields), a list longer than the screen.
  With `APPROVE_ACTION` on, so is a held change of the admin API that
  the approver's screen could not show whole, at its twin, and
  submitting such a request. A user's or a group's name, however long,
  never is.
  Clients make those changes in smaller steps. A guard of the project's
  whose arguments do not fit gives `guard(show=...)`; accounting's
  postings have no `show` yet, and their lines do not fit.
- **`check` names throttles counted per tenant unsaid.** A `Throttle` on
  a route served as a tenant (a `PathTenant`'s, or any route under
  `TenantMiddleware` but a tenants' collection) that says neither
  `per_tenant=True` nor `per_tenant=False` is warned of
  (`tenancy.W001`), so `check --deploy` fails until each says how it
  counts. Nothing counts differently: `per_tenant=True` keeps each
  tenant's count, as before, and `SILENCED_CHECKS` may name the warning
  ([tenancy.md](tenancy.md#caches-limits-and-logs)).
- **An approval of `auth.set_groups` grants the groups it held**
  ([ADR 0172](adr/0172-hold-admin-changes-by-what-they-grant-and-check-before-the-hold.md)).
  The request's arguments hold `groups` beside the names: each group
  added, by id, with the permissions it grants. A request approved under
  2.6 and not yet made holds no `groups`: the staff member's call submits
  a new request (`403 approval_pending`), which is approved again. Code
  that reads a request's arguments finds the new key; a group renamed,
  made again, or granted other permissions since the request was made
  makes another request. A `409 group_in_use` of your own, which kept a
  named group from changing, can go.
- **A deactivated superuser comes back as one is made.** With
  `APPROVALS.SUPERUSER_APPROVALS` above 0, `PATCH /admin/auth/users/{id}`
  with `"is_active": true` for an inactive superuser answers `403
  approval_required`. Take superuser away while they are inactive, make
  them active, then submit `auth.grant_superuser`.
- **An `auth.reactivate` request holds what the user comes back with**:
  their flags, groups, permissions, and grants on objects. Grant them
  anything while it waits, and the same call submits a new request.
- **`auth.set_staff` holds a staff member's return.** Where
  `ADMIN_APPROVALS` holds `auth.set_staff` and not `auth.reactivate`,
  `PATCH /users/{id}` with `"is_active": true` for an inactive staff
  member waits for approval, as making staff does. Hold
  `auth.reactivate` too to hold the return of anyone who holds access;
  a call that then makes a user staff and active again is refused (`403
  approval_required`): send two.
- **The admin API's twins run your `on_admin_change` checks**, with
  `change.asking` true (a group's twins run the `on_admin_group_change`
  checks). A check that only refuses needs nothing; one that writes (an
  audit row of its own, a count) should write nothing while `asking`, or
  it writes once at the twin and once at the change. A check that calls
  a guard `by_maker` (approvals' hold does) would submit a request at
  the twin: return while `asking`.
- **Approvals' hold runs after your checks.** It registers at
  `order=100`; a check you registered for `auth` to run before it can be
  registered for its own domain again. A check that must see the hold's
  outcome registers with an order above 100.
- **A reused password costs a try, and can now answer `429`.**
  `POST /admin/auth/users/{id}/password` compares the password with the
  user's history when the change is about to be made (after your
  checks; with `auth.set_password` held, once the approval is found). A
  reuse is refused, spends the change's authorization with
  `AUTH.ADMIN_ACTION` (confirm again to try another password; with the
  history on, every call spends its authorization before the checks, one
  that waits for approval too: confirm each), and
  counts for that staff member and that user: past `AUTH.LOGIN_RATE`
  of them (or `ACCOUNT_LOGIN_RATE` over the day), that staff member's
  password changes of that user answer `429 rate_limited` for a while,
  a new password too. Nothing else a staff member does newly answers
  `429`, and the user's own wrong passwords never count here.
- **`AdminChange` has `last_check`.** The admin API's comparison of a
  password with the history; approvals' hold runs it. Await it from a
  check of yours only once the change will surely be made, after every
  check that may refuse it. A check ordered after approvals' hold (above
  100) sees the approval spent: refuse password changes before it.
- **A check added to an admin route with `select_routes` runs after the
  admin API's rules.** It no longer sees requests the rules refuse
  (anonymous, not staff, without the model permission or the
  authorization); rules you put in front of it for that can go.
- **A PDF that decodes past 32 MiB is not laid over** (security).
  `overlay_pdf` and `page_sizes` decode `max_decoded` bytes at most, in
  all: the object streams of each PDF, and the content of each page laid
  over and of its overlay. 2.6 decoded them whole, held only by pypdf's
  limit of 75 MB on each stream, so a PDF of 170 KB could take hundreds
  of megabytes in the worker. Past the budget, the call raises
  `DocumentError`. For drawings or maps of many megabytes a page, pass a
  larger `max_decoded`, for that kind of document only
  ([What a PDF may decode](documents.md#what-a-pdf-may-decode)). A page
  laid over is now written back deflated: the PDF looks the same, and is
  smaller.
- **Some PDFs 2.6 read are refused unread** (security), by
  `overlay_pdf` and `page_sizes`, where a stream they decode has an
  image's filter, `/Crypt`, or one they do not know; an LZW, run-length,
  ASCIIHex, or ASCII85 stage after another filter, or two of them; such
  stages first on streams of more than `max_decoded / 32` bytes in all
  (1 MiB), or with LZW `max_decoded / 512` (64 KiB); or a predictor. An
  object stream that cannot be decoded is refused even when nothing uses
  it; the cross-reference streams list 65,536 entries at most (one for
  each 512 bytes of `max_decoded` where that is more); a file chains 32
  cross-reference sections at most, one for the file and one for each
  save added to it (two in a hybrid file); a damaged file, whose table
  pypdf would rebuild, an object stream held in another, a
  cross-reference stream decoded otherwise than with `FlateDecode`, and
  a file that names more than 32 objects its table lacks are refused. Few generators write any of
  these on a page's content (PDFs of the early 1990s used
  `[/ASCII85Decode /LZWDecode]`, now refused), and ReportLab's
  `[/ASCII85Decode /FlateDecode]` is read as before.
- **A request that needs a renderer where none is set is answered 503.**
  `renderer()` without `DOCUMENTS["RENDERER"]` raises
  `RendererNotConfigured`, which is the `ImproperlyConfigured` it raised
  in 2.6 and a `RendererUnavailable`: the HTTP layer answers it
  `503 renderer_unavailable`, where 2.6 answered 500. It is still logged
  as an error and sent to `ERROR_REPORTERS`, the first time a process
  asks for a renderer (a forked process too), and no more. Code that catches
  `ImproperlyConfigured` still catches it, but an exception handler of
  the project's own for `ImproperlyConfigured` no longer sees it, since
  `UpstreamError` comes first in its classes (the handler for the
  nearest class answers). Code that catches `RendererUnavailable`, to
  answer Gotenberg being down, now catches a missing setting too.
- **`PlaceholderError.malformed` lists the braces in another order.**
  `fill_docx` reads the body, then the headers, footers, footnotes, and
  endnotes, each kind by part name, where 2.6 read the parts in the order
  the document's content types listed them. A test that compares the
  whole tuple may need its order changed; `missing` and `unused` were
  sorted and stay so.

- **A PDF's page content is bounded, and decoded as `overlay_pdf`
  decodes it** (security). `CHECKED_PDF` refuses, 422 `unsafe_file`
  ("The file is too large or too complex to check."), a PDF whose pages'
  contents decode to more than 64 MiB in all, and one the shared policy
  refuses: a predictor or an image's filter on a page's content, ASCII85,
  ASCIIHex, run-length or LZW anywhere but first or past their share,
  more than 32 cross-reference sections, an object stream in another
  ([Checking documents](uploads.md#checking-documents), ADR 0175, ADR
  0176). A project that takes larger drawings gives its type a bound of
  its own: `FileType("application/pdf", ".pdf", PDF.matches,
  PdfCheck(max_content=128 * 1024 * 1024))`, and `UPLOAD_CHECKS` room to
  decode it: the check keeps what it decodes, and holds the largest
  stream twice, so a page of 127 MiB took 294 MiB, within the 512 MiB
  `MEMORY`; one of 250 MiB, with `max_content` of 256 MiB, needed 540
  MiB ([Memory to give](uploads.md#checking-documents)).
- **Office documents with an unended or deleted field, a link to a file,
  or a VML picture from elsewhere, are refused.** A Word field that pulls
  content in and never ends, or whose instruction is a tracked deletion; a
  `HYPERLINK` field to a file or a share; a VML picture, fill or stroke
  whose `src`, `o:href` or `o:althref` names a source, or whose `o:relid`
  names a hyperlink; and any of these in a part a relationship names
  whatever its content type (a header typed as a PNG), are 422
  `unsafe_file`. Accept or reject the tracked changes and save the
  document again.
- **An act's twin hands its verdict to the act.** An `upload_route` whose
  rule asks for an authorization checks the file at the twin and takes
  that verdict at the act, for the same caller and content, for 30
  minutes. A deploy that makes a check of your own stricter leaves the
  verdicts kept before it for those 30 minutes: empty the default cache
  with the deploy if that matters
  ([Bytes and streams](uploads.md#bytes-and-streams)).
- **An upload route checks its storage and directory before the body.**
  An alias `STORAGES` lacks, or a directory a storage refuses, raises
  before the file is received (still a 500), and a size given as a float
  is refused when the route is declared.
- **`DeviceSigned` reads a body last.** A request naming no device of the
  caller's is refused before its body is read, and a signed request takes
  two short transactions, the device's lookup and the request's record
  (which reads the device again), where it took one. Nothing to change;
  once every app in use sends `Content-Digest`, set
  `DEVICES.CONTENT_DIGEST = "required"`
  ([The body, read last](auth/devices.md#the-body-read-last)).

### Make and apply auth's migration for bound and held authorizations

The action authorizations table (`domainkit_authorizations`) gains three
nullable columns: `bound`, the SHA-256 of what an act's `binds` read of
its record, and `held_until` and `held_for`, how long and for which
request an authorization of an act held for approval is kept (ADR 0174).
Run `python manage.py makemigrations`, review the revision, then
`migrate` it before the new code serves. Until then every use of the
table fails: asking for an authorization, reading one (the rule of every
act), and the hourly purge raise `OperationalError`, so acts answer
`500`; `check` reports nothing of it. Authorizations asked before bind
no record and are held by nothing; an act that adds `binds` refuses
those once, `409 action_record_changed`, and the client asks again.
Nothing else changes until a project declares `binds`, or sets
`AUTH.AUTHORIZATIONS.HELD_TTL` or `ACTIONS`.

## 2.5 to 2.6

2.6 follows up 2.5. No table changes. Each item of this list says what a
project that relied on 2.5's behaviour changes.

- **A route of approvals' rules runs its own step's service.** A
  `ResourceRouter` route whose rule is `RequestActions`, `ApproveAction`,
  `RejectAction`, `CancelAction`, or `SubmitAction`, and whose service is
  another step's (`cancel_request` behind `RejectAction`, say) or one of
  the `record_*` functions, is refused when it is declared
  (`ImproperlyConfigured`): its rule takes any authorization of the act,
  and only the step's own service binds it to the request, so such a
  route took a step with an authorization asked for another request
  ([From the project's own code](contrib/approvals.md#from-the-projects-own-code)).
  Run the step's own service on the route, and the `record_*` functions
  from code no user reaches.

## 2.4 to 2.5

2.5 answers a third report from the project that runs an identity
service on domainkit. No table changes: there is no migration to make.
Deploy the workers before the web processes: the tasks that send a
verification code take the address or number it goes to (`value`),
which a 2.4 worker refuses, retrying the message until its attempts run
out. A code an upgraded worker sends is kept for its user (verification
codes are each user's own, below), which a 2.4 web process does not
read: it refuses that code until it is upgraded too, and takes it then,
within `AUTH.CODE_TTL`. Each item
of this list says what a project that relied on 2.4's behaviour changes.

- **A number given at signup with an address is on the account only once
  its own code is proved**
  ([ADR 0171](adr/0171-prove-a-number-given-at-signup-by-its-own-code.md)).
  `POST /auth/signup` with `{"email", "mobile_number"}` makes the account
  with the address alone when the address's code comes back; the number
  is held, and texted a code, which the user, logged in, sends to `POST
  /auth/mobile-verification/code`. Before, the address's code put the
  number on the account unproved, took it from an account that held it
  unproved (ending that account where it was its only login), and which
  mail the address got told whether an account used the number. An app
  that signs up with both asks for the number's code after the first
  login, as it does for a changed number; one that read the number from
  the new account at once reads it once proved. A signup by a number
  alone is unchanged.
- **Where addresses identify no one, a signup with an address and a
  number gets its code by text.** With `AUTH.IDENTIFIERS` such as
  `("mobile_number",)`, or `("username", "mobile_number")` and no
  username given, the code goes to the number (2.4 mailed it to the
  address), and `/signup/code` takes the number as its `identifier`. The
  address is held and mailed a code of its own, which the user sends to
  `POST /auth/email-verification/code`.
- **A `mobile_number` that `REQUIRED_FIELDS` names comes on its own.**
  Where numbers identify users, a signup gives it as `mobile_number`, not
  under `fields` (`422 unknown_user_field` there), and is proved as
  above. A client that sent it in `fields` moves it up.
- **Verification codes are each user's own.** Codes for
  `/email-verification` and `/mobile-verification` issued before the
  upgrade stop working; they last `AUTH.CODE_TTL`. Asking again with no
  body now counts against `EMAIL_RATE` per the number or address the code
  goes to, and can answer `429`.
- **`createsuperuser --promote` reads an id in any form, and refuses
  digits that are no number in international form**
  ([ADR 0165](adr/0165-let-an-operator-prove-a-superusers-address-or-promote-a-proved-account.md)):
  name a user whose username is written as digits by its id.
- **An `on_approve` hook's refusal denies the request.** A hook that
  raises `PermissionDenied`, or an HTTP error of 403, ends the request:
  the page's poll and wait answer `access_denied` at once, where the
  request waited until it expired; nothing is reported. A hook that
  called `deny()` itself before raising may drop the call: it still
  works. Any other error leaves the request waiting, as before
  ([Hooks of the web pages](auth/ciba.md#hooks-of-the-web-pages)).
- **Each kind of request has its own push text.** `CIBA.PUSH_TITLE` and
  `PUSH_BODY` default to `None`, and each kind has its words
  ([Push texts](auth/ciba.md#push-texts)). A project that set them keeps
  them, for every kind; to give each kind its own, unset them, or name
  each in `PUSH_TEXTS`. Code that read `PUSH_TITLE` gets `None` where it
  was not set. domainkit's catalogs have the new texts in Arabic; a
  project whose catalogs translate the push into other languages adds
  them there.
- **A session's asks are counted per user.** `START_RATE` counts what a
  signed-in session asks the phone per user, from any address. A test
  suite that raised `START_RATE` for one user's acts still needs it
  raised; one that raised it for many users behind one address no
  longer does.
- **A command gets the dataclasses it declares as instances.** A command
  field declared as a dataclass, such as a value object, or a tuple of
  one, whose body field is typed as that same class (or a tuple of it),
  got dicts from a command route, a batch, or a twin: a `@use_case`'s
  `tuple[Address, ...]` came as `({"street": ..., "city": ...},)`. It
  gets the instances the body validated, `(Address(...),)`. A service
  that rebuilt them (`Address(**item)`) drops that step; it gets
  pydantic's instances, built with their `__post_init__` but without
  their markers (`MaxLength`, ...) checked, as 2.4 built them before
  dumping them, so a service that relied on the rebuild to check the
  markers checks them itself. A `@field_serializer` on such a body field
  no longer applies to what the command gets. A field the command
  declares as `dict`, `Any`, `Mapping`, or anything else, a `list[...]`,
  and a field the body gives in a shape of its own (a nested model) still
  get what `model_dump()` gives.
- **A route that asks `StepUp(for_=...)` warns unless its service is
  decorated** with `requires_authorization` of that act; 3.0 refuses it
  ([below](#decorate-the-service-of-a-route-that-asks-for-an-acts-authorization)).
- **An act whose command holds a set is digested with the set sorted.**
  Its items came in their hashes' order, which differs between
  processes. An authorization asked for before the upgrade, and not yet
  spent, for such an act no longer matches it: ask again.
- **`check --deploy` warns about clamd reached in clear.** Where
  `UPLOAD_SCANNER` names `ClamAVScanner` on a host other than this one,
  without `tls`, `check` warns (`security.W007`), and `check --deploy`
  fails. Put a TLS proxy in front of clamd and set `tls`, or, where clamd
  is on a network only the service reaches, add `security.W007` to
  `SILENCED_CHECKS` ([clamd on another host](uploads.md#clamd-on-another-host)).
- **FCM tokens refused as `INVALID_ARGUMENT` with no field cost a dry
  run.** `FcmPushBackend.send` asks FCM about the token alone before it
  forgets it, and dead-letters the message when the token passes. A
  subclass that overrode `refusal` for this case may drop the override.
- **A catalog that translated the password rules' filled-in text
  translates their placeholders instead.** `MinimumLengthValidator` and
  `UserAttributeSimilarityValidator` now translate `...at least
  {min_length} characters.` and `The password is too similar to the
  {label}.`, then fill the value in; a project catalog that held `...at
  least 12 characters.` moves to the text with its placeholder.
- **A malformed `email` is refused with `type` `value_error`.** On
  `/signup`, `/password-reset` and `/email-verification`, an `email` not
  shaped as an address answers `Enter a valid email address.`, and its
  error's `type` is `value_error`, no longer `string_pattern_mismatch`.
  A client that read the type reads the new one.
- **`makemessages --check` can fail after upgrading.** It now extracts
  `gettext_noop`, and reads the project's own modules: the settings'
  package and `ROOT_ROUTER`'s. Run `makemessages` once and commit the
  catalogs. A text marked outside every domain that no catalog
  translates needs `LOCALE_PATHS`
  ([i18n.md](i18n.md#the-projects-own-messages)); without it, `--check`
  fails, and a run names the text, which reached clients in English.
- **Opt in to what 2.5 adds where it fits:** `SERVE["HTTP"] = "1"` unless
  the proxy speaks h2c to the server, `SERVE["EXIT_ON_FAILED_START"] =
  True` where an orchestrator restarts the container
  ([Server limits](reference/servers.md#http-versions)),
  `check_write_once` on an `S3Storage` whose store is not AWS S3 or R2
  ([A store that ignores create only](uploads.md#a-store-that-ignores-create-only)),
  the admin API's and approvals' acts
  ([below](#authorize-the-admin-apis-changes-and-a-requests-steps-as-acts)),
  and `pip install "domainkit[documents]"` for `domainkit.documents`
  ([documents.md](documents.md)).

### Authorize the admin API's changes and a request's steps as acts

Nothing changes until a project turns the new settings on. A project
that asks the phone's free-text step-up (`POST /auth/app-sign-in/step-up`,
deprecated, removed in 3.0) for the admin API's changes, or for
submitting, rejecting, or cancelling approval requests, moves those acts
to action authorizations
([The framework's own acts](auth/actions.md#the-frameworks-own-acts-the-admin-api-and-approvals)):

1. Turn on `AUTH["ADMIN_ACTION"]`, and `APPROVALS["SUBMIT_ACTION"]`,
   `REJECT_ACTION`, and `CANCEL_ACTION` (and `APPROVE_ACTION`) as the
   project needs. Each needs `domainkit.contrib.auth`.
2. Take away the `StepUp(max_age=...)` that `select_routes` added to
   those routes: it would ask for a window as well.
3. In the web client, on `401 authorization_required`, ask the route's
   twin (`POST <the route's path>/authorization`; where a `PATCH` and a
   `DELETE` share a path, `<path>/authorization/patch` and
   `.../delete`), have the phone confirm it,
   and send the act again with `Action-Authorization`, as for any act
   ([Move from the phone's free-text step-up](#move-from-the-phones-free-text-step-up-to-action-authorizations)).
4. Once nothing asks the free-text step-up, leave its routes out of
   ciba's router (`select_routes(..., without=[...])`), and the test that
   ignored its deprecation goes.

### Decorate the service of a route that asks for an act's authorization

A `ResourceRouter` route that asks `StepUp(for_=...)` and runs a
service not decorated with `requires_authorization` of that act warns
when it is declared (`DomainkitDeprecationWarning`), and 3.0 refuses it
([A use case as the act](auth/actions.md#a-use-case-as-the-act)).
Decorate the route's own service, not one it calls, so the route is
known to spend what it asks for. A route that runs a `@use_case` takes
the copy `requires_authorization(ACTION)(use_case)` makes: one that runs
the use case itself is refused in 2.5 already, as it never spent the
authorization.

## 2.3 to 2.4

2.4 answers a report from a project that runs an identity service on
domainkit. Tables of auth, ciba, and object permissions change: make and
apply the migrations the sections below name before the new code serves
requests. Each item of this list says what a project that relied on
2.3's behaviour changes.

- **Signups refuse a `username` where usernames identify no one.** With
  `AUTH.IDENTIFIERS` set to the email address or the mobile number, `POST
  /signup` with a `username` answers `422 unknown_user_field`, as it does
  for a number where numbers identify no one. Nobody logged in with it,
  and a name someone chose, such as another person's address, stood for
  them in operators' commands. A client that sent one drops it.
- **Commands try the identifiers first.** `changepassword`, `createtoken`,
  `cleartokens --user`, `resetmfa`, `addcertificate` and `unbinddevices`
  look a value up by `AUTH.IDENTIFIERS` before the username, and a value
  that is an email address or a mobile number never finds a user by
  username where usernames identify no one. Each says on stderr whom it
  found; stdout is unchanged. A user stored with such a username before
  2.4 is named by their address or number instead.
- **Reset and enrollment codes go only to proved identifiers**
  ([ADR 0141](adr/0141-codes-that-open-an-account-go-only-where-it-was-proved.md)).
  A user whose address or number was never proved (`email_verified`,
  `mobile_number_verified` false) gets no code there: staff-made accounts,
  and those from before 2.0, are the usual case. They log in and prove it,
  or staff set a password (`changepassword`). To send as 2.3 did while
  they do, set `AUTH.CODES_TO_UNPROVED = True`. A project that overrides
  `auth/signup_code_held` drops its advice to reset the password.
- **A user's own new address or number waits for its code**
  ([ADR 0153](adr/0153-hold-a-new-address-or-number-until-its-code-proves-it.md)).
  `POST /auth/email-verification` takes `{"email"}`, and
  `/mobile-verification` `{"mobile_number"}`: the new value is held, the
  code goes to it, and the account keeps the one it has until the code
  comes back. A route of your own that changed the user's address or
  number with `update` replaced it at once, unproved, and since 2.4 that
  costs the user their resets until they prove it: send the address and
  the number to these routes instead. Staff's admin API still sets them
  at once. These two routes refuse a body with any other key (`422`);
  one without a body works as before. Run the migration below.
- **Strict mode refuses unsigned password logins before the password.**
  With `CIBA.APPROVE_LOGINS = "always"`, `/login` and `/session` answer
  `403 phone_approval_required` to every request no bound device of the
  account signed, whatever the account or the password; a user with no
  device got `phone_enrollment_required` there before. An app that read
  that code to offer enrollment offers it on `phone_approval_required`
  when it holds no key. In every mode, a password staff set is changed
  on first use only once the login gates passed.
- **Codes and sign-in pushes follow the language.** Texts, mails, and
  ciba's push go out in the user's language where the user model names
  it (`LANGUAGE_FIELD`), else in the request's: with `LocaleMiddleware`
  and the default `LANGUAGES`, a client that asks in Arabic gets them in
  Arabic ([The language of codes](auth/logging-in.md#the-language-of-codes)).
  To keep them all in one language, list only it in `LANGUAGES`. Your own
  templates in `MAIL.TEMPLATE_DIRS`, and your own `PUSH_TITLE` and
  `PUSH_BODY`, are used as before. A request in another language than
  `LANGUAGE_CODE` hands it to the task that sends its code (`language`):
  deploy the workers before the web processes, as a 2.3 worker fails on
  that argument, and retries the message as it retries any task that
  fails.
- **`POST /devices/enroll/code` counts per recipient** against
  `AUTH.EMAIL_RATE`, as the other requests that send a code do. A client
  that asked for the same account's code more than five times an hour
  now gets `429 rate_limited`.
- **Name your durable handlers.** `check --deploy` warns about each one
  subscribed without `name=` (`messaging.W002`), as it warns about
  unnamed tasks. Give each a name, with its key until now in
  `formerly=`, so the messages queued under it still run:
  `@handles(Event, durable=True, name="billing.notify",
  formerly=["domains.billing.controllers.notify"])`. `check` names the
  key in its hint. Keep `formerly=` until those messages have run.
- **Task priorities count on RabbitMQ.** Each message carries its
  task's priority, and from RabbitMQ 4.0 a queue hands out higher ones
  first, among the messages waiting in it. A project whose tasks name a
  priority sees them go out in that order; one whose tasks name none
  sees no change ([Priorities on RabbitMQ](contrib/messaging.md#priorities-on-rabbitmq)).
  Under Redis Streams, NATS JetStream, Kafka or SQS, `check --deploy`
  warns about tasks that name a priority (`messaging.W004`): give urgent
  work a queue of its own, or silence it.
- **Live routes served by several processes want a fan-out.** `check
  --deploy` warns (`live.W003`) when the application serves WebSocket or
  event-stream routes, `LIVE_FANOUT` names no backend, and the server
  starts several worker processes or messaging is installed. Share the
  event types the routes stream and set `LIVE_FANOUT`
  ([Every process's events](live.md#every-processs-events)), or silence
  it if they stream no committed events.
- **Two durable handlers under one key are refused.** Two functions
  with one `module.QualName` (closures made by one factory, methods of
  two instances) shared a key, and workers ran the last one subscribed
  for the messages of both. `subscribe` now raises `ValueError` at
  startup: give each its own `name=`.
- **Approvals show each viewer their share of the requests.** `GET
  /requests` and `GET /requests/{id}` show a caller the requests they
  made and those whose policy names them among its approvers; another
  request answers `404`. Approvers and makers see what they saw before.
  Someone who holds `approvals.view_approvalrequest` alone, such as an
  auditor, sees only their own: give them
  `approvals.view_every_approvalrequest` (`migrate` creates it) to see
  every request, as in 2.3
  ([Who reads which requests](contrib/approvals.md#who-reads-which-requests)).
- **The admin API's checks come before `on_admin_change`.** A check of
  yours sees only changes the admin API would make: one that a bound, a
  field, or a password's validators refuse is refused first.
- **An async password rule is awaited, or refused.** A rule in
  `PASSWORD_VALIDATORS` whose `validate` is `async def` never ran before
  2.4: the password was taken unchecked. Every route and command now
  awaits it. Code of your own that calls `validate_password` with such a
  rule configured gets `ImproperlyConfigured`; call
  `await avalidate_password(...)` there instead.
- **Events carry `actor`**
  ([ADR 0147](adr/0147-name-who-caused-each-event-and-tell-the-old-address.md)).
  `dataclasses.fields(event)`, `asdict(event)`, and `dump_event` list
  it after `event_id` and `occurred_at`. Code that lists an event's own
  fields leaves it out as it leaves those two out, such as a test helper
  that compares an event's fields with a dict. Webhook bodies, live
  events, and audit details are unchanged. An event class that declares
  an `actor` of its own keeps it, and domainkit leaves that field alone;
  mypy reports the override, so rename it, or keep it with
  `# type: ignore[assignment]`. A test that lists the events `update`
  records sees `EmailChanged` or `MobileNumberChanged` after
  `UserUpdated` when the address or the number changed.

- **`seed` and `privacy export`/`erase` start the domains.** Their
  `on_startup` hooks run first, as for the application and the worker: a
  key service is asked to unwrap the data keys, and, where deployed, the
  audit trail's and the ledger's protections are checked. Where those
  refuse, the command refuses as the application would. Run `seed` after
  `migrate`, and from where the key service answers.

### Make and apply the object permissions' migration

With `domainkit.contrib.objectpermissions` installed, the grants table
gains two nullable columns, `expires_at` and `role`, and an index on
`expires_at`, for grants that expire and roles
([ADR 0151](adr/0151-object-grants-that-expire-roles-and-owners-a-caller-administers.md)).
Run `python manage.py makemigrations`, review the revision, then
`migrate`, before the new code serves requests: reading grants fails
until the columns exist. Grants made before never expire and name no
role. `grants_on` now lists the grants in force only.

### Make and apply the tokens' two new columns

The tokens table gains two nullable columns: `amr`, what a login proved,
and `idle_timeout`, the idle timeout a hook gave it
([ADR 0146](adr/0146-show-gates-how-a-login-was-made-and-let-a-login-keep-shorter-limits.md)).
Run `python manage.py makemigrations`, review the revision, then
`migrate`, before the new code serves requests. Logins made before show
no proof in `request.auth.origin`, and keep the settings' limits. A
login route of your own may pass `amr=` to `pass_login_gates` and
`issue_login`, beside `method=`
([How a login was made](auth/logging-in.md#how-a-login-was-made)).

### Make and apply ciba's migration

With `domainkit.contrib.ciba` installed, its sign-in requests table
gains a nullable column, `same_network`, where a QR code keeps what its
page asked of a phone on another network
([A policy per page](auth/ciba.md#a-policy-per-page),
[ADR 0155](adr/0155-choose-number-matching-and-the-same-network-per-page.md)).
Run `python manage.py makemigrations`, review the revision, then
`migrate`, before the new code serves requests: starting or reading a
sign-in fails until the column exists. Requests started before have
none, and follow `QR_SAME_NETWORK` as they did. A QR sign-in asks no
number unless `QR_NUMBER_MATCHING`, or a page's policy, asks it.

### Make and apply auth's migration for held addresses and numbers

A new address or number a user asks to change to is held in a new table
of auth's, `domainkit_pending_identifiers`, until its code comes back
([ADR 0153](adr/0153-hold-a-new-address-or-number-until-its-code-proves-it.md)).
Run `python manage.py makemigrations`, review the revision, then
`migrate`, before the new code serves requests: the verification routes
and their tasks, deleting or erasing a user, a signup that takes an
address, a staff change of an address, and the hourly purge read or
delete its rows, and fail until it exists.

### uvicorn becomes an extra in 3.0

Every install of 2.x brings uvicorn (`uvicorn[standard]`), even where
Granian serves. In 3.0 it becomes an extra, `domainkit[uvicorn]`.
`runserver` and the test plugin import it only when it serves, so
`runserver --server granian` already runs without it. Nothing changes
in 2.x, which has no such extra. On moving to 3.0, add it where the
project uses uvicorn: its image runs it (the `Dockerfile` that `init`
writes does), `runserver` serves with it, or a test uses `live_server`:

```toml
dependencies = ["domainkit[postgres,redis,sqlite,uvicorn]>=3.0,<4"]
```

### Make and apply auth's migration for action authorizations

Action authorizations ([Authorizing one act](auth/actions.md), ADR 0144)
are kept in a new table of auth's, `domainkit_authorizations`. Run
`python manage.py makemigrations`, review the revision, then `migrate`,
before the new code serves requests: deleting or erasing a user, the
hourly purge, and `cleartokens` delete its rows, and fail until it
exists. A project that declares no `Action` changes nothing else.

`auth.sca` is unchanged in 2.x: its table, its encoding, its routes, and
its names. 3.0 folds it into action authorizations: one table, one
encoding, and `request_authorization` a built-in action. A payment may be
declared as an `Action` now, with its amount, currency, and payee in the
command and in what is shown.

### Move from the phone's free-text step-up to action authorizations

`POST /app-sign-in/step-up` and `POST /app-sign-in/step-up/token` are
deprecated, and 3.0 removes them. The phone showed the page's words, and
its approval stepped the session up: a window for every guarded act,
with any arguments. Through 2.x they work as before, answer with a
`Deprecation` header (RFC 9745), and the asking route warns once a
process; `pytest -W error::domainkit.deprecation.DomainkitDeprecationWarning`
finds the tests that still ask it.

For each act the phone approved that way:

1. Declare it as an `Action`, register it, and decorate its service
   ([Authorizing one act](auth/actions.md#a-first-example)).
2. Put `StepUp(for_=...)` on its route in place of `StepUp(max_age=...)`.
3. In the web client, ask for an authorization of the act (`POST <the
   route's path>/authorization`), then the phone
   (`POST /auth/app-sign-in/authorizations/{id}`), poll
   `POST /auth/app-sign-in/authorizations/token`, and send what it
   answers in `Action-Authorization`. A page that waited on
   `/app-sign-in/step-up/wait` waits on
   `POST /auth/app-sign-in/authorizations/wait` instead.
4. In the mobile app, show a request of `kind` `authorization` from its
   `authorization` object (the title and details), and approve it with
   `{"match_code", "digest"}`, signed as before
   ([Confirming one act](auth/ciba.md#confirming-one-act)).

Its tables do not change: ciba's `kind` column takes `authorization`.

## 2.2 to 2.3

2.3 closes a hole in the phone modes: a stolen web session could bind a
phone of its thief's ([ADR 0134](adr/0134-bind-another-device-from-a-bound-one.md)).
It also lets a device's signed requests arrive in any order
([ADR 0135](adr/0135-keep-each-signed-request-not-the-last-time.md)).

### In the phone modes, more requests are signed

A security fix ([ADR 0134](adr/0134-bind-another-device-from-a-bound-one.md)).
With `CIBA.APPROVE_LOGINS` set, for a user who has a bound device, these
need a request one of their devices signs (`403 phone_required`
otherwise):

- `POST /auth/devices`: a second phone is bound from the first, which
  sends the new phone's public key ([Binding another device](auth/devices.md#binding-another-device));
- `POST /auth/devices/push-tokens` and `DELETE /auth/devices/push-tokens/{id}`:
  an app that registers its push token after binding its key signs that
  request, or registers the token before it binds;
- `POST /auth/tokens`: personal tokens are made from the app;
- `POST /auth/identity/{name}/link` and `DELETE /auth/identity/linked/{name}`.

Users with no bound device, and projects outside the phone modes, are not
affected. A refused request is answered before its body is read
(`403 phone_required` where a body that did not validate got `422`,
[ADR 0136](adr/0136-declare-the-login-check-on-the-route.md)).

`check` now warns (`ciba.W001`) while
`AUTH.CREDENTIAL_CHANGE_DEVICE_LOGINS` is `"end"` in the phone modes: set
`"keep"` or `"ask"`, or silence it.

### Make and apply the devices' migration

Signed requests are kept in a new table, `domainkit_device_signatures`
([ADR 0135](adr/0135-keep-each-signed-request-not-the-last-time.md)). Run
`python manage.py makemigrations`, review the revision, then `migrate`,
before the new code serves requests: a signed request is refused with an
error until the table exists.

### Signed requests pass in any order

A device's request no longer has to be later than its last one. An app
that worked around it, by sending its requests one at a time, may send
them together. `DeviceStore.advance` is gone: call `use` and `touch` if
your own code called it. A device's `last_used_at` is now written at most
once a minute.

## 2.1 to 2.2

2.2 shows where each login came from, lets the phone manage the user's
logins ([ADR 0130](adr/0130-show-where-each-login-came-from-and-manage-logins-from-the-phone.md)),
and lets a project choose what a password change does to the phone's
login ([ADR 0131](adr/0131-choose-what-a-password-change-does-to-the-devices-logins.md)).

### Make and apply the tokens' migration

The tokens table gains four nullable columns: `method`, `agent`, `ip`,
and `device_id`. Run `python manage.py makemigrations`, review the
revision, then `migrate`. Logins made before it show `null` for them until
they end.

### Name the method in your own login routes

A login route of your own that calls `issue_login` or
`start_browser_session` records the agent and the address on its own.
Pass `method=` (a `LoginMethod`, from `auth.integration`) so the user's
list says how it signed in, and `device_id=` for a device's own login.

### In the phone modes, web pages no longer list logins

With `CIBA.APPROVE_LOGINS` set, `GET /auth/tokens`, ending another login,
and logging out `everywhere` or `others` need a request signed by the
user's bound device (`403 phone_required` otherwise). A web page that
listed the user's sessions or personal tokens moves that to the mobile
app. Logging the page itself out is unchanged, and so is creating a
personal token. Users with no bound device are not affected.

### Unbinding a device ends its logins

`DELETE /auth/devices/{id}` ends the logins of the device it unbinds,
the login that bound it included. With `CIBA.APPROVE_LOGINS` set, it
needs a request signed by one of the user's devices: a web page that
unbinds devices moves that to the mobile app. A user who lost every
phone still recovers through `unbinddevices`.

### In the phone modes, the phone manages the second factors

With `CIBA.APPROVE_LOGINS` set, turning the authenticator app on or off,
new recovery codes, and adding or removing a passkey need a request
signed by one of the user's devices (`403 phone_required` otherwise). A
web page that offered them moves them to the mobile app. Users with no
bound device are not affected.

### Choose what a credential change does to the phone

`AUTH.CREDENTIAL_CHANGE_DEVICE_LOGINS` decides what a password change or
reset, and turning the second factor on, do to the phone's login. It keeps
2.1's behaviour by default (`"end"`). With the phone as identity, set
`"keep"`, or `"ask"` to let the user choose ([Credential changes and the phone](auth/logging-in.md#credential-changes-and-the-phone)).

## 2.0 to 2.1

2.1 adds sign-ins, step-ups and payments approved from the user's phone
([Sign-in approvals](auth/ciba.md)). Nothing changes in a project until
it installs the new app, and no existing table changes.

2.1.0 was never published: its release stopped before reaching PyPI.
Upgrade to 2.1.1, the first published 2.1 release, which has the same
changes.

### A client of the identity providers' login calls it by its new name

The identity providers' login, `POST /auth/identity/{name}/login`, is
named `start_provider_login`. Its operation id was `start_login`, which
was the passkey login's too: a project that mounted both apps failed
`check` (`openapi.E001`), and could not start in staging or production.
The path, the method and the answers are unchanged, so a client that
calls the URL changes nothing. A client generated from the OpenAPI
schema calls the method by the new name once you generate it again; code
that builds the URL with `url_path_for("start_login", ...)` names
`start_provider_login`. The passkey login keeps `start_login`.

### The devices app has an extra

`domainkit.contrib.devices` imports `cryptography`, which 2.0 installed
only through another extra. Install `domainkit[devices]`, and
`domainkit[devices,push]` for the new `domainkit.contrib.ciba`. An app
whose extra is missing now says which one to install.

### Phone approvals, if you want them

Install `domainkit.contrib.ciba` after `devices`, make and apply its
migration, and follow [Sign-in approvals](auth/ciba.md). Its strict
modes (`CIBA.APPROVE_LOGINS`) register a login gate. While a gate is
registered, a login that your own code issues without passing the gates
is refused (`LoginNotGated`): call `pass_login_gates` before you issue
one, or `gates_passed(user_id)` for one issued on the server's side
([Login gates](auth/ciba.md#login-gates)).

## 1.8 to 2.0

2.0 removes what 1.x deprecated, and refuses settings that never worked.
Each change below says what to do.

### Codes are the only way to prove an address

1.8 deprecated the links of password resets, email verification, and
signups (ADR 0126); 2.0 removes them. A project that set
`AUTH.VERIFICATION = "code"` (as `init` wrote) changes nothing but the
setting:

1. **Delete the link settings.** `AUTH.VERIFICATION`,
   `PASSWORD_RESET_URL`, `PASSWORD_RESET_TIMEOUT`,
   `EMAIL_VERIFICATION_URL`, `EMAIL_VERIFICATION_TIMEOUT`, and
   `SIGNUP_URL` are refused, each with a message that names it. Codes
   take `AUTH.CODE_TTL` and `AUTH.CODE_ATTEMPTS`.
2. **Move your pages to codes.** `POST /password-reset/confirm`,
   `/email-verification/confirm`, and `/signup/confirm` are gone. Your app
   posts the code instead: `/password-reset/code` with `{"identifier",
   "code", "new_password"}`, `/email-verification/code` with `{"code"}`,
   and `/signup/code` with `{"identifier", "code", "password"}`
   ([Password reset](auth/logging-in.md#password-reset)).
   `POST /password-reset` still takes `{"email"}`, and `{"identifier"}`.
3. **Let the worker drain first.** The tasks
   `domainkit.auth.send_password_reset` and
   `domainkit.auth.send_signup_link` are gone: a message for them still in
   the outbox waits `MESSAGING.UNKNOWN_TARGET_GRACE`, then goes to the dead
   letters. Links already mailed stop working.
4. **Replace your copies of the link templates**, if you wrote any:
   `auth/password_reset`, `auth/email_verification`, and
   `auth/signup_confirm` become `auth/password_reset_code`,
   `auth/email_verification_code`, and `auth/signup_code`, which take a
   `code` in place of a `link`.

`auth.E006` (link URLs over `http://`) is retired with the settings it
checked.

### Internals no longer pass on other modules' names

`domainkit.contrib.webhooks.guard` no longer passes on `MAX_URL_LENGTH`,
`METADATA_ADDRESSES`, `METADATA_HOSTS`, `NAT64`, `judged`, and `refusal`:
import them from `domainkit.http.guard`. They left the public API in 1.0,
importable through 1.x. `UnsafeTarget` stays public, from
`domainkit.contrib.webhooks`.

`apps.E002`, which refuses a `DomainConfig` that still defines a hook
method 1.0 removed, stays in 2.0: without it, an `erase_subject` left
behind would be skipped in silence, and an erasure would report success.

### A home database cannot keep a schema per tenant

`AUDIT.DATABASE`, `MESSAGING.DATABASE`, `IDEMPOTENCY.DATABASE`, and the
database that `DATABASE_ROUTING` gives a shared app (`auth`, `flags`, ...)
must not be one of `TENANCY.SCHEMA_DATABASES`: the application refuses to
start. Such a database holds no tables in its own schema, so the records
made outside a tenant failed. Put the shared tables on another alias, as
[Multi-tenancy](tenancy.md#a-schema-per-tenant-postgresql) shows, and run
plain `migrate` for it.

## 1.7 to 1.8

1.8 adds what ADR 0126 decides: users identified by the fields a project
chooses, and codes instead of links. Nothing changes in a project until
it sets the new options, but the tables change once.

### Run the migrations once

The built-in `User` gets `mobile_number` and `mobile_number_verified`,
and its `username` may be empty. Pending signups get a `mobile_number`,
and a new table, `domainkit_auth_codes`, keeps codes. Make and apply the
revisions:

```bash
python manage.py makemigrations -m "users by email or number"
python manage.py migrate
```

### Choose how users log in, and how they prove an address

Two options, both off by default:

```python
AUTH = {
    "IDENTIFIERS": ("email", "mobile_number"),
    "VERIFICATION": "code",
}
```

- `IDENTIFIERS` names the fields that log in. Without it, only the
  username logs in, as in 1.7. Before you add `email`, find addresses
  that two users share: neither of them can log in by that address
  until you change one, and new users cannot take one that is taken.
- `VERIFICATION` set to `"code"` sends codes of 6 digits instead of
  links. Your app then posts to `/email-verification/code`,
  `/password-reset/code`, and `/signup/code`. Where numbers identify
  users, install `domainkit.contrib.notifications` (`auth.E107`).

`POST /auth/login` takes `identifier`; `username` still works. `init`
writes both options for new projects.

### Links are deprecated

`AUTH.VERIFICATION = "link"` keeps working in 1.x and goes in 2.0. Move
to codes before then.

## 1.6 to 1.7

The security fixes of the outside review's first run (audit A1 to A7)
change what a project, or its clients, must do in seven places. The
changelog marks each one **Breaking:**.

### Let the evidence queue drain first

(A6-2) Deferred evidence is signed now (`SECRET_KEY`), and the worker refuses an
unsigned record: one inserted in the outbox would look the same. Before
deploying, let the worker run until no `domainkit.evidence.record`
message is pending; one left is refused and dies, and its evidence is
deferred again by the change that made it.

### Linking an identity asks for the password

(A2-1) `POST /auth/identity/{name}/link` takes `{"password": "..."}`, or follows
a step-up in the last ten minutes, as adding a passkey does; without
either it answers `401 reauthentication_required`.

### A login at a provider is finished where it started

(A2-2) `POST /auth/identity/{name}/login` and `/link` answer a `binding` with
the authorization URL. Keep it in the page (`sessionStorage`) and send it
with the callback, `{"code", "state", "binding"}`: a callback without it
answers 422, with another `identity_not_linked`. SAML's login sets a
cookie the ACS and `POST /auth/identity/ticket` read: redeem the ticket
with credentials, from the page's site.

### LDAP checks the directory's certificate

(A2-3) `ldaps://` and `START_TLS` check the directory's certificate and name
against the system's certificates. A directory whose certificate a
private authority signs needs `IDENTITY.LDAP.CA_CERTIFICATES`, the file
of that authority's. Staging and production refuse `ldap://` without
`START_TLS` (`security.E020`).

### Staff acting as a user neither make nor decide approval requests

(A3-1) Submitting, approving, rejecting, and cancelling an approval
request under impersonation answer 403.

### Downgrading a tenant's schema needs consent, and is recorded

(A6-3) In staging and production, `migrate --tenant NAME --downgrade`
needs `--allow-data-loss`, as a downgrade of the database's own schema
always did. There, each downgrade that runs is first recorded in the
audit trail (`operator.migrate.downgrade`): name who runs it with `--operator`
or `DOMAINKIT_OPERATOR`. If the record cannot be written, the downgrade
does not run.

### A browser login asks for the captcha too

With `AUTH.CAPTCHA` and `login` in its `ON`, `POST /auth/session` now
asks for the captcha, as `POST /auth/login` does. Browser clients send
the widget's token in `X-Captcha-Token`; without it the answer is
`400 captcha_failed`.

## 1.5 to 1.6

### Signup asks for the user model's other required fields

With `AUTH.SIGNUP_ENABLED` and a user model whose `REQUIRED_FIELDS` name
fields beyond the username and the email (the guide's `phone`),
`POST /signup` now takes them under `fields`, every one, and answers
`422 invalid_user_field` without them: before, it left them at their
defaults. Send them from the signup form, or take a field out of
`REQUIRED_FIELDS` that a new account need not give. Pending signups gain
a nullable `fields` column: `makemigrations`, then `migrate`.

### The audit commands are one

Cron jobs and runbooks that call the audit commands use their new names:

| Before | Now |
| --- | --- |
| `auditlog` | `audit log` |
| `auditlog --target T`, `auditlog --aggregate FIELD=ID` | `audit history --target T`, `audit history --aggregate FIELD=ID` |
| `auditseal` | `audit seal` |
| `auditverify` | `audit verify` |
| `auditverify --check-export FILE --public-key PEM` | `audit check-export FILE --public-key PEM` |
| `auditprune --keep-days N` | `audit prune --keep-days N` |
| `auditforget SUBJECT` | `audit forget SUBJECT` |

The trail keeps recording `operator.auditprune` and `operator.auditforget`;
the request path of an operator's run names the action (`audit forget`).

### `messages` takes an action

| Before | Now |
| --- | --- |
| `messages` | `messages status` |
| `messages --pending` (`--held`, `--locked`, `--dead`) | `messages list pending` (`held`, `locked`, `dead`) |
| `messages --retry ID` (or `all`) | `messages retry ID` (or `all`) |
| `messages --cancel ID` | `messages cancel ID` |
| `messages --release` | `messages release` |
| `messages --database ALIAS ...` | `messages ... --database ALIAS`, after the action |

Only `retry`, `cancel`, and `release` take `--operator`.

### `evidence` takes an action

`evidence --subject S` is `evidence list S`, and `evidence --verify KEY` is
`evidence verify KEY`. `webhooks` keeps its actions; each takes only its
own options now, and only `replay` takes `--operator`.

### `--operator` follows the action

Every command of several actions takes `--operator` after its action,
and only on the actions that change something: `privacy --operator alice
purge` is `privacy purge --operator alice`; `flags --operator alice set
beta` is `flags set beta --operator alice`. Listings and checks
(`flags list`, `accounting verify`, `approvals list`, `limits list`)
take none.

## 0.12 to 1.0

1.0 is the first release since 0.12.1. Everything 0.13, 0.14, and 0.15
changed reaches a project with it, as one upgrade. Most of 1.0 is new and
asks nothing until a project turns it on: money, the ledger, encrypted
fields, approvals, limits, step-up. This section lists what does change
for a 0.12 project, in the order to apply it. No setting of 0.12 changed
its default but `ERROR_FORMAT` and `OPENAPI_OPERATION_IDS` (below), and no
dependency's range changed but the `immudb` extra's protobuf floor.

1. **On 0.12.1, clear its deprecations.** Run the tests with
   `-W error::domainkit.deprecation.DomainkitDeprecationWarning`. Each
   warning 0.12 gives is a removal in 1.0: a `DomainConfig` hook method
   (`startup`, `erase_subject`, ...) moves to a function registered in
   `ready()`, and `expect_version` to `uow.expect` ([Removed](#removed-in-10-deprecated-in-012)).
2. **Change what 1.0 changes** for the project, from the lists below: the
   problem details, the personal tokens' re-authentication, the `CurrentUser`
   routes, the amounts in SQL, the tests that reach the database without
   `db`. Searching the project for each name finds them.
3. **Raise domainkit**: `domainkit[...]>=1.0,<2` in `pyproject.toml`, and
   install. The extras are the same, with `keys` (encrypted fields) and
   `audit` (signed checkpoints) for the features that need them.
4. **Make the revision before running anything.**
   `python manage.py makemigrations -m "domainkit 1.0"` writes one
   revision for the contrib apps the project installs: auth's
   `domainkit_step_ups`, `domainkit_sca_challenges`, and
   `domainkit_password_history`; the outbox's `ordering_key` and
   `sequence` columns, its index `ix_domainkit_messages_ordering`, and
   `domainkit_ordering_keys`. It only adds: 0.12's code runs against the
   new schema. Review it, and `migrate`. Until the database has it, the
   tests fail (`no column named ordering_key`), workers cannot claim, and
   deleting a user fails.
5. **Run the tests**, deprecations as errors: the names 1.0 deprecated
   ([below](#deprecated-removed-in-20)) fail with their replacement, and
   a test that reaches the database without requesting `db` fails with
   `DatabaseAccessRefused`, naming the fixture to request.
6. **`python manage.py check`** with the production settings and
   variables. 1.0 refuses a route that only reads `CurrentUser`
   (`security.E101`), and warns about tasks without a name
   (`messaging.W001`).
7. **Deploy in this order**: run the migration job; stop every 0.12
   worker; start the 1.0 workers; then roll out the 1.0 web processes. A
   0.12 worker finishes a message with an ordering key without making the
   next of its key pending, so the key waits for a 1.0 worker's cleanup
   round (or `messages --release`); and a worker that does not know a
   message's task waits for one that does
   ([operations.md](operations.md#migrations-in-a-rollout)).

### Removed in 1.0 (deprecated in 0.12)

Each was deprecated in 0.12, with a warning that named its replacement.

- **A domain that defines a removed hook method fails `setup()`**
  (DSGN-03). `startup`, `shutdown`, `check`, `seed`, `erase_subject`,
  `export_subject`, `check_application`, `deployment_warnings`, `stage`,
  `committed`, `stage_transaction`, `transaction_committed`, and
  `migrated` are no longer methods of `DomainConfig`, and nothing calls
  them. A configuration that still has one of these names, written in its
  class or a base, set on the class later, or on the instance, is refused
  with `apps.E002`, which names the domain, the class, and the point:
  `Domain 'search': SearchConfig.erase_subject is a hook method, which
  domainkit 0.13 removed; nothing would call it.` **Fix:** register the
  function in `ready()` under a name of its own, as 0.12's warning said:

  ```python
  class SearchConfig(DomainConfig):
      def ready(self) -> None:
          on_erase_subject(erase_documents)  # domainkit.apps.hooks
  ```

  The refusal stays, in 2.0 too, so a method left behind is
  never silently skipped: an `erase_subject` nothing called would keep a
  person's data through an erasure that reports success
  ([ADR 0111](adr/0111-refuse-a-removed-hook-method-until-1-0-wherever-the-domain-defines-it.md)
  and its amendment).
- **`HookPoint` takes no `method=`** (DSGN-03). It named the
  `DomainConfig` method a point called too; `HookPoint("x", method="...")`
  raises `TypeError`. 0.12 did not warn of it. **Fix:** drop the
  argument: `HookPoint("x", arguments=1, awaited=True)`.
- **`expect_version` is gone** (DSGN-14). `from
  domainkit.ddd.preconditions import expect_version` raises `ImportError`,
  and no expectation is held for the whole context any more. **Fix:** hold
  it on the unit of work the service changes the aggregate through:
  `with uow.expect(Order, order_id, versions):` in place of `with
  expect_version(Order, order_id, versions):`. The expectation is still checked
  again in each transaction of the unit of work (SEC-12a-6).

### The schema and the deploy

- **Auth has three new tables** (FIN-29, FIN-30, FIN-42):
  `domainkit_step_ups`, `domainkit_sca_challenges`, and
  `domainkit_password_history`. Only `StepUp`, the step-up and
  transaction routes, `PASSWORD_HISTORY`, and
  `PASSWORD_CHANGE_ON_FIRST_USE` read and write them; but deleting or
  erasing a user deletes the user's rows there, and the hourly purge of
  tokens and `cleartokens` clear old ones, so each fails until the
  database has them. **Fix:** run `makemigrations` and `migrate` in each
  project that installs auth, before 1.0 serves.
- **Messaging's outbox gained columns, a status, and a table** (FIN-19).
  The outbox has `ordering_key` and `sequence` columns, a `held` status,
  and an index, `ix_domainkit_messages_ordering`, and messaging has a
  table of ordering keys, `domainkit_ordering_keys`, in every database
  that holds an outbox. Workers claim through the new columns, so they
  fail until the database has them. **Fix:** run `makemigrations` and
  `migrate` in each project that installs messaging, before 1.0's
  workers start. Stop the 0.12 workers before any 1.0 process (a web
  process too) writes a message with an ordering key: a 0.12 worker
  finishes such a message without making the next of its key pending,
  and the key waits until a 1.0 worker's cleanup round, or
  `messages --release`, releases it (SEC-14c-3). `messages` prints a
  `held` line after `pending`: a script that reads its lines by position
  reads them by name instead. Nothing keeps the old schema.
- **A message for an unknown task or handler waits before it dies**
  (CONC-15). It is given back, its attempt not spent, until
  `MESSAGING.UNKNOWN_TARGET_GRACE` (an hour) has passed, then
  dead-lettered as before. A message whose target no deploy brings back
  is dead-lettered an hour later, and an ordered key waits behind it
  meanwhile. **Fix:** nothing, usually; `MESSAGING =
  {"UNKNOWN_TARGET_GRACE": 0}` keeps the old behaviour. Deploy workers
  before web processes when a release adds tasks or handlers, and name
  tasks (`@task(name=...)`): `check` warns about the others where
  deployed (`messaging.W001`).
- **`migrate` makes the audit trail's triggers afresh on every run**
  (FIN-74). With `domainkit.contrib.audit` on PostgreSQL, the triggers
  that keep `domainkit_audit_log` and `domainkit_audit_seals`
  append-only, and their functions, are made again after each `migrate`,
  so one dropped, disabled, or changed is put back. A revision that calls
  `protect(op)` still applies. **Fix:** run `migrate` as the role that
  owns the tables, as before; an `unprotect(op)` lasts until the next
  `migrate`.
- **Revisions are rendered in batch mode** (DATA-13), on PostgreSQL too,
  so they apply on SQLite. Nothing to do; a revision written before is
  unchanged.
- **SQLite checks foreign keys** (DATA-18). Every connection runs
  `PRAGMA foreign_keys = ON`: a row naming no parent is refused, as on
  PostgreSQL, and `ON DELETE CASCADE` deletes children. `migrate` checks
  the rows already stored and refuses, naming the tables, rows that name
  a missing parent. **Fix:** delete or repair those rows (`PRAGMA
  foreign_key_check` lists them), then migrate again; a test that stored
  a child without its parent stores the parent first.
- **The `immudb` extra needs protobuf 6.33.5 or later** (INFRA-15). The
  floor was 5.27.0, which has two advisories. **Fix:** a project that pins
  protobuf below 6.33.5 lifts the pin; immudb's SDK works with 6.33.5 to
  7.x.

### Errors and the API

- **Errors are RFC 9457 problem details by default** (DSGN-26).
  `ERROR_FORMAT` defaults to `"problem+json"`: every error is
  `application/problem+json` with `type`, `title`, `status`, and
  `instance`, and keeps `detail`, `code`, `request_id`, and `errors`
  where they were. A client that reads those fields is unaffected; one
  that checks for `application/json` is not. **To keep the old
  behaviour:** `ERROR_FORMAT = "domainkit"`.
- **Operation ids are route names by default** (DSGN-26).
  `OPENAPI_OPERATION_IDS` defaults to `"route_name"`, as every generated
  project already set it, so a generated client's method names change for
  a project that did not. **To keep the old behaviour:**
  `OPENAPI_OPERATION_IDS = "fastapi"`.
- **Every 429 answers `rate_limited`** (WEB-25). A route's `Throttle` and
  auth's code, link, and signup throttles answered `throttled`, and its
  login and password throttles `login_throttled`. **Fix:** a client that
  switched on either switches on `rate_limited`, or on the status, and
  waits as `Retry-After` says. Every code is listed in
  [reference/problems.md](reference/problems.md).
- **`If-Match: *` no longer satisfies `require_if_match`** (WEB-22): it is
  `428 precondition_required`, as a write without `If-Match`. **Fix:**
  clients send the `ETag` of the version they change.
- **A lost race is a 409, wherever it surfaces** (FIN-78). A lost
  optimistic race flushed before the commit (by a query, a savepoint, or
  an `on_stage` hook such as the audit trail's) raised SQLAlchemy's
  `StaleDataError` and answered 500; it is `ConcurrentUpdate`, 409
  `concurrent_update`, and `uow.run(retry=...)` runs it again. **Fix:**
  nothing; a client that retried on 500 retries on 409.
- **A live connection's lifetime is at most a day** (WEB-24):
  `WEBSOCKET_MAX_LIFETIME`, `SSE_MAX_LIFETIME`, and a route's
  `LiveLimits(max_lifetime=...)` above 86,400 seconds are refused at
  startup. **Fix:** lower them; clients reconnect. A route that turns
  re-authorization off (`LiveLimits(reauth_interval=None)`) is warned
  about where deployed (`live.W002`).

### Users and logins

- **A route that only reads `CurrentUser` declares no rule** (DSGN-17).
  Staging and production refuse to start with one (`security.E101`), and
  `check` lists it. **Fix:** add `dependencies=[Public]` to a route that
  anyone may call, or take `AuthenticatedUser` where a login is meant.
- **Making a personal token asks the user to authenticate again, and the
  token ends** (AUTH-16). `POST /auth/tokens` needs a step-up within 5
  minutes from users with a second factor, and the `password` in the body
  from the others; tokens work `AUTH.PERSONAL_TOKEN_MAX_AGE` (90 days) at
  most, and `expires_in_days: null` is refused; `login` is no longer a
  token's name. **Fix:** clients send the password, or step up first;
  `AUTH = {"PERSONAL_TOKEN_MAX_AGE": None}` keeps tokens that never end.
  Tokens already made keep their lifetime.
- **An account takes 100 wrong passwords a day at most** (AUTH-19), from
  `AUTH.ACCOUNT_LOGIN_RATE`, beside `LOGIN_RATE`. A user whose account a
  stranger floods logs in from the browsers they used before; a new one
  waits for the day, or a password reset. **Fix:** nothing, or set the
  rate (`None` keeps 0.12's, with no daily bound, and `check` warns of it
  where deployed, `auth.W006`).
- **Turning a second factor on ends the user's other logins and tokens**
  (AUTH-20), personal tokens and registered certificates
  included. **Fix:** make personal tokens, and register certificates,
  after turning it on.
- **An open live connection no longer keeps its login alive**
  (SEC-15c-2). With `AUTH.IDLE_TIMEOUT`, the re-checks of an open
  WebSocket or event stream wrote the login's last use, so a page left
  open kept its login for good; they write none now, and the connection
  closes once its login idles out. Nothing changes without
  `IDLE_TIMEOUT`. **Fix:** a page that should stay open sends a request
  now and then, or the project raises `IDLE_TIMEOUT`.
- **A JSON Web Token issued longer ago than `AUTH.IDLE_TIMEOUT` is
  refused** (SEC-15c-1), whatever its `ACCESS_TOKEN_TTL`: a JSON Web Token
  login's use is its refreshes. Nothing changes without `IDLE_TIMEOUT`, or
  with an access token that lives no longer than it (the default, 5
  minutes, is shorter than the 5-minute floor plus its tenth). **Fix:**
  refresh before the timeout, or raise it.

### The worker and tasks

- **Task names are at most 255 characters** (CONC-16): `@task` and
  `@periodic` refuse a longer `name=` (or module path) with
  `ImproperlyConfigured`, and `event_bus.subscribe(..., durable=True)` a
  durable handler whose `module.QualName` is longer, with `ValueError`.
  PostgreSQL refused their messages anyway. **Fix:** a shorter `name=`, or
  a shorter module or name for the handler.
- **A handler's own `CancelledError` is a failure** (CONC-17). One raised
  while the worker is not stopping (a cancelled task or future awaited)
  is retried with backoff, counts as an attempt, and dead-letters the
  message after `max_attempts`, as `HandlerCancelled`; it used to be
  retried at once, uncounted, for ever. **Fix:** nothing; a handler that
  cancels on purpose raises another error.
- **`Worker`'s mechanics are private** (DSGN-22). `claim`, `relay`,
  `relay_ids`, `take_over`, `process`, `schedule_periodic`, and
  `relay_promoted` start with `_`. **Fix:** run a worker with `run` or
  `run_once`; `contrib.messaging.admin` releases stalled messages.

### Amounts in SQL

- **An amount in SQL is multiplied or divided by a number, not by another
  amount** (TEST-11). `amount * other` and `amount / other`, where `other`
  is an amount too (an `ExactDecimal` column, or a parameter or literal
  typed as one), raise `TypeError` when the expression is built. On
  SQLite they gave minor units times minor units (9.95 times a typed 2
  was 1990.00); on PostgreSQL they gave the product. **Fix:** give the
  factor as a number: a plain `Decimal` or an untyped
  `bindparam("rate")`, or a column declared `Integer` or `Numeric` (a
  rate is not an amount); `+` and `-` between amounts are unchanged.
- **A factor of an amount in SQL has at most 9 places** (TEST-07).
  `amount * factor` and `amount / factor`, with `factor` a value or a
  bound parameter, refuse a factor of more places (`ValueError`), where
  they used to compute with it (and on SQLite counted a bound one as an
  amount). **Fix:** round the factor to 9 places with the rule the domain
  needs, or compute the product in Python with `Money.multiply`.

### Tests

- **A test that did not request the database is refused it** (TEST-08).
  The pytest plugin raises `domainkit.db.DatabaseAccessRefused` when a
  test reaches the test databases without requesting `db`, or a fixture
  that does (`client`, `async_client`, `live_server`, ...); the message
  names the fixture. 0.12 let such a test read whatever the test before
  it left, and, run first, reach the databases the test settings name.
  **Fix:** request `db` in the test, or in its function-scoped fixtures
  that reach the database; a module- or session-scoped fixture opens the
  databases with `with database_blocker.unblock():`. **To keep the old
  behaviour:** `@pytest.mark.allow_database` on a test, or `pytestmark =
  pytest.mark.allow_database` in a module; or, for the whole suite,
  `domainkit_allow_database = true` under `[tool.pytest.ini_options]`.
- **The `settings` fixture reads settings read-only** (TEST-19): a test
  that changed a mapping in place (`settings.CACHES["x"] = ...`) gets
  `TypeError`. **Fix:** assign the whole setting,
  `settings.CACHES = {**settings.CACHES, "x": {...}}`, which is validated
  and undone when the test ends.

### Names that left the public API

- **Internals left the public API** (DSGN-22). No longer listed, still
  importable unchanged until 2.0, then gone: `domainkit.http.routing`
  (it rests on FastAPI's internals; `FLATTEN_ROUTERS` is the public
  switch), `domainkit.contrib.webhooks.guard` (`UnsafeTarget` stays in
  `domainkit.contrib.webhooks`), `http.responses.BuiltJSONResponse`,
  `http.CURSOR_PATTERN`, `http.middleware.request_path`,
  `db.create_engine` and `db.mapper_registry`, `conf.describe_errors`
  and `unwrap_settings`, `storage.check_name` and `new_name`,
  `lifecycle.shutdown_timeout`, `debug.statement_shape`,
  `architecture.iter_imports`, `FRAMEWORK_PACKAGES`, `LAYERS`,
  `DOMAIN_ALLOWED_PREFIXES`, and `project_rules`, the device-proof
  helpers of `contrib.auth.devices` but `DEVICE_COOKIE`, and
  `audit.checkpoints`' `FORMAT`, `EXPORT_FORMAT`, `key_id`,
  `load_public_key`, and `signing_key`. **Fix:** copy what a project used.
- **`Credentials` takes `login=`, not `session=`** (DSGN-28). The field
  that holds a JWT login's family is `login`; reading `session` still
  works, with a warning, until 2.0. **Fix:** a custom authentication
  class that builds `Credentials(..., session=...)` passes `login=`.

### Deprecated, removed in 2.0

Each old name worked through 1.5, warning with
`DomainkitDeprecationWarning`; the release after 1.5 removed them, so each
name has one home (DSGN-23, DSGN-24, DSGN-29):

- `on_check` of `domainkit.apps.hooks` is `on_ready`: it is the readiness
  probe, not `manage.py check`'s.
- `DomainConfig.RETENTION` is `retention`, as its siblings are written.
- `MESSAGING.SHUTDOWN_TIMEOUT` is `MESSAGING.DRAIN_TIMEOUT`, apart from
  the process's `SHUTDOWN_TIMEOUT`.
- `domainkit.contrib.messaging.dbclock` is `domainkit.db.dbclock`.
- `domainkit.contrib.contenttypes`' `DEFAULT_PERMISSIONS`,
  `InstalledModel`, `ModelOptions`, `installed_models`, and
  `model_options` are `domainkit.db.models`' (`content_type_key` is
  `model_key`).
- `contrib.webhooks.WebhookSettings` is `WebhooksSettings`.
- `Credentials.session` is `Credentials.login`.

`apps.E002`, which refuses a hook method 1.0 removed, stays in 2.0 too:
without it, an `erase_subject` left behind would be skipped in silence.

### Projects that ran 0.13 to 0.15 from source

The money API, the ledger, keys and encrypted fields, approvals, limits,
and step-up are new in 1.0 for a 0.12 project. A project that used them
from `main` before 1.0 also meets these changes, made before any release
had them.

- **Names renamed before release** (DSGN-23, DSGN-28,
  [ADR 0125](adr/0125-freeze-the-public-api-for-1-0-renaming-what-was-never-published.md)),
  with no alias:

  | Was | Is |
  | --- | --- |
  | `contrib.accounting.on_booking`, `accounting.hooks.on_posting` | `accounting.hooks.on_stage_entry`, `on_post_entry` |
  | `apps.hooks.on_erase_check` | `apps.hooks.on_retain_subject` |
  | `contrib.accounting.CAPTURES`, `REVERSALS` (sources) | `CAPTURE_SOURCE`, `REVERSAL_SOURCE` |
  | `contrib.approvals.ENTRIES`, `CAPTURES`, `REVERSES`, `SUPERUSERS` (actions) | `POST_ENTRY`, `CAPTURE_HOLD`, `REVERSE_ENTRY`, `GRANT_SUPERUSER` |
  | `AccountIsClosed`, `PeriodIsClosed` | `AccountNotOpen`, `PeriodNotOpen` (codes unchanged) |
  | `ReversalNotReversible` (`reversal_not_reversible`) | `EntryNotReversible` (`entry_not_reversible`) |
  | `approvals.PolicyGone` (`policy_not_found`) | `PolicyRemoved` (`policy_removed`) |
  | `approvals.on_guard` | not public: `guard()` registers |
  | `contrib.limits.check` | `check_movements` |
  | `ACCOUNTING.RETRY_ATTEMPTS` | `ACCOUNTING.MAX_ATTEMPTS` |
  | `AUTH.PASSWORD_PRESET` | `AUTH.PRESET` |
  | `keys.ring`'s `FORMAT`, `forget_held`, `sealed_key`; `keys.aws`' `sign` and credential loaders | not public (DSGN-22) |

- **A backend that does not list its grants on objects leaves users to
  superusers** (SEC-16a-5). While a backend of `AUTH.BACKENDS` other than
  `ModelBackend` keeps `BaseBackend.object_grants`, only superusers change
  users and groups through the admin API, and `check` warns
  (`auth.W007`). **Fix:** answer `object_grants(holder)` in each such
  backend: with the grants it holds, or an empty set if it grants on no
  object.
- **Accounts may not go negative unless opened so** (FIN-97).
  `may_go_negative` defaults to `False` (`Account.open`, `OpenAccount`,
  `POST /accounts`): an account opened without it refuses what would
  leave less than nothing available. **Fix:** open clearing, settlement,
  and external accounts with `may_go_negative=True`; those already stored
  keep the flag they were opened with.
- **A currency a threshold or a limit does not name is refused**
  (FIN-89). With `APPROVALS.ENTRY_THRESHOLDS` set, an entry in another
  currency needs approval; a subject bounded by limits in some currency
  is refused (`currency_not_limited`) a movement in another. **Fix:**
  name every currency the ledger or the project moves, or give an amount
  for all of them: `"*": "10000"` in `ENTRY_THRESHOLDS`, `"currency": "*"`
  in a default of `LIMITS.DEFAULTS`.
- **The books are closed up to the latest closed period's end** (FIN-87).
  No entry is dated on or before it, in a period or in a gap, whatever
  `REQUIRE_PERIOD` says; no period is opened there; and a period closes
  only once those before it are closed (`earlier_period_open`). **Fix:**
  close periods in date order; date corrections in an open period, as
  reversals.
- **Reversals over the thresholds need approval** (FIN-88). With
  `ENTRY_THRESHOLDS`, reversing an entry over them is refused
  (`approval_required`): submit `accounting.reverse_entry` with
  `{"entry_id": ..., "booked_on": ...}`; its makers need
  `accounting.reverse_entry`. An approved reversal is counted against
  limits and frees nothing. **Fix:** route large corrections through
  approvals.
- **Limits keep the movements of bounded subjects only** (PERF-26,
  revising SEC-15b-5). A subject no window bounds keeps no usage; a limit
  set, changed, or enabled mid-window counts the ledger's postings since
  its start, for accounts and holders. An actor's movements, and those a
  project counts with `consume` outside the ledger, count from when a
  window bounds them. **Fix:** set limits before their window starts
  where it matters for actors and a project's own kinds; old usage rows
  of unbounded subjects age out with `KEEP_USAGE`.
- **`EntryPosted` has `amounts` and `digest`** (FIN-96), both required.
  **Fix:** code that builds an `EntryPosted` itself passes them; the
  ledger does.
- **Erasure keeps the books and the approvals, pseudonymised** (FIN-94).
  `privacy erase` is held (`ErasureHeld`; the command names why) while
  the person holds an open account with money on it or held; done, their
  accounts name one `erased:` holder instead of none, and their approval
  requests keep their commands, sealed again for the pseudonym. **Fix:**
  close a customer's accounts before erasing them; register
  `on_retain_subject` in a domain whose records a law asks to keep.
- **Erasure closes the person's accounts, and checks again as it erases**
  (SEC-16b-4). The ledger's `on_erase_subject` hook raises `ErasureHeld`
  when money landed after the check, and closes the open accounts it
  pseudonymises (all empty by then); the hooks of the domains that hold
  erasures run before anything else is erased. **Fix:** nothing, unless
  an `on_erase_subject` hook of a domain with an `on_retain_subject`
  relied on running after the aggregates were erased.
- **The ledger's movements are counted against limits at the commit**
  (SEC-16b-2). `enforce` works an entry's movements out as it is booked;
  `count_staged` (an `on_stage` hook) counts them as the posting commits,
  after its other `on_stage_entry` hooks and its own work, under a
  shared lock of each ledger kind that counting back takes alone. A
  refusal (`limit_exceeded`) is still raised by `post_entry` and the
  other services, and still rolls the posting back. **Fix:** an
  `on_stage_entry` hook that read the entry's usage in the same
  transaction reads it after the commit. Limits add the table
  `domainkit_limits_kept` (SEC-16b-9): run `makemigrations` and `migrate`.
- **Removing a limit counts back** (SEC-16b-3) for the limit of its code
  that then applies; a movement of more than usage keeps (14 digits) is
  refused, `MovementOutOfRange` (422 `amount_out_of_range`, SEC-16b-5).
- **`maker_rule` is checked again when an approved request runs**
  (SEC-16b-7), on the maker with no credentials. **Fix:** a guard whose
  `maker_rule` reads the submitting request (scopes, a step-up) checks
  that in its route instead.
- **`accounting verify` compares the books with the trail** (SEC-16b-6):
  with `domainkit.contrib.audit`, an entry not as its `EntryPosted`
  recorded it is `entry_differs`, and fails the command. **Fix:** nothing;
  investigate what it names.
- **A hold's default expiry is on the database's clock**: `HOLD_TTL`
  counts from the database's present on PostgreSQL, and a given
  `expires_at` is checked against it, as its expiry is (FIN-99).
- **Derived schemas leave `Encrypted` fields out** (DSGN-19). `schema_for`
  without `include`, `crud=True` with `writable=ALL_FIELDS`, and the admin
  no longer show or take a field stored encrypted. **Fix:** name it where
  it should be served: `schema_for(Card, include=["id", "number"])`,
  `writable=[..., "number"]`, or the admin's `schema=`; a create body that
  needs it names it in `writable`.
- **A missing money check is added** (DATA-20). `makemigrations` writes
  the both-or-neither check of every nullable money pair the database
  lacks, also of a pair made optional. **Fix:** run `makemigrations` and
  `migrate`; a row holding half a pair fails the check: repair it first.

## 0.11 to 0.12

### Breaking changes

Each changes what a project gets without changing its code. Each says how
to keep the old behaviour, for as long as the project needs it.

- **A hook registered with the wrong arguments fails `setup()`**
  (DSGN-03). The points of `domainkit.db.hooks` (`on_stage`, ...) check a
  function when it registers: it must take the arguments the point calls
  it with, and be an `async` function. 0.11 accepted it and failed the
  first commit or `migrate` that called it. **Fix:** give the hook the
  point's arguments (`async def hook(uow, events, tasks)` for `on_stage`).
  A `HookPoint` of a project's own checks nothing unless it is made with
  `arguments=` or `awaited=`.
- **`dumpdata` leaves guarded tables out** (DATA-12). A dump of every
  table leaves out users and their password hashes, groups, permissions,
  content types, object grants, and dynamic settings, and says so on
  standard error; naming one, or a domain that holds one, is refused.
  **Fix:** dump them on purpose: `dumpdata --include-guarded`, which is
  recorded in the audit trail and, in staging and production, needs
  `--operator` or `DOMAINKIT_OPERATOR`. **To keep the old behaviour:**
  `--include-guarded` on every dump that should hold them.
- **Test tokens need the test settings** (SEC-12a-2). `user_token`,
  `token_for`, and the clients' `authenticated` and `authenticated_as`
  (`client_for`, `admin_client`) raise `ImproperlyConfigured` unless
  `ENVIRONMENT = "test"`: they sign users in without credentials.
  **Fix:** run the code that uses them with the test settings, as the
  database fixtures already require. There is no setting to keep the old
  behaviour.
- **Fixtures load only password hashes the configured hashers would
  make** (SEC-12a-3). A user's hash that no hasher of
  `AUTH.PASSWORD_HASHERS` identifies, or that it would make again more
  strongly (fewer iterations, a shorter salt), refuses the fixture.
  **Fix:** dump and load users between environments with the same
  hashers, or write unusable passwords (`!...`) in the fixture and reset
  them after loading. There is no setting to keep the old behaviour.
- **Dumps of personal data name their operator where deployed**
  (SEC-12a-7). In staging and production, `dumpdata` of a table whose
  model has fields marked `Personal` needs `--operator` or
  `DOMAINKIT_OPERATOR`, and is recorded. **Fix:** set `DOMAINKIT_OPERATOR`
  where `dumpdata` runs. There is no setting to keep the old behaviour.

### Deprecated, removed in 0.13

Each still works in 0.12, with a `DomainkitDeprecationWarning` that names
its replacement.

| Deprecated | Use instead | Audit id |
| --- | --- | --- |
| Overriding a hook method of `DomainConfig`: `startup`, `shutdown`, `check`, `seed`, `erase_subject`, `export_subject`, `check_application`, `deployment_warnings` | A function registered in `ready()` on the point of the same name: `domainkit.apps.hooks.on_startup(...)`, `on_shutdown`, `on_check`, `on_seed`, `on_erase_subject`, `on_export_subject`, `on_check_application`, `on_deployment_warnings`. The warning names the domain and the point, at the method's line | DSGN-03 |
| Overriding `stage`, `committed`, `stage_transaction`, `transaction_committed`, or `migrated` (0.10 added the points, without a warning) | `domainkit.db.hooks.on_stage(...)`, `on_committed`, `on_stage_transaction`, `on_transaction_committed`, `on_migrated` in `ready()` | DSGN-03 |
| `with expect_version(Order, order_id, versions):` (`domainkit.ddd.preconditions`), which every unit of work of the context honoured | `with uow.expect(Order, order_id, versions):` on the unit of work the service changes the aggregate through | DSGN-14 |

`DomainConfig.databases` and `holds_table` stay methods: they answer where
a domain's tables are ([ADR 0101](adr/0101-register-every-domain-hook-and-check-it-when-it-registers.md)).
A method moves like this:

```python
# 0.11
class SearchConfig(DomainConfig):
    async def startup(self) -> None:
        await index.connect()


# 0.12
from domainkit.apps.hooks import on_startup


class SearchConfig(DomainConfig):
    def ready(self) -> None:
        on_startup(index.connect)
```

## 0.10 to 0.11

### Breaking changes

Each changes what a project gets without changing its code. Each says how
to keep the old behaviour, for as long as the project needs it.

- **Scoped credentials need a rule that checks scopes** (AUTH-01).
  `SCOPED_CREDENTIALS` defaults to `"refuse"`: credentials limited to
  scopes, on a route that declares no rule that checks scopes, get `403
  permission_denied`, where 0.10 served them with a warning naming the
  route. **Fix:** add `HasScope(...)`, a permission rule, or
  `AllowScoped()` to the route, or to a router it is included through.
  **To keep the old behaviour:** `SCOPED_CREDENTIALS = "warn"`, which
  serves them with the security log line, without the deprecation
  warning.
- **Anonymous idempotency keys are refused** (DATA-09).
  `IDEMPOTENCY.ANONYMOUS` defaults to `"refuse"`: an `Idempotency-Key`
  sent with no actor and none of `CREDENTIAL_HEADERS` gets 400
  `idempotency_key_needs_caller`, and the request does not run. A
  credential header in which authentication found no one
  (`Authorization: Bearer null`, a gateway's fixed `X-API-Key`) names no
  caller either (SEC-11a-1). **Fix:**
  the client authenticates, or sends the request without the key (a
  login or a sign-up, which has no caller yet, sends none).
  **To keep the old behaviour:** `IDEMPOTENCY = {"ANONYMOUS":
  "per-address"}`, which scopes such keys to the caller's address, shared
  by every caller behind one NAT or proxy. `check` no longer warns
  `idempotency.W002`: take it out of `SILENCED_CHECKS` if it is there.
- **`crud=True` needs the fields it writes** (WEB-09).
  `ResourceRouter(crud=True)` without `writable=`, or without both
  `create=` and `update=`, raises `TypeError` when the router is built,
  where 0.10 warned. **Fix:** `writable=[...]` naming the fields the
  writes take, or both schemas. **To keep the old behaviour:**
  `writable=ALL_FIELDS`, which takes every plain field.
- **`loaddata` only adds rows** (DATA-12). A fixture with a row whose
  primary key is stored is refused, and nothing is loaded. **To keep the
  old behaviour:** `loaddata --replace`.
- **Guarded tables** (DATA-12). `loaddata` refuses a fixture that writes
  users, groups, permissions or their links, content types, object
  grants, or dynamic settings, and `flush` keeps those tables, with
  tokens, TOTP authenticators, and recovery codes. **To keep the old
  behaviour:** `--include-guarded` on either command.
- **A named operator for `loaddata` and `flush` where deployed**
  (DATA-12). In staging and production the login name no longer names the
  operator of these two commands. **Fix:** `--operator NAME`, or
  `DOMAINKIT_OPERATOR` set where they run.
- **Request ids from trusted proxies only** (WEB-13). A caller's own
  `X-Request-ID` is dropped, and its request gets a new id. **Fix:** have
  the proxy listed in `TRUSTED_PROXIES` set the header, overwriting a
  client's (nginx: `proxy_set_header X-Request-ID $request_id;`), or set
  `ACCEPT_REQUEST_ID = "never"` if it passes headers through as they
  came. **To keep the old behaviour:** `ACCEPT_REQUEST_ID = "anyone"`.
- **Attempts counted in each process refuse to start** (AUTH-09). With
  `domainkit.contrib.auth` installed, staging and production refuse to
  start, and `check` fails (`auth.E105`, which `SILENCED_CHECKS` cannot
  name), while `RATE_LIMIT_BACKEND` is a `MemoryBackend`, the default:
  each process would allow `LOGIN_RATE` on its own. **Fix:**
  `RATE_LIMIT_BACKEND = "domainkit.http.ratelimit.RedisBackend"`, with
  `RATE_LIMIT_CACHE` naming a Redis cache every process shares. **To keep
  the old behaviour** (one process): `AUTH = {"REQUIRE_SHARED_THROTTLES":
  False}`, which warns (`auth.W004`).
- **Attempts that cannot be counted are refused** (AUTH-09). In staging
  and production, while Redis is down, logins, second-factor codes,
  password changes, and requests that send mail answer `503
  throttle_unavailable` ([runbook.md](runbook.md#logins-are-refused-while-redis-is-down)).
  **To keep the old behaviour:** `AUTH = {"THROTTLE_FAIL_CLOSED": False}`.
- **Committed idempotency keys outlive their TTL** (FIN-84). With
  `domainkit.contrib.idempotency` installed, a key whose request committed
  (or pinned it for a commit on another database) is kept as a tombstone
  for `IDEMPOTENCY.TOMBSTONE_SECONDS` (7 days) once `TTL_SECONDS` passes,
  without its response: a retry with the same key and request gets 409
  `idempotency_response_unavailable` instead of running again, and another
  request with the key gets 422 `idempotency_key_reused`. **Fix:** run
  `makemigrations` and `migrate` for the keys table's new `tombstone`
  column before the new version serves requests; clients that reuse a key
  on purpose send a new one. **To keep the old behaviour:**
  `IDEMPOTENCY = {"TOMBSTONE_SECONDS": 0}`.
- **Messaging's new table, and stale commits refused** (FIN-80).
  Messaging has an inbox table, `domainkit_inbox`, in every database that
  holds an outbox: projects that install messaging run `makemigrations`
  and `migrate` before deploying, or each worker's cleanup logs a failure
  every round. A unit of work that a durable handler or task commits on
  its outbox's database, after another worker took its message over (the
  run outlived its lease), now raises `LeaseLost` and rolls back; the
  worker holding the message runs it. Before, both runs committed. **Fix:**
  let `LeaseLost` propagate from handlers that catch errors around their
  commit. Nothing keeps the old behaviour: it committed the work twice.

### Removed in 0.11

Each was deprecated in 0.10, with a warning that named its replacement.

| Removed | Use instead | Audit id |
| --- | --- | --- |
| `from domainkit.contrib.auth import IsAuthenticated` (and `AllowAny`, `BasePermission`, `HasObjectPermissions`, `HasPermissions`, `HasScope`, `IsAuthenticatedOrReadOnly`, `IsUnscoped`, `ModelPermissions`, `NotAuthenticated`, `Public`, `Require`, `check_object_permissions`, `mark_public`, `open_routes`) raises `ImportError` | `from domainkit.http.access import ...`, the same objects | DSGN-02 |
| The same names from `domainkit.contrib.auth.access`, and `AllOf`, `AnyOf`, `Not`, `Rule`, `SAFE_METHODS`, `MODEL_ACTIONS`, `bearer`, `public`, `scopes_of`, `routes_need_users`, `unguarded_routes` | `domainkit.http.access`; `IsAdminUser`, `IsSuperuser`, `AuthenticatedUser`, `CurrentUser`, and `current_user` stay in `domainkit.contrib.auth` | DSGN-02 |
| `AUTH.REQUIRE_ROUTE_RULES`: settings that set it are refused when they load | `REQUIRE_ROUTE_RULES` | DSGN-02 |
| `Query` from `domainkit.ddd.query` | `domainkit.ddd.Selection` | DSGN-09 |
| `domainkit.contrib.evidence.model` | `domainkit.contrib.evidence` (or its `models`) | DSGN-09 |

## 0.9 to 0.10

### Breaking changes

Each changes what a project gets without changing its code. Each says how
to keep the old behaviour, for as long as the project needs it.

- **Deny by default without `contrib.auth`** (DSGN-02). A project that
  authenticates its own way, without `domainkit.contrib.auth`, now has deny
  by default too: a route that declares no rule (`Require`, `CurrentUser`,
  a `reads_principal` dependency, or `Public`) is listed by `check`, and
  staging and production refuse to start. **Fix:** give each route its
  rule, or `dependencies=[Public]` for a route anyone may call. **To keep
  the old behaviour:** `REQUIRE_ROUTE_RULES = False`.

- **API docs off in deployments** (WEB-08). `API_DOCS` defaults to
  `None`, which follows `DEBUG`: with `DEBUG = False`, `/docs`, `/redoc`,
  and `/openapi.json` are not served. **To keep them:** `API_DOCS = True`.
- **Security headers on every response** (WEB-08). A response that sets
  none gets `Content-Security-Policy: default-src 'none'; frame-ancestors
  'none'`, a `Permissions-Policy` that turns powerful browser features off,
  and `Cross-Origin-Opener-Policy: same-origin`. **Fix:** a route that
  answers HTML for a browser sets its own policy. **To keep the old
  headers:** `SECURE_CONTENT_SECURITY_POLICY = None` (and `None` for
  `SECURE_PERMISSIONS_POLICY` and `SECURE_CROSS_ORIGIN_OPENER_POLICY`).
- **Route templates in logs** (WEB-10). The access log, error lines,
  error reports, and the request context show the matched route's template
  (`/accounts/{account_id}`) or `unmatched`, not the path as sent. **To
  keep the old lines:** `ACCESS_LOG_PATH = "raw"`.
- **Queued log records carry no exception object** (CONC-04). A record
  behind `BackgroundHandler` has its traceback formatted into `exc_text`
  and no `exc_info`; every formatter writes the same text. **Fix:** a
  handler that needs the exception object goes on the logger itself, not
  behind the queue.
- **A browser login ends the session its cookie held** (AUTH-05). **To
  keep the old session working:** `AUTH.COOKIE.END_PREVIOUS_SESSION =
  False`.
- **At most 100 logins a user** (AUTH-08). A login past
  `AUTH.MAX_LOGINS` ends the oldest; personal tokens neither count nor
  end. **To keep the old behaviour:** `AUTH.MAX_LOGINS = 10000`.
- **Device proofs** (SEC-r1-2). `/login` and `/session` set the
  `__Host-dk_device` cookie, and a client that sends it is counted apart
  from its address. **To keep the old behaviour:** `AUTH.DEVICE_TTL =
  None`.
- **A rolled-back or forked ledger is refused** (DATA-05). With
  `AUDIT.LEDGER` set, an immudb ledger behind the state a client last
  verified raises `LedgerRolledBack`, and a forked one `LedgerTampered`, on
  every call and on `/ready`, after restarts too. **Fix:** after restoring
  a ledger from a backup on purpose, delete its row from
  `domainkit_ledger_states` once the restore is checked. Projects with the
  audit app run `makemigrations` and `migrate` for the new tables.

- **The audit trail's baseline refuses by default** (DATA-04,
  SEC-10b-8). With `domainkit.contrib.audit` installed, staging and
  production refuse to start without `AUDIT.LEDGER`, `AUDIT.NAMESPACE`, a
  sealing schedule (messaging's worker, or `AUDIT.SEALED_EXTERNALLY`), and
  the `protect()` triggers; `check` fails with `audit.E001` to `audit.E003`,
  and `SILENCED_CHECKS` cannot name them. **Fix:** set the four
  ([contrib/audit.md](contrib/audit.md#in-staging-and-production)). **To
  keep the old behaviour:** `AUDIT = {"REQUIRE_BASELINE": False}`, which
  warns as 0.9 did.
- **Keyed requests need a deadline** (SEC-r2-4, SEC-10b-8). With
  `domainkit.contrib.idempotency` installed, staging and production refuse
  to start, and `check` fails (`idempotency.E002`), when `REQUEST_TIMEOUT`
  is `None` or `DeadlineMiddleware` is not in `MIDDLEWARE`. **Fix:** set
  `REQUEST_TIMEOUT` below `IDEMPOTENCY.LEASE_SECONDS` and list the
  middleware, as `init`'s settings do. **To keep the old behaviour:**
  `IDEMPOTENCY = {"REQUIRE_DEADLINE": False}`, which warns as 0.9 did.

### Deprecated, removed in 0.11

Each still works in 0.10, with a `DomainkitDeprecationWarning` that names
its replacement.

| Deprecated | Use instead | Audit id |
| --- | --- | --- |
| `from domainkit.contrib.auth import IsAuthenticated` (and `AllowAny`, `BasePermission`, `HasObjectPermissions`, `HasPermissions`, `HasScope`, `IsAuthenticatedOrReadOnly`, `IsUnscoped`, `ModelPermissions`, `NotAuthenticated`, `Public`, `Require`, `check_object_permissions`, `mark_public`, `open_routes`) | `from domainkit.http.access import ...`, the same objects | DSGN-02 |
| `domainkit.contrib.auth.access` (all of it: the names above, and `AllOf`, `AnyOf`, `Not`, `Rule`, `SAFE_METHODS`, `MODEL_ACTIONS`, `scopes_of`) | `domainkit.http.access`; `IsAdminUser`, `IsSuperuser`, `AuthenticatedUser`, `CurrentUser`, and `current_user` stay in `domainkit.contrib.auth` | DSGN-02 |
| `AUTH.REQUIRE_ROUTE_RULES` | `REQUIRE_ROUTE_RULES` | DSGN-02 |
| `ResourceRouter(crud=True)` without `writable=`, or without both `create=` and `update=` | `writable=[...]` naming the fields, or `writable=ALL_FIELDS` | WEB-09 |
| `Query` from `domainkit.ddd.query` | `domainkit.ddd.Selection` | DSGN-09 |
| `domainkit.contrib.evidence.model` | `domainkit.contrib.evidence` (or its `models`) | DSGN-09 |
| Scoped credentials on a route that declares no rule that checks scopes (`SCOPED_CREDENTIALS = "warn"`) | `HasScope(...)`, a permission rule, or `AllowScoped()` on the route; `SCOPED_CREDENTIALS = "refuse"` refuses them now | AUTH-01 |
| `IDEMPOTENCY.ANONYMOUS` left unset (`"per-address"` by default; `check` warns `idempotency.W002` in staging and production) | `IDEMPOTENCY = {"ANONYMOUS": "per-address"}` to keep it, or `"refuse"`, 0.11's default, now | DATA-09 |

The whole list, with the dates, is in
[versioning.md](versioning.md#deprecations-planned-for-011), and every
change in the [CHANGELOG](../CHANGELOG.md).
