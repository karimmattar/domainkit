# 0159. Read a replica only while it is known to be within its bound

- Status: Accepted
- Date: 2026-11-16
- Amends: [0024](0024-replicas-and-derived-tables.md)

## Context

[ADR 0024](0024-replicas-and-derived-tables.md) sends read-only units of
work to an alias's replicas in turn, and says: "There is no automatic
failover: a replica that is down fails its requests, and `/ready`
reports it." A project that runs an identity service on domainkit marks
its permission checks read-only, so a replica behind its primary would
let them read stale memberships (user report D1). `replica=False` now
keeps such reads on the primary. What stayed open:

- A replica far behind (replay held up by a long query, a network
  partition, a standby being rebuilt) is read as if it were current.
  Nothing bounds how stale a read may be.
- A replica out of reach fails every read-only unit of work sent to it.
- `/ready` checks every alias, replicas included. One replica down takes
  every process out of rotation, though the primary could serve the
  reads.

What others do:

- **Django** leaves lag to the project. Its primary/replica router example
  "doesn't provide any solution for handling replication lag".
- **Rails** switches roles per request. Writes, and reads within `delay`
  (2 seconds by default) of the user's last write, go to the primary;
  the last write is kept in the session. It promises "read your own
  write" only, measures no lag, and "doesn't support automatic load
  balancing of replicas".
- **Laravel**'s `sticky` option reads from the write connection for the
  rest of a request once the request wrote.
- **Spring** routes by the transaction's read-only flag
  (`AbstractRoutingDataSource`, `LazyConnectionDataSourceProxy`'s
  read-only data source), with no lag or health logic.
- **PgBouncer** knows nothing of lag, and has "no mechanisms to skip
  unreachable hosts". **libpq**'s `target_session_attrs` picks a standby
  by its role, not its lag.
- **Pgpool-II** checks every `sr_check_period` (10 seconds). Past
  `delay_threshold` (WAL bytes) or `delay_threshold_by_time` (from
  `pg_stat_replication.replay_lag`), it "stops sending the SELECT queries
  to the standby server and starts routing everything to the primary
  server", until the standby catches up. Both are off by default.
  **ProxySQL** shuns a MySQL replica past `max_replication_lag` (off by
  default) until it is back under it.
- **GitLab**'s load balancer checks each replica at most every
  `replica_check_interval` (60 seconds, randomized up to twice that),
  skips one more than `max_replication_lag_time` (60 seconds) behind,
  and reads the primary when none is left. It reads lag on the replica as
  `now() - pg_last_xact_replay_timestamp()`. Because a replica may "not
  replay WAL data for a while, despite being up to date ... when a
  primary does not receive any writes", when that check fails it compares
  the replica's replay position with the primary's WAL. After a write it
  keeps the user on the primary until a replica replays the write, or
  for 30 seconds.

Ways to measure lag on PostgreSQL, and what each costs:

- `now() - pg_last_xact_replay_timestamp()` on the standby: one cheap
  statement. The timestamp is when the last replayed commit was made on
  the primary. It is NULL until a transaction is replayed, it grows while
  the primary commits nothing (an idle primary looks far behind), and it
  compares the two servers' clocks.
- `pg_last_wal_receive_lsn() = pg_last_wal_replay_lsn()`, read as "no
  lag", is the usual guard against an idle primary in monitoring. It also
  reads "no lag" when the WAL receiver stalled or lost its connection:
  the standby replayed all it received, not all the primary wrote. Wrong
  for routing.
- `pg_stat_replication.replay_lag` on the primary: needs each standby
  mapped to its `application_name`, shows NULL a short while after an idle
  standby caught up, and its columns need `pg_read_all_stats`.
- The primary's `pg_current_wal_lsn()` against the standby's
  `pg_last_wal_replay_lsn()`: exact, in bytes rather than seconds, and it
  needs both servers.

The options considered:

1. **Leave it to projects**, with `replica=False` and the docs. What the
   report found.
2. **Measure lag in every read-only unit of work.** Exact; a round trip
   before every read takes back much of what a replica saves.
3. **A loop per process that probes every replica on a period.** It
   probes idle processes too, and is one more task to start and stop
   with the application.
4. **A reading per replica, kept, and refreshed in the background** when
   read-only work finds it older than a second. A reading holds while its
   lag plus its age is within the bound.
5. **Stickiness across requests**, as Rails (the session) and GitLab
   (Redis) keep it: it needs a session or a shared cache keyed by user,
   which domainkit's core has neither of.

## Decision

Option 4, with read-your-writes within a task.

- **`MAX_LAG`**, on a replica's `DATABASES` entry: seconds, 1 to 86400,
  None by default. Only a replica takes it. Without it, a replica is read
  as before.
- **When a replica is read.** While its last reading's lag plus the
  reading's age is within `MAX_LAG`: a replica that stopped replaying
  since cannot be further behind. Otherwise read-only work reads another
  replica within its bound, in turn, or the primary; never a replica past
  its bound.
- **Readings.** Each event loop keeps one per replica. Read-only work
  that finds it older than a second (half of `MAX_LAG` if less) starts a
  probe as a background task (`domainkit.db.replica_lag`), one per replica
  at a time, and does not wait for it. An idle process sends none. The
  first read after a quiet spell longer than `MAX_LAG` reads the primary
  while its probe runs. A probe may take 5 seconds.
- **The probe**, on PostgreSQL. One statement on the replica:
  `pg_is_in_recovery()`, and `clock_timestamp() -
  pg_last_xact_replay_timestamp()`. A server not in recovery is its own
  primary: no lag. Within `MAX_LAG`, that age is the lag. Past it, or
  NULL: the primary's `pg_current_wal_lsn()`, then whether the replica's
  `pg_last_wal_replay_lsn()` reached it. If it did, no lag as of the
  primary's answer (an idle primary); if not, the age, or unknown. On
  SQLite the replica only has to answer `SELECT 1`. A probe that fails or
  overruns counts as out of reach.
- **Units of work** pick where to read when they are made, and check
  again each time they are entered, keeping their replica while it may
  still be read. `run` enters one again for each run.
- **Read your own writes.** After a unit of work commits on a primary
  with replicas, read-only work later in the same task (a request, the
  handling of a message) reads a replica with `MAX_LAG` only once a
  reading shows it replayed that commit: the reading's time less its lag
  is at or after the commit. As Laravel's `sticky`, within one request.
- **Each switch**, reads leaving a replica or coming back to it, is a log
  line (a warning when they leave, saying why) and
  `db_replica_switches_total{database, to}`. Each lag read is
  `db_replica_lag_seconds{database}`.
- **`/ready`** leaves out a replica with `MAX_LAG`, and its `STATEMENTS`
  pool: reads have the primary while it is away. One without it is
  checked as before.

## Consequences

- A project bounds how stale a read-only unit of work may read, and can
  lose a replica without losing reads or readiness. A read that must see
  other requests' latest commits, such as a permission check, still
  keeps to the primary with `replica=False`.
- The bound relies on the primary's and the replica's clocks agreeing
  (NTP), as `pg_last_xact_replay_timestamp()` compares them.
- While no replica is read, the primary takes the reads: size its pool
  and `max_connections` for that.
- Work already running on a replica that goes away fails, as before.
  Reads leave it within about a second, when the next probe fails.
- A process that reads probes each replica at most about once a second
  per event loop: one statement, or three while the primary is idle (one
  of them on the primary). Each loop logs its own switches.
- Read-your-writes holds within a task. A later request of the same
  client may read a replica that has not replayed its write yet, at most
  `MAX_LAG` behind.
- 0024's "no automatic failover" no longer holds for a replica with
  `MAX_LAG`; the rest of 0024 stands.

## References

- Django, [Multiple databases: an example](https://docs.djangoproject.com/en/5.2/topics/db/multi-db/)
- Rails, [Multiple Databases with Active Record: automatic role switching](https://guides.rubyonrails.org/active_record_multiple_databases.html)
- Laravel, [Read and write connections](https://laravel.com/docs/12.x/database#read-and-write-connections)
- Spring, [`AbstractRoutingDataSource`](https://docs.spring.io/spring-framework/docs/current/javadoc-api/org/springframework/jdbc/datasource/lookup/AbstractRoutingDataSource.html) and [`LazyConnectionDataSourceProxy`](https://docs.spring.io/spring-framework/docs/current/javadoc-api/org/springframework/jdbc/datasource/LazyConnectionDataSourceProxy.html)
- PgBouncer, [configuration](https://www.pgbouncer.org/config.html); libpq, [connection parameters](https://www.postgresql.org/docs/current/libpq-connect.html)
- Pgpool-II, [streaming replication check](https://www.pgpool.net/docs/latest/en/html/runtime-streaming-replication-check.html)
- ProxySQL, [backend monitoring](https://www.proxysql.com/documentation/backend-monitoring)
- GitLab, [database load balancing](https://docs.gitlab.com/administration/postgresql/database_load_balancing/), [its design](https://docs.gitlab.com/ee/development/database/load_balancing.html), and [`host.rb`](https://gitlab.com/gitlab-org/gitlab/-/blob/master/lib/gitlab/database/load_balancing/host.rb)
- PostgreSQL, [recovery information functions](https://www.postgresql.org/docs/current/functions-admin.html) and [`pg_stat_replication`](https://www.postgresql.org/docs/current/monitoring-stats.html)
- Adjust, [checking PostgreSQL lag with `pg_last_xact_replay_timestamp`](https://engineering.adjust.com/post/checking_postgresql_lag_with_pg_last_xact_replay_timestamp/)
