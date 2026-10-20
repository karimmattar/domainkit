# 0041. Answer privacy requests from Personal markers, through units of work

- Status: Accepted
- Date: 2026-10-19

## Context

The audit trail keeps personal data only as pseudonyms, and can forget a
person ([ADR 0018](0018-evidence-subjects-are-internal-ids.md) and the
audit trail's `forget`). The domains' own tables had nothing: no way to
export what they hold about someone (subject access), to erase it, or to
delete rows kept longer than they should be (retention).

Options considered:

- A registry where each domain lists its personal columns. It would drift
  from the model, and repeat what event fields already declare with the
  `Personal` marker.
- Erasing with SQL `UPDATE`s on the columns. Fast, but it bypasses the
  aggregates: no version bump (a concurrent change would silently undo the
  erasure), no event, nothing in the audit trail, and no domain rule can
  say a field must keep a value.
- Hooks every domain writes (`export`, `erase`), as Django apps would.
  Precise, but nothing is covered until every domain writes them, and
  nothing checks that they cover every field.
- Retention by partition dropping. domainkit partitions large tables
  ([ADR 0010](0010-postgresql-range-partitioning.md)) and can prune them,
  but most tables are not partitioned, and some data must outlive the
  person in it (an invoice, for tax).

## Decision

- **The `Personal` marker, on aggregate fields too**, names what is
  personal and whose it is (`of`). `domainkit.contrib.privacy` finds every
  mapped aggregate of an installed domain with such fields; both fields
  must be mapped to columns.
- **Export reads** every marked field whose subject column matches, in
  every database of the domain's target, grouped by domain and model.
- **Erasure goes through units of work.** Each aggregate is loaded,
  erased, and committed with a `PersonalDataErased` event naming the
  fields, never the values: versions, the audit trail, the outbox, and
  handlers all see it. Defaults cover the common cases (`None` for
  optional fields, `""` for text, a unique `erased-` value for unique
  text); any other field must be erased by the aggregate itself
  (`erase_personal_data`), or `check` reports it. Fields already erased
  are left alone, so a request can be run again. Then the subject's audit
  keys are forgotten.
- **Retention is declared on `DomainConfig.RETENTION`**, per table (rows
  deleted in batches, a transaction each) or per aggregate (personal fields
  erased, rows kept). Policies are checked when the application is built.
  Append-only tables (the audit trail, its seals, tables tagged
  `append_only`) are refused; a delete the database itself refuses is
  reported, recorded as a failure, and never forced.
- **Every operation is audited**, and the app requires
  `domainkit.contrib.audit`.

## Consequences

Projects get subject access, erasure, and retention for every aggregate
they mark, with the same markers their events use, and erasure keeps
every invariant the units of work enforce. Erasing goes aggregate by
aggregate, so it is slower than an `UPDATE`; a request touches one
person's rows, and retention erases in batches. Data outside marked
aggregate fields (free text that mentions a name, files in a storage, a
search index) is not found: handlers of `PersonalDataErased` are where a
project erases it. Auth's user model carries no markers yet, so its
usernames and emails are not exported or erased by this app. Erased rows
stay; deleting a person's account is the domain's own use case.
