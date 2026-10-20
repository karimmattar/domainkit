# 0032. Offer RFC 9457 problem details as an error format

- Status: Accepted
- Date: 2026-10-19

## Context

Every error answers `{"detail", "code", "request_id"}`, plus `errors` for
invalid fields. Clients switch on `code`. RFC 9457 (which obsoletes RFC
7807) defines problem details, `application/problem+json` with `type`,
`title`, `status`, `detail`, and `instance`, and gateways, client
libraries, and API guidelines increasingly expect it. Changing the default
body would break every existing client that reads `detail` and `code`
where they are; keeping only the current body leaves projects that must
follow such guidelines to rewrite every error themselves, including those
the middleware answers before any route runs.

The question was also what `type` should be. RFC 9457 says it identifies
the problem type and should resolve to documentation, and that
`about:blank` means the type is the HTTP status alone, whose `title` is
then the status phrase.

## Decision

- `ERROR_FORMAT` chooses the body: `"domainkit"` (the default, unchanged)
  or `"problem+json"`. The choice is made where every error body is built
  (`error_response`), so the middleware's refusals (hosts, body sizes,
  rate limits, idempotency, 500s) follow it too.
- In problem details, `status` and `detail` are the error's, `instance` is
  the request's path (percent-encoded, never the query), and `code`,
  `request_id`, and `errors` stay as extension members, so a client
  written for one format finds `code` in both.
- `type` is `about:blank`, and `title` the status phrase, unless
  `ERROR_TYPE_BASE_URL` is set: then each code is a type of its own, the
  URL plus the code, titled with the code in words. Codes stay the
  machine-readable identity; the URL only makes it resolvable.
- OpenAPI documents the format in use: error responses become
  `application/problem+json` with a `ProblemDetails` schema.

## Consequences

Projects choose the format once, in settings, and get it for every error.
The default changes nothing. Field errors keep their `loc`, `msg`, and
`type` rather than RFC 9457's suggested JSON pointers, so clients parse
them the same way in both formats. A project that sets
`ERROR_TYPE_BASE_URL` takes on serving a page for each code it can return.
Errors raised by FastAPI applications mounted inside the main one still use
their own handlers.
