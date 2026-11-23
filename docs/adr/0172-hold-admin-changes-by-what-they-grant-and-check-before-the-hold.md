# 0172. Hold admin changes by what they grant, and check before the hold

- Status: Accepted; amended by [0179](0179-bind-the-admin-acts-groups-by-id-and-keep-a-held-confirmation-to-its-request.md); amended 2026-11-23 (at the end)
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
| **Each group's id, the permissions it grants, and its grants on single objects (how many, and a SHA-256 of them, sorted), beside the names (`SetGroups.groups`, `HeldGroup`)** | Requests of 2.6 not yet made are approved again. The admin API hands the groups it read to the checks (`AdminChange.groups`), with what every backend's `object_grants` says of each: approvals reads no object permissions app's tables, and stays within the apps' public names. An approver sees a count of the grants, not which. |

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

`Reactivate` holds what the user comes back with, as the change leaves
it: the flags, each group (`HeldGroup`), their own permissions, and their
grants on objects (count and digest). Holding the user alone let the
maker, while the user waited inactive, make them a superuser or add a
group no hold covered, and the approval of a reader's return brought
back more (found in review).

Without `auth.reactivate`, `auth.set_staff` holds a staff member's
return, as making staff: the return gives staff back. With both held, a
call that makes a user staff and active again is refused (`403
approval_required`): one request for both would spend one hold's
approvals, possibly fewer, on the other's change. A superuser's return
is a superuser made: with `SUPERUSER_APPROVALS`, the admin API refuses
it (`approval_required`) as it refuses making one, whatever
`ADMIN_APPROVALS` holds; the superuser is demoted while inactive, made
active, and promoted by an approved `auth.grant_superuser`.

**A group's permissions.** `auth.set_group_permissions`, held when the
call grants a group a permission. Its parties are the group's members as
stored when the request is decided (`GroupRepository.member_ids`): each
gains what it grants. Taking away, renaming, deleting, and creating a
group (it has no members) are not held: none grants anyone more, now
that `auth.set_groups` binds ids. Alone it is bypassed: a group made
with the permissions, then joined, grants them. So it is to be held with
`auth.set_groups`, and `check` warns where it is not (`approvals.W001`,
in staging and production: a minor release may add a warning). Grants
made outside the admin API need guards of the project's own: the docs
say so.

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

**A reused password.** Where the history is compared decides what it
tells.

| Option | What it costs |
| --- | --- |
| After every check, at the change (2.6) | An approval is spent first, then the change is refused (A19). |
| Before the checks, with the validators (the first form of this record) | An oracle: while a held change waited, or a project's check refused, each guess was answered, and only a hit cost anything (found in review). |
| When the change is about to be made, the hold first looking for an approved request, then comparing, then spending (the second form) | A race: calls sent at once on one approval each found it, each compared, and one spent it; one approval answered as many guesses as calls (found in review). |
| **When the change is about to be made: after the checks; where a hold applies, under the approved request's lock, by the call that claims it, before it is marked executed (`AdminChange.last_check`, run once)** | A field more on `AdminChange`. The comparison reads only, so it waits on no write under the lock. |

While a check refuses the change or its approval waits, nothing is
compared: every password gets the same answer. A call that loses the
claim compares nothing, and answers `approval_pending`.

**The authorization of a password compared.** With `AUTH.ADMIN_ACTION`,
calls sent at once carry one confirmation.

| Option | What it costs |
| --- | --- |
| Spent with the change, or after a reuse is refused (the third form) | A hit spent it once the approval's lock was let go: the next call compared under it, spent the approval, and then could not spend the authorization: the approval was gone and nothing changed (found in review). |
| Spent under the approval's lock, with the approval | On SQLite the lock is the database's write lock: the authorization's write, on auth's connection, would wait on it for good. |
| **Spent first, before the checks, in a transaction of its own, wherever the history may be compared** | A call that then waits for approval, or that a check refuses, has spent its confirmation: one confirmation, one try, whatever its answer. |

So the call that holds the confirmation is the only one that compares,
and the only one that may spend an approval; the others answer
`action_not_authorized` before anything is compared, whatever their
passwords. The approval is marked executed only by a call that goes on
to make the change.

`last_check` is awaited by approvals' hold, or by the admin API after
the checks. A check of the project's that awaited it before the hold
would answer a reuse (`422`) where another password gets
`approval_pending`: the docs require that such a check run after every
check that may refuse, and only once the change is sure. A code guard
would need each hook's order, and still could not tell which later
check may refuse. A check ordered after the hold sees the approval
spent: it should not refuse a password change, or its refusal and the
hold's answer a reuse differently.

A reuse counts against a key of the staff member and the user
together, at `AUTH.LOGIN_RATE` and `ACCOUNT_LOGIN_RATE`, taken before
anything is checked and given back unless the password was found: the
user's own counts of wrong passwords (`password_throttle`, which the
first form used) are apart, so a hijacked session's wrong checks never
hold back a staff reset, and a staff member's reuses never refuse the
user's login. Past the rate, that staff member's password changes of
that user wait (`429`), a new password too: a hit answered past the
rate would still be an answer.

A grant on an object is bound with when it ends
(`ObjectGrant.expires_at`, not compared): a grant renewed to a later
date is another. Roles come as their permissions' grants. A grant held
twice (directly and through a group, or from two backends) is kept
once, with its latest end (`ObjectGrant.lasting`, in `object_grants`),
so its digest does not depend on the order its copies came in.

**What the approver reads.** ADR 0173 gave a guard a `show`. The holds
of this record show what they grant in words, never their arguments as
JSON: `auth.set_groups` the user, then each group added in a few words,
as many on a line as fit ("readers (…9abc) 3 perms + 2 object grants":
its name read now, the end of its id, and what it grants); `auth.reactivate`
the user, the flags they come back with, their groups by name, and how
many permissions and grants on objects they hold; `auth.set_group_permissions`
the group by its name now (the name held, if it is gone), and the keys.
A group's item shows four characters of its id, where a user's or a
group's own line shows twelve (`named`): long enough to tell two groups
apart at a glance, short enough that two share a line, so a request of
six groups shows them all. A name too long for its line is cut, measured
as the screen lays it out: with `WHOLE`, what is refused is a grant too
large to show, never a name. A user who comes back in more groups than
a screen shows comes back in steps: some groups taken away, the return,
then the groups added again, each held. `on_admin_group_screen` lets
approvals refuse, at a stored group's twin, a held grant its approver
could not see whole, as `on_admin_screen` does for a user's change.

## Decision

- `SetGroups` holds `groups`: each group added, as `HeldGroup(id, name,
  permissions, object_grants, object_grants_digest)`, sorted. The admin API gives the checks the groups it
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
- A reused password is compared when the change is about to be made
  (`last_check`), refused, counted for the staff member and the user,
  and leaves the approval; with `ADMIN_ACTION`, the call spends its
  authorization before anything is checked.
- The holds show what they grant in words (`Guard.show`), and
  `on_admin_group_screen` checks a group's twin.
- `Reactivate` holds what the user comes back with; a superuser's return
  asks `on_make_superuser`; `approvals.W001` warns of
  `auth.set_group_permissions` held without `auth.set_groups`.

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
- A staff member who sends many of one user's old passwords waits
  (`429`) before setting that user's password again.
- `order=` places a hook among all domains' hooks where the point runs
  them one after another; the lifespan and `/ready` run each domain's
  hooks apart, and there it sorts a domain's own.
- New names: `HookPoint`'s `order=`; `AddedGroup`, `GroupChange`,
  `on_admin_group_change`, `on_admin_group_screen` (`auth.gates`);
  `ObjectGrant.expires_at` and `ObjectGrant.lasting`; `AdminChange.groups`,
  `.asking`, and `.last_check`; `approvals.W001`; `REACTIVATE`,
  `SET_GROUP_PERMISSIONS`, `HOLD_ORDER`,
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

## Amendment (2026-11-23): making staff holds what the user comes back with

A project's notes on 2.9 (user report A26, and row 2 of A17) found that
this record gave a return its holds and left making staff without them.

- **Making staff gives back what the user holds.** The admin API and a
  project's back office ask for `is_staff`, as Django's admin site does
  [7]. Taking staff away waits for no one and leaves the user's groups,
  permissions, and superuser flag as they were. `SetStaff` held the user
  and the values set beside, and its screen showed those; `Reactivate`
  holds and shows what the user comes back with.
- **The act's screen named the flag the change sets.** The admin API's
  act of making staff showed "Staff: yes", and of a return "Active:
  yes". Where approvals hold nothing, the staff member's phone is the
  only screen of the change.

What others do: OWASP's Transaction Authorization Cheat Sheet [1] asks
that the user see the significant data ("What You See Is What You
Sign"), and that it not change between authorization and execution;
NIST SP 800-53 AC-2 [2] asks that enabling and modifying an account
follow approval.

| Option | What it costs |
| --- | --- |
| Refuse making staff a user who holds a named key or is a superuser (the project's `422 granted_by_name`) | Policy, not display: a project that makes such users staff may not. Grants on objects and other keys still pass unseen. |
| Show what the user holds, read when the screen is built, and hold nothing more | The approval binds the user alone: what they hold when the call comes back is not what the approver read. |
| Take groups and permissions away with staff | Changes what unmaking staff does, in a minor release; a user may keep groups for what is not staff's. |
| **`SetStaff` holds what the user comes back with, as `Reactivate` does (the superuser flag as the change leaves it, each group as `HeldGroup`, their own permissions, their grants on objects: count and digest), and shows it as `show_reactivation` does** | The arguments change: a request stored by 2.9 is approved again (below). Under `WHOLE`, a user in many groups made staff no longer fits one screen, and is made staff in steps, as a return is. |

How a change is refused is this record's: the maker's call builds the
command from what the user holds now, and finds no approved request
with its fingerprint when anything changed; it submits a request of its
own (`403 approval_pending`), and the approved one is never spent. With
`HELD_TTL`, a confirmation kept for the first request refuses the call
(`403 action_not_authorized`, ADR 0179).

**Requests stored by 2.9.** They hold the user and the values alone.

| Option | What it costs |
| --- | --- |
| Leave the new fields out of the arguments when empty, as 2.8 left `values` | A 2.9 request of a user who holds nothing is still spent; one of a user who holds anything is not. Safe, but the shape of a request then depends on what the user holds, and an approver of 2.9 approved it on a screen that said nothing of what they held. |
| A migration that rewrites stored requests | It would write what no approver read, under a seal made again. |
| **Always hold the new fields: no request of 2.9 is ever found, and the call submits a new one, which is approved again** | One more approval for each request in flight at the upgrade. As 2.7 did for `SetGroups` of 2.6. |

The fields have defaults, so a request of 2.9 still loads: it shows,
and is rejected, cancelled, or expires as before; approved, it is never
spent.

**The act's screen.** A change of the admin API that makes a user staff,
or active again, shows the flags they come back with ("Superuser",
"Staff", set by the change or not) and the keys `NAMED_PERMISSIONS`
names that they hold, directly or through a group ("Grants"), read from
the stored user when the twin is asked. The act binds the change, not
what the user holds: its digest is unchanged, so no authorization asked
under 2.9 is refused. Binding the holds too would refuse those, for a
window of one authorization's life; where approvals hold the change,
`SetStaff` and `Reactivate` bind them.

**A reading for a project's own grants.** `named_keys_held(user)`
(`domainkit.contrib.approvals`, beside `held_groups`): the keys
`NAMED_PERMISSIONS` names that a user holds, directly or through a
group, active or not, sorted. A superuser's flag is read apart, as the
screens show it apart. The act's screen reads the same in auth: auth
imports nothing of approvals (ADR 0140).

Consequences: **security, a change of behaviour in a minor release**: a
request to make staff stored by 2.9 is approved again; approvers and
staff read more lines; the project removes its `granted_by_name` hook.
New names: `SetStaff.is_superuser`, `.groups`, `.permissions`,
`.object_grants`, `.object_grants_digest`; `named_keys_held`. No table
changes.

Reference added:

7. Django, *django.contrib.auth*, "is_staff: Allows this user to access
   the admin site":
   <https://docs.djangoproject.com/en/5.2/ref/contrib/auth/#django.contrib.auth.models.User.is_staff>
