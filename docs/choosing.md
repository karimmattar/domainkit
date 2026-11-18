# Choosing a feature

Some needs have more than one tool in domainkit, and the tools look
alike: an idempotency key and the inbox both stop work from running
twice, and money limits and rate limits both say "limit". This page names
each need, the tool built for it, and the tool to leave alone. Each
feature's page says the same at more length, in its section "When to use
it, and when not to".

On this page you find which tool to use:

- to run work once, or after a commit;
- to confirm an act;
- to keep a record of what happened;
- to limit what a caller does or moves;
- to keep money;
- to keep tenants apart;
- to store data, and files;
- to tell other services and clients;
- to keep a rule on values;
- to serve a route.

## Running work once, or after a commit

| You need | Use | Not |
| --- | --- | --- |
| A client that retries a `POST`, because it got no answer, must not run it twice. | [Idempotency keys](contrib/idempotency.md): the client sends `Idempotency-Key`. | The inbox: it sees messages, not requests. |
| A task or handler that the worker runs twice must commit its database work once. | [The inbox](contrib/messaging.md#once-in-the-database-the-inbox): `@inbox`. | An idempotency key: it sees requests only. |
| A value that must never be accepted twice, such as a payment's reference. | A [unique column](databases.md#unique-values). | Idempotency keys: they are kept for a while, then forgotten. |
| Work after a commit that must happen even if the process dies: a mail, a call to a provider. | A [task](contrib/messaging.md#background-tasks) or a [durable handler](contrib/messaging.md#durable-event-handlers), through the outbox. | `tasks.spawn`: the work dies with the process. |
| Work in this process that may be lost on a restart, such as warming a cache. | [`spawn` and `supervise`](runtime.md#background-tasks-spawn-and-supervise). | The outbox: it stores, retries, and dead-letters each message. |
| One customer's messages run one at a time, in the order they committed. | [Ordering keys](contrib/messaging.md#one-at-a-time-per-key-ordering-keys). | A single worker for every message. |

## Confirming an act

| You need | Use | Not |
| --- | --- | --- |
| The user proves again, for a while, that it is them, for any act: adding a payee, changing an address. | [A step-up](auth/step-up.md#stepping-up): `StepUp(max_age=...)`. | An action authorization, when the act's arguments do not matter. |
| The user confirms one payment, of one amount to one payee, once. | [A transaction authorized for one amount and payee](auth/step-up.md#transactions-authorized-for-one-amount-and-payee). | A step-up: it opens every guarded act, with any arguments. |
| The user confirms one act with the arguments shown ("End Alice's membership of Acme"), once. | [An action authorization](auth/actions.md): `StepUp(for_=...)`. | A step-up, for the same reason. |
| Other people must agree before a command runs (maker-checker). | [Approvals](contrib/approvals.md): `@requires_approval`. | A step-up or an authorization: the maker gives those alone. |
| The confirmation comes from the user's bound phone, not from the page that asks. | [Sign-in approvals](auth/ciba.md) (CIBA), for a sign-in or for one act. | A code typed into the page that asks. |

## Keeping a record

| You need | Use | Not |
| --- | --- | --- |
| Who did what, when, and from where, for every committed change. | [The audit trail](contrib/audit.md). | Evidence: a ledger write for every change. |
| A fact you may have to prove later exactly as it was: a verification result, a signed consent, a report sent. | [Evidence](contrib/evidence.md), on an immudb ledger, with a receipt for each record. | The audit trail alone: it says who acted, not what the document held. |
| A record's earlier versions, and who changed it. | [History](contrib/audit.md#history), from the audit trail. | A table of versions of your own. |

## Limiting what a caller does or moves

| You need | Use | Not |
| --- | --- | --- |
| Bound how much money moves: per payment, or per day and per count. | [Limits](contrib/limits.md) (`contrib.limits`), counted in the use case's transaction. | Rate limits: they count requests, not amounts, and outside the transaction. |
| Bound how many requests a caller sends to the whole service. | [Rate limits](cache.md#rate-limits-for-every-request): `RATE_LIMIT`. | `contrib.limits`: it counts movements of money. |
| A tighter limit on one costly route, such as an export. | [A throttle](cache.md#throttles-for-one-route): `Throttle("5/hour")`. | `RATE_LIMIT` lowered for every route. |
| Stop a large payment split into many under an approval threshold. | Limits per window, beside approvals ([what maker-checker does not stop](contrib/approvals.md#what-maker-checker-does-not-stop)). | A lower threshold alone. |

## Keeping money

| You need | Use | Not |
| --- | --- | --- |
| An exact amount and its currency, in the domain, tables, and JSON. | [`Money`](money.md). | `float`, or a `Decimal` without its currency. |
| A balance with a rule or two, in one service, with no accounting reports: credits in an app, a simple wallet. | A balance field, as in [the payment recipe](recipes/payments.md). | The ledger: more tables, and entries to post. |
| Money that must balance, be reported, and be reconciled: customers' wallets, a payment service, a lender; holds, periods, a trial balance. | [The double-entry ledger](contrib/accounting.md). | A balance field: it has no postings to check, close, or reconcile. |
| Approval of postings above an amount. | [Large postings](contrib/approvals.md#large-postings), with approvals. | A check in each use case. |

## Keeping tenants apart

Every way keeps a tenant's caches, rate limits, logs, and tasks apart
([multi-tenancy](tenancy.md)). They differ in where rows live, and what
that costs:

| You need | Use | What it costs |
| --- | --- | --- |
| Many tenants of similar size, in one database. | [Rows](tenancy.md#rows-tenantowned): `TenantOwned`. | The least: one database, one pool, one migration. A report across tenants runs in `all_tenants()`. Raw SQL is not filtered. |
| A large tenant on a database of its own, or tenants spread across databases for size. | [A database per tenant](tenancy.md#a-database-per-tenant): a shard group, `PINNED`. | A pool and migrations per database. A report across tenants reads each database. |
| Each tenant's tables in a schema of its own, on PostgreSQL. | [A schema per tenant](tenancy.md#a-schema-per-tenant-postgresql): `SCHEMA_DATABASES`. | `migrate --tenant` for every tenant, shared tables on another alias, and no `all_tenants()`: a report loops over the tenants. |

## Storing data, and files

| You need | Use | Not |
| --- | --- | --- |
| Reads taken off the primary database. | [Read replicas](databases.md#read-replicas). | A second database the domain writes to. |
| A domain on a database of its own. | [Routing domains to databases](databases.md#routing-domains-to-databases). | Shards, for one domain that fits one database. |
| Rows split across databases by a key, past what one database holds. | [Sharding](databases.md#sharding). | Partitioning, which keeps one database. |
| A large table's old rows dropped or archived by time, on PostgreSQL. | [Partitioning](databases.md#partitioning). | Deleting old rows in batches. |
| A field encrypted at rest, and a person erased by destroying a key. | [Encrypted fields](contrib/keys.md). | Encryption for a field you search by text, range, or order: only equality works. |
| An upload checked by its content before it is kept. | [`save_upload`](uploads.md#receiving-uploads), or a resource route that takes a file. | Presigned uploads: the file does not pass through the application. |
| Large files sent or fetched by the client, straight to object storage. | [Presigned URLs](uploads.md#presigned-urls). | Uploads through the application, which hold a request open. |

## Telling other services and clients

| You need | Use | Not |
| --- | --- | --- |
| Another service learns of an event, signed, and retried until it answers. | [Webhooks](contrib/webhooks.md). | Live connections: they reach only clients that are connected. |
| A browser or an app sees committed changes while it is connected. | [Live events](live.md), over WebSockets or Server-Sent Events. | Webhooks, which call a server's URL. |
| Your own code reacts to an event after the commit. | [A durable handler](contrib/messaging.md#durable-event-handlers). | A webhook to your own service. |

## Keeping a rule on values

| You need | Use | Not |
| --- | --- | --- |
| A limit on one field: a length, a range, a format. | [Validation markers](validation.md), on the field. | A check in each route: the aggregate and the column would not keep it. |
| A rule across the fields of one aggregate. | [`clean()`](validation.md#rules-across-fields-clean). | A check in the route. |
| A rule that depends on the aggregate's state, such as "an archived account takes no deposits". | A method of the aggregate, as [tutorial part 2](tutorial/2-rules.md) writes it. | A marker: it sees one value. |
| A value that no other row may hold. | A [unique column](databases.md#unique-values). | A marker, or a read before the write, which two requests pass at once. |

## Serving a route

| You need | Use | Not |
| --- | --- | --- |
| An HTTP API over an aggregate: list, read, and run its use cases. | [A resource](resources.md). | Routes written one by one. |
| One route that serves no aggregate, such as the rates of [tutorial part 0](tutorial/0-first-route.md). | A FastAPI route with an access rule. | A resource. |
| A route of your own beside a resource's, on one of its aggregates. | [`ResourceRouter.fetch`](resources.md#routes-of-your-own-on-a-resource). | A query that skips the resource's reach. |

## See also

- [Optional apps](contrib.md): every app, its setting, and its tables.
- [Learning domainkit](learning.md): the order to read the guides in,
  and the glossary.
- [The request flow](request-flow.md): what each stage does, and what
  it costs.
