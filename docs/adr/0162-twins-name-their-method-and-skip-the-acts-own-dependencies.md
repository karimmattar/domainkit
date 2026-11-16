# 0162. Let twins name their method where a path is shared, and skip the act's own dependencies

- Status: Accepted
- Date: 2026-11-16
- Amends: [0144](0144-authorize-one-act-not-a-window-of-time.md)

## Context

[ADR 0144](0144-authorize-one-act-not-a-window-of-time.md) gave each
command route whose rules ask for an authorization of the act a twin,
`POST <the route's path>/authorization`. The twin builds the command as
the route does, asks what the act would check, and answers what the rule
stored. A project that runs its acts this way reported two gaps.

- **A route's own limit counts each act twice** (user report A14). A
  command route takes no `dependencies=`, so a per-route limit went into
  `provide`, as a dependency that gives a field (`sender`, `granter`).
  The twin runs `provide`, so the limit counted the ask and the act: 50
  invitation messages a day became 25 acts. The project's limit skips a
  request whose route path ends in `/authorization`.
- **Two twins of one path collide** (user report A17). A `PATCH` and a
  `DELETE` of one item that both ask would both have
  `POST /items/{id}/authorization`, and the router refuses the second.
  The project turned withdrawing a document into `POST .../withdraw`.

### What a twin runs

| Part of the route | On the twin | Why |
| --- | --- | --- |
| The resource's and the route's rules | Yes, the asking one passing | No one asks for an authorization of an act they could not make. |
| Reach: scope, parent, rules on the object | Yes | The same. |
| `provide` | Yes | It builds the command, and the authorization covers the command's digest. |
| The body's schema and limit | Yes | The same command, the same bytes. |
| The resource's dependencies (`ResourceRouter(..., dependencies=[...])`) | Yes | FastAPI runs a router's dependencies on each of its routes. |
| The route's own dependencies | (none existed) | |

### What others do

- **Kubernetes' dry run** [1] runs a request through admission, the
  checks included, and persists nothing. A webhook with side effects must
  skip them when the request says `dryRun: true`, and must declare that it
  does (`sideEffects: NoneOnDryRun`), or a dry-run request fails. A twin
  is such a dry run of the act: what checks runs, what counts must know.
- **FastAPI** [2] runs a path operation's `dependencies=` on that
  operation only, for checks that raise and work whose value is not used.
  A twin is another operation.
- **OWASP API4:2023** [3] asks to limit how often one client can run one
  operation, tuned to the business: such a limit is of the act, not of
  the asks before it.
- **RFC 9396** [4] describes what is authorized as actions at locations.
  An act is a method at a path, and two acts of one path differ by their
  method.

### Options for the limit

| Option | A check of who may act | A limit of the act |
| --- | --- | --- |
| `dependencies=` run on the act and its twin, and a marker | Holds on the twin, wherever it is written | Counts twice, unless it reads the marker |
| **`dependencies=` run on the act alone, and a marker for what runs on both** | Holds on the twin as a rule (`rule=`); not as a dependency | Counts once, unchanged |
| A marker alone | Holds | Stays in `provide`, and reads the marker |
| The twin skips `provide` | Holds | The command could not be built as the act's |

Access in domainkit is a rule ([ADR 0080](0080-access-rules-are-core-auth-is-one-provider.md)):
`check`, `open_routes`, OpenAPI's `401` and `403`, and
`Operation.rules` read rules, and none sees a dependency that refuses.
A rule already holds on every route of the resource, twins included. So
the route's own dependencies are left to the act, as Kubernetes leaves a
webhook's side effects to the request that is not a dry run.

### Options for the shared path

| Option | What it costs |
| --- | --- |
| Refuse, as 2.4 does | The project changes its API. |
| One twin per path, the act named by `?method=` or a body field | One operation, whose body is either act's: OpenAPI cannot show each. |
| The method in every twin's path | Moves every twin that 2.4 serves: breaking. |
| The bare path for the route declared first, the method for the others | Which act the bare path authorizes depends on the order of declaration. |
| **The method in the path of each twin of a shared path** | Declaring the second moves the first. Nothing 2.4 serves moves: 2.4 refused that declaration. |

## Decision

### A twin of a shared path names its route's method

- A route alone on its path keeps its twin at `<path>/authorization`.
- Where routes of one path ask by several methods, each twin's path names
  its route's method in lower case: `POST <path>/authorization/patch`,
  `POST <path>/authorization/delete`. Declaring the second moves the
  first's twin there, with its operation in `operations`.
- Two asking routes of one path and one method, or a route of the
  project's own where a twin goes, still raise `ImproperlyConfigured`
  when declared.
- A twin keeps its name (`<route name>_authorization`). With
  `OPENAPI_OPERATION_IDS = "route_name"`, the default, that name is its
  operation id, which does not change when it moves; with `"fastapi"`,
  FastAPI's id holds the path, and changes with it.

### A route's own dependencies are the act's

- `command_route(dependencies=[Depends(...)])`, and so `use_case(...,
  dependencies=...)`, gives a command route FastAPI dependencies of its
  own, run after its rules (the resource's and the route's): a request
  they refuse is not counted. What the route checks itself comes after
  them: the aggregate within reach (`404`), the rules on it (`403`), the
  body (`422`), and `If-Match` (`412`); a request refused there is
  counted.
- Its twin runs none of them. It runs what decides whether the caller may
  act and what builds the command: the rules, the reach, `provide`, the
  body's schema and limit, and the resource's dependencies.
- `asking_authorization(request)`, from `domainkit.resources`, is true on
  a twin's request, from routing on: code that runs on both, in
  `provide`, a rule, or the resource's dependencies, skips what counts
  acts. It is Kubernetes' `dryRun` for a twin.
- A check of who may act goes in a rule, which the twin asks too, and the
  documentation says so where `dependencies` is described.

## Consequences

- **The project's limit moves to `dependencies=`** and drops its path
  check. A limit left in `provide`, or in the resource's dependencies,
  reads `asking_authorization` instead of the path.
- **A check written as a dependency does not hold on the twin.** A caller
  it refuses, and the rules allow, can ask for an authorization of the
  act and read what the act would show; the act itself stays refused.
  This is the cost of counting once without the marker; the
  documentation puts checks in rules.
- **Declaring a second asking route on a path moves the first's twin.** A
  client of the first follows it to `<path>/authorization/<method>`. The
  documentation says so.
- **Batches** take no `dependencies=` yet: they have no twin, and the
  resource's dependencies or a `Throttle` of a scope of its own limit
  them.
- **New names**: `command_route(dependencies=...)` and
  `asking_authorization`. Nothing is renamed or removed.

## References

1. Kubernetes, *Dynamic Admission Control*, side effects:
   <https://kubernetes.io/docs/reference/access-authn-authz/extensible-admission-controllers/#side-effects>
2. FastAPI, *Dependencies in path operation decorators*:
   <https://fastapi.tiangolo.com/tutorial/dependencies/dependencies-in-path-operation-decorators/>
3. OWASP API Security Top 10 2023, *API4: Unrestricted Resource
   Consumption*:
   <https://api-security.owasp.org/editions/2023/en/0xa4-unrestricted-resource-consumption>
4. RFC 9396, *OAuth 2.0 Rich Authorization Requests*:
   <https://www.rfc-editor.org/rfc/rfc9396>
