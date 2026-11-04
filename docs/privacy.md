# Privacy requests and retention

People may ask what a service holds about them, and ask for it to be
erased. Regulations (the GDPR's rights of access and erasure, and its
storage limitation) also say how long data may be kept.
`domainkit.contrib.privacy` answers both across every installed domain.
It reads the same `Personal` markers that the audit trail reads, and it
records each operation in the audit trail.

On this page you learn how to:

- mark the fields that hold personal data, and say whose they are;
- export everything about one person, with `privacy export`;
- erase a person's data, with `privacy erase`, and know what it reaches;
- erase and export the copies that your own domain keeps, with hooks;
- hold an erasure where a law asks you to keep a record;
- delete or erase old rows on a schedule, with `Retention` and
  `privacy purge`.

The decisions are in
[ADR 0041](adr/0041-privacy-through-personal-markers.md).

## A first example

Install privacy with the audit trail, which it requires:

```python
INSTALLED_DOMAINS = [
    # ...
    "domainkit.contrib.audit",  # required: every operation is audited
    "domainkit.contrib.privacy",
]
```

Mark each field that holds personal data, with the field that names the
person:

```python
from typing import Annotated, NewType
from uuid import UUID

from domainkit.ddd import AggregateRoot, Personal

CustomerId = NewType("CustomerId", UUID)


class Customer(AggregateRoot[CustomerId]):
    name: Annotated[str, Personal(of="id")]  # the customer is the person
    email: Annotated[str | None, Personal(of="id")] = None
```

Then export and erase a customer by their id:

```bash
python manage.py privacy export --subject 3f2a9c1e-0d2b-4c55-9f0e-1f2a3b4c5d6e --output ann.json
python manage.py privacy erase --subject 3f2a9c1e-0d2b-4c55-9f0e-1f2a3b4c5d6e --reason "ticket 812"
```

`erase` asks for the subject again, then answers:

```text
This erases the personal data of 3f2a9c1e-0d2b-4c55-9f0e-1f2a3b4c5d6e in every installed domain, for good. Type the subject again to go on: 3f2a9c1e-0d2b-4c55-9f0e-1f2a3b4c5d6e
Erased the personal data of 3f2a9c1e-0d2b-4c55-9f0e-1f2a3b4c5d6e in 1 aggregate, and forgot their audit keys.
```

The customer's row stays, with `name` set to `""` and `email` to `None`.

## Marking personal data

Mark each field of an [aggregate](learning.md#words) that holds personal
data, with the field that names whose it is. Events are marked the same
way ([audit.md](contrib/audit.md#audit-who-did-what-when-from-where)):

```python
from typing import Annotated, NewType
from uuid import UUID

from domainkit.ddd import AggregateRoot, Personal

CustomerId = NewType("CustomerId", UUID)
OrderId = NewType("OrderId", UUID)


class Customer(AggregateRoot[CustomerId]):
    name: Annotated[str, Personal(of="id")]  # the customer is the person
    email: Annotated[str | None, Personal(of="id")] = None


class Order(AggregateRoot[OrderId]):
    customer_id: CustomerId
    delivery_address: Annotated[str | None, Personal(of="customer_id")] = None
    courier_id: str
    courier_phone: Annotated[str | None, Personal(of="courier_id")] = None
```

- Both the marked field and its `of` field must be mapped to columns.
  One aggregate may hold data about several people: `delivery_address` is
  the customer's, and `courier_phone` the courier's.
- The *subject* is the value of the `of` field, as text: a user id, a
  customer id. Requests name people by these internal ids, never by an
  email address.
- Write an id as Python prints it, the form that text columns and the
  audit trail hold: a UUID in lower case with dashes, a number without a
  sign, spaces, or leading zeros. Where domains name people by UUID or
  integer, other spellings (`3F2A...`, `{3f2a...}`, `urn:uuid:3f2a...`,
  `042`, `+42`) are refused with the form to use, so a person is never
  exported or erased in part: `Write the subject as its id prints: 42.`
- A subject is 1 to 255 printable characters.
- Mark the whole field. A marker inside a value object or a list is
  refused, as for events.
- `manage.py check` lists the markers that privacy cannot follow, and
  staging and production refuse to start with them. `problems()` returns
  the same sentences in code.
- Users of `domainkit.contrib.auth` are marked already. Erasing one also
  ends the account and its credentials, and keeps the username taken
  ([users.md](auth/users.md#users-personal-data)).

`personal_models()` returns every installed aggregate with marked fields,
as `PersonalModel`s: each has its `domain`, `model`, `cls`, and
`subjects` (each `of` field, and the fields about that subject).

## Subject access: `privacy export`

```bash
python manage.py privacy export --subject 3f2a... --output ann.json
```

The export holds every marked field about the subject, in every installed
domain and every database that a domain uses (each shard too), as JSON:

```json
{
  "subject": "3f2a...",
  "exported_at": "2026-10-19T12:00:00+00:00",
  "domains": {
    "shop": {
      "customer": [{"id": "3f2a...", "fields": {"name": "Ann", "email": "ann@example.com"}}],
      "order": [{"id": "91c0...", "fields": {"delivery_address": "1 Nile St"}}]
    }
  },
  "copies": {
    "phones": {"phones": [{"phone": "+33612345678", "verified_at": "..."}]},
    "identity": {"identities": [{"provider": "idp", "subject": "1098765", "email": "ann@example.com", "linked_at": "..."}]},
    "devices": {"push_tokens": [{"platform": "fcm", "created_at": "..."}]},
    "webhooks": {"deliveries": [{"id": "...", "event_type": "customer.registered", "status": "succeeded", "created_at": "...", "body": {"type": "...", "data": {"customer_id": "3f2a...", "email": "ann@example.com"}}, "withheld": false}]},
    "idempotency": {"responses": [{"created_at": "...", "status_code": 201, "body": "{\"email\": \"ann@example.com\"}", "withheld": false}]},
    "messaging": {"messages": [{"id": "...", "kind": "task", "target": "shop.send_receipt", "status": "dead", "arguments": {"customer_id": "3f2a...", "email": "ann@example.com"}}]}
  }
}
```

`copies` holds what installed domains keep about the subject outside
their aggregates, by the domain's label. Each domain gives them through
its own `on_export_subject` hook:

- `phones`: the numbers the user proved, and when.
- `identity`: the accounts at identity providers linked to the user: the
  provider, the user's id there, the email, and when it was linked.
- `devices`: the user's passkeys and bound devices, by name and dates
  only, and their push tokens, by platform only.
- `webhooks`: the webhook deliveries about the subject.
- `idempotency`: the responses kept for their `Idempotency-Key` retries.
- `messaging`: the outbox messages that still hold their data.
- `limits`, `accounting`, `approvals`: limits, accounts, and approval
  requests about the subject, when those apps are installed.

The subject of `phones`, `identity`, and `devices` is a user's id (a
UUID). Another subject finds nothing there.

A copy may hold other people's data too (an appointment names the patient
and the doctor). So each copy gives only what is about the subject:

- of a webhook body's `data` and of a message's arguments, the fields
  whose `Personal(of=...)` names them, with that `of` field;
- of a kept response, the body only if they were its caller
  (`"withheld": true` otherwise: it is what the caller saw).

A webhook body whose data `expose(payload=...)` picked is withheld too,
because which of its fields are whose is not known. Your own hooks should
do the same: `domainkit.ddd.fields_about(cls, values, subject)` picks a
subject's fields by the markers of `cls`.

`--output` writes a new file that only its owner can read (mode `0600`),
and refuses an existing one (`ann.json exists; name a new file.`).
Without `--output`, the export goes to standard output. In code,
`await export_subject(subject)` returns the same data:

```python
from domainkit.contrib.privacy import export_subject

data = await export_subject("3f2a9c1e-0d2b-4c55-9f0e-1f2a3b4c5d6e")
# {'phones': [{'phone': '+33612345678', 'verified_at': '...'}]}
data["copies"]["phones"]
```

Each export is recorded in the audit trail (`privacy.export`).

## Erasure: `privacy erase`

```bash
python manage.py privacy erase --subject 3f2a... --reason "ticket 812"
```

It asks for the subject again before it starts; `--noinput` does not
ask. The reason (default `request`) is one line of at most 255
characters. It is recorded with every erasure, so name a ticket, not the
person.

In code, `await erase_subject(subject, reason=...)` does the same, and
returns how many aggregates it changed. Erasure runs in this order:

1. **The holds.** Every domain's `on_retain_subject` hook is asked
   whether the data must be kept for now
   ([What the law keeps](#what-the-law-keeps-holding-an-erasure)). One
   answer holds the whole erasure, and nothing is erased.
2. **The aggregates.** Each aggregate that holds data about the subject
   is loaded through a [unit of work](learning.md#words), and its marked
   fields are erased:
   - a field that may be `None` becomes `None`;
   - text becomes `""`, or a unique `erased-...` value when a unique
     constraint covers it;
   - any other field (a date of birth, a value object) is erased by the
     aggregate itself, which defines the method, so its rules still hold:

     ```python
     from datetime import date


     class Patient(AggregateRoot[PatientId]):
         born_on: Annotated[date, Personal(of="id")]

         def erase_personal_data(self, fields: frozenset[str]) -> None:
             if "born_on" in fields:
                 self.born_on = date(self.born_on.year, 1, 1)  # keep the year
     ```

3. **The event.** The unit of work commits with a `PersonalDataErased`
   event: the model, the aggregate's id, the fields' names (never their
   values), and the reason. So the version is raised (a concurrent change
   fails instead of being lost), the audit trail records it, and your
   handlers can react, for example to remove a search index entry.
4. **The copies.** Every installed domain erases the copies it keeps
   about the subject outside its aggregates, through its
   `on_erase_subject` hooks. These are the numbers, identities, passkeys,
   devices, and push tokens of `contrib.phones`, `contrib.identity`, and
   `contrib.devices`, and the webhook deliveries, kept responses, and dead
   outbox messages ([the table below](#what-erasure-reaches-and-when)).
   Each domain that erased something is recorded, with counts only
   (`privacy.copies_erased`).
5. **The audit keys.** The subject's keys in the audit trail are
   forgotten, so their pseudonyms there can no longer be linked to them
   (`audit forget`), and the erasure itself is recorded, with the reason.

Rows stay: an order is still an order, without the person in it. Deleting
an account altogether is your domain's own use case. Erasing again is
harmless: fields already erased are left alone (an
`erase_personal_data` method should do the same).

`PersonalDataErased` has the fields `model` (`<domain>.<model>`),
`aggregate_id`, `fields`, and `reason` (`request`, or `retention` from a
purge):

```python
from domainkit.contrib.privacy import PersonalDataErased
from domainkit.ddd import event_bus


async def drop_from_index(event: PersonalDataErased) -> None:
    if event.model == "shop.customer":
        await search_index.delete(event.aggregate_id)


event_bus.subscribe(PersonalDataErased, drop_from_index)
```

### What erasure reaches, and when

Personal data is copied out of aggregates as the application works: into
events, the messages that carry them, the bodies that webhooks send, the
responses that `Idempotency-Key` retries replay. Erasure finds a copy by
the people it is about: the subjects of the `Personal` fields of the
event or the task arguments that it came from. So mark those too. Some
copies are erased at once; others only expire.

| Where | Erased at once | Only expires, or stays |
| --- | --- | --- |
| Aggregates with `Personal` fields | Every marked field, through a unit of work | Unmarked fields stay: mark them |
| The audit trail (`domainkit.contrib.audit`) | The subject's keys, so every pseudonym of them is unlinkable | Unmarked event fields stay in `details` for good, because the trail is append-only: mark every personal field of an audited event |
| Webhook deliveries (`domainkit.contrib.webhooks`) | The body of each delivery about the subject; a pending one is cancelled, and `replay` sends none of them again. Its row and attempt log stay, without the body | What receivers were already sent: tell them yourself |
| Kept responses (`domainkit.contrib.idempotency`) | Each response the subject asked for, or whose request committed an event about them. The key stays, so a retry gets 409 `idempotency_response_unavailable` and never runs again | A response about the subject that another caller got, with no event naming them, until `IDEMPOTENCY.TTL_SECONDS` (24 hours by default). Then a key that committed is kept as a tombstone for `IDEMPOTENCY.TOMBSTONE_SECONDS` (7 days): without its response, its headers, or whom it was about, only a one-way hash of the caller and the key, which erasure and exports cannot find or link to anyone, as an erased key keeps |
| Outbox messages (`domainkit.contrib.messaging`) | Dead messages about the subject: their arguments are blanked, and `messages retry` refuses them | Pending ones run as planned, and are blanked when done; the erasure logs how many, so run it again once they have run (what they wrote, such as a new delivery, is erased then). Done and cancelled messages hold no arguments. Error texts (`last_error`) keep what an exception said, for `MESSAGING.RETENTION_SECONDS` (7 days) once finished, and for good while dead: keep personal data out of exception messages. The inbox (`domainkit_inbox`) holds handler names, message ids, and times only |
| Limits set for a person, and their counted movements (`domainkit.contrib.limits`) | Both: each limit whose subject is the person is removed (audited, `LimitRemoved`), and the usage of each kind of subject they are; `privacy export` lists them first | The audit trail's `LimitSet` rows, which name the subject's id, as every row names its actor's ([limits.md](contrib/limits.md#privacy)) |
| Accounts kept for a person (`domainkit.contrib.accounting`) | The link: each account's `holder`, the one field in which the ledger keeps the subject, names one `erased:` pseudonym, the same for all their accounts; `privacy export` lists their accounts and postings first. While they hold an open account with money on it or held, the erasure is held (`erasure_held`) | The books themselves: balances, entries, and postings, which the law asks to keep and which are append-only. Account codes and names, references, descriptions, memos, and statement lines are kept as written, so nothing names the subject after erasure only if the project kept personal data out of them: an opaque code, never the person's id ([accounting.md](contrib/accounting.md#accounts)) |
| Encrypted fields (`domainkit.contrib.keys`) | The subject's keys, in every hosting database, so every value sealed for them reads as None, in replicas, and in backups and dumps taken afterwards (one taken before still opens them until it expires, or is restored and the erasure applied again: [keys.md](contrib/keys.md#backups-taken-before-an-erasure)); and the blind indexes of those values | Their ciphertext, which nothing opens; values of theirs sealed under a data key instead (a field with no subject); blind indexes outside the rows naming them ([keys.md](contrib/keys.md#erasing-a-person-crypto-shredding)) |
| Approval requests (`domainkit.contrib.approvals`) | The link: the subject's id, as maker or approver, becomes one `erased:` id, and the seals are made again for it; their pending requests are cancelled, an approved one of theirs not run yet fails, and the reasons they wrote are blanked; `privacy export` lists their requests and decisions first | The record of who approved what, which a business must keep: each request's command as approved, its decisions, and its fingerprint, verifying; reasons others wrote ([approvals.md](contrib/approvals.md#audit-and-privacy)) |
| Verified numbers (`domainkit.contrib.phones`), identities at providers (`domainkit.contrib.identity`), passkeys, bound devices, and push tokens (`domainkit.contrib.devices`) | All of them, through each app's own `on_erase_subject` hook, as deleting the user does: the numbers and pending codes, the provider accounts (their subject and email), the keys, devices, and tokens. Each app's `on_export_subject` hook lists them under `copies` (`phones`, `identity`, `devices`), keys and devices by name and tokens by platform only. The subject is the user's id | The audit trail's rows of them (`identity.identity_linked`, `devices.passkey_added`), which name the user's id as every row names its actor's |
| Caches | Nothing | Each cache's timeout (the principal cache: `AUTH.PRINCIPAL_CACHE.TTL`, 5 seconds) |
| Logs, error reports, backups, replicas, exports written to files | Nothing, but the encrypted fields of the subject, whose keys are gone, in copies made after the erasure | Your retention of each: name it in your records of processing; a backup from before the erasure holds the subject's keys too |

### The copies your domain keeps

A domain of your own that keeps copies (a search index, a read model, a
CSV it sends) erases and exports them the same way. It registers hooks in
its `DomainConfig.ready()`, and only the domains that registered are
called:

```python
from domainkit.apps import DomainConfig
from domainkit.apps.hooks import on_erase_subject, on_export_subject


async def erase_documents(subject: str, reason: str) -> dict[str, int]:
    removed = await index.delete_by_customer(subject)
    return {"documents": removed}  # counts only: they are audited


async def export_documents(subject: str) -> dict[str, object]:
    found = await index.find_by_customer(subject)
    return {"documents": found} if found else {}


class SearchConfig(DomainConfig):
    def ready(self) -> None:
        on_erase_subject(erase_documents)
        on_export_subject(export_documents)
```

- An erase hook takes `(subject, reason)`, and an export hook takes
  `(subject)`. Both are coroutine functions. A hook with other arguments
  fails `setup()`, naming the domain.
- An erase hook returns counts by kind: lower-case names and whole
  numbers. Anything else is refused (`ImproperlyConfigured`), because it
  would go into the audit trail. Count the copies left to finish under
  `pending`; the erasure then warns you to run it again.
- An export hook returns what it found, or `{}`.
- A domain's erase hooks are summed into one record of what it erased,
  and its exports are merged under its label.
- `on_erase_subject(hook, domain="search")` registers for another
  installed domain's label, from outside its `ready()` (a test, a
  command). `contrib.phones`, `contrib.identity`, and `contrib.devices`
  register their hooks under their own labels this way.
- Hooks run in `INSTALLED_DOMAINS` order, and each domain's in the order
  it registered them.

Until 0.12, these hooks were the methods `erase_subject` and
`export_subject` of `DomainConfig`. 1.0 removed them, and `setup()`
refuses a domain that still defines one (`apps.E002`, through 1.x),
because nothing would call it.

### What the law keeps: holding an erasure

The GDPR's right to erasure gives way where a law asks a business to keep
a record (Article 17(3)(b)). A bank keeps its books, a payment's
authorization, and who approved it, for as long as its regulator asks
(five to ten years, as a rule).

Before any data is erased, every domain's `on_retain_subject` hooks are
asked whether the person's data must be kept for now. One sentence from
any of them holds the whole erasure: nothing is erased, `privacy.erase`
is recorded as denied with each domain's sentence, and `ErasureHeld`
(`erasure_held`) is raised. `privacy erase` reports it, naming the
domains (FIN-94):

```python
from domainkit.apps import DomainConfig
from domainkit.apps.hooks import on_retain_subject


async def hold_loans(subject: str, reason: str) -> str | None:
    if await loans.open_for(subject):
        return "Has a loan still running: the contract is kept until it ends."
    return None


class LendingConfig(DomainConfig):
    def ready(self) -> None:
        on_retain_subject(hold_loans)
```

```text
Error: Nothing was erased. Kept by lending: Has a loan still running: the contract is kept until it ends.
```

- A hook answers a sentence of 1 to 255 printable characters, or `None`.
  Anything else raises `ImproperlyConfigured`.
- The sentence is shown to the operator and recorded. Never put the
  person's data in it.
- `ErasureHeld.held` maps each domain's label to its sentence.
- The erasing hooks run only once every check let the erasure go on.

A check and the erasure are two transactions, so what changed between
them may hold the erasure after all. So the `on_erase_subject` hooks of
the domains that hold erasures run first, before anything else is erased.
One of them may raise `ErasureHeld` itself, after it checked again under
its own locks (the ledger does, SEC-16b-4). That is recorded as denied
the same way. Only a second holding domain that holds it then leaves what
the first one erased.

The ledger holds the erasure of a holder of an open account with money on
it or held. It closes their open accounts as it erases, so that no
account takes money for an erased holder;
[accounting.md](contrib/accounting.md#accounts) says how to settle an
account that holds an erasure.

What the law asks to keep after the relationship ends, the ledger and
approvals keep pseudonymized, not deleted: the books and the evidence of
who checked whom, with the person's id replaced by one `erased:` id.

!!! warning
    **That is pseudonymization, not anonymization.** The record still
    links to the person through what else is kept: the trail's actors and
    `request_executed`'s approvers, the accounts' history, the business's
    own records. The ids of others that a request is about (`parties`: a
    payee, the user a grant promotes) stay as written in its command. So
    the erased data is still personal data under the GDPR, kept for the
    reason the law gives.

## Retention: `privacy purge`

Declare on a domain's configuration how long its rows are kept, with
`Retention`:

```python
from datetime import timedelta

from domainkit.apps import DomainConfig
from domainkit.contrib.privacy import Retention


class ClinicConfig(DomainConfig):
    retention = (
        # Delete access log rows after 90 days.
        Retention(
            table="clinic_access_log", field="accessed_at", keep=timedelta(days=90)
        ),
        # Keep visits for 10 years, but erase their notes after one.
        Retention(
            aggregate=Visit, field="visited_at", keep=timedelta(days=365), erase=True
        ),
    )
```

```bash
python manage.py privacy purge --dry-run   # count what would go
python manage.py privacy purge             # in a nightly job
```

The command prints one line per table and database:

```text
clinic.clinic_access_log on default: would delete 3 row(s) from before 2026-07-21T12:00:00+00:00
clinic.clinic_visits on default: would erase 3 row(s) from before 2025-10-19T12:00:00+00:00
```

Without `--dry-run`, it says `deleted` and `erased`. With no policy, it
says `No domain declares a retention.`

- `field` is a `UTCDateTime` or `Date` column. Rows older than `keep` go.
- A `Retention` names a `table` or an `aggregate`, not both, and `keep`
  is positive. `erase=True` needs an `aggregate`. Other policies raise
  `ValueError`.
- A table's old rows are deleted 500 at a time, each batch in its own
  transaction, so a purge never holds a table for long.
- With `erase=True`, an aggregate's old rows stay, and their marked
  fields are erased through units of work, as `privacy erase` does, with
  a `PersonalDataErased` event (reason `retention`). Its marked fields
  must all be optional (they become `None`), so erased rows are never
  selected again.
- A policy names a table of its own domain. **Append-only tables are
  never purged**: the audit trail and its seals, and any table tagged
  `info={"append_only": True}`, are refused at `check`.
- If the database itself refuses a delete (a table protected by a
  trigger, as `domainkit.contrib.audit.protection.protect` makes it; rows
  that other rows still refer to), that table is reported and recorded as
  a failure. Nothing is disabled or forced, the other tables are purged,
  and the command fails (`Not purged: ...`). Forget people in the audit
  trail instead (`audit forget --anonymous-days`).
- Each table and database purged is recorded in the audit trail
  (`privacy.purge`, with the rows and the cutoff).

In code, `await purge(dry_run=False)` returns a list of `Purged`: each
has `policy` (`<domain>.<table>`), `database`, `action` (`deleted`,
`erased`, or `would delete`, `would erase`), `count`, `before`, and
`refused` (why the database stopped it, or `None`).
`retention_policies()` returns every domain's policies, resolved to
tables, as `Policy` objects.

## In the audit trail

| Action | Target | Details |
| --- | --- | --- |
| `privacy.export` | the subject | `records`, `copies` (the domains that gave copies) |
| `privacy.erase` | the subject | `aggregates`, `reason`; when held, `held` (each domain's sentence) |
| `privacy.copies_erased` | the subject | `domain`, `copies` (counts by kind), `reason` |
| `privacy.personal_data_erased` | | `model`, `aggregate_id`, `fields`, `reason` |
| `privacy.purge` | `<domain>.<table>` | `database`, `before`, `operation` (`deleted` or `erased`), `rows`; `reason` when refused |

The actor is whoever runs them. From the `privacy` command, it is the
operator, `operator:<name>`: from `--operator` (after the action:
`privacy erase --subject ... --operator alice`), `DOMAINKIT_OPERATOR`, or
the login name ([operators](reference/commands.md#operators)). From code
run outside a request, it is `system`.

## Options

| Command | Options |
| --- | --- |
| `privacy export` | `--subject` (required), `--output` (a new file, mode `0600`), `--operator` |
| `privacy erase` | `--subject` (required), `--reason` (default `request`), `--noinput`, `--operator` |
| `privacy purge` | `--dry-run`, `--operator` |

| `Retention` field | What it sets |
| --- | --- |
| `field` | The `UTCDateTime` or `Date` column (or aggregate field) that dates each row. |
| `keep` | How long rows are kept: a positive `timedelta`. |
| `table` | The table whose old rows are deleted. |
| `aggregate` | The aggregate whose old rows are erased (with `erase=True`). |
| `erase` | `False`: delete rows. `True`: keep rows and erase their personal fields. |

`check_subject(subject)` and `check_reason(reason)` return their argument
if `privacy` would accept it, and raise `ValueError` with the reason
otherwise.

## Errors and refusals

| What you see | Why | What to do |
| --- | --- | --- |
| `Write the subject as its id prints: 42.` | The id is written in another form (`042`, upper case, braces). | Write it as Python prints it. |
| `A subject is 1 to 255 characters, without control codes.` | An empty or too long subject. | Pass the person's id. |
| `A reason is 1 to 255 characters, without control codes.` | A bad `--reason`. | Name a ticket. |
| `Erasure cancelled.` | The subject typed again did not match. | Type it again, or pass `--noinput`. |
| `Nothing was erased. Kept by ...` (`ErasureHeld`, `erasure_held`) | A domain must keep the data for now. | Settle what holds it, then erase again. |
| `ann.json exists; name a new file.` | `--output` names an existing file. | Name a new file. |
| `ImproperlyConfigured` | A marker privacy cannot follow, or a hook that answered something other than counts or a sentence. | Fix the marker or the hook; `check` lists the markers. |
| `Not purged: ...` | The database refused to delete a table's rows. | Remove the policy for an append-only table. |

## Common mistakes

- **Naming people by email address.** Requests name people by their
  internal id. Find the id first, then export or erase.
- **Leaving fields of events unmarked.** The audit trail keeps them for
  good, and erasure cannot find their copies. Mark every personal field
  of every event and task argument.
- **Putting the person's id in an account code or a reference.** Those
  are kept as written. Use an opaque code, and put the person in
  `holder` only.
- **Putting personal data in exception messages.** `last_error` keeps
  them. Write fixed messages.
- **Returning data from an erase hook.** It goes into the audit trail,
  so only counts are accepted.

## See also

- [Audit](contrib/audit.md#audit-who-did-what-when-from-where): the audit
  trail, `Personal` on events, and `audit forget`.
- [Keys](contrib/keys.md#erasing-a-person-crypto-shredding): erasing a
  person by forgetting their keys.
- [Users](auth/users.md#users-personal-data): what erasing a user does to
  the account.
- [Commands reference](reference/commands.md#operators): operators and
  the `privacy` command.
