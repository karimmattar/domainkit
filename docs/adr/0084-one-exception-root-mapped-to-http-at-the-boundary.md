# 0084. Root every exception at DomainkitError, and map errors to HTTP at the boundary

- Status: Accepted
- Date: 2026-10-26

## Context

The framework defined 105 exception classes on 0.9.2, under 37 roots of
their own: `ImproperlyConfigured`, `DomainError`, `CacheError`,
`BrokerError`, `LedgerError`, `StorageError`, `CommandError`,
`BadSignature`, `DecryptionFailed`, the mail and notification failures on
bare `Exception`, and the rest on `ValueError`, `RuntimeError`,
`LookupError`, `TimeoutError`, `OSError`, starlette's `HTTPException`, or
httpx's errors. A caller that wanted to catch "whatever domainkit raised",
a worker logging a subsystem that is down, for example, had to list them
(DSGN-06 of the 0.9.0 design audit). SQLAlchemy (`SQLAlchemyError`), httpx
(`HTTPError`), Django, and Litestar each give that caller one class.

Some modules below the HTTP layer raised HTTP exceptions themselves. The
password hashers, which a management command calls too, raised a 503
`APIError` when their queue was full, so a command-line caller received an
HTTP exception. `domainkit.http.errors` already mapped `DomainError`
subclasses to status codes: the domain never names HTTP.

Options considered:

- **Re-parent each root on `DomainkitError` alone.** Simple, but every
  `except ValueError` or `except TimeoutError` in projects would stop
  catching what it caught.
- **Add `DomainkitError` beside each root's base.** Multiple inheritance;
  every existing handler keeps working. A walk of every class's method
  resolution order before and after shows each only gains `DomainkitError`.
- **An `InfrastructureError` level between the root and the subsystems.**
  Useful, but it would put `FileTooLarge` (a refusal) beside
  `CacheUnavailable` (an outage); left for later, when the subsystems'
  errors are sorted by kind.

## Decision

- `domainkit.exceptions.DomainkitError(Exception)` is the root of every
  exception class domainkit defines, public or not. A class with a base
  outside domainkit keeps it, first, and gains `DomainkitError` second:
  `class InvalidQuery(ValueError, DomainkitError)`. `DomainError` is one,
  so business errors are caught with the rest. Warnings are not:
  `DomainkitDeprecationWarning` stays a `DeprecationWarning`.
- `domainkit.exceptions` is public (`__all__`): `DomainkitError`,
  `ImproperlyConfigured`, and `ServiceBusy`.
- Below the HTTP layer, code raises errors of its own, never an HTTP
  exception, and `domainkit.http.errors` maps them, as it maps
  `DomainError`. `ServiceBusy(message, retry_after=...)`, with a `code`,
  says that something the operation needs is busy; the HTTP layer answers
  it with 503, its message and code, and `Retry-After`, in a route and in
  a batch item alike (`busy_refusal`). `PasswordChecksBusy` is one.
- The mapping changes no response: a test pins the saturated login's
  status, headers, and body in both error formats, and passes unchanged on
  0.9.2.
- Names: a root ends in `Error`; other classes name what happened
  (`PasswordChecksBusy`, `SignatureExpired`). Existing names stay.
- `domainkit_tests/test_exceptions.py` imports every module in a process
  of its own and fails on a class outside the root.

## Consequences

- `except DomainkitError` catches the framework, once.
- A new exception class must have the root; the test says which one does
  not.
- `PasswordChecksBusy` is no longer an `HTTPException`: code that caught it
  as one catches it by its own name, or as `ServiceBusy`.
- Route modules (logins, recovery, admin APIs) still raise `APIError`:
  they are the HTTP layer. Moving their refusals to domain errors would
  change no answer but touch every one; it is not planned.
