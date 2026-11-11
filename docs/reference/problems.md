# Problem codes

Every error body carries a `code`: a short, machine-readable name of the
error, such as `not_found` or `insufficient_funds`. Clients switch on it,
so it is public. This page lists every code that domainkit answers with,
its HTTP status, and the class or module that answers it.

On this page you find:

- what a code promises, and what may change;
- how your own errors get their codes;
- every code of the framework and its apps, from A to Z.

## What a code promises

A code listed here keeps its meaning and its status across releases, as
a public name does ([versioning.md](../versioning.md#what-is-public)).
With `ERROR_FORMAT = "problem+json"`, the `type` of the error ends in the
code too. The `detail` is for people: it is in the request's language,
and it may change.

```json
{"type": "about:blank", "title": "Not Found", "status": 404, "detail": "Not Found", "instance": "/nowhere", "code": "not_found", "request_id": "9d319f0bc63345f5fdcf152e7013bd03"}
```

`domainkit_tests/test_problem_codes.py` fails when the framework can
answer a code that this page does not list, or when this page lists a
code that the framework no longer answers (DSGN-20).

Every 429 is `rate_limited`, whichever limit refused: `RATE_LIMIT`, a
route's `Throttle`, or a login, code, or link throttle of
`domainkit.contrib.auth` (0.12 answered `throttled` and `login_throttled`
too). `Retry-After` says when to try again.

How each status is reached, and which fields a body holds, is in
[responses.md](responses.md#errors).

## Codes of your own

Every class that domainkit defines sets its code itself, so renaming a
class never changes it; `DomainError` refuses a class of domainkit's own
that sets none. A project's `DomainError` without a `code` still gets one
from its name: `ProductNotFound` is `product_not_found`. Set `code` on the
class to keep it whatever the class is renamed to:

```python
from domainkit.ddd import BusinessRuleViolation, NotFound


class ProductNotFound(NotFound):
    """No product has that id."""


class OutOfStock(BusinessRuleViolation):
    code = "out_of_stock"


print(ProductNotFound.code)
print(OutOfStock.code)
```

```text
product_not_found
out_of_stock
```

`ProductNotFound` answers 404, and `OutOfStock` 422, as their base
classes do ([Statuses and codes](responses.md#statuses-and-codes)). A
unique constraint may name its own code
([databases.md](../databases.md#unique-values)).

## Every code

| Code | Status | Answered by |
| --- | --- | --- |
| `access_denied` | 400 | `AccessDenied` (contrib.ciba.exceptions): The phone denied the sign-in. |
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
| `authorization_pending` | 400 | `AuthorizationPending` (contrib.ciba.exceptions): The phone has not answered yet: poll again after the interval. |
| `bad_request` | 400 | Starlette or FastAPI. |
| `business_rule_violation` | 422 | `BusinessRuleViolation` (ddd.exceptions): The request is well-formed but breaks an invariant of the model. |
| `captcha_failed` | 400 | contrib.auth.captcha: The captcha was missing or not solved. |
| `captcha_unavailable` | 503 | `CaptchaUnavailable` (contrib.auth.captcha): The captcha provider could not check the token: 503. |
| `check_violation` | 422 | http.errors: A value breaks a rule of the data: it is out of the range or set the database accepts. |
| `code_expired` | 422 | `CodeExpired` (contrib.phones.exceptions): No code works for this number: none sent, too old, or tried too often. |
| `concurrent_update` | 409 | `ConcurrentUpdate` (ddd.exceptions): Someone else changed the aggregate first; reload it and try again. |
| `conflict` | 409 | `Conflict` (ddd.exceptions): The operation clashes with the current state, such as a duplicate. |
| `content_too_large` | 413 | http.middleware: Request body too large. |
| `csrf_failed` | 403 | contrib.auth.cookies: a cookie session's unsafe request from an untrusted origin, or without its CSRF token. |
| `currency_mismatch` | 422 | `CurrencyMismatch` (money): Amounts of two currencies were added, subtracted, or compared. |
| `currency_not_limited` | 422 | `CurrencyNotLimited` (contrib.limits.exceptions): A movement in a currency none of its subject's limits names (FIN-89). |
| `database_busy` | 503 | http.errors: The database is busy; try again shortly. |
| `deadline_exceeded` | 503 | http.deadlines: The request took too long; try again shortly. |
| `device_already_bound` | 409 | `DeviceAlreadyBound` (contrib.devices.exceptions): The user has a bound device: approve the new one from it. |
| `device_login_off` | 404 | `DeviceLoginOff` (contrib.devices.exceptions): `DEVICES.DEVICE_LOGIN` is off: devices do not log in here. |
| `device_logins_not_asked` | 422 | contrib.auth.api: This server decides what this change does to your devices' logins: leave keep_device_logins out. |
| `device_not_found` | 404 | `DeviceNotFound` (contrib.devices.exceptions): The user has no device with that id. |
| `device_signature_refused` | 401 | `DeviceSignatureRefused` (contrib.devices.exceptions): The request is not signed by a device of the user, now, once. |
| `different_network` | 403 | `DifferentNetwork` (contrib.ciba.exceptions): The phone is not on the address of the page (`QR_SAME_NETWORK`). |
| `domain_error` | 400 | `DomainError` (ddd.exceptions): A business rule refused the operation; the message is safe to show. |
| `duplicate_value` | 409 | `DuplicateValue` (ddd.exceptions): A value that must be unique is taken (a unique constraint refused it). |
| `earlier_period_open` | 422 | `EarlierPeriodOpen` (contrib.accounting.exceptions): Closing a period while one before it is still open. |
| `email_taken` | 409 | `EmailTaken` (contrib.auth.exceptions): Another user has that email address, and it identifies users. |
| `empty_file` | 400 | http.uploads: The file is empty. |
| `enrollment_off` | 404 | `EnrollmentOff` (contrib.devices.exceptions): `DEVICES.ENROLLMENT` is off: devices do not enroll here. |
| `enrollment_refused` | 401 | `EnrollmentRefused` (contrib.devices.exceptions): The code, the password, or the key's signature does not hold. |
| `entry_not_found` | 404 | `EntryNotFound` (contrib.accounting.exceptions): No journal entry has that id. |
| `entry_not_reversible` | 422 | `EntryNotReversible` (contrib.accounting.exceptions): Reversing a reversal: it would move the money of the entry it undid again. |
| `erasure_held` | 422 | `ErasureHeld` (contrib.privacy.operations): A person's data must be kept for now: nothing of it was erased (FIN-94). |
| `evidence_conflict` | 409 | `EvidenceConflict` (contrib.evidence.models): A different record already uses that evidence id. |
| `evidence_not_found` | 404 | `EvidenceNotFound` (contrib.evidence.models): No evidence is stored under that key. |
| `expired_token` | 400 | `SignInExpired` (contrib.ciba.exceptions): The request's time is up before the phone approved it. |
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
| `identifier_required` | 422 | `IdentifierRequired` (contrib.auth.exceptions): A user needs at least one identifier: a username, an email, or a number. |
| `identity_email_taken` | 409 | `EmailTaken` (contrib.identity.exceptions): An account has the identity's email: log in to it, then link the identity. |
| `identity_last_way_in` | 422 | `LastWayIn` (contrib.identity.exceptions): Unlinking would leave the user no way to log in. |
| `identity_not_linked` | 422 | `NoAccount` (contrib.identity.exceptions): The identity is linked to no account, and none may be made for it. |
| `identity_provider_error` | 502 | `ProviderError` (contrib.identity.exceptions): The provider refused, could not be reached, or answered what does not hold. |
| `identity_taken` | 409 | `IdentityTaken` (contrib.identity.exceptions): The identity is linked to another account already. |
| `inexact_amount` | 422 | `InexactAmount` (money): An amount with more places than its currency has; nothing is rounded. |
| `infected_file` | 422 | http.uploads: The virus scan refused the file. |
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
| `invalid_field` | 422 | `InvalidField` (ddd.exceptions): A field was given a value its markers rule out (`MaxLength`, `Range`, ...). |
| `invalid_filter` | 422 | http.filters: a filter, lookup, ordering, or search a `FilterSet` does not offer, or over its bounds. |
| `invalid_flag` | 422 | `InvalidFlag` (contrib.flags.models): A flag's rules are out of range. |
| `invalid_grant` | 422 | `InvalidGrant` (contrib.objectpermissions.services): A grant names a permission of another model, or no one or two at once. |
| `invalid_group_name` | 422 | `InvalidGroupName` (contrib.auth.exceptions): The group name is empty or too long. |
| `invalid_history_query` | 422 | contrib.audit.api: Name a target, or an aggregate's field and id. |
| `invalid_host` | 400 | http.middleware: Invalid host header. |
| `invalid_idempotency_key` | 400 | contrib.idempotency.middleware: Idempotency-Key must be 1 to 255 printable ASCII characters. |
| `invalid_limit` | 422 | `InvalidLimit` (contrib.limits.exceptions): A limit, or a movement checked against limits, that cannot be as written. |
| `invalid_mobile_number` | 422 | `InvalidMobileNumber` (contrib.auth.exceptions): The mobile number is not one in international form (E.164). |
| `invalid_money` | 422 | `MoneyError` (money): An amount or a currency that money cannot hold; the HTTP layer answers 422. |
| `invalid_password` | 422 | `InvalidPassword` (contrib.auth.exceptions): The password breaks the configured rules (``PASSWORD_VALIDATORS``). |
| `invalid_phone` | 422 | `InvalidPhone` (contrib.phones.exceptions): Not a phone number in international form, such as `+33612345678`. |
| `invalid_setting_value` | 422 | `InvalidSettingValue` (contrib.flags.exceptions): The value does not fit the dynamic setting's type. |
| `invalid_statement` | 422 | `InvalidStatement` (contrib.accounting.exceptions): A statement that cannot be imported: its format, or one of its lines. |
| `invalid_tenant` | 400 | domainkit.tenancy: The tenant the request names is not a tenant id. |
| `invalid_token` | 400 | contrib.auth.mfa: The login has expired; log in again. |
| `invalid_transaction` | 422 | `InvalidTransaction` (contrib.auth.sca): The amount, payee, or purpose cannot be authorized as given. |
| `invalid_user_field` | 422 | `InvalidUserField` (contrib.auth.exceptions): A value does not fit the type of the user field it is for. |
| `invalid_username` | 422 | `InvalidUsername` (contrib.auth.exceptions): The username is empty, too long, or uses characters it may not. |
| `invalid_value` | 422 | http.errors: A value cannot be stored: it is too long, out of range, or holds a character the database refuses. |
| `invalid_verification_code` | 400 | contrib.auth.recovery: The code is wrong or has expired; ask for a new one. |
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
| `mobile_number_taken` | 409 | `MobileNumberTaken` (contrib.auth.exceptions): Another user has that mobile number. |
| `multiple_found` | 409 | `MultipleFound` (ddd.exceptions): A lookup that expects one aggregate met several (`get_or_create`). |
| `no_enrolment` | 400 | contrib.auth.mfa: No enrolment is waiting for a code; start one first. |
| `no_open_period` | 422 | `NoOpenPeriod` (contrib.accounting.exceptions): No accounting period covers the entry's date (``REQUIRE_PERIOD``). |
| `no_second_factor` | 409 | contrib.auth.stepup: Turn on a second factor first: confirming it is you needs one. |
| `no_session` | 400 | contrib.auth.cookies: This request has no browser session. |
| `not_a_member` | 403 | domainkit.tenancy: `TENANCY.MEMBERSHIP` says the caller does not belong to the tenant. |
| `not_acceptable` | 406 | resources.renderers: This resource cannot answer in a media type the request accepts. |
| `not_applied` | 424 | resources.batches: Another item of the batch failed, so nothing was applied. |
| `not_authenticated` | 401 | http.access: Authentication credentials were not provided. |
| `not_found` | 404 | `NotFound` (ddd.exceptions): The requested aggregate does not exist. contrib.auth.signup: Not Found. contrib.flags.dependencies: Not found. Starlette or FastAPI. |
| `not_the_maker` | 403 | `NotTheMaker` (contrib.approvals.exceptions): Only whoever submitted a request may cancel it. |
| `overloaded` | 503 | http.shedding: The server is busy; try again shortly. |
| `passkey_not_found` | 404 | `PasskeyNotFound` (contrib.devices.exceptions): The user has no passkey with that id. |
| `passkey_refused` | 401 | `PasskeyRefused` (contrib.devices.exceptions): A passkey's answer does not hold; the reason is logged, not sent. |
| `passkeys_off` | 404 | `PasskeysOff` (contrib.devices.exceptions): `DEVICES.RP_ID` is not set: no passkeys here. |
| `password_change_required` | 403 | contrib.auth.api: This password was set for you: choose a new one to log in. |
| `password_checks_busy` | 503 | `PasswordChecksBusy` (contrib.auth.hashers): Every hashing thread is busy and the queue is full. |
| `password_required` | 400 | contrib.auth.stepup: Enter your password as well. |
| `period_closed` | 422 | `PeriodNotOpen` (contrib.accounting.exceptions): An entry, or a new period, dated on or before the end of a closed period. |
| `period_name_taken` | 409 | `PeriodNameTaken` (contrib.accounting.exceptions): Another period has that name. |
| `period_not_found` | 404 | `PeriodNotFound` (contrib.accounting.exceptions): No accounting period has that id. |
| `periods_overlap` | 422 | `PeriodsOverlap` (contrib.accounting.exceptions): A new period whose dates another period covers, or that ends before it starts. |
| `permission_denied` | 403 | `PermissionDenied` (ddd.exceptions): The actor may not perform the operation. |
| `phone_already_verified` | 409 | `PhoneAlreadyVerified` (contrib.phones.exceptions): The user verified this number already. |
| `phone_approval_required` | 403 | `PhoneApprovalRequired` (contrib.ciba.exceptions): This login must be approved from the user's mobile app (`CIBA.APPROVE_LOGINS`). |
| `phone_enrollment_required` | 403 | `PhoneEnrollmentRequired` (contrib.ciba.exceptions): The user has no bound device, and every login comes from one (`CIBA.APPROVE_LOGINS = "always"`). |
| `phone_not_found` | 404 | `PhoneNotFound` (contrib.phones.exceptions): The user has no verified number with that id. |
| `phone_required` | 403 | `PhoneRequired` (contrib.ciba.exceptions): Only the user's mobile app manages their logins and the ways into the account (`CIBA.APPROVE_LOGINS`, ADR 0130, ADR 0134). |
| `phone_unavailable` | 409 | `PhoneUnavailable` (contrib.ciba.exceptions): No phone of the signed-in user can be asked now: none is bound, `MAX_PENDING` requests wait, or the user reported one within `REPORT_COOLDOWN`. |
| `policy_removed` | 409 | `PolicyRemoved` (contrib.approvals.exceptions): The request's guard or policy no longer exists: it is neither decided nor run. |
| `precondition_failed` | 412 | `PreconditionFailed` (ddd.exceptions): The caller expected another version of the aggregate than the stored one. |
| `precondition_required` | 428 | resources.conditional: Send If-Match with the ETag of the version you are changing. |
| `qr_code_expired` | 422 | `QrCodeExpired` (contrib.ciba.exceptions): The QR code is unknown, was claimed, or expired. |
| `rate_limited` | 429 | http.ratelimit: `RATE_LIMIT` or a route's `Throttle`; contrib.auth: login, password, code, link, and signup throttles. |
| `reauthentication_required` | 401 | `ReauthenticationRequired` (contrib.auth.stepup): A change to how one logs in needs the password, or a recent step-up. |
| `reference_reused` | 409 | `ReferenceReused` (contrib.accounting.exceptions): A source and reference that name an entry or a hold made otherwise. |
| `referenced` | 409 | http.errors: a foreign key refused the change. |
| `refresh_not_supported` | 400 | contrib.auth.authentication: Tokens are not refreshed here; log in again. |
| `request_not_pending` | 409 | `RequestNotPending` (contrib.approvals.exceptions): The request was approved, rejected, cancelled, or has expired. |
| `request_tampered` | 409 | `RequestTampered` (contrib.approvals.exceptions): The request was changed outside the application: its seal does not hold. |
| `request_timeout` | 408 | http.middleware: The request body took too long to arrive. |
| `scanner_unavailable` | 503 | `ScannerUnavailable` (storage.scanning): The virus scanner could not scan the upload: 503, nothing kept. |
| `scope_not_held` | 403 | contrib.auth.personal_tokens: a token asked for a scope its owner does not hold. |
| `second_factor_required` | 401 | `SecondFactorRequired` (contrib.devices.exceptions): The user has a second factor: enrollment needs its code too. |
| `second_factor_unreadable` | 409 | `SecondFactorUnreadable` (contrib.auth.mfa): No key held opens the user's TOTP secret: `SECRET_KEY` changed without it in `SECRET_KEY_FALLBACKS` (`resealmfa`, `resetmfa`). |
| `service_busy` | 503 | `ServiceBusy` (exceptions): Something the operation needs is busy now; trying again later may work. |
| `sign_in_not_found` | 404 | `SignInNotFound` (contrib.ciba.exceptions): The user has no sign-in request with that id that waits for an answer. |
| `sign_in_unknown` | 400 | `SignInUnknown` (contrib.ciba.exceptions): No request has that handle, or it was redeemed, or is another client's. |
| `signup_taken` | 409 | contrib.auth.signup: An account already uses this username or this address; sign up again with another username, or log in. |
| `slow_down` | 400 | `SlowDown` (contrib.ciba.exceptions): A poll came before the interval: wait 5 seconds more from now on. |
| `step_up_required` | 401 | contrib.auth.access: Confirm it is you: authenticate again to do this. |
| `step_up_unsupported` | 400 | contrib.auth.stepup: These credentials cannot step up: log in, then step up. |
| `tenant_mismatch` | 403 | `TenantMismatch` (ddd.tenancy): An aggregate of another tenant was handed to this tenant's repository. |
| `tenant_not_named` | 400 | domainkit.tenancy: The request names no tenant, where `TENANCY.REQUIRED`. |
| `tenant_required` | 403 | `TenantRequired` (ddd.tenancy): Tenant data was asked for with no tenant current. |
| `throttle_unavailable` | 503 | contrib.auth.throttling: Attempts cannot be counted right now; try again later. |
| `token_lifetime_too_long` | 422 | contrib.auth.personal_tokens: A personal token asked for longer than `AUTH.PERSONAL_TOKEN_MAX_AGE`, or for no end. |
| `token_not_found` | 404 | contrib.auth.personal_tokens: No such token. |
| `too_many_connections` | 503 | http.live: `LIVE_MAX_CONNECTIONS` or `LIVE_MAX_CONNECTIONS_PER_CALLER` is reached. |
| `too_many_keys` | 409 | `TooManyKeys` (contrib.devices.exceptions): The user has `MAX_PASSKEYS` (or `MAX_DEVICES`) already. |
| `too_many_phones` | 409 | `TooManyPhones` (contrib.phones.exceptions): The user holds `PHONES.MAX_PHONES` numbers already. |
| `too_many_tokens` | 409 | contrib.auth.personal_tokens: the user holds the most personal tokens allowed. |
| `transaction_closed` | 409 | `TransactionClosed` (contrib.ciba.exceptions): The transaction was confirmed or used already, or expired. |
| `transaction_confirmed` | 409 | contrib.auth.sca: This transaction was confirmed already. |
| `transaction_expired` | 409 | contrib.auth.sca: This transaction can no longer be confirmed; start it again. |
| `transaction_limit_exceeded` | 422 | `TransactionLimitExceeded` (contrib.limits.exceptions): A movement over the most one movement may be (``per_transaction``). |
| `transaction_needs_other_factors` | 409 | `TransactionNeedsOtherFactors` (contrib.ciba.exceptions): `AUTH.SCA.METHODS` asks for more than the phone's key proves: confirm with `/sca/{id}/confirm`. |
| `transaction_not_authorized` | 403 | `TransactionNotAuthorized` (contrib.auth.sca): No authorization holds for this transaction: get one, then try again (403). |
| `transaction_not_found` | 404 | `TransactionNotFound` (contrib.auth.sca): No transaction of the user waits under that id (404). |
| `unauthorized` | 401 | http.metrics: A valid metrics token is required. |
| `unbalanced_entry` | 422 | `UnbalancedEntry` (contrib.accounting.exceptions): An entry whose debits and credits differ in some currency. |
| `unknown_action` | 404 | `UnknownAction` (contrib.approvals.exceptions): No guarded command has that name. |
| `unknown_currency` | 422 | `UnknownCurrency` (money): A code that is neither an ISO 4217 currency nor a registered one. |
| `unknown_group` | 404 | `UnknownGroup` (contrib.auth.exceptions): No group has that name. |
| `unknown_identity_provider` | 404 | `UnknownProvider` (contrib.identity.exceptions): No identity provider has that name. |
| `unknown_permission` | 404 | `UnknownPermission` (contrib.auth.exceptions): No permission has that key (``<domain>.<codename>``). |
| `unknown_setting` | 404 | `UnknownSetting` (contrib.flags.exceptions): No dynamic setting is declared with that key. |
| `unknown_user_field` | 422 | `UnknownUserField` (contrib.auth.exceptions): The user model has no such field, or it changes another way. |
| `unsafe_url` | 422 | `UnsafeURL` (http.guard): A URL that may not be called; the message never quotes it. |
| `unsafe_webhook_url` | 422 | `UnsafeTarget` (contrib.webhooks.guard): A webhook URL that may not be called; the message never quotes it. |
| `unsupported_device_key` | 422 | `UnsupportedKey` (contrib.devices.exceptions): Not a P-256 or Ed25519 public key. |
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
| `wrong_code` | 422 | `WrongCode` (contrib.phones.exceptions): The code is not the one sent; a few more tries are left. |
| `wrong_match_code` | 422 | `WrongMatchCode` (contrib.ciba.exceptions): The number typed is not the page's: the sign-in is denied. |
| `wrong_password` | 400 | contrib.auth.api: Your old password was entered incorrectly. contrib.auth.mfa: Your password was entered incorrectly. contrib.auth.stepup: Your password was entered incorrectly. |

## See also

- [Errors and responses](responses.md#errors): the error body, and which
  exception gives which status.
- [Validation](../validation.md): the errors of refused values.
- [Versioning](../versioning.md#what-is-public): why codes never change.
