# 0027. Run batches as many use cases, atomic on request; stream exports, bounded

- Status: Accepted
- Date: 2026-10-20

## Context

Clients that change many aggregates at once (an import, a bulk archive)
send one request per item, or ask for an endpoint that takes many. DRF
leaves bulk writes to third-party packages, which mostly set fields in
one transaction. Here each change is a use case through an application
service that opens and commits its own unit of work, so "many items" and
"one transaction" pull against each other.

Listings also need CSV and NDJSON for spreadsheets and data tools, chosen
by `Accept` as DRF's renderers are. CSV opened in a spreadsheet runs cells
that look like formulas (CSV injection), and an export of every row can
be arbitrarily large.

## Decision

- `batch_route` builds one command per item and runs its service, as
  `command_route` does for one, answering `207` with a result per item:
  the status, code, and detail a request of its own would have had. The
  item count is bounded (100 by default) and every item is validated
  before any runs.
- By default each item is its own use case, and a failure leaves the
  others done. `"atomic": true` lends the services one open unit of work
  whose commit, enter, and exit do nothing; the batch runs all items in a
  savepoint and commits once, or rolls everything back at the first
  failure and answers the other items `424 not_applied`. Events are
  published once, after the commit. Services need no change.
- Only refusals become results (domain errors, HTTP errors, database
  refusals); any other exception is a bug and fails the request.
- Exports stream every match of the listing's query in keyset chunks,
  after a count: more than `max_export` is refused with `422`, and the
  stream stops at the bound regardless. Negotiation picks the most
  specific `Accept` range per offered type; nothing acceptable is `406`.
  CSV text that starts with `=`, `+`, `-`, `@`, a tab, or a carriage
  return gets a leading apostrophe, unless it is a plain number; so does
  text where those signs follow spaces of any kind or invisible
  characters, which spreadsheets skip, and their full-width and small
  forms, which some read as the signs. NDJSON escapes U+0085, U+2028,
  and U+2029, which JSON leaves raw but line readers split on.
- Each item asks the batch route's own rules about its aggregate, as a
  command route does.

## Consequences

Atomic batches hold one transaction for all items, so they are slower
under contention and bounded for that reason too; errors raised only at
the final commit (a constraint checked at commit) fail the whole request
rather than one item. Items cannot send `If-None-Match`; they send their
own `if_match`. Exports read in several transactions, so a row changed
while streaming may appear in its old or new state, once. Spreadsheets
show the apostrophe of neutralised cells, which is the point; a cell of
spaces then a minus sign and text gets one too. A security review found
the variants the first guard missed, and the batch rules not asked about
objects; each has a regression test.
