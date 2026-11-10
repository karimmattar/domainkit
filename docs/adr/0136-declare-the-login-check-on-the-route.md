# 0136. Declare the login check on the route

- Status: Accepted
- Date: 2026-11-10

## Context

Routes that add or remove a way into an account run auth's
`on_manage_logins` checks: in the phone modes, only a request the user's
bound device signs passes
([ADR 0130](0130-show-where-each-login-came-from-and-manage-logins-from-the-phone.md)).
Each route called the check in its body, and the check was added route by
route:

| Release | Routes that got the check |
| --- | --- |
| 2.2.0, ADR 0130 | listing tokens, ending another login, `everywhere`, `others` |
| 2.2.0, ADR 0131 | unbinding a device |
| 2.2.0, ADR 0132 | the authenticator app, recovery codes, passkeys |
| 2.2.2, ADR 0134 | binding a device, push tokens, personal tokens, linked identities |

Each round found routes the one before missed, the last one from an
audit. Nothing told the author of a new route to think of it. The
framework already refuses a route that declares no access rule
(`REQUIRE_ROUTE_RULES`): a rule that is declared can be checked for.

A check called in the body also runs after the body is read: a request
with a body that does not validate was answered `422`, not the check's
refusal.

## Decision

### The check is a dependency of the route

`MANAGES_LOGINS` (`domainkit.contrib.auth.gates`, and `auth.integration`)
is a dependency that runs the `on_manage_logins` checks on the request's
user:

```python
@router.post("/devices", dependencies=[MANAGES_LOGINS])
```

It runs before the body is read, so a refused request gets the check's
answer whatever it sent. A request with no user is `401`. A project's own
route that adds a way into an account declares it the same way.

Three routes still call `check_manage_logins` in their body, because only
some of their requests manage other logins: `POST /logout` (with
`everywhere` or `others`), `DELETE /tokens/{id}` (a token that is not the
request's own), and `POST /password` (asked to end the devices' logins).

### A test refuses a route that declares nothing

`test_account_routes` walks every route of the auth apps that a signed-in
user changes something with. Each one must either:

- declare `MANAGES_LOGINS`;
- or be listed in the test, with the reason it needs no check.

A new route that does neither fails the test, and so does a listed route
that no longer exists. A second test sends an empty request from a web
session to every declared route, in the phone modes, and expects
`403 phone_required`.

The check runs in the framework's own tests, not at a project's startup:
only the framework's routes can be classified by the framework.

## Consequences

- **A new account route cannot miss the check silently.** Its author
  declares it, or writes down why not.
- **A refused request is answered before its body is validated:** `403
  phone_required` where a malformed body got `422` before.
- **Projects have one name to declare** on their own routes.
