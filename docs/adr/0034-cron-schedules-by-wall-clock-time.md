# 0034. Run cron schedules by wall-clock time, queued ahead of each run

- Status: Accepted
- Date: 2026-10-19

## Context

`@periodic(every=...)` runs a task once per fixed interval, counted from
the Unix epoch. Business schedules are not intervals: "at 03:00 on
weekdays, Cairo time" moves against UTC twice a year, and the hour around
a daylight saving change either does not exist or happens twice. Django
projects use cron, or Celery beat's crontab schedules, for this.

Three choices were open:

- **A library** (croniter, cronsim) or our own parser. A runtime
  dependency for some 200 lines is a cost every project pays, and the DST
  rule would be the library's, not ours to document.
- **What a DST change does.** Elapsed-time semantics run every 24 hours
  and drift by an hour against the wall clock. Wall-clock semantics must
  say what happens to a time that is skipped or repeated. Vixie cron and
  cronie run skipped times soon after the jump and do not run repeated
  times again, except for jobs with a wildcard hour or minute, which
  follow the clock.
- **How workers agree on a run.** Interval tasks queue the current slot
  every poll, deduplicated by a key. A cron run can be queued when it is
  due, which makes a worker that starts just after the time either miss it
  or run it late depending on how "due" is read; or queued ahead, when the
  previous one comes due, which keeps one row per run and lets a run whose
  time passes during a short outage still happen.

## Decision

- `domainkit.contrib.messaging.cron` parses five-field expressions, names,
  steps, lists, ranges, and the usual macros, with no new dependency.
  Day of month and day of week combine as in cron (either, when both are
  restricted). An expression that never runs is refused when parsed.
- Times are wall-clock times in an IANA zone, through `zoneinfo`; the
  `tzdata` extra provides the database where the system has none
  (Windows).
- A skipped time runs once, at the end of the gap. A repeated time runs
  once, at its first occurrence, unless the hour field starts with `*`,
  in which case it runs in both passes. Unlike cron, a wildcard minute
  under a fixed hour (`* 1 * * *`, "during 01:00") does not exempt a
  schedule: that hour happens once. A run is never doubled: runs are keyed
  by their instant.
- The worker of the home outbox queues a cron task's next run as soon as
  the previous one is due, with `dedupe_key = cron:<task>:<epoch>`, so
  every worker writes the same row and only one survives. Runs missed while
  no worker was up are not made up, except the one already queued.
- `every=` keeps its behaviour.

## Consequences

- Schedules read like crontab lines and keep their local time across DST,
  with a rule that is documented and tested minute by minute.
- The next run of each cron task sits in the outbox ahead of time; it
  shows in `messages --pending` as `scheduled`, not in the lag.
- A changed expression takes effect after the run already queued under the
  old one. Removing a task leaves its queued run, which dead-letters as an
  unknown task.
- The parser assumes a zone's offset never jumps by more than three hours
  at once when searching for the next run; larger jumps (Samoa skipping a
  day in 2011) still resolve correctly because such times fall in a gap.
