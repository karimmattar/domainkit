# 0070. Name the operator of data-changing commands, and audit what they do

- Status: Accepted
- Date: 2026-10-25

## Context

Some management commands change or erase stored data with an operator's
authority, not the application's: `messages --retry` re-runs dead
handlers (payments among them), `--cancel` stops queued work,
`auditforget` erases pseudonym keys, `loaddata` replaces rows, `flush`
deletes them, `partitions --prune` drops partitions, and `privacy`
exports, erases, and purges. Up to 0.9.0 they ran as `system`, knew
nothing of who ran them, and only `privacy` left audit rows (as
`system`). PCI DSS 10.2 and SOC 2 ask that such actions be attributable
(re-audit N8, audit 0.9.0 DATA-07, FIN-65).

The audit trail is append-only, so the form of what these commands write
(their actor, their action names) is kept for good. The options for the
identity were: a new required flag (breaks scripts that run them today);
an identity from the environment only (invisible in shell history, easy to
leave unset); a flag, then an environment variable, then the OS user.

## Decision

- A command class sets `operator_actions = True` to take `--operator
  NAME`. The operator is that option, else `DOMAINKIT_OPERATOR`, else the
  user the process runs as (`getpass.getuser()`). A name is 1 to 64
  letters, digits, `.`, `_`, `@`, `+`, or `-`, starting with a letter or
  digit; another, or none at all, refuses the command before it sets up,
  with a message that never quotes the name.
- The command runs with a request context whose actor is
  `operator:<name>` (method `COMMAND`, path the command's name, a fresh
  request id), so what it audits itself, and work it queues, names the
  operator.
- Each action is recorded once it ends, as `operator.<command>[.<action>]`
  (`operator.messages.retry`, `operator.auditforget`, ...), `success` or
  `failure` with the error's class, its counts, and `operator_source`
  (`option`, `environment`, `system`), when `domainkit.contrib.audit` is
  installed. Dry runs, listings, and refusals before acting are not. A
  successful action whose row cannot be written fails the command; a
  failed one logs that its row was lost and raises its own error.
- `auditforget` records how many subjects and keys, not which subjects.
- The option is not added to every command: a project command may have an
  `--operator` of its own.

## Consequences

- An operator's retry, cancel, erasure, load, flush, prune, or purge is
  attributable from the trail, with how the name was found: `system`
  marks the weakest source, the OS user, which in a container may be
  `root`.
- Scripts keep working; they gain an identity from the environment or
  the OS user. 0.10 may require `--operator` or `DOMAINKIT_OPERATOR` in
  staging and production.
- Operator names land in the append-only trail and cannot be forgotten
  there: projects should use staff ids or handles, not email addresses.
- `privacy`'s option goes before its action (`privacy --operator alice
  purge`), as argparse reads a command's own options before its
  subcommand's.

## Amendment (2026-10-25): credentials and settings, and what "the OS user" is

A security review of 0.9.1 (SEC-r2-2) found that the commands an insider
would use against another person still ran as `system`: `resetmfa` turns
a user's second factor off, `createtoken` and `addcertificate` mint
credentials for a user, `changepassword` and `createsuperuser` set
passwords, `cleartokens` logs a user out, `flags` changes flags and
dynamic settings (a provider's key among them), and `webhooks replay`
sends events to receivers again. Their audit rows said `system`.

- These commands set `operator_actions = True` too. What they change is
  already audited by domain events (`auth.mfa_disabled`,
  `auth.token_created`, `auth.password_changed`, `auth.user_created`,
  `auth.user_logged_out`, `flags.flag_changed`, `flags.setting_changed`,
  `flags.setting_reset`), which now name `operator:<name>`; they add no
  `operator.<command>` row, as `privacy` adds none. `webhooks replay`,
  which no event records, is recorded as `operator.webhooks.replay` with
  how many deliveries were asked for and queued.
- Their listings (`flags list`, `flags settings`, `webhooks
  subscriptions`, `webhooks deliveries`) run as the operator too, since
  the operator is found before the command parses its action, but record
  nothing. `evidence`, `auditlog`, and `auditverify` only read, and take
  no operator.
- Each now needs a name, as `partitions` does: a container whose user has
  none sets `DOMAINKIT_OPERATOR`, for `cleartokens` run from cron too.

The decision above calls the last source "the OS user". It is the login
name `getpass.getuser()` returns: the `LOGNAME`, `USER`, `LNAME`, or
`USERNAME` environment variable, the first set, and only without them the
name of the process's user id. Whoever runs the command sets those as
easily as `DOMAINKIT_OPERATOR`, so `operator_source = system` is no
stronger than the other sources; the name attributes, it does not
authenticate, as the decision says.
