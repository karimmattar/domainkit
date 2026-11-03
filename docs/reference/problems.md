# Problem codes

Every error body carries a `code` (and, with
`ERROR_FORMAT = "problem+json"`, a `type` that ends in it): clients switch
on it, so it is public. A code listed here keeps its meaning and its status
across releases, as a public name does ([versioning.md](../versioning.md#what-is-public));
`detail` is for people, in the request's language, and may change.
`domainkit_tests/test_problem_codes.py` fails when the framework can answer
a code this page does not list, or this page lists one it no longer
answers (DSGN-20).

Every class domainkit defines sets its code itself, so renaming a class
never changes it; `DomainError` refuses one of domainkit's own that sets
none. A project's `DomainError` without a `code` still gets one from its
name (`ProductNotFound` is `product_not_found`): set `code` on the class to
keep it whatever the class is renamed to. A unique constraint may name its
own code ([databases.md](../databases.md#unique-values)).

Every 429 is `rate_limited`, whichever limit refused: `RATE_LIMIT`, a
route's `Throttle`, or a login, code, or link throttle of
`domainkit.contrib.auth` (0.12 answered `throttled` and `login_throttled`
too). `Retry-After` says when to try again.

How each status is reached, and which fields a body holds, is in
[responses.md](responses.md#errors).

| Code | Status | Answered by |
| --- | --- | --- |
| `account_closed` | 422 | `AccountNotOpen` (contrib.accounting.exceptions): A posting or a hold on an account that is closed. |
| `account_code_taken` | 409 | `AccountCodeTaken` (contrib.accounting.exceptions): Another account has that code. |
| `account_not_empty` | 422 | `AccountNotEmpty` (contrib.accounting.exceptions): Closing an account that still has a balance, or holds. |
| `account_not_found` | 404 | `AccountNotFound` (contrib.accounting.exceptions): No account has that id or code. |
| `already_decided` | 409 | `AlreadyDecided` (contrib.approvals.exceptions): This person approved or rejected the request already (409). |
| `amount_out_of_range` | 422 | `AmountOutOfRange` (money): An amount where it is kept cannot hold it: too many digits, or places. Also `MovementOutOfRange` (contrib.limits.exceptions): a movement, the sum of an entry's postings for a subject, larger than limits keep. |
| `approval_not_needed` | 422 | `ApprovalNotNeeded` (contrib.approvals.exceptions): No policy asks for approval of this command: call it directly. |
| `approval_pending` | 403 | `ApprovalPending` (contrib.approvals.exceptions): A guarded command called directly was submitted for approval instead (403). |
| `approval_request_not_found` | 404 | `RequestNotFound` (contrib.approvals.exceptions): No approval request has that id. |
| `approval_required` | 403 | `ApprovalRequired` (contrib.approvals.exceptions): A guarded command was called directly: it runs only once approved (403). |
| `authentication_failed` | 401 | `AuthenticationFailed` (contrib.auth.authentication): Credentials were presented and do not hold. |
| `bad_request` | 400 | Starlette or FastAPI. |
| `business_rule_violation` | 422 | `BusinessRuleViolation` (ddd.exceptions): The request is well-formed but breaks an invariant of the model. |
| `captcha_failed` | 400 | contrib.auth.captcha: The captcha was missing or not solved. |
| `captcha_unavailable` | 503 | `CaptchaUnavailable` (contrib.auth.captcha): The captcha provider could not check the token: 503. |
| `check_violation` | 422 | http.errors: A value breaks a rule of the data: it is out of the range or set the database accepts. |
| `concurrent_update` | 409 | `ConcurrentUpdate` (ddd.exceptions): Someone else changed the aggregate first; reload it and try again. |
| `conflict` | 409 | `Conflict` (ddd.exceptions): The operation clashes with the current state, such as a duplicate. |
| `content_too_large` | 413 | http.middleware: Request body too large. |
| `csrf_failed` | 403 | contrib.auth.cookies: a cookie session's unsafe request from an untrusted origin, or without its CSRF token. |
| `currency_mismatch` | 422 | `CurrencyMismatch` (money): Amounts of two currencies were added, subtracted, or compared. |
| `currency_not_limited` | 422 | `CurrencyNotLimited` (contrib.limits.exceptions): A movement in a currency none of its subject's limits names (FIN-89). |
| `database_busy` | 503 | http.errors: The database is busy; try again shortly. |
| `deadline_exceeded` | 503 | http.deadlines: The request took too long; try again shortly. |
| `domain_error` | 400 | `DomainError` (ddd.exceptions): A business rule refused the operation; the message is safe to show. |
| `infected_file` | 422 | http.uploads: The virus scan refused the file. |
| `invalid_field` | 422 | `InvalidField` (ddd.exceptions): A field was given a value its markers rule out (`MaxLength`, `Range`, ...). |
| `duplicate_value` | 409 | `DuplicateValue` (ddd.exceptions): A value that must be unique is taken (a unique constraint refused it). |
| `earlier_period_open` | 422 | `EarlierPeriodOpen` (contrib.accounting.exceptions): Closing a period while one before it is still open. |
| `empty_file` | 400 | http.uploads: The file is empty. |
| `entry_not_found` | 404 | `EntryNotFound` (contrib.accounting.exceptions): No journal entry has that id. |
| `erasure_held` | 422 | `ErasureHeld` (contrib.privacy.operations): A person's data must be kept for now: nothing of it was erased (FIN-94). |
| `evidence_conflict` | 409 | `EvidenceConflict` (contrib.evidence.models): A different record already uses that evidence id. |
| `evidence_not_found` | 404 | `EvidenceNotFound` (contrib.evidence.models): No evidence is stored under that key. |
| `export_too_large` | 422 | resources.renderers: This export would be too large; narrow it with filters. |
| `field_not_editable` | 403 | `FieldNotEditable` (resources.policies): A partial change sets a field the caller may not change. |
| `file_not_found` | 404 | `FileMissing` (storage.base): No file has the name (a malformed name names none): 404 over HTTP. |
| `file_too_large` | 413 | http.uploads: The file is too large. |
| `fingerprint_mismatch` | 409 | `FingerprintMismatch` (contrib.approvals.exceptions): What was approved is not what the request holds. |
| `flag_not_found` | 404 | `FlagNotFound` (contrib.flags.exceptions): No flag has that name. |
| `group_name_taken` | 409 | `GroupNameTaken` (contrib.auth.exceptions): Another group has that name. |
| `group_not_found` | 404 | `GroupNotFound` (contrib.auth.exceptions): No group has that id. |
| `hold_not_active` | 409 | `HoldNotActive` (contrib.accounting.exceptions): Capturing or releasing a hold that was captured, released, or has expired. |
| `hold_not_found` | 404 | `HoldNotFound` (contrib.accounting.exceptions): No hold has that id. |
| `idempotency_key_in_use` | 409 | contrib.idempotency.middleware: A request with this Idempotency-Key is still in progress. |
| `idempotency_key_needs_caller` | 400 | contrib.idempotency.middleware: An Idempotency-Key needs a caller this service knows: authenticate, or send the request without the key. |
| `idempotency_key_reused` | 422 | contrib.idempotency.middleware: This Idempotency-Key was used for a different request. |
| `idempotency_key_taken_over` | 409 | `KeyTakenOver` (contrib.idempotency.operations): A retry took the key over after this request's lease ran out. |
| `idempotency_response_unavailable` | 409 | contrib.idempotency.middleware: The request with this Idempotency-Key already ran, and its response is no longer kept; look up the result instead of retrying. contrib.idempotency.middleware. |
| `inexact_amount` | 422 | `InexactAmount` (money): An amount with more places than its currency has; nothing is rounded. |
| `insufficient_funds` | 422 | `InsufficientFunds` (contrib.accounting.exceptions): A change that would take an account that may not go negative below zero. |
| `interested_party` | 403 | `InterestedParty` (contrib.approvals.exceptions): Whoever the command is about may not approve or reject it. |
| `internal_error` | 500 | http.middleware: Internal server error. resources.batches: Internal server error. |
| `invalid_amount` | 422 | `InvalidAmount` (contrib.accounting.exceptions): An amount the ledger cannot hold: not positive, or of more than four places. |
| `invalid_arguments` | 422 | `InvalidArguments` (contrib.approvals.exceptions): A command's arguments do not make the command, or are too large. |
| `invalid_code` | 401 | contrib.auth.mfa: The code is wrong, or was used already. |
| `invalid_content_length` | 400 | http.middleware: Invalid Content-Length header. |
| `invalid_credentials` | 401 | contrib.auth.api: Unable to log in with the given credentials. |
| `invalid_cursor` | 422 | http.pagination: The cursor is invalid; start again without one. |
| `invalid_email` | 422 | `InvalidEmail` (contrib.auth.exceptions): The email address is not one plain address mail can go to. |
| `invalid_entry` | 422 | `InvalidEntry` (contrib.accounting.exceptions): An entry that cannot be posted as written: its lines, or its accounts. |
| `invalid_filter` | 422 | http.filters: a filter, lookup, ordering, or search a `FilterSet` does not offer, or over its bounds. |
| `invalid_flag` | 422 | `InvalidFlag` (contrib.flags.models): A flag's rules are out of range. |
| `invalid_grant` | 422 | `InvalidGrant` (contrib.objectpermissions.services): A grant names a permission of another model, or no one or two at once. |
| `invalid_group_name` | 422 | `InvalidGroupName` (contrib.auth.exceptions): The group name is empty or too long. |
| `invalid_history_query` | 422 | contrib.audit.api: Name a target, or an aggregate's field and id. |
| `invalid_host` | 400 | http.middleware: Invalid host header. |
| `invalid_idempotency_key` | 400 | contrib.idempotency.middleware: Idempotency-Key must be 1 to 255 printable ASCII characters. |
| `invalid_limit` | 422 | `InvalidLimit` (contrib.limits.exceptions): A limit, or a movement checked against limits, that cannot be as written. |
| `invalid_money` | 422 | `MoneyError` (money): An amount or a currency that money cannot hold; the HTTP layer answers 422. |
| `invalid_password` | 422 | `InvalidPassword` (contrib.auth.exceptions): The password breaks the configured rules (``PASSWORD_VALIDATORS``). |
| `invalid_setting_value` | 422 | `InvalidSettingValue` (contrib.flags.exceptions): The value does not fit the dynamic setting's type. |
| `invalid_statement` | 422 | `InvalidStatement` (contrib.accounting.exceptions): A statement that cannot be imported: its format, or one of its lines. |
| `invalid_token` | 400 | contrib.auth.mfa: The login has expired; log in again. contrib.auth.recovery: The link is invalid or has expired. |
| `invalid_transaction` | 422 | `InvalidTransaction` (contrib.auth.sca): The amount, payee, or purpose cannot be authorized as given. |
| `invalid_user_field` | 422 | `InvalidUserField` (contrib.auth.exceptions): A value does not fit the type of the user field it is for. |
| `invalid_username` | 422 | `InvalidUsername` (contrib.auth.exceptions): The username is empty, too long, or uses characters it may not. |
| `invalid_value` | 422 | http.errors: A value cannot be stored: it is too long, out of range, or holds a character the database refuses. |
| `invalid_webhook_event_types` | 422 | `InvalidEventTypes` (contrib.webhooks.models): A subscription's event types are missing or malformed. |
| `last_superuser` | 409 | `LastSuperuser` (contrib.auth.exceptions): The step would leave no active superuser to run the system. |
| `limit_exceeded` | 422 | `LimitExceeded` (contrib.limits.exceptions): A movement over what a limit allows in its window: an amount, or a count. |
| `limit_not_found` | 404 | `LimitNotFound` (contrib.limits.exceptions): No limit has that id. |
| `limit_taken` | 409 | `LimitTaken` (contrib.limits.exceptions): Another limit has the same code, kind, direction, currency, and subject. |
| `lock_not_available` | 409 | `LockNotAvailable` (ddd.repository): Another transaction holds the row's lock, and the caller would not wait. |
| `maker_cannot_decide` | 403 | `MakerCannotDecide` (contrib.approvals.exceptions): Whoever submitted a request may not approve or reject it. |
| `maker_not_allowed` | 403 | `MakerNotAllowed` (contrib.approvals.exceptions): Whoever submitted the command may not make it (any more). |
| `method_not_allowed` | 405 | Starlette or FastAPI. |
| `metrics_disabled` | 403 | http.metrics: Set METRICS_TOKEN to expose metrics in staging and production. |
| `mfa_enabled` | 409 | contrib.auth.mfa: A second factor is on; turn it off first. |
| `missing_value` | 422 | http.errors: A required value is missing. |
| `no_enrolment` | 400 | contrib.auth.mfa: No enrolment is waiting for a code; start one first. |
| `no_open_period` | 422 | `NoOpenPeriod` (contrib.accounting.exceptions): No accounting period covers the entry's date (``REQUIRE_PERIOD``). |
| `no_second_factor` | 409 | contrib.auth.stepup: Turn on a second factor first: confirming it is you needs one. |
| `no_session` | 400 | contrib.auth.cookies: This request has no browser session. |
| `not_acceptable` | 406 | resources.renderers: This resource cannot answer in a media type the request accepts. |
| `not_applied` | 424 | resources.batches: Another item of the batch failed, so nothing was applied. |
| `not_authenticated` | 401 | http.access: Authentication credentials were not provided. |
| `multiple_found` | 409 | `MultipleFound` (ddd.exceptions): A lookup that expects one aggregate met several (`get_or_create`). |
| `not_found` | 404 | `NotFound` (ddd.exceptions): The requested aggregate does not exist. contrib.auth.signup: Not Found. contrib.flags.dependencies: Not found. Starlette or FastAPI. |
| `not_the_maker` | 403 | `NotTheMaker` (contrib.approvals.exceptions): Only whoever submitted a request may cancel it. |
| `overloaded` | 503 | http.shedding: The server is busy; try again shortly. |
| `password_change_required` | 403 | contrib.auth.api: This password was set for you: choose a new one to log in. |
| `password_checks_busy` | 503 | `PasswordChecksBusy` (contrib.auth.hashers): Every hashing thread is busy and the queue is full. |
| `password_required` | 400 | contrib.auth.stepup: Enter your password as well. |
| `period_closed` | 422 | `PeriodNotOpen` (contrib.accounting.exceptions): An entry, or a new period, dated on or before the end of a closed period. |
| `period_name_taken` | 409 | `PeriodNameTaken` (contrib.accounting.exceptions): Another period has that name. |
| `period_not_found` | 404 | `PeriodNotFound` (contrib.accounting.exceptions): No accounting period has that id. |
| `periods_overlap` | 422 | `PeriodsOverlap` (contrib.accounting.exceptions): A new period whose dates another period covers, or that ends before it starts. |
| `permission_denied` | 403 | `PermissionDenied` (ddd.exceptions): The actor may not perform the operation. |
| `policy_removed` | 409 | `PolicyRemoved` (contrib.approvals.exceptions): The request's guard or policy no longer exists: it is neither decided nor run. |
| `precondition_failed` | 412 | `PreconditionFailed` (ddd.exceptions): The caller expected another version of the aggregate than the stored one. |
| `precondition_required` | 428 | resources.conditional: Send If-Match with the ETag of the version you are changing. |
| `rate_limited` | 429 | http.ratelimit: `RATE_LIMIT` or a route's `Throttle`; contrib.auth: login, password, code, link, and signup throttles. |
| `reference_reused` | 409 | `ReferenceReused` (contrib.accounting.exceptions): A source and reference that name an entry or a hold made otherwise. |
| `referenced` | 409 | http.errors: a foreign key refused the change. |
| `refresh_not_supported` | 400 | contrib.auth.authentication: Tokens are not refreshed here; log in again. |
| `request_not_pending` | 409 | `RequestNotPending` (contrib.approvals.exceptions): The request was approved, rejected, cancelled, or has expired. |
| `request_tampered` | 409 | `RequestTampered` (contrib.approvals.exceptions): The request was changed outside the application: its seal does not hold. |
| `request_timeout` | 408 | http.middleware: The request body took too long to arrive. |
| `entry_not_reversible` | 422 | `EntryNotReversible` (contrib.accounting.exceptions): Reversing a reversal: it would move the money of the entry it undid again. |
| `scanner_unavailable` | 503 | `ScannerUnavailable` (storage.scanning): The virus scanner could not scan the upload: 503, nothing kept. |
| `scope_not_held` | 403 | contrib.auth.personal_tokens: a token asked for a scope its owner does not hold. |
| `second_factor_unreadable` | 409 | `SecondFactorUnreadable` (contrib.auth.mfa): No key held opens the user's TOTP secret: `SECRET_KEY` changed without it in `SECRET_KEY_FALLBACKS` (`resealmfa`, `resetmfa`). |
| `service_busy` | 503 | `ServiceBusy` (exceptions): Something the operation needs is busy now; trying again later may work. |
| `signup_taken` | 409 | contrib.auth.signup: An account already uses this username or this address; sign up again with another username, or log in. |
| `step_up_required` | 401 | contrib.auth.access: Confirm it is you: authenticate again to do this. |
| `step_up_unsupported` | 400 | contrib.auth.stepup: These credentials cannot step up: log in, then step up. |
| `throttle_unavailable` | 503 | contrib.auth.throttling: Attempts cannot be counted right now; try again later. |
| `token_lifetime_too_long` | 422 | contrib.auth.personal_tokens: A personal token asked for longer than `AUTH.PERSONAL_TOKEN_MAX_AGE`, or for no end. |
| `token_not_found` | 404 | contrib.auth.personal_tokens: No such token. |
| `too_many_connections` | 503 | http.live: `LIVE_MAX_CONNECTIONS` or `LIVE_MAX_CONNECTIONS_PER_CALLER` is reached. |
| `too_many_tokens` | 409 | contrib.auth.personal_tokens: the user holds the most personal tokens allowed. |
| `transaction_confirmed` | 409 | contrib.auth.sca: This transaction was confirmed already. |
| `transaction_expired` | 409 | contrib.auth.sca: This transaction can no longer be confirmed; start it again. |
| `transaction_limit_exceeded` | 422 | `TransactionLimitExceeded` (contrib.limits.exceptions): A movement over the most one movement may be (``per_transaction``). |
| `transaction_not_authorized` | 403 | `TransactionNotAuthorized` (contrib.auth.sca): No authorization holds for this transaction: get one, then try again (403). |
| `transaction_not_found` | 404 | `TransactionNotFound` (contrib.auth.sca): No transaction of the user waits under that id (404). |
| `unauthorized` | 401 | http.metrics: A valid metrics token is required. |
| `unbalanced_entry` | 422 | `UnbalancedEntry` (contrib.accounting.exceptions): An entry whose debits and credits differ in some currency. |
| `unknown_action` | 404 | `UnknownAction` (contrib.approvals.exceptions): No guarded command has that name. |
| `unknown_currency` | 422 | `UnknownCurrency` (money): A code that is neither an ISO 4217 currency nor a registered one. |
| `unknown_group` | 404 | `UnknownGroup` (contrib.auth.exceptions): No group has that name. |
| `unknown_permission` | 404 | `UnknownPermission` (contrib.auth.exceptions): No permission has that key (``<domain>.<codename>``). |
| `unknown_setting` | 404 | `UnknownSetting` (contrib.flags.exceptions): No dynamic setting is declared with that key. |
| `unknown_user_field` | 422 | `UnknownUserField` (contrib.auth.exceptions): The user model has no such field, or it changes another way. |
| `unsafe_url` | 422 | `UnsafeURL` (http.guard): A URL that may not be called; the message never quotes it. |
| `unsafe_webhook_url` | 422 | `UnsafeTarget` (contrib.webhooks.guard): A webhook URL that may not be called; the message never quotes it. |
| `unsupported_file_type` | 415 | http.uploads: This type of file is not accepted. |
| `upstream_error` | 502 | `UpstreamError` (exceptions): A service this one called failed it: unavailable, too slow, or too large. |
| `upstream_response_too_large` | 502 | `ResponseTooLarge` (http.client): An answer was longer than ``MAX_RESPONSE_BYTES``, as sent or decompressed. |
| `upstream_timeout` | 504 | `CallTimeout` (http.client): A call took longer than its ``timeout``, retries included. |
| `upstream_unavailable` | 503 | `CircuitOpen` (http.client): Calls to a host fail at once while its circuit is open. |
| `user_not_found` | 404 | `UserNotFound` (contrib.auth.exceptions): No user has that id. |
| `username_taken` | 409 | `UsernameTaken` (contrib.auth.exceptions): Another user has that username. |
| `validation_error` | 422 | http.errors: The request is invalid. |
| `webhook_delivery_not_found` | 404 | `DeliveryNotFound` (contrib.webhooks.admin): No webhook delivery has that id. |
| `webhook_subscription_limit` | 422 | `SubscriptionLimitReached` (contrib.webhooks.owned): The owner has ``WEBHOOKS.MAX_SUBSCRIPTIONS_PER_OWNER`` subscriptions. |
| `webhook_subscription_not_found` | 404 | `SubscriptionNotFound` (contrib.webhooks.models): No webhook subscription has that id. |
| `wrong_password` | 400 | contrib.auth.api: Your old password was entered incorrectly. contrib.auth.mfa: Your password was entered incorrectly. contrib.auth.stepup: Your password was entered incorrectly. |
