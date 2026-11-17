# 0179. Bind the admin act's groups by id, and keep a held confirmation to its request

- Status: Accepted
- Date: 2026-11-17
- Amends: [0172](0172-hold-admin-changes-by-what-they-grant-and-check-before-the-hold.md),
  [0174](0174-bind-an-act-to-the-record-it-decides-on-and-keep-a-held-acts-authorization.md)

## Context

A project's back office confirms each change of the admin API on the
phone (`AUTH.ADMIN_ACTION`, ADR 0163), holds its grants for a second
person (`APPROVALS.ADMIN_APPROVALS`, ADR 0172), and keeps a confirmation
while its change waits (`AUTHORIZATIONS.HELD_TTL`, ADR 0174). Its notes
on 2.7 (user report A17, rows 1, 4, 5 and 6) found four gaps.

1. **The act bound group names (row 4, proven by the project's audit).**
   ADR 0172 made approvals' `SetGroups` hold each group's id and grants,
   but the act the staff member confirms, `AdminAct`, kept `added` and
   `removed` as names. The maker asked to add a user to "readers" and
   confirmed "Adds: readers"; the change waited, and `HELD_TTL` kept the
   confirmation. "readers" was renamed and another group took the name.
   The same call, with the same confirmation, built another `SetGroups`
   (another id) and submitted a request for it. Once that was approved,
   the confirmation kept for the first request, which still waited, made
   the change: the act's digest was the same, and a held authorization
   lived while its own request waited, whatever request its call then
   spent. ADR 0174 had said the new request "needs a confirmation of its
   own once approved"; it held only when the first request was over.
2. **A held grant held no fields (row 5).** `auth.set_staff` and
   `auth.reactivate` held what the change grants. A `PATCH` that also set
   an address or a number was approved without it, and once approved,
   the call made with any other fields spent the approval. And
   `update_user` found an address or a number taken only at its commit,
   after approvals' hold had spent the approval: the change was refused,
   the approval gone, and its approver had never seen the value.
3. **Counts hid the key that mattered (row 6).** "readers (…9abc) 3
   perms" and "Also holds: 3 permissions" do not say which. A project
   that gives `TENANCY.ALL_TENANTS_PERMISSION` (ADR 0177) through a group
   had its approvers approve that key unseen; the project refused the key
   in a group altogether.
4. **Staff reuses were counted apart (row 1).** ADR 0172 counts a staff
   member's reuses of a user's old passwords per staff member and user,
   apart from the user's own counts, so that neither holds the other
   back. Two staff members acting together each have a count of their
   own at one user's history. The project had counted them with the
   user's own before 2.7.

### What others do

- **OWASP's Transaction Authorization Cheat Sheet** [1] asks that the
  significant data the user authorizes be the data executed, generated
  and stored on the server, with no way for the client to change it
  between authorization and execution. A group's name is a label; its id
  and what it grants are the data.
- **Microsoft Entra PIM** [2] approves a request by the identifier of
  the assignment request, which names the role and the principal by
  object id, never by display name: a renamed group is the same group,
  and a new group with the old name is another.
- **Django** validates uniqueness before saving: `full_clean()` runs
  `validate_unique()` after the fields' checks, and a `ModelForm`'s
  `is_valid()` runs it before `save()` [3], so a form refuses a taken
  value before anything is written. The database's constraint stays,
  for what changes in between.
- **NIST SP 800-63B-4** [4] asks that a verifier limit the failed
  attempts that can be made on the subscriber account; OWASP's
  Authentication Cheat Sheet [5] asks that every way to test a password
  be limited. A count per guesser bounds one guesser, not the account.

### Options: what the act binds

| Option | What it costs |
| --- | --- |
| Refuse renaming or deleting a group that a pending request names (the project's `409 group_in_use`) | Auth would read approvals' requests (ADR 0140), and a group made again elsewhere would still pass, as ADR 0172 found. |
| **The act binds each group added as `SetGroups` holds it (id, name, keys, grants on objects: count and digest) and each group taken away by id (`AdminAct.groups`, `.taken`)** | The digest of a change of groups changes: one asked under 2.7 is refused once. Other acts must not change: the two fields are left out of the arguments when empty. |

### Options: a held confirmation and another request

| Option | What it costs |
| --- | --- |
| Void the authorization when its call submits another request (the first form of this record) | A race, found while building this change: calls sent at once on one approved request, one claiming it, the others finding it carried out and submitting a copy; the copies voided the authorization before the claiming call spent it, and the approval was spent for nothing. Asking whether the first request still waits narrows it to a state that changes between two calls sent at once, across two databases. |
| Refuse it only when it would spend another request | The call still submits a request it can never make, and the maker learns it only once that is approved. |
| **The holding app refuses the call before it spends, waits on, or submits any request but the one the authorization is kept for (`refuse_held_elsewhere`, `403 action_not_authorized`, the trail's `held_elsewhere`)** | One more call for an app that holds acts. Nothing is written, so no call made at once can lose its spend to it; the authorization still serves its own request. |

### Options: fields sent with a held grant

| Option | What it costs |
| --- | --- |
| Refuse fields with a held grant (`403 approval_required`, the project's `grant_alone`) | A 2.7 client that sends a return and a field together is refused where it waited for approval: it breaks 2.7 callers. |
| **Hold them: `SetStaff.values` and `Reactivate.values`, every field and flag the call sets beside the grant, as JSON, shown by label ("Mobile number") and bound by the fingerprint** | The approver judges more; under `WHOLE`, more is refused at the twin and made in steps. Left out of the arguments when empty, so a pending request of 2.7 is found and spent by the same call that sets nothing else. |

Holding is the safer of the two that keep 2.7's callers: nothing an
approver did not see is made, and nothing that waited before is refused.

### Options: naming keys

| Option | What it costs |
| --- | --- |
| Refuse the key in a group and with a return (the project's `422 granted_by_name`) | Policy, not display: a project that grants it by a group may not. |
| A setting in `APPROVALS` (the project's proposal) | The admin API's own act, auth's, would not read it: auth imports nothing of approvals (ADR 0140). |
| **`AUTH.AUTHORIZATIONS.NAMED_PERMISSIONS`, the keys a screen names wherever it counts, by default `TENANCY.ALL_TENANTS_PERMISSION` where set; shown on a line of their own ("Grants") before the groups, and first in a list of keys** | A line more under `WHOLE`: a change that filled the screen is refused, and made in steps. Approvals reads it through `auth.integration`. |

The line comes before the groups, so that a screen cut and counted keeps
it; in a list, a named key first is never among those counted.

### Options: counting staff reuses

A setting, off: on, a reuse also counts against the user's own counts of
wrong passwords (`password_limits`, which `password_throttle` takes).
Off is 2.7's guarantee: a hijacked session's wrong checks never hold
back a staff reset, and staff reuses never refuse the user's checks. A
project that counts every guess at the account turns it on, and accepts
both.

## Decision

- `AdminAct` binds `groups` (`ActGroup`: id, name, keys, grants on
  objects) and `taken` (ids), left out of its arguments when empty. Its
  screen shows a group added as approvals does ("auditors (…9abc) 3
  perms"). Auth's `grants_digest` is the one digest of grants on
  objects, which approvals' `HeldGroup` takes too.
- `refuse_held_elsewhere(held_for)` (`auth.actions`): an authorization
  the act took that is kept for something else refuses the call.
  Approvals' `run_by_maker` asks it before it spends or waits on a
  request, and before it submits one.
- `SetStaff` and `Reactivate` hold `values`. `update_user` and
  `create_user`, and their twins, refuse a username, an address that
  identifies users, or a value of any unique column another user has,
  before the `on_admin_change` checks (`_not_taken`); the commit still
  refuses one taken in between.
- `NAMED_PERMISSIONS` names keys on approvals' screens of
  `auth.set_groups`, `auth.reactivate`, `auth.set_group_permissions`,
  `auth.set_permissions`, and on the admin API's act.
- `held_groups(names)` (`domainkit.contrib.approvals`): a project's own
  grant holds the groups it names as `SetGroups` does, and compares them
  when it is made.
- `AUTH.STAFF_REUSES_COUNT_AS_PASSWORD_CHECKS`, off.

## Consequences

- **Security, a change of behaviour in a minor release**: an
  authorization of a change of groups asked under 2.7 is refused once; a
  call made again on a held authorization whose command changed is
  refused where it submitted a request; a `PATCH` that sets fields with
  a held grant waits for an approval that holds them; a taken value is
  refused before a project's checks. The upgrading notes say so.
- The project removes its `409 group_in_use`, `grant_alone`, and
  `granted_by_name`, and its own count of reuses.
- With `ALL_TENANTS_PERMISSION` set, screens that grant it gain a line.
- The calls that lose a race on one approved request now answer
  `action_not_authorized`, where they submitted copies of the request.
- New names: `AUTHORIZATIONS.NAMED_PERMISSIONS`,
  `STAFF_REUSES_COUNT_AS_PASSWORD_CHECKS`, `held_groups`,
  `refuse_held_elsewhere`, and in `auth.integration` `group_item`,
  `list_item`, `field_label`, `named_permissions`, `named_first`,
  `grants_digest`; `SetStaff.values`, `Reactivate.values`. No table
  changes.

## References

1. OWASP Cheat Sheet Series, *Transaction Authorization Cheat Sheet*:
   <https://cheatsheetseries.owasp.org/cheatsheets/Transaction_Authorization_Cheat_Sheet.html>
2. Microsoft Graph, *Get approval* (PIM for groups and roles: approvals
   by the identifier of the assignment request):
   <https://learn.microsoft.com/en-us/graph/api/approval-get?view=graph-rest-beta>
3. Django, *Model instance reference*, "Validating objects":
   <https://docs.djangoproject.com/en/5.2/ref/models/instances/#validating-objects>
4. NIST SP 800-63B-4, *Authenticators*, 3.2.2 Rate limiting:
   <https://pages.nist.gov/800-63-4/sp800-63b/authenticators/>
5. OWASP Cheat Sheet Series, *Authentication Cheat Sheet*, "Protect
   Against Automated Attacks":
   <https://cheatsheetseries.owasp.org/cheatsheets/Authentication_Cheat_Sheet.html#protect-against-automated-attacks>
