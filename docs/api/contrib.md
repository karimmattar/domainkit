# Public API: the other optional apps

Every `domainkit.contrib` app but auth: accounting, admin, approvals, audit,
content types, evidence, flags, idempotency, limits, mail, messaging,
notifications, object permissions, privacy, and webhooks ([contrib.md](../contrib.md)). Part of
the [public API](../api.md), under its compatibility policy.

## `domainkit.contrib.accounting`

A double-entry ledger: accounts, journal entries, holds, periods, reconciliation.

- `CAPTURES`: The source of the entries captures post; each one's reference is the hold's id.
- `REVERSALS`: The source of reversing entries; each one's reference is the reversed entry's id.
- `Account`: An account of the chart: one currency, one type, open or closed.
- `AccountClosed`: An account was closed: it takes no more postings or holds.
- `AccountCodeTaken`: Another account has that code.
- `AccountId`: An account's id (a `UUID`).
- `AccountIsClosed`: A posting or a hold on an account that is closed.
- `AccountNotEmpty`: Closing an account that still has a balance, or holds.
- `AccountNotFound`: No account has that id or code.
- `AccountOpened`: An account was opened.
- `AccountStatus`: Whether an account takes postings.
- `AccountType`: What an account counts, which decides its normal balance.
- `AccountingSettings`: How the ledger posts, holds, and reconciles; every key is optional.
- `AccountingUnitOfWork`: One transaction on the ledger, wherever `accounting` is routed.
- `CaptureHold`: Post the hold's amount (or `amount`, less) out of its account into another.
- `CloseAccount`: Close an account with nothing on it.
- `ClosePeriod`: Close a period: no entry may be dated in it afterwards.
- `Entry`: A posted journal entry: balanced lines, never changed once posted.
- `EntryId`: A journal entry's id (a `UUID`).
- `EntryNotFound`: No journal entry has that id.
- `EntryPosted`: A journal entry was posted; `reverses` names the entry it undoes.
- `Hold`: An amount reserved on an account until it is captured, released, or expires.
- `HoldFinished`: A hold was captured into an entry, released, or expired (`status`).
- `HoldId`: A hold's id (a `UUID`).
- `HoldNotActive`: Capturing or releasing a hold that was captured, released, or has expired.
- `HoldNotFound`: No hold has that id.
- `HoldPlaced`: An amount was reserved on an account.
- `HoldStatus`: Where a hold is: reserving, or finished one of three ways.
- `InsufficientFunds`: A change that would take an account that may not go negative below zero.
- `InvalidAmount`: An amount that is not one of its currency: unknown, inexact, or not positive.
- `InvalidEntry`: An entry that cannot be posted as written: its lines, or its accounts.
- `InvalidStatement`: A statement that cannot be imported: its format, or one of its lines.
- `Line`: One line of an entry to post: an amount on one side of one account.
- `NoOpenPeriod`: No accounting period covers the entry's date (`REQUIRE_PERIOD`).
- `OpenAccount`: Open an account of the chart.
- `OpenPeriod`: Open an accounting period from `starts_on` to `ends_on`, both included.
- `Period`: Dates entries may be booked on, from `starts_on` to `ends_on` included.
- `PeriodClosed`: An accounting period was closed: no entry may be dated in it any more.
- `PeriodId`: An accounting period's id (a `UUID`).
- `PeriodIsClosed`: An entry dated in a period that is closed.
- `PeriodNameTaken`: Another period has that name.
- `PeriodNotFound`: No accounting period has that id.
- `PeriodOpened`: An accounting period was opened.
- `PeriodStatus`: Whether a period takes entries.
- `PeriodsOverlap`: A new period whose dates another period covers, or that ends before it starts.
- `PlaceHold`: Reserve `amount` on an account, once per `source` and `reference`.
- `PostEntry`: Post a journal entry, once per `source` and `reference`.
- `Posting`: One line of a posted entry: an amount on one side of one account.
- `ReferenceReused`: A source and reference that name an entry or a hold made otherwise.
- `ReleaseHold`: Make a hold's amount available again, without posting anything.
- `ReversalNotReversible`: Reversing a reversal: it would move the money of the entry it undid again.
- `ReverseEntry`: Post the entry that undoes another, dated `booked_on` (default: today).
- `Side`: A posting's side.
- `TrialBalance`: Every account with a balance or movements in a period, and the totals.
- `UnbalancedEntry`: An entry whose debits and credits differ in some currency.
- `capture_hold`: Capture a hold into an entry; capturing it again returns it as captured.
- `close_account`: Close an account whose balance is zero and that holds nothing.
- `close_period`: Close a period, once the postings dated in it so far have committed.
- `defer_entry`: Post `command`'s entry once `uow` commits, through the outbox, once.
- `expire_holds`: Release every active hold whose time is up, each in its own transaction.
- `general_ledger`: The postings dated in a period, streamed as CSV or JSON, with totals.
- `on_booking`: See every entry as it is booked: `(uow, entry, accounts)`; raise to refuse it.
- `open_account`: Open an account; another with the same code is `account_code_taken`.
- `open_period`: Open a period; one whose dates another covers is refused.
- `place_hold`: Reserve an amount on an account, or return the hold placed under its reference.
- `post_entry`: Post a balanced entry, or return the one posted under its reference before.
- `release_hold`: Release a hold; one released or expired already is returned as it is.
- `reverse_entry`: Post the reversal of an entry: each of its postings on the other side.
- `trial_balance`: The trial balance of a period: every account with a balance or movements.
- `verify_ledger`: Check the books: entries balance, balances and held amounts add up.

## `domainkit.contrib.accounting.api`

The ledger over HTTP: accounts, entries, holds, periods, reports, reconciliation.

- `accounts`: The chart of accounts: list, read, open, close, and reconcile.
- `entries`: Journal entries: list, read, post, and reverse; never change or delete.
- `holds`: Holds: list, read, place, capture, and release.
- `periods`: Accounting periods: list, read, open, close, and their reports.
- `router`: Every route of the ledger, to mount under a prefix of the project's.

## `domainkit.contrib.accounting.hooks`

Hook points the ledger offers other apps (ADR 0101).

- `on_booking`: See every entry as it is booked: `(uow, entry, accounts)`; raise to refuse it.
- `on_posting`: Check an entry before `post_entry` posts it: `(command)`; raising refuses it.

## `domainkit.contrib.accounting.reconciliation`

Reconciliation: external statements matched to the ledger's postings.

- `Break`: Something a reconciliation left unmatched.
- `ByAmountAndDate`: A line and a posting of the same amount and currency, near in date, alone.
- `ByReference`: A line and a posting with the same reference, amount, and currency, near in date.
- `CsvStatement`: A statement as CSV text, with a header row.
- `Imported`: What `import_statement` did: lines stored, and lines it had already.
- `MatchingRule`: A way to pair statement lines with postings (`ACCOUNTING.MATCHING_RULES`).
- `PostingItem`: An unmatched posting on the account, as rules see it.
- `Reconciled`: What `reconcile` did: matches by rule, and the breaks left.
- `StatementItem`: An unmatched statement line, as rules see it.
- `StatementLine`: One line of an external statement, as a source reads it.
- `StatementSource`: Where statement lines come from: a file, a bank's API.
- `breaks_of`: The breaks the account's last reconciliation left, by date.
- `import_statement`: Store `source`'s lines for an account; a line stored before is skipped.
- `reconcile`: Match an account's statement lines to its postings; record matches and breaks.

## `domainkit.contrib.accounting.reports`

Reports of a period: the trial balance and the general ledger, as CSV and JSON.

- `GENERAL_LEDGER_FORMAT`: What a general ledger's JSON says it is (`format`).
- `TRIAL_BALANCE_FORMAT`: What a trial balance's JSON says it is (`format`).
- `CurrencyTotal`: What one currency's rows add up to: zero `net`, in balanced books.
- `LedgerProblem`: Something `verify_ledger` found wrong with the books.
- `PeriodSummary`: The period a report is of.
- `TrialBalance`: Every account with a balance or movements in a period, and the totals.
- `TrialBalanceRow`: One account's line: its balance before the period, its movements, and after.
- `general_ledger`: The postings dated in a period, streamed as CSV or JSON, with totals.
- `trial_balance`: The trial balance of a period: every account with a balance or movements.
- `verify_ledger`: Check the books: entries balance, balances and held amounts add up.
## `domainkit.contrib.approvals`

Maker-checker: commands that run only once other people approve them.

- `CAPTURES`: The guarded action of a hold's capture over its currency's threshold.
- `ENTRIES`: The guarded action of postings over their currency's threshold.
- `SUPERUSERS`: The guarded action of making a user a superuser.
- `AlreadyDecided`: This person approved or rejected the request already (409).
- `ApprovalGiven`: One person approved a request; `approvals` of `required` so far.
- `ApprovalNotNeeded`: No policy asks for approval of this command: call it directly.
- `ApprovalPending`: A guarded command called directly was submitted for approval instead (403).
- `ApprovalRequest`: A command that runs only once enough people other than its maker approve it.
- `ApprovalRequired`: A guarded command was called directly: it runs only once approved (403).
- `ApprovalsSettings`: How requests wait, who approves them, what is guarded; every key optional.
- `ApprovalsUnitOfWork`: One transaction on the requests, wherever `approvals` is routed.
- `ApproveRequest`: Approve a request, as `approver`, holding the `fingerprint` they saw.
- `CancelRequest`: Withdraw a request, as its `maker`.
- `Decision`: One person's approval or rejection of a request, and what they saw.
- `FingerprintMismatch`: What was approved is not what the request holds.
- `GrantSuperuser`: Make a user a superuser: they hold every permission.
- `Guard`: A guarded use case: its command, its service, and its policies.
- `InterestedParty`: Whoever the command is about may not approve or reject it.
- `InvalidArguments`: A command's arguments do not make the command, or are too large.
- `MakerCannotDecide`: Whoever submitted a request may not approve or reject it.
- `MakerNotAllowed`: Whoever submitted the command may not make it (any more).
- `NotTheMaker`: Only whoever submitted a request may cancel it.
- `Policy`: Who approves a command, how many of them, and for how long it waits.
- `PolicyGone`: The request's guard or policy no longer exists: it is neither decided nor run.
- `RejectRequest`: Reject a request, as `approver`, for `reason`.
- `RequestApproved`: A request has its approvals; its command is queued to run.
- `RequestCancelled`: The maker withdrew a request.
- `RequestExecuted`: An approved command ran.
- `RequestExpired`: A request's time ran out before its approvals came.
- `RequestFailed`: An approved command was refused when it ran; `error` is the refusal's code.
- `RequestId`: A request's id (a `UUID`).
- `RequestNotFound`: No approval request has that id.
- `RequestNotPending`: The request was approved, rejected, cancelled, or has expired.
- `RequestRejected`: One person rejected a request, with a reason (kept on the request).
- `RequestStatus`: Where a request is: waiting, decided, or run.
- `RequestSubmitted`: A maker submitted a command for approval.
- `RequestTampered`: The request was changed outside the application: its seal does not hold.
- `SubmitRequest`: Submit a guarded command for approval: its action, its arguments, its maker.
- `Threshold`: Amounts, one per currency, over which a policy applies.
- `UnknownAction`: No guarded command has that name.
- `Verdict`: What one person decided about a request.
- `approve_request`: Approve a request; the approval that completes it queues its command.
- `approved`: Whether `command` is the approved command of `action` being run.
- `cancel_request`: Withdraw a pending request, as its maker.
- `execute_request`: Run an approved request's command, once (the task `EXECUTE`).
- `expire_requests`: Expire the pending requests whose time is up; how many there were.
- `fingerprint`: SHA-256 of the action and the arguments, as canonical JSON (64 hex digits).
- `guard`: Register the guard of a use case decorated `requires_approval(action)`.
- `guards`: Every registered guard, by action.
- `on_guard`: The guards the installed domains registered: `on_guard(Guard(...))`.
- `refuse_if_guarded`: Refuse `action` done outside approvals, if a guard is registered for it.
- `reject_request`: Reject a request for a reason; one rejection ends it.
- `requires_approval`: Guard a use case: called directly, it needs no approval, or is refused.
- `submit_command`: Store `command` of the guard `action` as a pending request of `maker`'s.
- `submit_request`: Store a guarded command, given as JSON arguments, as a pending request.

## `domainkit.contrib.approvals.api`

Approval requests over HTTP: submit, list, read, approve, reject, cancel.

- `requests`: Approval requests: list, read, approve, reject, and cancel.
- `router`: Every route of the requests, to mount under a prefix of the project's.

## `domainkit.contrib.approvals.rules`

Who may decide a request: its policy's approvers, and the rule approving asks.

- `IsApprover`: One of the people a request's policy lets decide it.
- `MayApprove`: The rule approving a request asks for besides its approvers: a step-up, say.
- `RequestActions`: The rules of each route of the requests (`ModelActions`' protocol).
- `configured_rule`: `APPROVALS.APPROVE_RULE`, imported: a rule instance, or None.

## `domainkit.contrib.admin`

A generic admin over REST: Django's `admin.site.register`, without HTML.

- `AdminCommand`: A command the admin runs on a model, through its application service.
- `AdminMeta`: What the admin serves, for a generic client.
- `AdminQuery`: A query the admin answers, through its handler (`query_route`).
- `AdminSite`: Registered models, served under one prefix with `GET /meta`.
- `ModelAdmin`: One registered model: how the admin lists, reads, and changes it.
- `command`: An admin action: `command` run by `service` (see `command_route`).
- `query`: An admin query: `query` answered by `handler` (see `query_route`).
- `register`: Serve `model` in the admin; see the package for the options.
- `site`: The default site; `register` registers with it.

## `domainkit.contrib.audit`

An append-only audit trail: who did what, when, from where.

- `AuditSettings`: What to record; every key is optional.
- `Outcome`: Result of an audited action.
- `action_for`: Name an event's action `<domain label>.<event in snake case>`.
- `forget`: Delete the keys of `subjects` in every audited database.
- `forget_anonymous`: Delete the keys of anonymous callers' days before `before`.
- `pseudonym_of`: The pseudonym `value` of `field` has in the trail for `subject`.
- `record`: Record an action in its own transaction (denials, failures, reads).
- `seal`: Seal the rows of `database`'s trail not sealed yet; count them.
- `seal_all`: Seal, then anchor, every audited database; what each one got.
- `verify`: Check `database`'s chain against its rows, and against `ledger`.

## `domainkit.contrib.audit.api`

Records' history over HTTP, for staff (Django admin's "History").

- `router`: The routes: records' history over HTTP, for staff (Django admin's "History").

## `domainkit.contrib.audit.checkpoints`

Signed checkpoints of the audit chain: where it may be pruned, what is exported.

- `EXPORT_FORMAT`: What an export says it is.
- `FORMAT`: What a checkpoint document says it is; another value is refused.
- `Checkpoint`: The chain of `database` had `link` at seal `seq`, as signed.
- `CheckpointInvalid`: A checkpoint, or an export, that does not verify: never trust it.
- `check_export`: Check an export offline, with the public key alone; its checkpoints by database.
- `key_id`: A short name of `public_key`: the first 16 hex digits of its SHA-256.
- `load_public_key`: The Ed25519 public key in `pem`, as a verifier pins it.
- `signing_key`: The key `AUDIT.CHECKPOINT_KEY` holds; only the commands that sign read it.

## `domainkit.contrib.audit.history`

A record's history: the audit rows about one target or one aggregate.

- `history`: The newest `limit` entries about `target`, or about `aggregate`.

## `domainkit.contrib.audit.ledgerstates`

Ledger states kept in `AUDIT.DATABASE`, for every process and host to start from.

- `DatabaseStateStore`: Trusted ledger states, in `domainkit_ledger_states` on alias `database`.

## `domainkit.contrib.audit.protection`

Make the audit trail append-only in the database itself (PostgreSQL).

- `protect`: Refuse UPDATE, DELETE, and TRUNCATE on `table` (the audit trail).
- `protect_migrated`: After `migrate`: protect the trail and its seals afresh (`on_migrated`).
- `unprotect`: Remove the protection (downgrades, retention jobs run as the owner).

## `domainkit.contrib.audit.pruning`

Retention that keeps the chain: prune below a signed checkpoint; export checkpoints.

- `ExportRefused`: A trail does not verify: no checkpoint of it is signed.
- `PruneRefused`: The trail cannot be pruned as asked; nothing was deleted.
- `Pruned`: What `prune` did to one database's trail.
- `export`: Every audited database's checkpoints, and one of its head, signed.
- `prune`: Prune `database`'s trail below a checkpoint of its last seal before `before`.

## `domainkit.contrib.audit.tables`

The audit trail: append-only, one row per recorded action, sealed in a chain.

- `audit_seals`: Each row's place in the hash chain (see `seals`); append-only too.

## `domainkit.contrib.contenttypes`

Content types: one row per kind of stored object (Django's `contenttypes`).

- `DEFAULT_PERMISSIONS`: Actions that get a permission for every content type, unless `Meta` says.
- `ContentType`: A kind of stored object: its domain label and lower-case class name.
- `InstalledModel`: A mapped class of an installed domain, and what it declares.
- `ModelOptions`: What a mapped class declares about itself (Django's `Meta`).
- `content_type_key`: The `(domain, model)` of a mapped class.
- `installed_models`: Every class mapped onto a table that an installed domain holds now.
- `model_options`: Read `cls.Meta`: `verbose_name`, `default_permissions`, `permissions`.
- `sync_content_types`: Insert a row for every installed model; return ids by `(domain, model)`.

## `domainkit.contrib.evidence`

Evidence: facts kept on a ledger, provably unchanged (see ADR 0011).

- `Evidence`: What happened, about whom, and the digests that back it up.
- `EvidenceConflict`: A different record already uses that evidence id.
- `EvidenceNotFound`: No evidence is stored under that key.
- `EvidenceReceipt`: Proof of where a record is and what it says; keep it with your data.
- `EvidenceSettings`: Where evidence is kept, how large a record may be, how keys hide subjects.
- `EvidenceStore`: Record evidence once; read it back verified (the application's port).
- `EvidenceTampered`: The ledger copy does not match its receipt, or was written again.
- `LedgerEvidenceStore`: Evidence on a `Ledger`: immudb in deployments, `MemoryLedger` in tests.
- `RecordedEvidence`: Evidence read back from the ledger, with its receipt.
- `evidence_key`: `<namespace>/<subject digest>/<evidence id>`.
- `evidence_store`: The configured store, on the ledger `EVIDENCE.LEDGER` names.
- `sha256`: `sha256:<hex>` of `data`: how evidence refers to artifacts and records.
- `subject_digest`: The key part for `subject`, so keys never show it.

## `domainkit.contrib.flags`

Feature flags and dynamic settings, stored in the database and cached.

- `DynamicSetting`: The stored value of a dynamic setting; `id` is its key.
- `DynamicSettingDefinition`: A setting changed at runtime, declared with `dynamic_setting`.
- `Flag`: A feature switched on for everyone, no one, or some users; `id` is its name.
- `FlagChanged`: A flag was created, or its rules changed.
- `FlagDeleted`: A flag was deleted; it is off for everyone again.
- `FlagNotFound`: No flag has that name.
- `FlagsSettings`: Where flags are cached, and for how long; every key is optional.
- `FlagsUnitOfWork`: One transaction on the flags tables, wherever `flags` is routed.
- `InvalidFlag`: A flag's rules are out of range.
- `InvalidSettingValue`: The value does not fit the dynamic setting's type.
- `RequireFlag`: A FastAPI dependency: 404 unless flag `name` is on for the caller.
- `SettingChanged`: A dynamic setting got a new value (JSON).
- `SettingReset`: A dynamic setting went back to its default.
- `UnknownSetting`: No dynamic setting is declared with that key.
- `declared_settings`: Every declared dynamic setting, by key.
- `dynamic_setting`: Declare a setting staff can change at runtime, typed and validated.
- `flag_enabled`: Whether flag `name` is on for `user` (anyone without one).

## `domainkit.contrib.flags.admin`

Flags and dynamic settings over REST, for staff.

- `router`: The routes: flags and dynamic settings over REST, for staff.

## `domainkit.contrib.idempotency`

Replay-safe unsafe requests with the `Idempotency-Key` header.

- `IdempotencySettings`: How long keys live and which requests may carry one.

## `domainkit.contrib.idempotency.middleware`

`Idempotency-Key`: run a POST once, replay its response on retries.

- `IdempotencyMiddleware`: Store and replay responses of requests carrying `Idempotency-Key`.

## `domainkit.contrib.keys`

Keys for encrypted fields: data keys under a KMS, and per-person keys.

- `AwsKmsSettings`: `KEYS["AWS_KMS"]`: the KMS key data keys are wrapped under.
- `ColumnStatus`: Which keys seal the values of one encrypted column of one database.
- `KeyStatus`: What `key_status` found.
- `KeyStoreConflict`: A subject's key could not be stored: their keys were erased meanwhile.
- `KeysSettings`: Where the key-encryption key is, and the data keys it wraps.
- `Reencrypted`: What `reencrypt` moved to the current key.
- `TableSubjectKeyStore`: Reads and adds subjects' keys in the connection's own transaction.
- `VaultSettings`: `KEYS["VAULT"]`: the transit key data keys are wrapped under.
- `key_status`: Count, in every hosting database, what each data key seals.
- `reencrypt`: Move everything still under a retired key to the current one.
- `ring_for`: The ring `KEYS` describes, its subjects' keys in their table.
- `shred_subject`: Destroy `subject`'s keys everywhere; return how many there were.

## `domainkit.contrib.keys.tables`

The subjects' keys: one per person, wrapped under a data key.

- `subject_keys`: Subjects' keys: the 32-byte key under `wrapping_key`, nonce first (60 bytes).
## `domainkit.contrib.limits`

Limits on money movements: per transaction, per window, per subject, per currency.

- `ACCOUNT`: The kind of subject that is a ledger's account, by its id.
- `ACTOR`: The kind of subject that is who acts: the request's actor.
- `HOLDER`: The kind of subject that is the holder an account is kept for.
- `ChangeLimit`: Set a limit's bounds, all of them: one left out is None (no bound).
- `Direction`: Which movements of a subject a limit counts.
- `InvalidLimit`: A limit, or a movement checked against limits, that cannot be as written.
- `Limit`: What one kind of subject may move in one currency and direction.
- `LimitDefault`: A limit the settings give, unless a stored limit of the same key replaces it.
- `LimitExceeded`: A movement over what a limit allows in its window: an amount, or a count.
- `LimitId`: A limit's id (a `UUID`).
- `LimitNotFound`: No limit has that id.
- `LimitRemoved`: A limit was removed: a default of the settings may apply again.
- `LimitSet`: A limit was made or changed: what it allows, as it now stands.
- `LimitTaken`: Another limit has the same code, kind, direction, currency, and subject.
- `LimitsSettings`: Default limits, and how long usage is kept; every key is optional.
- `LimitsUnitOfWork`: One transaction on the limits, wherever `limits` is routed.
- `Movement`: An amount of money moving, and the subjects whose limits it counts against.
- `RemoveLimit`: Remove a limit: a default of the settings with its key applies again.
- `SetLimit`: Make the limit of this key, or set the bounds of the one there is.
- `TransactionLimitExceeded`: A movement over the most one movement may be (`per_transaction`).
- `Usage`: What one subject used of one limit in its current window.
- `Window`: The time a limit's sum and count are taken over.
- `change_limit`: Set a limit's bounds.
- `check`: Refuse `movements` as `consume` would now, counting nothing.
- `consume`: Count `movements` against their subjects' limits, or refuse them.
- `define_limit`: Make a limit; another of its key is `limit_taken` (409).
- `purge_usage`: Delete usage older than `LIMITS.KEEP_USAGE`, and subjects idle as long.
- `release`: Give back what was counted under `key`; how many movements it gave back.
- `remove_limit`: Remove a limit.
- `set_limit`: Make the limit of the command's key, or set the bounds of the one there is.
- `usage_of`: What `subject` of `kind` used of each limit that bounds it, now.

## `domainkit.contrib.limits.api`

Limits over HTTP: list, read, make, change, and remove them; read usage.

- `limits`: The limits: list, read, make, change, and remove.
- `router`: Every route of the limits (see the module).

## `domainkit.contrib.mail`

Email through the outbox (Django's `django.core.mail`).

- `BaseEmailBackend`: Sends messages somewhere; subclasses say where.
- `ConsoleBackend`: Writes each message to standard output, for development.
- `EmailMessage`: An email: a plain-text body, and an HTML one if it has one.
- `MailDeliveryFailed`: The server did not take a message; the worker tries again later.
- `MailSettings`: Where mail goes, who sends it, and how often sending is retried.
- `MemoryBackend`: Keeps messages in `outbox`, for tests: nothing leaves the process.
- `SmtpBackend`: Hands messages to an SMTP server (`MAIL.SMTP`) on a worker thread.
- `SmtpSettings`: `MAIL["SMTP"]`: the server `SmtpBackend` hands messages to.
- `TemplateDoesNotExist`: No directory holds the template.
- `TemplateError`: A template cannot be rendered: bad syntax, or a value it lacks.
- `deliver`: Send `messages` now, through the backend; return how many were sent.
- `get_backend`: A new instance of `MAIL.BACKEND`.
- `outbox`: Messages `MemoryBackend` sent, oldest first (Django's `mail.outbox`).
- `render_mail`: A message from the template `template`, for `to`.
- `send`: Queue `message`; return it with its sender and id.
- `send_mail`: Queue one message to `recipient_list` (Django's `send_mail`).

## `domainkit.contrib.mail.backends`

Where mail goes: an SMTP server, the console, or a list for tests.

- `BaseEmailBackend`: Sends messages somewhere; subclasses say where.
- `ConsoleBackend`: Writes each message to standard output, for development.
- `MailDeliveryFailed`: The server did not take a message; the worker tries again later.
- `MemoryBackend`: Keeps messages in `outbox`, for tests: nothing leaves the process.
- `SmtpBackend`: Hands messages to an SMTP server (`MAIL.SMTP`) on a worker thread.
- `outbox`: Messages `MemoryBackend` sent, oldest first (Django's `mail.outbox`).

## `domainkit.contrib.messaging`

Transactional outbox, durable event handlers, and background tasks.

- `AlreadyHandled`: The inbox holds this message: an earlier run committed its work.
- `Attempt`: The run a worker is making of a message, as its task sees it.
- `Backoff`: Seconds to wait before running a failed message again.
- `Budget`: Slots for the messages a worker process runs at once.
- `Chain`: Tasks that run one after another, each once the one before succeeds.
- `LeaseLost`: Another worker took the message over; this run's commit is refused.
- `MessagingSettings`: Delivery, retry, and worker options; every key is optional.
- `Worker`: Run outbox messages of one database.
- `chain`: Run `first`, then each task of `then` once the one before succeeds.
- `current_attempt`: The run of the message being processed, or None outside the worker.
- `enqueue`: Queue `function(**kwargs)` now, in its own transaction.
- `inbox`: Commit a handler's or task's database work once per message.
- `ordered`: Run a task's or durable handler's messages one at a time per key, in order.
- `periodic`: Register an argument-free task that runs on a schedule, cluster-wide.
- `task`: Register an async function as a background task (`uow.defer(task)`).
- `tasks_kept`: Put the registered tasks back as they were when the block ends.

## `domainkit.contrib.messaging.admin`

Operator queries: queue depth, lag, listings, retries, and cancelling.

- `blanked_dead_letters`: Count dead messages (one, or all) whose arguments were blanked.
- `cancel`: Cancel a pending message that no worker is running; report whether it was.
- `list_messages`: List messages in `state`, most urgent (or, for dead ones, newest) first.
- `queue_counts`: Count messages by status in alias `database` (default: the home outbox).
- `queue_lag`: Seconds the oldest due message of each queue has waited to start.
- `release_stalled`: Make the next message of each stalled ordering key pending; how many.
- `retry_dead`: Return dead messages (one, or all) to the queue with fresh attempts.

## `domainkit.contrib.messaging.dbclock`

The clock that decides when outbox work is due and when locks expire.

- `Moment`: A time to compare or store: a value, or the database's clock in SQL.
- `at`: `seconds` from now (earlier when negative), on the database's clock.
- `now`: The current time as a database of `dialect` should see it.
- `uses_database`: Report whether the database's clock decides on `dialect` right now.

## `domainkit.contrib.messaging.testing`

pytest fixtures for code that publishes events and queues background work.

- `CapturedEvents`: Domain events published after commits, in order.
- `DeferredExecutor`: Runs committed messages right after each commit, in the committing loop.
- `Drained`: What one `drain` ran.
- `Outbox`: Messages committed during a test, and a way to run them.
- `QueuedMessage`: A message committed to an outbox during a test.
- `QueuedWorkFailed`: Queued work failed, or kept queueing more, while a test ran it.
- `drain`: Run every due message of every outbox here, until none is left.

## `domainkit.contrib.notifications`

Text messages and push notifications through the outbox.

- `ConsolePushBackend`: Writes push notifications to standard output, for development.
- `ConsoleSmsBackend`: Writes text messages to standard output, for development.
- `MemoryPushBackend`: Keeps notifications in `push_outbox`, for tests.
- `MemorySmsBackend`: Keeps text messages in `sms_outbox`, for tests.
- `NotificationFailed`: The provider did not take a message; the worker tries again later.
- `NotificationsSettings`: Where text messages and push notifications go, and how often to retry.
- `PushBackend`: Sends push notifications through a provider.
- `PushMessage`: A push notification to one device, by the token its platform gave it.
- `SmsBackend`: Sends text messages through a provider.
- `SmsMessage`: A text message to a phone number in E.164 form (`+33612345678`).
- `deliver_push`: Send push notifications now, skipping the outbox (from the worker).
- `deliver_sms`: Send text messages now, skipping the outbox (from the worker).
- `get_push_backend`: A new instance of `NOTIFICATIONS.PUSH_BACKEND`.
- `get_sms_backend`: A new instance of `NOTIFICATIONS.SMS_BACKEND`.
- `push_outbox`: Notifications `MemoryPushBackend` sent, oldest first.
- `send_push`: Queue a push notification to the device `token` names.
- `send_sms`: Queue a text message to `to` (E.164: `+33612345678`).
- `sms_outbox`: Text messages `MemorySmsBackend` sent, oldest first.

## `domainkit.contrib.notifications.backends`

Ports for text messages and push notifications, with development adapters.

- `ConsolePushBackend`: Writes push notifications to standard output, for development.
- `ConsoleSmsBackend`: Writes text messages to standard output, for development.
- `MemoryPushBackend`: Keeps notifications in `push_outbox`, for tests.
- `MemorySmsBackend`: Keeps text messages in `sms_outbox`, for tests.
- `NotificationFailed`: The provider did not take a message; the worker tries again later.
- `PushBackend`: Sends push notifications through a provider.
- `SmsBackend`: Sends text messages through a provider.
- `push_outbox`: Notifications `MemoryPushBackend` sent, oldest first.
- `sms_outbox`: Text messages `MemorySmsBackend` sent, oldest first.

## `domainkit.contrib.objectpermissions`

Permissions on single objects (django-guardian's grants), for users or groups.

- `Grants`: What `VisibleTo` reads in memory (`InMemoryObjectPermissions`).
- `InMemoryObjectPermissions`: Grants in a dictionary, for tests; `VisibleTo(grants=...)` reads it.
- `InvalidGrant`: A grant names a permission of another model, or no one or two at once.
- `ObjectPermission`: `permission` on one object, for one user or one group.
- `ObjectPermissionBackend`: Permissions granted on an object to a user or to one of their groups.
- `ObjectPermissionGranted`: A user or a group may now do something to one object.
- `ObjectPermissionId`: NewType creates simple unique types with almost zero runtime overhead.
- `ObjectPermissionRepository`: Grants: find them with `about(...)`.
- `ObjectPermissionRevoked`: A grant on one object was taken back.
- `ObjectPermissionsUnitOfWork`: One transaction on the grants of one database (the objects').
- `VisibleTo`: Objects on which `permission` is granted to a user or their groups.
- `grant_permission`: Let `user` (or the members of `group`) have `permission` on `obj`.
- `grants_on`: Every grant on `obj`; who may see them is the caller's to check.
- `revoke_permission`: Take back `permission` on `obj` from `user` or `group`; how many.
- `visible_scope`: A resource's `scope`: what the caller may see by `permission`.
- `visible_to`: What `user` may see of `model`: None (everything), or `VisibleTo`.

## `domainkit.contrib.privacy`

Privacy requests and retention across installed domains (GDPR's rights).

- `PersonalDataErased`: An aggregate's personal fields were erased, at a request or by retention.
- `PersonalModel`: An aggregate of an installed domain with fields marked `Personal`.
- `Policy`: A domain's `Retention`, resolved to a table and its dating column.
- `Purged`: What `purge` did to one table on one database.
- `Retention`: How long rows of one table, or one aggregate's personal data, are kept.
- `check_reason`: Return `reason` if it can go into the audit trail: one line, 1 to 255.
- `check_subject`: Return `subject` if it can name someone, written as its id prints.
- `erase_subject`: Erase every field marked `Personal` about `subject`; return how many changed.
- `export_subject`: Every field marked `Personal` about `subject`, as JSON-ready data.
- `personal_models`: Every installed aggregate with personal fields.
- `problems`: What keeps privacy requests or retention from working, as sentences.
- `purge`: Apply every domain's `RETENTION`, on every database it uses.
- `retention_policies`: Every installed domain's `RETENTION`, resolved.

## `domainkit.contrib.webhooks`

Outgoing webhooks: committed events, signed and delivered to subscribed URLs.

- `InvalidSignature`: A delivery that is not signed with the secret, or is stale.
- `UnsafeTarget`: A webhook URL that may not be called; the message never quotes it.
- `WebhookSettings`: Where deliveries run, how often they are tried, and what they may reach.
- `expose`: Send committed `event_type` events to the subscribers of `name`.
- `new_secret`: Return a new random secret, `whsec_` and 32 bytes in base64.
- `queue_deliveries`: Queue a delivery of one event to each subscriber; return how many.
- `sign`: Return `v1,<signature>` of `body` sent as `message_id` at `timestamp`.
- `verify`: Check a delivery as a receiver would; return its id.

## `domainkit.contrib.webhooks.admin`

The webhooks admin API: subscriptions and their deliveries, for staff.

- `router`: The routes: the webhooks admin API: subscriptions and their deliveries, for staff.

## `domainkit.contrib.webhooks.owned`

The self-service webhooks API: a subject manages its own subscriptions only.

- `SubscriptionLimitReached`: The owner has `WEBHOOKS.MAX_SUBSCRIPTIONS_PER_OWNER` subscriptions.
- `owned_router`: The self-service routes, with `owner` naming whose subscriptions are whose.
- `router`: The self-service routes, for subscriptions owned by the user's id.
- `user_owner`: The owner of a user's subscriptions: the user's id, as text.

## `domainkit.contrib.webhooks.guard`

Which webhook URLs may be called: the guard against server-side request forgery.

- `MAX_URL_LENGTH`: The longest URL a guarded call accepts, in characters.
- `METADATA_ADDRESSES`: Build an immutable unordered collection of unique elements.
- `METADATA_HOSTS`: Build an immutable unordered collection of unique elements.
- `NAT64`: This class represents and manipulates 128-bit IPv6 networks.
- `Address`: An IPv4 or IPv6 address.
- `Network`: An IPv4 or IPv6 network.
- `UnsafeTarget`: A webhook URL that may not be called; the message never quotes it.
- `checked_address`: The address to connect to for `url`, once all of its host's pass.
- `checked_url`: Parse `url` and check everything that does not need a lookup.
- `judged`: The address whose kind decides: an IPv4 address inside an IPv6 one.
- `lookup`: Every address `host` resolves to, on the guard's threads (`resolver`).
- `refusal`: Why `address` may not be called, or None if it may.

## `domainkit.contrib.webhooks.signing`

Signatures in the Standard Webhooks scheme (https://www.standardwebhooks.com).

- `InvalidSignature`: A delivery that is not signed with the secret, or is stale.
- `sign`: Return `v1,<signature>` of `body` sent as `message_id` at `timestamp`.
- `verify`: Check a delivery as a receiver would; return its id.
