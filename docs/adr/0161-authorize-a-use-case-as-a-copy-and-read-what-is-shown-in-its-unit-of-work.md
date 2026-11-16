# 0161. Authorize a use case as a copy, and read what is shown in its unit of work

- Status: Accepted
- Date: 2026-11-16

## Context

[ADR 0144](0144-authorize-one-act-not-a-window-of-time.md) built action
authorizations around a service, `service(command, uow)`: an `Action`
names the command class, `@requires_authorization(ACTION)` decorates the
service, and `StepUp(for_=...)` on the route asks. A project that writes
every use case with `@use_case` (the simple layout `startdomain` writes)
reported three things in the way (user report A13):

- **The decorator does not take a use case.** It wraps the call
  `service(command, uow, ...)` and returns a plain function. A
  `UseCase` is called `use_case(uow, **fields)`, and routes wire its
  `run(command, uow)`. `ResourceRouter.use_case` needs the `UseCase`
  itself, for its command, fields, and name.
- **Guarding the use case in place guards every way in.** The project
  runs the same use case from two routes: the web page's, which asks for
  an authorization of the act, and the phone's, which is the user's own
  device and asks none. Approvals solved the first point for its guard
  (`requires_approval` above `@use_case` guards a copy's `run`, user
  report P1), but approvals wants every way in guarded; an act wants
  only the route that asks.
- **`show` needs a concrete unit of work.** `show(command)` builds what
  the user is shown from stored state, so it opens a unit of work. In the
  simple layout, `controllers.py` (the application layer) holds the use
  cases and the ports; the unit of work on the database lives in
  `models.py` (infrastructure), which `controllers.py` may not import
  (`domainkit.architecture`). So the action cannot sit next to its use
  case.

The project worked around all three with `authorized(use_case, action)`,
a copy of the use case whose `run` spends the authorization, in an
`actions.py` per domain that imports both layers.

Two smaller notes of the same report (A17) concern the same flow:

- The twin's `201` (`ActionAuthorizationOut`) does not say what may
  confirm the authorization. A page that offers a passkey or a code asks
  `GET /authorizations/{id}` again to learn `methods`.
- `domainkit.testing` helps a test confirm a transaction (`confirm_sca`),
  not an act. The project's tests read the digest from the stored row to
  confirm acts that ask the phone, which a code cannot confirm.

### What others do

- **Django's `method_decorator`** applies a function decorator to a
  method by wrapping it, leaving the function it was given as it was [1].
  Approvals' `_guarded_use_case` copies the use case and replaces the
  copy's `run`, for the same reason: `UseCase.__call__` runs `run`, so one
  attribute guards every way into the copy.
- **Django's `permission_required("app.perm")`** names a permission by
  string and resolves it per request [2]: the decorator needs nothing but
  the name where it is written.
- **The composition root** [3]: an application wires its object graph
  in one place, near its entry point; the inner layers depend on ports,
  and the outer layer hands them the concrete adapters. Here the routes
  are that place: `ResourceRouter(unit_of_work=...)` already gives each
  route its concrete unit of work.
- **OWASP's Transaction Authorization Cheat Sheet** [4] asks that the
  user see the significant data of the operation (1.1), that the server
  enforce the method (2.2), and that each credential work once (1.5).
  None of that changes; what changes is where the code that builds what
  is shown may live, and what the client is told of the method.
- **CIBA** [5] answers its authentication request with what the client
  needs for its next step (`auth_req_id`, `expires_in`, `interval`), and
  **RFC 9470** [6] puts what a step-up needs in the challenge itself: a
  client should not need a second request to learn how to go on.
- **DRF's `force_authenticate`** [7], and domainkit's own `token_for`
  ([ADR 0107](0107-make-test-users-through-the-domain-and-sign-in-through-a-protocol.md)),
  let a test skip a step it does not test, through a protocol the
  installed domain implements, refused outside tests (SEC-12a-2).

### Options

For the use case:

| Option | Way in guarded | Cost |
| --- | --- | --- |
| Guard the use case in place, as approvals does | Every way in, the phone's route included | The phone's route would ask for an authorization it has no screen to ask for. |
| **Return a guarded copy; leave the use case as it was** | The routes given the copy | The route that asks names the copy, written where routes are wired. |
| `requires_authorization("purpose")`, the action resolved when called | Where the decorator sits | Above `@use_case` in `controllers.py` it guards in place (the first row's cost); `check` and the digest need the action earlier than a call. |

For what `show` reads in:

| Option | Where the action may live | Cost |
| --- | --- | --- |
| `show(command)` opens a unit of work itself (2.4) | Where the concrete unit of work is importable | Not in `controllers.py`; an `actions.py` per domain that imports both layers. |
| `Action(unit_of_work=...)`, or `register_action(action, unit_of_work=...)` | The action's module must import it, or `ready()` must | Another place that names the concrete unit of work; the route already has one. |
| **`show(command, uow)`: the asker lends the unit of work the act runs in** | Anywhere: `show` reads through the port | The twin lends its route's; a route of one's own passes the class, `unit_of_work=`. |

## Decision

- **`requires_authorization(action)` takes a `@use_case`** and returns
  a copy whose `run`, and so its call, spends the authorization, as a
  decorated service does. The use case itself is unchanged: give the
  copy to the route that asks (`ResourceRouter.use_case`, with
  `StepUp(for_=...)`), and the use case to the routes, tasks, and tests
  that run it without an act. A use case is guarded only for its own
  command: another, or a class its command derives from (`Command`,
  whose digest binds no field), is refused (`ImproperlyConfigured`).
- **A route that asks must run a service that spends.** When a
  `ResourceRouter` declares a route whose rule asks, the rule may name
  a problem with the route's service (`service_problem`), and the route
  is refused. `StepUp(for_=P)` refuses the use case itself, and a
  service that spends another act's authorization: neither ever spent
  `P`. A service without the mark of `requires_authorization` may call
  one that has it, as a 2.4 project may have written: it warns
  (`DomainkitDeprecationWarning`), and 3.0 refuses it. `check`'s
  `auth.E110` looks at a purpose across the application, so it could
  not see the use case itself on one route once its copy was on
  another; it stays for plain routes.
- **`Action(command=<a use case>)`** takes the use case's command. The
  attribute stays a class, for type checkers too: only the constructor
  takes the union.
- **`show(command, uow)`.** An `Action` whose `show` has a parameter
  named `uow` (given by name) reads in the unit of work the act runs in,
  so it reads through the port, and the action can live beside its use
  case. A new one is made by `request_action_authorization(...,
  unit_of_work=<its class>)`, which a caller cannot hand over open, and
  opened for `show`; `show` is lent it as one whose `commit` is refused,
  and a block `async with uow:` in it enters nothing; it is left without
  a commit. Without `unit_of_work`, such an action is refused
  (`ImproperlyConfigured`). A `show(command)`, or a 2.4 `show` with a
  parameter of its own (`language="en"`), is called as before.
- **The twin lends its route's unit of work.** It passes the route's
  unit of work, not yet open, as `uow=` to a rule whose
  `ask_authorization` takes `uow` by name, and calls one written for 2.4
  (or whose signature cannot be read) as before. `AsksAuthorization` keeps its signature, so a rule of 2.4
  still type-checks as one; its docstring names the parameter.
  `StepUp(for_=...)` takes it.
- **The answers say what may confirm.** `ActionAuthorizationOut`, the
  twin's `201`, carries `methods`, as `GET /authorizations/{id}` does;
  so does approvals' approve twin and the act the phone lists, which
  are built from it.
- **Test helpers.** `domainkit.testing.authorize_act(client, url, app=None,
  *, json=None, password=None, prefix="/auth")` asks the twin and
  confirms; `confirm_authorization(client, authorization_id, app=None,
  ...)` confirms one asked elsewhere. With an `Authenticator`, through
  the confirm route, as `confirm_sca` does. Without one, the installed
  domain that keeps authorizations confirms it in the test as proving
  the `methods` the act asks (`AuthorizationConfirmer`, a protocol
  `AuthConfig` implements), refused unless `ENVIRONMENT = "test"`, as
  the test tokens are. Asking is not skipped: the user needs a way to
  confirm the act, as outside tests.

## Consequences

- In the simple layout, the action, its `show`, and the use case live
  in `controllers.py`; `routes.py` wires the guarded copy on the route
  that asks. No `actions.py` that imports two layers.
- A route of one's own whose action reads a unit of work passes
  `unit_of_work=`; forgetting it is an `ImproperlyConfigured` at the
  first ask, in development and tests, not a wrong screen.
- Whether a `show` reads a unit of work, and whether a rule takes
  `uow`, is read from the name of a parameter. A `show(command,
  uow=None)` counts as reading one. A rule of 2.4 keeps working; one
  that wants the unit of work adds the parameter.
- A `ResourceRouter` route that asks `StepUp(for_=...)` and runs a
  service without the mark warns when declared: a 2.4 route whose
  undecorated service called a decorated one worked, and keeps working
  until 3.0; its service is decorated instead. A plain FastAPI route is
  still `check`'s to judge (`auth.E110`).
- `methods` is a new field of three answers. Clients that read JSON
  leniently see nothing change.
- The test confirmation skips the second factor, so it is held to the
  test settings like the test tokens; a test of the phone's or a
  passkey's own flow still goes through their routes.

## References

1. Django, "Decorating the class" (`method_decorator`):
   <https://docs.djangoproject.com/en/5.2/topics/class-based-views/intro/#decorating-the-class>
2. Django, the `permission_required` decorator:
   <https://docs.djangoproject.com/en/5.2/topics/auth/default/#the-permission-required-decorator>
3. Mark Seemann, "Composition Root":
   <https://blog.ploeh.dk/2011/07/28/CompositionRoot/>
4. OWASP, Transaction Authorization Cheat Sheet:
   <https://cheatsheetseries.owasp.org/cheatsheets/Transaction_Authorization_Cheat_Sheet.html>
5. OpenID Connect CIBA Core 1.0, "Successful Authentication Request
   Acknowledgement":
   <https://openid.net/specs/openid-client-initiated-backchannel-authentication-core-1_0.html#rfc.section.7.3>
6. RFC 9470, OAuth 2.0 Step Up Authentication Challenge Protocol:
   <https://www.rfc-editor.org/rfc/rfc9470>
7. Django REST framework, "Forcing authentication":
   <https://www.django-rest-framework.org/api-guide/testing/#forcing-authentication>
