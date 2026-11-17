# 0172. Hold admin changes by what they grant, and check before the hold

- Status: Accepted
- Date: 2026-11-17
- Amends: [0154](0154-hold-admin-changes-for-approval-and-show-requests-to-those-they-concern.md),
  [0163](0163-authorize-admin-changes-and-requests-steps-as-acts.md)

## Context

A project that runs an identity service on the framework holds its
back office's grants for a second person with `APPROVALS.ADMIN_APPROVALS`
(ADR 0154) and confirms each change on the phone with
`AUTH.ADMIN_ACTION` (ADR 0163). Its audit found that an approval could
grant more than its approver saw, and that the order of the checks spent
approvals for nothing (user reports A21, A20, A19, and two rows of A17).

1. **`auth.set_groups` held groups by name (A21, High).** The request
   held `SetGroups(user_id, added, removed)` with the groups' names. The
   staff member's call, made again once the request was approved, found
   the request by the fingerprint of those names, and the admin API then
   joined whatever group had the name. Approved for "readers", the maker
   renamed readers away and admins to "readers", sent the call again,
   and the user joined admins; deleting readers and making a new
   "readers" did the same. Renaming and deleting a group ran no check.
2. **No hold covered a return (A21).** `auth.set_staff` held making a
   user staff, not `{"is_active": true}` for a deactivated staff member
   or superuser: one superuser brought back an account that holds
   everything, alone.
3. **A group's permissions went around the holds (A20).** `PUT
   /groups/{id}/permissions` grants every member, and no hold, and no
   check, saw it. A project's own ways to a grant (an invitation that
   makes staff in groups) are outside the admin API.
4. **The hold ran before the project's checks (A19).** Hooks ran in
   `INSTALLED_DOMAINS` order. Approvals' `check_admin_change` spends the
   maker's approved request when it runs; a check of a domain installed
   after approvals, or the admin API's own refusals after its hooks (a
   password the user had before), then refused a change whose approval
   was gone. The project registered a hook for `auth`, the first domain,
   that ran every other hook before approvals' own.
5. **Twins ran no project check (A17).** ADR 0163 kept the
   `on_admin_change` checks off the twins because approvals' check
   writes. So the phone confirmed a change the project refused at once.
6. **`select_routes` checks ran before the admin API's rules (A17).**
   The admin API took its staff rule as an endpoint parameter
   (`actor: Annotated[AnyUser, Depends(Require(...))]`). FastAPI solves a
   route's `dependencies=[...]` first, so a project's check added with
   `select_routes` ran for anyone, signed in or not.
7. **A reused password spent nothing (A17).** The act binds only that a
   password is set. A password among the user's last ones was refused at
   the change, after the authorization was checked and before it was
   spent: one confirmation tested guesses against the user's history,
   counted against no rate.

### What others do

- **OWASP's Transaction Authorization Cheat Sheet** [1] asks that what is
  authorized not change between authorization and execution (2.6, 2.8),
  and that the user see the significant data ("What You See Is What You
  Sign"). A name is not the data: what a group grants is.
- **NIST SP 800-53 AC-2** [2] asks that accounts be created, *enabled*,
  modified, disabled, and removed in line with the organization's
  criteria, and that creating them need approval; **AC-5** asks for
  separation of duties. Enabling an account again is a grant like
  creating it.
- **Ordered hooks.** Wagtail's `hooks.register(name, order=...)` [3]
  runs hooks by an integer, lowest first, `INSTALLED_APPS` order without
  one; core hooks are placed so an app goes before (`order=-1`) or after
  (`order=1`). pluggy's `tryfirst` and `trylast` [4] give two places
  only, and those within each keep registration order.
- **FastAPI** runs a dependency declared twice for a request once and
  caches its value [5]: a route that declares its rule among its
  `dependencies` and takes the user from the same `Depends` runs it once,
  first.
- **Throttling guesses.** OWASP's Authentication Cheat Sheet [6] asks that
  every way to test a password be limited; `AUTH.LOGIN_RATE` and
  `ACCOUNT_LOGIN_RATE` already bound a signed-in user's own password
  checks (`password_throttle`).

### Options

**What `auth.set_groups` binds.**

| Option | What it costs |
| --- | --- |
| Refuse renaming or deleting a group that a pending or approved request names (the project's `409 group_in_use`) | The group routes would read approvals' requests (auth would know approvals, ADR 0140), groups stay stuck while a request waits, and a group made again elsewhere still passes. |
| The groups' ids | A group granted more since the approval still passes. |
| **Each group's id and the permissions it grants, beside the names (`SetGroups.groups`, `HeldGroup`)** | Requests of 2.6 not yet made are approved again. The admin API hands the groups it read to the checks (`AdminChange.groups`). |

**How a changed group is refused.** The maker's call finds the approved
request by the fingerprint of the command it builds now. A group that
changed builds another command: no approved request holds it, so the
call submits a request of its own, which shows what the group grants
now, and the approved one is never spent. A refusal of its own, with a
problem code, would tell the maker no more than `approval_pending` with
the new request's id, and would still leave them that request to ask
for. No new code is needed.

**A return.**

| Option | What it costs |
| --- | --- |
| Hold every reactivation | Holds the return of every customer account that holds nothing. |
| **`auth.reactivate`, for a user who holds access once the change is made: staff, a superuser, a member of a group, or granted a permission, directly or on an object** | What a backend grants only an active user (a tenant member's keys) is not known here; the docs say to guard it. |

Without `auth.reactivate`, `auth.set_staff` holds a staff member's
return, as making staff: the return gives staff back. With both held, a
call that makes a user staff and active again is refused (`403
approval_required`): one request for both would spend one hold's
approvals, possibly fewer, on the other's change.

**A group's permissions.** `auth.set_group_permissions`, held when the
call grants a group a permission. Its parties are the group's members as
stored when the request is decided (`GroupRepository.member_ids`): each
gains what it grants. Taking away, renaming, deleting, and creating a
group (it has no members) are not held: none grants anyone more, now
that `auth.set_groups` binds ids. Grants made outside the admin API need
guards of the project's own: the docs say so.

**The order of the checks.**

| Option | What it costs |
| --- | --- |
| The admin API calls approvals' check last, by name | Auth would import approvals (ADR 0140 keeps them apart). |
| `first` and `last` flags (pluggy) | Two places; a project cannot put a check between approvals' and another `last`. |
| **An integer `order=` on any `HookPoint`, lowest first, 0 by default, then domain order (Wagtail); approvals registers at 100 (`HOLD_ORDER`)** | A number to document. `Hook` keeps its two fields: a tuple of three would break code that unpacks it. |

**The group routes' checks.**

| Option | What it costs |
| --- | --- |
| Call `on_admin_change` with new kinds and no user | A check written for a user's changes, typed for a user, would be given group changes, and one that reads `target is None` as "creating a user" would misread them: a change of behaviour in a minor release. |
| **A point of their own, `on_admin_group_change(actor, group, change)`, with a `GroupChange` (`group_create`, `group_rename`, `group_permissions`, `group_delete`)** | One more point to register a check on. |

**The twins.** They run the checks with `asking` true (`AdminChange`,
`GroupChange`), and approvals' hold submits and spends nothing then. A
check of the project's that writes should write nothing while `asking`;
the docs and the upgrade notes say so. The twin still does not compare a
password with the user's history: unconfirmed, it would answer each
guess.

**The order against `select_routes`.**

| Option | What it costs |
| --- | --- |
| `select_routes` puts its dependencies after the endpoint's parameters | Changes what ADR 0145 promised for every route of every router. |
| The docs saying so | The footgun stays on the admin API. |
| **The admin API declares each route's rule among its `dependencies`, and takes the staff member from the same `Depends`; the docs say what a parameter rule means elsewhere** | Twenty-five decorators name their rule twice. |

**A reused password.** Checked before the `on_admin_change` checks, with
the validators' care, so no approval is spent on a password that will
be refused (A19). Each check counts as a wrong password of the user's
(`password_throttle`), given back when the password is not reused; a
reuse spends the change's authorization in a transaction of its own,
committed before the refusal. One confirmation, one guess.

## Decision

- `SetGroups` holds `groups`: each group added, as `HeldGroup(id, name,
  permissions)`, sorted. The admin API gives the checks the groups it
  read (`AdminChange.groups`, `AddedGroup`). A change of groups that does
  not name each group it adds is refused (`ImproperlyConfigured`).
- `ADMIN_APPROVALS` takes `auth.reactivate` (`Reactivate`, policy
  `auth.reactivation`) and `auth.set_group_permissions`
  (`SetGroupPermissions`, policy `auth.group_permissions`).
- `HookPoint(...)(function, order=0)`: lowest first, then
  `INSTALLED_DOMAINS` order, then registration order. Approvals registers
  its checks at `HOLD_ORDER` (100).
- The group routes call `on_admin_group_change`. The twins call the
  checks with `asking`. Each admin route declares its rule among its
  `dependencies`.
- A reused password is refused before the checks, counts, and spends the
  authorization.

## Consequences

- **Security, and a change of behaviour**: an approval of `auth.set_groups`
  grants the groups it held. A request of 2.6 approved and not yet made
  holds no `groups`: the call submits a new one, which is approved again.
- The project removes its `409 group_in_use`, its reactivation guard, its
  guard of a group's permissions, its `auth` hook that ran the others
  first, its route dependencies in front of its checks, and its rules in
  front of `select_routes` checks.
- A check of the project's runs at twins too, with `asking` true.
- Approvers see more: each group's id and permissions. A long list is cut
  on a phone's screen as other arguments are (user report A23 is about
  that, separately).
- Not done here: a `Policy` whose count and approvers follow the settings
  as they change (A20's last proposal), and a taken address refused
  before the checks (A19): `refuse_taken_emails` runs with the commit.
- New names: `HookPoint`'s `order=`; `AddedGroup`, `GroupChange`,
  `on_admin_group_change` (`auth.gates`); `AdminChange.groups` and
  `.asking`; `REACTIVATE`, `SET_GROUP_PERMISSIONS`, `HOLD_ORDER`,
  `HeldGroup`, `Reactivate`, `SetGroupPermissions` (approvals);
  `GroupRepository.member_ids`; `UserRepository.set_password(...,
  reuse_checked=)`. No table changes.

## References

1. OWASP Cheat Sheet Series, *Transaction Authorization Cheat Sheet*:
   <https://cheatsheetseries.owasp.org/cheatsheets/Transaction_Authorization_Cheat_Sheet.html>
2. NIST SP 800-53 Rev. 5, AC-2 *Account Management* and AC-5
   *Separation of Duties*: <https://csrc.nist.gov/pubs/sp/800/53/r5/upd1/final>
3. Wagtail, *Hooks* (the `order` argument):
   <https://docs.wagtail.org/en/stable/reference/hooks.html>
4. pytest, *Writing hook functions* (`tryfirst`, `trylast`):
   <https://docs.pytest.org/en/stable/how-to/writing_hook_functions.html>
5. FastAPI, *Sub-dependencies* ("Using the same dependency multiple
   times"): <https://fastapi.tiangolo.com/tutorial/dependencies/sub-dependencies/>
6. OWASP Cheat Sheet Series, *Authentication Cheat Sheet*, "Protect
   Against Automated Attacks":
   <https://cheatsheetseries.owasp.org/cheatsheets/Authentication_Cheat_Sheet.html#protect-against-automated-attacks>
