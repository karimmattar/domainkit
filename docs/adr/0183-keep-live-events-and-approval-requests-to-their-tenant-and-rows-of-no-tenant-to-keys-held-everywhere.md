# 0183. Keep live events and approval requests to their tenant, and rows of no tenant to keys held everywhere

- Status: Accepted; amended 2026-11-18 (at the end)
- Date: 2026-11-18
- Amends: [0057](0057-live-committed-events-in-process.md), [0121](0121-hold-guarded-commands-as-sealed-requests-and-count-approvals-under-the-requests-lock.md), [0143](0143-fan-live-events-out-through-postgresql-or-redis.md), [0150](0150-take-the-tenant-from-the-path-and-let-members-hold-permissions.md)

## Context

The security audit of 2.8.0 (A28-SEC-01, A28-SEC-02, A28-SEC-04) found
three framework channels that carry tenants' data and did not know about
tenants. ADR 0150 promised that "a role never reaches beyond its tenant",
and docs/tenancy.md listed what is scoped: rows, databases, caches,
limits, logs, tasks. These three were not on the list, and nothing said
so:

- **Live events** (ADR 0057, ADR 0143). `live_events` handed each
  committed event to every subscriber of its type whose rule allowed it.
  An event carried no tenant, nor did the message between processes, so
  a rule could not tell. The framework's own rules ask about the
  connection's tenant only: `InTenant()` allows every object while a
  tenant is served, and `HasMemberPermissions` counts the connection's
  role.
- **Approval requests** (ADR 0121). A request carried no tenant. Listing
  and deciding asked `HasPermissions`, which counts `MembershipBackend`'s
  keys for the tenant served: a role in one tenant read and decided
  another's requests. The approval queued the command with the
  approver's tenant (the outbox's headers), and the worker ran it there.
- **Limits and the ledger.** Their rows belong to no tenant by design.
  Their routes asked `ModelActions`, so a role in one tenant that names
  `limits.change_limit` or `accounting.view_account` reached every
  tenant's rows. Approvals' guards of postings, of making superusers and
  of admin changes asked the same keys of makers and approvers.

Constraints:

- These fixes ship in a patch release (2.8.1) and in 2.9: nothing a
  project without `TENANCY` relies on may change. A security fix may
  change behaviour under `TENANCY`, said in the changelog and
  upgrading.md.
- The core never imports a contrib app (ADR 0080): `HasPermissions` and
  the hub are core, `get_all_permissions` is `contrib.auth`'s.
- Approval requests are sealed (ADR 0121): a seal that changes for rows
  already stored would refuse every pending request.
- Approvals' database is one alias outside every shard group, never one
  of `SCHEMA_DATABASES` (`require_standalone`): the requests of every
  tenant share one table.

How others do it:

- OWASP's Multi-Tenant Security Cheat Sheet asks to "derive tenant
  context from the authenticated producer and bind it to the message",
  and at the consumer to "re-establish tenant context"; not to "treat a
  tenant identifier in a queued message as authorization proof"; to give
  global jobs "explicit identities and scopes rather than a fabricated
  tenant"; to "give intentionally shared entries an explicit global
  namespace and document why they are safe to share"; and not to "allow
  one tenant unbounded consumption of a shared queue" [1].
- OWASP ASVS 5.0 asks that operations never affect tenants "with which
  they do not have permissions to interact" (8.4.1) [2].
- Action Cable applications set the tenant once, on the connection, and
  apply it around each command [3]. A Rails user reports that Action
  Cable's broadcasts reach other tenants' clients, and asks the framework
  for a way to keep each broadcast to its tenant, rather than hashing
  stream names [4].
- ADR 0150 already keeps a member's keys out of every ceiling: the
  admin API, token scopes and object grants read `get_all_permissions`,
  which leaves out tenant-scoped backends.

Options for live events:

- **A tenant field on `DomainEvent`.** Every event's stored format
  would change (outbox, audit trail, `payload`), and a project's event
  that names its own `tenant_id` would clash.
- **Rules that compare the event's tenant.** Each project would write
  them, and a rule that forgets fails open, as `InTenant` did.
- **The hub keeps the tenant beside the event** (`current_tenant()` when
  it is published, in the commit's own context), and each subscriber the
  tenant it was opened in; an event reaches a subscriber of the same
  tenant only. No tenant is a tenant of its own. An event of another
  tenant never enters a subscriber's buffers, so it cannot make it "too
  slow" either.
- To hear every tenant on purpose: subscribing inside `all_tenants()`
  (it is about rows, and an event stream's body is sent after the route
  returns), or a keyword, as `Throttle(per_tenant=...)` names a count
  shared across tenants. The keyword is explicit at every call.
- Across processes: the message names the tenant (`n`) only where one is
  served, so messages of no tenant stay as 2.8.0 sent them, and a
  message without it is of no tenant: during a rolling deploy, a process
  of 2.8.0 reaches no tenant's connections, rather than all.

Options for approval requests:

- **`TenantOwned`.** Its repositories refuse every read with no tenant
  current (`TenantRequired`), which breaks every project without
  `TENANCY`, and every request made outside a tenant; and its column is
  not null, which rows already stored cannot fill.
- **Scope the API's list alone** (`scope=`). The services, the twins, a
  maker's held call (`run_by_maker`), auth's hold hook, and the worker
  read by id, and stay open.
- **A nullable `tenant_id`, and the repository reads the tenant's
  requests alone** (None matching None, every tenant inside
  `all_tenants()`), by id and by query: every channel at once.
- For the seal: cover the tenant always (every stored request's seal
  fails), never (a row moved to another tenant around the application is
  decided there), or **where the request has one** (a request of no
  tenant, as every stored one is, keeps its seal).
- For the run: the message's tenant (what the approver's request
  served), or **the request's own**, read from the sealed row, as OWASP
  asks.
- For a back office that decides every tenant's requests over HTTP: an
  opt-in through `ALL_TENANTS_PERMISSION`. It would widen what that key
  grants, and the routes' rules would have to count keys held everywhere
  on another tenant's request. Left for later: such code decides inside
  `all_tenants()`.

Options for rows of no tenant:

- **Make limits and accounts `TenantOwned`.** A ledger per tenant is
  another product, and a change of stored format.
- **Refuse to mount their routers under a tenant** (a startup error).
  Every project that serves all its routes under `TenantMiddleware`
  would fail to start.
- **Count only the keys held everywhere**, as ADR 0150's ceilings do.
  The core cannot call `contrib.auth`'s `get_all_permissions`; asking
  the principal as where no tenant is served (`no_tenant()`) leaves out
  `MembershipBackend`'s keys, which it grants in the current tenant
  alone, for any principal. A keyword on `HasPermissions`,
  `HasModelPermission` and `ModelActions` says so, and one on a guard
  says it for its makers and approvers: otherwise approvals' guards of
  the ledger would be a way around its routes.
- For telling a project which apps keep rows of no tenant: a warning
  fails `check --deploy` for every project with `TENANCY` and the
  ledger, though nothing is left to fix; an info lists them and never
  refuses.

## Decision

- **Live events keep their tenant.** `LiveEvents.publish` offers each
  event with `current_tenant()`; a subscription hears the tenant served
  when it subscribes (None: no tenant), and only events of that tenant
  enter its buffers. `subscribe`, `stream` and `forward` take
  `per_tenant=True`; `per_tenant=False` hears every tenant's events, the
  rule alone deciding. A shared event's fan-out message names its tenant
  as `"n"` where one is served; a message without `"n"` is of no tenant;
  one whose `"n"` is not a tenant id is dropped, and logged.
- **Approval requests keep their tenant.** `ApprovalRequest.tenant_id`
  (nullable `String(63)`) is the tenant a request was submitted in, None
  for none. `RequestRepository` reads the requests of the tenant being
  served alone, by id and by query (another's is not found, before any
  precondition is checked); a request of no tenant is read where no
  tenant is served; inside `all_tenants()` every request is. So the
  list, a read, approving, rejecting, cancelling, their twins and
  checks, a maker's held call, and auth's hold hook find the tenant's
  requests alone, from routes and from code.
- **The seal covers the tenant**, where the request has one. A request
  of no tenant keeps the seal it had.
- **The approved command runs in the request's tenant.**
  `execute_request` reads the request inside `all_tenants()` to learn
  its tenant, then runs everything else in it (`tenant(...)`, or
  `no_tenant()`): the maker's check, the command, the request's end.
  The message's tenant decides nothing. `expire_requests` expires every
  tenant's requests, each committed as its tenant; `privacy erase`
  reaches every tenant's.
- **`everywhere=True`** on `HasPermissions(*perms, everywhere=...)`
  (and `HasObjectPermissions`), `HasModelPermission(...,
  everywhere=...)`, and `ModelActions(..., everywhere=...)` asks the
  principal as where no tenant is served: a member's role does not count.
  Unset, nothing changes, and the check makes no more calls.
- **Limits' and the ledger's routes** ask their keys `everywhere`.
- **`Guard(everywhere=...)`**, and `guard(..., everywhere=...)`: its
  makers and approvers are checked as where no tenant is served (the
  submit route's rules, `IsApprover`, the list's approvers, the run's
  maker check, `approvers_of`). The guards of postings, captures and
  reversals, of making superusers, and of admin changes say so.
- **`tenancy.I001`**: with `TENANCY` set, `check` names the installed
  apps whose rows every tenant shares, `domainkit.contrib.accounting`
  and `domainkit.contrib.limits`, as an info.

## Consequences

- Under `TENANCY`, a connection hears its tenant's events alone; a back
  office route that streams every tenant's says `per_tenant=False`. A
  connection served as no tenant no longer hears a tenant's events.
- Under `TENANCY`, a request is listed, read, decided and run in its
  tenant alone. A back office that served approvers as another tenant
  decides there no more: its approvers are served as the request's
  tenant, or its code decides inside `all_tenants()`.
- Requests stored before have no tenant: they are read where no tenant
  is served. Setting `tenant_id` on such a row by hand makes its seal
  fail, so it is decided or cancelled where no tenant is served, or left
  to expire. A message that runs a request approved before runs it in
  the request's tenant, none for such a row.
- A tenant's role reaches no limit, no account, and no guarded posting;
  staff who hold the keys everywhere reach them from any tenant, as
  before.
- A project without `TENANCY` sees no change: every event and request is
  of no tenant, every message is as before, and every seal holds. Each
  query of the requests adds `tenant_id IS NULL`.
- The table gains a column: `makemigrations`, then `migrate`, before the
  new code serves.
- During a rolling deploy, connections served as a tenant miss the
  events that processes of 2.8.0 fan out.

## References

1. OWASP Cheat Sheet Series, *Multi-Tenant Security Cheat Sheet*:
   <https://cheatsheetseries.owasp.org/cheatsheets/Multi_Tenant_Security_Cheat_Sheet.html>
2. OWASP ASVS 5.0, *V8 Authorization*, 8.4.1:
   <https://github.com/OWASP/ASVS/blob/master/5.0/en/0x17-V8-Authorization.md>
3. AnyCable, *Multi-tenancy vs. Cables: introducing Action Cable command
   callbacks*: <https://blog.anycable.io/p/multi-tenancy-vs-cables-introducing>
4. Ruby on Rails Discussions, *Broadcast/broadcast_to security in a
   multitenant environment*:
   <https://discuss.rubyonrails.org/t/broadcast-broadcast-to-security-in-a-multitenant-environment/82436>

## Amendment (2026-11-18): staff APIs, an everywhere guard's requests, and each request committed as its tenant alone

The review of this work found three gaps of the same kind, closed before
release:

- **Staff APIs counted a staff member's role.** `staff_rules` (the admin
  API, and the staff APIs of flags and webhooks), the audit trail's
  history, and the admin site asked `HasPermissions`: staff holding a
  key only through a role in the tenant served read the users of every
  tenant, and changed flags every tenant shares. Their rows belong to no
  tenant. `staff_rules` asks `ModelActions(..., everywhere=True)`, the
  history asks `audit.view_history` everywhere, and the admin site asks
  each model's permissions everywhere unless the model is `TenantOwned`,
  whose rows are the tenant's served. `is_staff` is held everywhere
  already; the admin API's ceilings counted keys held everywhere before
  (ADR 0150).
- **A role's `view_every` read an everywhere guard's requests.** The
  approvals list counted `approvals.view_every_approvalrequest` through a
  role, so a role read the postings and admin changes submitted in its
  tenant. The key held everywhere shows every request; held only through
  a role, it shows the requests of guards that are not `everywhere`. So
  the consequence "a tenant's role reaches ... no guarded posting" holds.
- **Code across tenants committed inside `all_tenants()`.** Expiry
  served each request's tenant inside `all_tenants()`, which stayed on:
  the handlers its commit ran read every tenant's rows. A helper of the
  core's tenancy module, `tenant_alone(tenant_id)`, internal for now,
  serves one tenant (or none) with `all_tenants()` off inside it. Expiry
  reads each request's tenant, then locks, expires and commits it as
  that tenant alone;
  `privacy erase` commits each tenant's requests as that tenant alone;
  approving, rejecting and cancelling commit as the request's tenant
  alone, so a decision made inside `all_tenants()` publishes and queues
  as that tenant. A run checks the seal before it serves the request's
  tenant, so a tenant changed around the application fails the request
  (`request_tampered`) rather than raise; the failure commits as no
  tenant. `approvers_of` asks as the request's tenant.

`everywhere` asks as where no tenant is served: it relies on a backend
that grants per tenant reading `current_tenant()`, as
`MembershipBackend` does.
