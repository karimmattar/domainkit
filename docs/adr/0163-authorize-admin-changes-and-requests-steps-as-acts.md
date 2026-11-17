# 0163. Authorize admin changes, and a request's steps, as acts

- Status: Accepted; amended 2026-11-17 (at the end); amended by [0172](0172-hold-admin-changes-by-what-they-grant-and-check-before-the-hold.md)
- Date: 2026-11-16
- Amends: [0144](0144-authorize-one-act-not-a-window-of-time.md),
  [0154](0154-hold-admin-changes-for-approval-and-show-requests-to-those-they-concern.md)

## Context

A project that runs an identity service on the framework moved its
sensitive changes from a step-up window to action authorizations (ADR
0144): the phone shows the act in the server's words, and its approval
is worth that act, with its arguments, once. Two sets of acts could not
move, and it reported them (user report A6):

- **The admin API's changes**: making staff, giving groups and
  permissions, setting a password, impersonating, deleting a user, and the
  group changes. Its routes (`contrib/auth/admin.py`) are plain functions:
  no service a project can decorate with `requires_authorization`, and no
  `/authorization` twin. A project that put `StepUp(for_=...)` on them
  with `select_routes` was refused by `check` (`auth.E110`: "no decorated
  service spends it"), and rightly: nothing would spend it.
- **Approvals' submit, reject, and cancel.** 2.4 gave approving a
  built-in action (`APPROVALS.APPROVE_ACTION`, `approvals.approve`); the
  other three steps have none.

So those acts kept `StepUp(max_age=...)`, and the only phone step-up a
web session has is the free-text `POST /app-sign-in/step-up` that 2.4
deprecates and 3.0 removes (ADR 0144). The project keeps that route
mounted for these acts alone, and a test ignores its deprecation. The
most sensitive acts of a back office are the ones that cannot be
authorized one by one.

### What others do

- **OWASP's Transaction Authorization Cheat Sheet** [1] asks that the
  user see and confirm the data significant to the operation ("What You
  See Is What You Sign"), that each authorization be unique to its
  operation and used once (1.5, 2.10), that it be enforced on the server
  (2.1), and that the data not change between authorization and execution
  (2.6, 2.8). The window of a step-up meets none of the last three.
- **OWASP ASVS 5.0** [2] asks for "further authentication with at least
  one factor or secondary verification before performing highly
  sensitive transactions or operations" (7.5.3, level 3), and full
  re-authentication before changing what authentication rests on (7.5.1).
- **Microsoft Entra's protected actions** [3] attach a policy (MFA, a
  stronger method) to single administrative permissions, such as
  updating a Conditional Access policy or deleting an object for good.
  It is asked "at the time the user attempts to perform the protected
  action", "independent of the role being used": per act, not per
  session.
- **GitHub's sudo mode** [4] is the window: after one confirmation, the
  dangerous actions pass for a few hours.
- **Built-in admins that are acts**: approvals' own `approvals.approve`
  (ADR 0144) binds the request and the fingerprint the approver was
  shown. Rich Authorization Requests [5] carry what is authorized as typed
  data, so a server can show it and bind the grant to it.

### Options

**The admin API.**

| Option | What it costs |
| --- | --- |
| Let `check` accept `StepUp(for_=...)` on the admin routes, and leave the twins and the spend to the project | The admin routes are the framework's: a project cannot reach inside them to spend, nor build their commands for a twin. Nothing would spend it. |
| One action per kind of change (`auth.set_staff`, `auth.set_password`, ...), each opt-in | Twelve purposes, twelve settings or a list of them, for one question: does this admin API's change need a confirmation of its own. Approvals already names the risky kinds for a second person (`ADMIN_APPROVALS`). |
| **One built-in action, `auth.admin_change`, whose command is the change (`AdminAct`: its kind, the user or group, what it sets, adds and takes away), opt-in by `AUTH.ADMIN_ACTION`, with a twin on every route that changes something** | A round trip per change. The twins are written by hand, as the routes are. One more read to show the user's name. |

**Approvals' submit, reject, and cancel.**

| Option | What it costs |
| --- | --- |
| A recipe with `select_routes` and the project's own actions | The project would write the command, the show, the twin, and the spend for routes it does not own: what the report asks the framework to stop requiring. |
| One setting for all three | Rejecting stops a request; ADR 0154 keeps a step-up off it. A project may want submitting and cancelling confirmed and rejecting free. |
| **Three actions, `approvals.submit`, `approvals.reject`, `approvals.cancel`, each opt-in by its own setting, as `APPROVE_ACTION` is** | Three booleans more in `APPROVALS`. |

**Where the admin change is spent**, and when it is checked.

| Option | What it costs |
| --- | --- |
| A rule that spends | ADR 0144, question 1: a later rule or the change itself may fail, and burn it. |
| Spent before the `on_admin_change` checks | Approvals' check of a held change writes in a transaction of its own; the spend, written first in auth's, holds a lock it then waits on (SQLite), or is spent for a change that only waits for approval. |
| Spent after the checks, unchecked until then | A change other than the one authorized is held for approval first, or spends an approval (`by_maker`), before it is refused. |
| **Checked before the checks, spent after them, in the change's transaction** | Two calls instead of one: `check_authorization`, then `spend_authorization`. |

**What `check` says of code that spends.** `auth.E110` took only a
service decorated `requires_authorization`; the docs already offered
`spend_authorization` for code that is not a service, which `check`
then refused. A declaration where the action is declared,
`spent_in_code(action)`, keeps the check (a forgotten spend still fails)
and lets that code, the admin API's included, pass it.

## Decision

### The admin API: `auth.admin_change`

- `AUTH.ADMIN_ACTION` (off by default). On, every route of the admin API
  that changes a user or a group asks for an authorization of the action
  `auth.admin_change` (`Action-Authorization`), in place of a step-up
  window; auth registers the action when it starts. Off, the admin API is
  as it was.
- **The act's arguments are the change** (`AdminAct`): its kind
  (`AdminChange.kind`: `create`, `update`, `delete`, `password`,
  `logout`, `impersonate`, `groups`, `permissions`; or a group's:
  `group_create`, `group_rename`, `group_permissions`, `group_delete`),
  the user or group, the name a user or group is given, the values it
  sets (`AdminChange.values`, as JSON, and whether a user created is
  active), what it adds and takes away (sorted), and whether it leaves a
  usable password. Never a password:
  the digest is stored, and a hash of a password is attackable offline
  (ADR 0154). The subject is `auth.user:<id>` or `auth.group:<id>`
  (`new` for one being created).
- **What staff are shown** names the user (`get_username()`, their first
  identifier) or group, and the change: "Change a user's groups", "User:
  alice@example.com", "Adds: auditors". The flags that grant or take
  access (`is_superuser`, `is_staff`, `is_active`) come first and always
  show; a list too long for a line shows its first items and how many
  more ("auditors, clerks and 4 more"), so no group or permission added
  goes unsaid. Of the other fields, six lines in all at most, the last
  counting those not shown. Titles and labels are in the request's
  language.
- **A twin per changing route**: `POST <its path>/authorization`; where a
  `PATCH` and a `DELETE` share a path (`/users/{id}`, `/groups/{id}`),
  `<path>/authorization/patch` and `<path>/authorization/delete`, as a
  resource router names its twins of one path (user report A17). Each
  twin's endpoint carries the twins' marker, `domainkit_twin`. A twin
  takes the route's body, runs the route's own checks (the staff
  member's bounds, the fields, a password's validators, impersonation's
  rules, the last active superuser, and the new `on_create_staff` checks,
  which approvals uses to refuse creating staff while making staff is
  held), and answers `201`. It runs no `on_admin_change` check: those
  may write (approvals submits). What only the change finds (a name
  taken, a password the user had before) is refused after the
  confirmation. Off, it answers `404`.
- **Nothing loaded while off.** The admin API imports the action's
  module (`admin_actions`) on first use, with the setting on: a project
  that keeps it off loads no module more per request.
- **Checked, then spent.** `_admin_change` checks the request's
  authorization against the change before the `on_admin_change` checks
  (another change is refused, and voided, before it is held for approval
  or spends one), and spends it after them, in the route's transaction:
  spent if and only if the change commits. A change held for approval
  (`approval_pending`) spends nothing: once the request is approved, the
  same call makes it with an authorization, the first one while it lives
  (`AUTH.AUTHORIZATIONS.TTL`) or a new one.

### Approvals: `approvals.submit`, `approvals.reject`, `approvals.cancel`

- `APPROVALS.SUBMIT_ACTION`, `REJECT_ACTION`, and `CANCEL_ACTION` (each
  off by default), as `APPROVE_ACTION`: each needs
  `domainkit.contrib.auth`, and registers its action when approvals
  starts.
- `approvals.submit`: the command is `SubmitRequest` (the action, the
  arguments, the maker), on `approvals.action:<action>`; its twin is
  `POST /requests/authorization`; it shows "Submit a request", the
  action, and the arguments as they would be stored.
- `approvals.reject` and `approvals.cancel`: the command is the
  decision (the request, the person, and the reason), on
  `approvals.request:<id>`; their twins are `POST
  /requests/{id}/reject/authorization` and `.../cancel/authorization`;
  they show the action, the reason, and the arguments.
- Each twin refuses what the act would refuse (the maker rejecting,
  someone other than the maker cancelling, an unknown action, arguments
  that make no command), and the service spends it, as `approve_request`
  does: with the request's commit where the requests share auth's
  database. With a setting on, the service called from code with no
  request's authorization is refused (`AuthorizationRequired`): the act
  is the caller's own.
- Only `POST /requests` is `approvals.submit`. A guarded service called
  directly, and the admin API's held changes, submit as before; their
  own routes are the acts.

### `check`

- `spent_in_code(action)` (`auth.actions`) says that code spends an
  action's authorizations with `spend_authorization`. `auth.E110` refuses
  a route that asks for an action nothing spends: no decorated service,
  and no such declaration. `auth.admin_change` is declared so: a project
  that adds `StepUp(for_="auth.admin_change")` to a copy of the admin
  router passes `check`.
- `check` takes the declaration at its word: code that declares and
  never spends passes it. The request does not: an authorization a rule
  accepted and nothing spent is voided when the response starts, and
  logged; under `DEBUG` and in tests the request fails (`500
  authorization_unspent`), as for a forgotten decorator.

## Consequences

- A project drops the free-text step-up for these acts: it turns the
  settings on, removes its `StepUp(max_age=...)` from those routes, and
  its client asks the twin, confirms (the phone, a code, a passkey), and
  sends the change with `Action-Authorization`. 3.0 can remove
  `/app-sign-in/step-up` without leaving a back office without the phone.
- **Staff confirm every change**: each costs a round trip and a push or a
  code, one read of the row, and one write. A bulk edit of many users is
  many confirmations; that is the point of the setting, and it is off by
  default.
- **The screen shows what fits, and says what it leaves out.** Many
  groups added show as the first few and a count; many fields changed are
  counted past what fits. The flags never are. The page shows the whole
  change; the phone shows the user, the kind, the flags, and the first
  values.
- **A user's name on the phone** is personal data on the staff member's
  own device, as an approval's arguments are (ADR 0144); neither reaches
  the trail.
- **Rejecting can wait for a second factor** where a project turns
  `REJECT_ACTION` on. ADR 0154 kept a step-up off it, as stopping a
  request should not wait: the setting is separate for that reason.
- **The twins of a `PATCH` and a `DELETE` of one path** cannot share
  `<path>/authorization`: each names its method, as a resource router's
  do (user report A17). A twin is a route of its own to `select_routes`:
  a project leaves it in or out with its route.
- **New names**: `AUTH.ADMIN_ACTION`; `APPROVALS.SUBMIT_ACTION`,
  `REJECT_ACTION`, `CANCEL_ACTION`; `spent_in_code` in `auth.actions`;
  `SubmitAction`, `RejectAction`, `CancelAction` in `approvals.rules`;
  `on_create_staff` and `StaffCheck` in `auth.gates`; twelve twins on the
  admin API and three on the requests. No table changes.

## References

1. OWASP Cheat Sheet Series, *Transaction Authorization Cheat Sheet*:
   <https://cheatsheetseries.owasp.org/cheatsheets/Transaction_Authorization_Cheat_Sheet.html>
2. OWASP ASVS 5.0, V7.5 Defenses Against Session Abuse:
   <https://github.com/OWASP/ASVS/blob/master/5.0/en/0x16-V7-Session-Management.md>
3. Microsoft Entra ID, *What are protected actions*:
   <https://learn.microsoft.com/en-us/entra/identity/role-based-access-control/protected-actions-overview>
4. GitHub, *Introducing GitHub Sudo Mode*:
   <https://github.blog/news-insights/introducing-github-sudo-mode/>
5. RFC 9396, *OAuth 2.0 Rich Authorization Requests*:
   <https://www.rfc-editor.org/rfc/rfc9396>

## Amendment (2026-11-17): the steps from the project's own code

With a setting on, `submit_request`, `approve_request`, `reject_request`,
and `cancel_request` refuse a call that no request authorized, from code
too (`AuthorizationRequired`). That holds for the project's trusted code
as well: a periodic task that withdraws the requests left waiting, a
management command, an event handler that withdraws a request when what
it is about is deleted. Such code has no phone to ask. The functions that
do the work without asking, `record_submission`, `record_approval`,
`record_rejection`, and `record_cancellation`, were not public, so the
project had to turn a setting off, or import a name the version policy
does not cover (follow-up of 2.5.0).

[ADR 0161](0161-authorize-a-use-case-as-a-copy-and-read-what-is-shown-in-its-unit-of-work.md)
decided this shape for a use case: the route that asks runs the guarded
copy, and the use case stays as it was for the routes, tasks, and tests
that run it without an act. A process engine does the same with its own
API: Camunda checks authorizations "only if a user is currently
authenticated", and otherwise leaves them to the application that embeds
it ([When are authorizations checked](https://docs.camunda.org/manual/latest/user-guide/process-engine/authorization-service/#when-are-authorizations-checked)).
A service layer is a set of use cases "that can be used by any adapter",
an API or a command line
([Architecture Patterns with Python, chapter 4](https://www.cosmicpython.com/book/chapter_04_service_layer.html)):
what a person must prove is the entrypoint's to ask.

| Option | What it costs |
| --- | --- |
| A parameter of the guarded services (`authorized=False`) | One name that asks or not by an argument, which a route could pass as well. |
| A facade for code that also checks who acts: the approver against the policy through auth, as `_check_maker` checks the maker when the command runs | Four more names beside the services; a check that no service makes, and that can ask only what holds without a request: an approver rule that reads credentials refuses, and a principal of the project's own, or a project without auth, is not checked. |
| **The four functions, public as they are** | Code that names a person answers for who acts, as it does with every service of approvals. |

- `record_submission`, `record_approval`, `record_rejection`, and
  `record_cancellation` are public, in `domainkit.contrib.approvals`.
  Each takes its service's command and unit of work and returns the
  request. The services run them: with a setting on, behind the spend of
  the authorization; off, as they are. So they refuse all the services
  refuse but the missing authorization. A submission refuses an unknown
  action, arguments that make no command, and a command no policy
  holds. The other steps refuse a seal that does not hold and a request
  no longer pending. A decision also refuses a request whose time is
  up, the maker, a party to the command, a second decision, another
  fingerprint (approving), and a guard or policy gone. A withdrawal
  refuses anyone but the maker, and takes a request whose guard is gone,
  or whose time is up before the worker marks it expired, as
  `cancel_request` does. The approval that
  completes a request queues its command, and each step is an event the
  trail records with the context's actor (`system` in a periodic task,
  and in a management command unless it sets `operator_actions`).
- **Who acts is the caller's**, as for every service of approvals since
  ADR 0121: the routes take the maker and the approver from the principal
  and ask their rules (an approver of the policy, the approve rule,
  acting in one's own name, the model permissions, the guard's permission
  and `maker_rule` at submission). The functions ask none of them. The
  run asks the maker's (FIN-93), but only of a maker that is a user's
  id, with auth installed, on a connection without credentials, where a
  `maker_rule` that reads them refuses; a maker that names no user is
  never asked. With `APPROVE_ACTION` off, the default, `approve_request`
  runs this very function, so code already approved in the name it
  gave. The docs tell code that acts for a person it did not
  authenticate to check them first (`approvers_of`).
- **For the project's own code, never for a route a user reaches.** A
  call from a task and a call from a route look the same as they run,
  and an in-process handler of an event that a route commits runs inside
  that route's request, as the handler that withdraws a request whose
  subject is deleted may. A static look at a route's endpoint would not
  see a call made through the project's own service. So a route that
  asks for nothing is not refused; the docs and the docstrings state the
  rule.
- **A route that asks for the act and runs one is refused where it can
  be told.** Approvals' rules take any authorization of their act; the
  service binds it to the request and its arguments when it spends it.
  A record function spends none, so an authorization confirmed for one
  request would take the step on another (found in review).
  `DecisionAction`, and so `ApproveAction`, `RejectAction`,
  `CancelAction`, and `SubmitAction`, answers `service_problem`, as
  `StepUp(for_=...)` does (ADR 0161): a `ResourceRouter` route of
  approvals' rules that runs a record function, or another step's
  service, is refused when it is declared. A service of the project's
  own passes: the rule cannot tell what it calls. Any other route that
  asks and runs one takes the step, then leaves the authorization
  unspent: under `DEBUG` and in tests the request fails after the step
  is committed (`500 authorization_unspent`); elsewhere it answers as
  the step did, and the authorization is voided and logged. A
  `ResourceRouter` route that asks `StepUp(for_=...)` warns when it is
  declared.
- No setting, behaviour, or table changes. Four public names: a minor
  release (2.6).
