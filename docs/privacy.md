# Privacy requests and retention

People may ask what a service holds about them, and ask for it to be
erased; regulations (GDPR's access and erasure rights, and its storage
limitation) say how long data may be kept. `domainkit.contrib.privacy`
answers both across every installed domain, from the same `Personal`
markers the audit trail reads, and records each operation in the audit
trail. The decisions are in
[ADR 0041](adr/0041-privacy-through-personal-markers.md).

```python
INSTALLED_DOMAINS = [
    ...,
    "domainkit.contrib.audit",  # required: every operation is audited
    "domainkit.contrib.privacy",
]
```

## Marking personal data

Mark each field of an aggregate that holds personal data, with the field
that names whose it is, as events are marked
([contrib.md](contrib.md#audit-trail)):

```python
from typing import Annotated

from domainkit.ddd import AggregateRoot, Personal


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
  One aggregate may hold data about several people (`delivery_address` is
  the customer's, `courier_phone` the courier's).
- The subject is the value of the `of` field, as text: a user id, a
  customer id. Requests name people by these internal ids, never by an
  email address.
- Write an id as Python prints it, the form text columns and the audit
  trail hold: a UUID in lower case with dashes, a number without a sign,
  spaces, or leading zeros. Where domains name people by UUID or integer,
  other spellings (`3F2A...`, `{3f2a...}`, `urn:uuid:3f2a...`, `042`,
  `+42`) are refused with the form to use, so a person is never exported
  or erased in part.
- Mark the whole field: a marker inside a value object or a list is
  refused, as for events.
- `manage.py check` lists markers privacy cannot follow, and staging and
  production refuse to start with them.
- Users of `domainkit.contrib.auth` are marked already: erasing one also
  ends the account and its credentials, and keeps the username taken
  ([auth.md](auth.md#users-personal-data)).

## Subject access: `privacy export`

```bash
python manage.py privacy export --subject 3f2a... --output ann.json
```

Every marked field about the subject, in every installed domain and every
database a domain uses (each shard too), as JSON:

```json
{
  "subject": "3f2a...",
  "exported_at": "2026-10-19T12:00:00+00:00",
  "domains": {
    "shop": {
      "customer": [{"id": "3f2a...", "fields": {"name": "Ann", "email": "ann@example.com"}}],
      "order": [{"id": "91c0...", "fields": {"delivery_address": "1 Nile St"}}]
    }
  }
}
```

`--output` writes a new file only its owner can read (mode `0600`), and
refuses an existing one; without it the export goes to standard output.
In code, `await export_subject(subject)` returns the same data.

## Erasure: `privacy erase`

```bash
python manage.py privacy erase --subject 3f2a... --reason "ticket 812"
```

It asks for the subject again before it starts (`--noinput` does not).
Subjects and reasons are one line of at most 255 characters; the reason
(default `request`) is recorded with every erasure, so name a ticket, not
the person.
For every aggregate holding data about the subject:

- the aggregate is loaded through a unit of work and its marked fields
  are erased: a field that may be `None` becomes `None`; text becomes
  `""`, or a unique `erased-...` value when a unique constraint covers it;
- any other field (a date of birth, a value object) is erased by the
  aggregate itself, which defines the method, so its rules still hold:

  ```python
  def erase_personal_data(self, fields: frozenset[str]) -> None:
      if "born_on" in fields:
          self.born_on = date(self.born_on.year, 1, 1)  # keep the year
  ```

- the unit of work commits with a `PersonalDataErased` event (the model,
  the aggregate's id, and the fields' names, never their values), so the
  version is bumped (a concurrent change fails instead of being lost), the
  audit trail records it, and your handlers can react, say, to purge a
  search index;
- then the subject's keys in the audit trail are forgotten, so their
  pseudonyms there can no longer be linked to them (`auditforget`), and
  the erasure itself is recorded, with the reason.

Rows stay: an order is still an order, without the person in it. Deleting
an account altogether is your domain's own use case. Erasing again is
harmless: fields already erased are left alone (a method of the aggregate
should do the same). In code: `await erase_subject(subject, reason=...)`.

## Retention: `privacy purge`

Declare on a domain's configuration how long its rows are kept:

```python
from datetime import timedelta

from domainkit.apps import DomainConfig
from domainkit.contrib.privacy import Retention


class ClinicConfig(DomainConfig):
    RETENTION = (
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

- `field` is a `UTCDateTime` or `Date` column; rows older than `keep` go.
- A table's old rows are deleted, 500 at a time, each batch its own
  transaction, so a purge never holds a table for long.
- With `erase=True`, an aggregate's old rows stay and their marked fields
  are erased through units of work, as `privacy erase` does, with a
  `PersonalDataErased` event (reason `retention`). Its marked fields must
  all be optional (they become `None`), so erased rows are never selected
  again.
- A policy names a table of its own domain. **Append-only tables are
  never purged**: the audit trail and its seals, and any table tagged
  `info={"append_only": True}`, are refused at `check`. If the database
  itself refuses a delete (a table protected by a trigger, as
  `domainkit.contrib.audit.protection.protect` makes it; rows other rows
  still refer to), that table is reported and recorded as a failure,
  nothing is disabled or forced, the others are purged, and the command
  fails. Forget people in the audit trail instead (`auditforget
  --anonymous-days`).
- Each table and database purged is recorded in the audit trail
  (`privacy.purge`, with the rows and the cutoff).

## In the audit trail

| Action | Target | Details |
| --- | --- | --- |
| `privacy.export` | the subject | `records` |
| `privacy.erase` | the subject | `aggregates`, `reason` |
| `privacy.personal_data_erased` | | `model`, `aggregate_id`, `fields`, `reason` |
| `privacy.purge` | `<domain>.<table>` | `database`, `before`, `operation` (`deleted` or `erased`), `rows`; `reason` when refused |

The actor is whoever runs them. From the `privacy` command it is the
operator, `operator:<name>`, from `--operator` (before the action:
`privacy --operator alice erase --subject ...`), `DOMAINKIT_OPERATOR`, or
the login name ([operators](reference.md#operators)); from code run
outside a request, `system`.
