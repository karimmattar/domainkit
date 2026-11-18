# 0188. Write down the core's layering, and hold the boundaries it draws

- Status: Accepted
- Date: 2026-11-18
- Amends: [0003](0003-enforced-layer-rules.md), [0140](0140-auth-asks-other-apps-through-hooks.md), [0182](0182-let-a-gateway-raise-a-routes-problems-and-name-users-by-the-fields-a-project-chooses.md)

## Context

The architecture audit of 2.8.0 (A28-ARCH) found the code itself in good
shape: no eager import cycle in 581 modules, few complex functions, clean
`__all__` lists. The weak points were at the seams, where rules were
written down for projects but not for the framework, or held only in part:

- **The core's own layering is undeclared** (ARCH-04). ADR 0080 places
  the access rules in "layer 4 of the package's layering", but no page
  or test says what the layers are. `CONTRIBUTING.md` names one rule:
  the core never imports `domainkit.contrib`. Under a natural layering
  the audit counted 28 imports that point up, 14 of them adapters
  importing the outgoing HTTP client, which sits in the serving package
  (ADR 0050).
- **The app-cycle guard sees pairs only** (ARCH-05). `Layout.mutual()`
  keeps two apps that import each other. Auth, privacy, and audit import
  one another in a ring of three, which no pair shows: auth subscribes to
  privacy's `PersonalDataErased`, privacy writes audit's trail, and
  audit's history API is guarded by auth's rules.
- **Apps use 94 core names that no `__all__` publishes** (ARCH-06), and
  63 more imports reach public names through internal modules (21 of
  them `APIError` through `domainkit.http.errors`, which had no
  `__all__`, E10). The boundary test checked only imports between apps.
- **The layer rule lets a gateway take web types** (ARCH-03).
  `ALLOWED_MODULES` lets the infrastructure layer import
  `domainkit.http.uploads` and `domainkit.http.client`, and the rules
  read names: `from domainkit.http.uploads import UploadFile,
  StreamingResponse, serve` passes. ADR 0182 held `domainkit.http.problems`
  to its `__all__` from the start, and planned the same for the two 2.x
  modules in steps: a warning in a later 2.x, a refusal in 3.0.
- **Append-only triggers were written twice** (ARCH-07). Audit and
  accounting each made the same function and triggers, and each checked
  them at startup, differently: audit's check read every tenant schema
  that holds a trail and refused a trigger given arguments; accounting's
  read the tables its `search_path` found, and counted such a trigger.
  The audit read this as a ledger in tenant schemas going unchecked.
  That cannot happen: `DATABASE_ROUTING['accounting']` refuses a
  database that keeps a schema per tenant. The copies had drifted all
  the same, and the next fix to one would have to be found in the other.
- Smaller ones: `contrib.objectpermissions` needs auth but declared no
  requirement (ARCH-10); AWS's signing and credentials lived in the KMS
  adapter, whose private names S3 and the cloud module imported
  (ARCH-11); two pieces of module state had no public reset, against
  ADR 0113 (ARCH-14); complexity had no gate (E3); 26 lines read
  `result.rowcount` under `# type: ignore[attr-defined]` (E4); and seven
  digest encoders differ in `ensure_ascii` and `allow_nan` (E7).

Constraints: 2.9 is a minor release, so nothing a project relies on
changes, and a stricter default needs a deprecation path. Other work on
2.9 edits the backends that import the HTTP client, so the client stays
where it is for now.

How others do it:

- import-linter's layers contract lets a higher layer import a lower
  one and not the reverse, and counts chains of imports too [1]. Here
  every edge but the listed ones goes down, so a chain up must pass a
  listed edge: checking edges is enough while the list is kept.
- A ring of any length is a strongly connected component of the import
  graph, which Tarjan's algorithm finds in one pass [2].
- PostgreSQL's `pg_trigger` says whether a trigger fires
  (`tgenabled`), under a `WHEN` (`tgqual`), for some columns only
  (`tgattr`), and with arguments (`tgnargs`) [3].
- RFC 8785 (JCS) gives one text per JSON value: members sorted by UTF-16
  code units, ECMAScript's number form, and errors for lone surrogates;
  integers past 2**53 - 1 belong in strings [4].
- ruff's C901 and PLR0913/PLR0915 bound a function's complexity,
  arguments, and statements [5].

## Decision

### The core's layers

The core's packages (`domainkit.<name>`, a module or a directory) fall in
five layers, from the bottom. A package imports its own layer and those
below it, never one above.

| Layer | Packages |
| --- | --- |
| 0. Foundations | `checks`, `clock`, `deadlines`, `deprecation`, `exceptions`, `tasks`, `tracing`, `utils` |
| 1. Settings and the model | `domainkit` (the root: `setup()`), `apps`, `conf`, `context`, `ddd`, `encryption`, `i18n`, `log`, `money`, `reporting`, `signing` |
| 2. Adapters | `broker`, `cache`, `cloud`, `db`, `documents`, `inspection`, `keys`, `ledger`, `search`, `storage` |
| 3. Serving | `debug`, `http`, `lifecycle`, `resources`, `tenancy` |
| 4. Tools | `__main__`, `architecture`, `management`, `testing` |

The contrib apps sit above them all (ADR 0080). Every import counts: at
the top of a module, in a function, under `TYPE_CHECKING`, or by a loader
given a module's name, as the project rules count them.
`domainkit_tests/test_core_layers.py` holds the table and the edges up
that 2.8 had, each with its reason; the list only shrinks, and a new
package must say its layer:

- 16 imports of `domainkit.http.client` by adapters, until the client
  moves to a package below them (a later release, after the backends'
  work lands);
- the settings' import of `domainkit.architecture` (they validate
  `EXTRA_LAYERS` against its layer names) and of the default virus
  scanner;
- the database's lazy imports of `domainkit.tenancy`, which names a
  tenant's schema;
- the annotation of `DomainConfig.databases(router)`, which names the
  router (ADR 0101).

### The apps' boundaries

- **Rings of any length.** `Layout.cycles()` replaces `mutual()`: the
  strongly connected components of the graph of apps, at any depth.
  `ALLOWED_CYCLES` lists the one ring left, auth, privacy, and audit,
  with its reason. Auth forgets a user's credentials on
  `PersonalDataErased`, which a retention purge publishes too;
  `on_erase_subject` runs only for `privacy erase`, with a subject rather
  than the user erased, so moving to it would keep a purged user's
  credentials. ADR 0140 is amended.
- **The core's names, from the apps' side.** An app imports from the
  core only the names a module's `__all__` lists, from that module.
  `ALLOWED_CORE` lists, by name, the 96 left in 2.9; it only shrinks.
  By name rather than by importer: the debt is a name to publish, and
  splitting a module of an app moves no entry.
- **Published now**, each where it lives: `domainkit.context`
  (`current_request`, `bind_request`, `RequestContext`, `SYSTEM_ACTOR`),
  `domainkit.utils.module_loading` (`import_string`, `lazy_names`),
  `domainkit.utils.rates` (`parse_rate`, `PERIODS`),
  `domainkit.utils.decimals.jsonable`, `domainkit.db.routing.DatabaseRouter`,
  `domainkit.log.SECURITY_LOGGER`, and an `__all__` for
  `domainkit.http.errors`: the names `domainkit.http` takes from it
  (`ErrorResponse`, `error_response`, `error_responses`) and the two
  problems. Admin, object permissions, and personal tokens import from
  the public modules they had bypassed.
- `contrib.objectpermissions` requires `contrib.auth`.

### The layer rule's 2.x modules

`domainkit.architecture.layer_warnings` finds, in the infrastructure
layer, a name taken from `domainkit.http.client` or
`domainkit.http.uploads` that their `__all__` does not list (Starlette's
`UploadFile` and `StreamingResponse`, `httpx2`, `StoredUpload`), and
`uploads.serve`, which answers a request with a response: a route's work,
not a gateway's. `check` lists each as `architecture.W001`: it passes,
and `check --deploy` fails until the import changes or the warning is
silenced. 3.0 refuses them, as `domainkit.http.problems` is refused
today. The modules stay allowed, with every name of their `__all__` but
`serve`: `save_upload`, `receive_upload` and `Upload` (ADR 0177), and
the client. ADR 0003 and ADR 0182 are amended.

### Append-only tables

`domainkit.db.protection` makes and checks the triggers, for audit,
accounting, and any domain of a project:

- `append_only_statements(table)`, `trigger_names(table)`, and
  `append_only_body(table)`: the statements of 2.8, the same function
  names and bodies, so triggers made then still count.
- `protect_append_only(connection, tables)`: afresh, for each table the
  connection's `search_path` finds.
- `missing_append_only(connection, tables)` and
  `unprotected(database, tables)`: one check, the stricter of the two: a
  trigger counts only as made, enabled for the application's sessions,
  with no `WHEN`, columns, or arguments, running the PL/pgSQL function
  made for its table, its body the same but for whitespace.
  `unprotected` reads a database's own schema and each tenant's schema
  that holds the first table, as audit's did, and names the place;
  `migrate_command(place)` says what makes it.

Audit and accounting keep their own names (`protect`, `missing_triggers`,
`accounting.protection.missing`) on top of it. The ledger's check now
refuses a trigger given arguments, as the trail's did.

### Smaller decisions

- **AWS.** Signature Version 4 and the credential chain move to
  `domainkit.cloud.aws`, which S3, SQS, SNS, SES, and KMS use.
  `domainkit.keys.aws` imports every name it held, the same objects.
  The chain still raises `KeyProviderError`, as in 2.8.
- **Resets.** `domainkit.contrib.mail.admins.reset_reports()` and
  `domainkit.contrib.flags.declared_kept()`, which the pytest plugin
  calls (ADR 0113).
- **Complexity gates.** ruff's C901 (complexity over 15), PLR0913 (over
  10 arguments, keyword-only ones counted), and PLR0915 (over 50
  statements) run in CI's lint job. The 24 files over them when the gate
  came are listed in `pyproject.toml`; a file leaves the list once its
  functions are split.
- **`domainkit.db.affected_rows(result)`**: the rows an `UPDATE` or
  `DELETE` changed, typed, and a `TypeError` for a result that counts
  none. It replaces the waived `rowcount` reads, but in files other 2.9
  work edits.
- **`domainkit.utils.canonical.canonical_json(value)`**: RFC 8785's text,
  in UTF-8, for new digests. It refuses NaN, the infinities, integers
  past 2**53 - 1, lone surrogates, and keys that are not strings. The
  seven encoders whose digests are stored keep their own form.

Options weighed:

- **Move the HTTP client now** (the audit's fix for ARCH-04). It would
  take 14 edges off the list, but other 2.9 work edits every backend
  that imports it. Later, with a re-export from `domainkit.http.client`.
- **Layers kept in code** (`domainkit.architecture.CORE_LAYERS`). A
  public table the framework would have to keep stable; the test and
  this record are enough for contributors.
- **Refuse the 2.x modules' other names now.** It would break `check` in
  a minor release for imports ADR 0182 said would pass until a warning
  came first.
- **Split `domainkit.http.uploads`** into a web-free half for gateways
  (the audit's longer fix). Larger, and the warning reaches the same
  place by 3.0.
- **Subscribe auth to `on_erase_subject`** (the audit's fix for the
  ring). It would leave a purged user's credentials in place.
- **Move `PersonalDataErased` to the core** to break the ring. Its module
  is part of its name in tracebacks and handlers' keys: not for a minor
  release.
- **The core's names baselined by importer.** Stricter, but every split
  of an app's module would change the list.
- **`json.dumps(sort_keys=True, ...)`** for canonical JSON. It sorts by
  code point, writes `2.0` and `1e-07`, and so disagrees with other
  languages' RFC 8785 for the values that differ.

## Consequences

- A contributor sees which core packages may import which, and a new
  edge up fails a test, with the reason of each one left.
- A fourth app that closes a longer ring fails CI. A new import of an
  unpublished core name by an app fails too; publishing it, or using its
  public home, shrinks the list.
- Projects get `architecture.W001` where a gateway imports a web type
  through an allowed module; `check --deploy` fails until it changes.
  3.0 refuses it.
- The names published here, and the new ones (`domainkit.db.protection`,
  `affected_rows`, `canonical_json`, `layer_warnings`, and the two
  resets), follow the deprecation policy.
- A trigger given arguments on the ledger's tables fails the startup check
  where deployed, as one on the trail's did; `migrate` puts it back.
- `AwsCredentials.__module__` is `domainkit.cloud.aws`.
- A test's admin report and dynamic settings no longer reach the next
  test.
- New code over the complexity gates fails lint; work merged into 2.9
  after this may need its own entry, or a split.

## References

1. import-linter, *Layers*:
   <https://import-linter.readthedocs.io/en/stable/contract_types/layers/>
2. R. Tarjan, *Depth-first search and linear graph algorithms*, SIAM
   Journal on Computing 1(2), 1972:
   <https://doi.org/10.1137/0201010>
3. PostgreSQL 16, *pg_trigger*:
   <https://www.postgresql.org/docs/16/catalog-pg-trigger.html>
4. RFC 8785, *JSON Canonicalization Scheme (JCS)*:
   <https://www.rfc-editor.org/rfc/rfc8785>
5. ruff, *complex-structure (C901)* and *too-many-arguments (PLR0913)*:
   <https://docs.astral.sh/ruff/rules/complex-structure/>,
   <https://docs.astral.sh/ruff/rules/too-many-arguments/>
6. AWS, *Signature Version 4 for API requests*:
   <https://docs.aws.amazon.com/IAM/latest/UserGuide/reference_sigv.html>
