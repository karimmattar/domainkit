# 0154. Hold admin changes for approval, and show requests to those they concern

- Status: Accepted; amended 2026-11-16 (at the end); amended by [0163](0163-authorize-admin-changes-and-requests-steps-as-acts.md)
- Date: 2026-11-16
- Amends: [0121](0121-hold-guarded-commands-as-sealed-requests-and-count-approvals-under-the-requests-lock.md)

## Context

A project that builds on the framework reported gaps in maker-checker
(user report A9, and the approvals notes of P3). 2.4 closed some of them
already: `on_admin_change(actor, target, change)` lets a project refuse a
change of auth's admin API, `select_routes` adds dependencies to named
routes, and `403 approval_pending` names the approval request. Three
things stayed open.

1. **Risky admin acts cannot wait for a second person.** Only making a
   superuser is guarded (`SUPERUSER_APPROVALS`, through
   `on_make_superuser`). The report asked for guarded actions
   `auth.set_staff`, `auth.set_groups`, `auth.set_password` and
   `auth.impersonate`. `on_admin_change` can only refuse, and its
   `AdminChange` names the fields a change sets, not their values: a
   check cannot tell making staff from unmaking them.
2. **Every viewer reads every request.** `GET /requests` shows each
   request's command to anyone with `approvals.view_approvalrequest`:
   amounts and accounts, and, with admin changes, the user staff act
   for and why. `Guard.parties` is a plain function of the command: it
   cannot look anything up. Nobody tells the approvers that a request
   waits, though `RequestSubmitted` is recorded.
3. **No staff rule or step-up on submit, reject and cancel.** The
   project mounted the router through a helper of its own to add them.

### What others do

- **Standards.** PCI DSS v4.0.1 requirement 8.2.4 asks that "addition,
  deletion, and modification of user IDs, authentication factors, and
  other identifier objects" be "authorized with the appropriate approval"
  and "implemented with only the privileges specified on the documented
  approval" [1]. NIST SP 800-53 separates duties (AC-5) and has account
  changes approved by named roles (AC-2) [2]. SOX audits of IT general
  controls test that access to financial systems is granted only after a
  documented approval, by someone other than the requester [3].
- **Two ways to carry out an approved change.** Okta Access Requests
  and AWS's TEAM for IAM Identity Center apply the change themselves once
  approved: Okta assigns the group, TEAM creates the permission set
  assignment [4, 5]. Teleport's access requests leave it to the
  requester: once approved, they log in again with the request's id
  (`tsh login --request-id=...`) and get the roles then [6]. Where the
  result goes to the requester (a certificate, a session), the requester
  claims it.
- **Who sees pending requests.** Microsoft Entra PIM lists an approver's
  requests at "Approve requests", filtered to the current user as
  approver (`filterByCurrentUser(on='approver')`); administrators who are
  not approvers can still view every pending request [7]. TEAM's
  approvers "see all of the requests that are pending your approval" [5];
  Okta's approvers work from an inbox of the tasks assigned to them [4].
- **Notifying approvers.** PIM emails each approver when a request
  waits, with a link to it [7]. TEAM notifies the approver group [5].
  Okta sends the request's summary to the task's assignees [8].

### Options

**How an approved admin change is carried out.**

| Option | What it costs |
| --- | --- |
| Refuse the change (`approval_required`), and have staff submit a command at `POST /requests` that the worker runs, as `auth.grant_superuser` does | The worker cannot hand an impersonation's token to anyone, and a password would sit in the request for good. Each change needs a service in approvals that re-implements the admin route and its bounds. |
| The worker for staff, groups and permissions; something else for passwords and impersonation | Two ways for one feature, and the admin logic still copied into approvals for three of them. |
| **The admin API submits the change and answers `approval_pending`; once approved, the same call by the same staff member makes it (Teleport's way)** | Staff send the call twice. In return the change is made by the admin route itself, with every check of its own as of then, in the staff member's live session; nothing of the admin API is copied; tokens and passwords never pass through the request. |

**What binds the approval to the change.** The request holds the change,
not the call: the user, and for groups and permissions what the call
adds and takes away, sorted. The call made again finds the approved
request by its maker and the fingerprint of that change. A password is
left out: approvers could not judge it, and any derivative of it kept in
a record that lives for good (a hash, an HMAC) can be attacked offline
once the key leaks. The request approves setting a password; the
validators check the one set.

**Who reads which requests.**

| Option | What it costs |
| --- | --- |
| Keep showing every request, with a setting to narrow it | Projects that do not read the release notes keep the leak. |
| Hide the arguments from viewers who are not approvers | The request still shows; the fingerprint can no longer be worked out from what is shown. |
| **Show the requests one made and those one may decide; a new permission shows all** | Viewers who are neither lose sight of the others' requests: a narrowing a project may notice. Approvers and makers see what they did. |

**Telling approvers.**

| Option | What it costs |
| --- | --- |
| Send mail from approvals | Templates, addresses, languages and channels that are the project's. |
| A recipe alone | Every project writes "who may approve this" itself, and gets it wrong in the same places (the maker, the parties, superusers). |
| **`approvers_of(request)`, and a recipe: a durable handler of `RequestSubmitted` that mails them** | Users found through auth's tables: holders through another backend are not found. |

**Rules on submit, reject and cancel.** A setting per route
(`SUBMIT_RULE`, `REJECT_RULE`, ...) would repeat what `select_routes`
does for every framework router, for one router. The rules are the
project's (staff, a step-up, an IP range): a tested recipe with
`select_routes` covers them, and a step-up stays off rejecting.

## Decision

### Admin changes held for approval

- `APPROVALS.ADMIN_APPROVALS` maps each of `auth.set_staff`,
  `auth.set_groups`, `auth.set_permissions`, `auth.set_password` and
  `auth.impersonate` to the approvals it needs (1 to 10). Empty, the
  default, holds nothing. `APPROVALS.ADMIN_APPROVERS` names the
  permission approvers hold; unset, superusers approve, as
  `SUPERUSER_APPROVERS` does. The setting needs `domainkit.contrib.auth`.
- Approvals registers a guard for each action named, and a check with
  auth's `on_admin_change`. The check maps the change to a command
  (`SetStaff`, `SetGroups`, `SetPermissions`, `SetPassword`,
  `Impersonate`); making staff, adding a group or a permission, setting
  a password and impersonating are held, and changes that only take
  away are not. Creating a user who is staff is refused while making
  staff is held. Staff acting as someone else are refused.
- The guards are run **by their maker** (`Guard(by_maker=True)`, a
  general option): a direct call of the guarded service submits the
  command as the caller's and raises `ApprovalPending`, or raises it
  with the id of the caller's request for that command that waits
  already. Approving queues nothing. The same call by the same maker,
  once the request is approved, spends it (checked as the worker checks
  a run, marked executed) and runs the service as the approved request.
  For the admin guards the service does nothing: the admin route goes on
  and makes the change. The guards are not submitted at `POST /requests`
  (`http=False`), and the user a change is about is a party who does not
  decide it.
- The admin API runs its own checks (bounds, fields, password
  validators) before `on_admin_change`, so nothing is submitted that the
  route would refuse, and the approval is not spent on such a call.
  `AdminChange.values` gives the values a change sets (`is_staff` true or
  false, an impersonation's reason; never a password).

### Who reads which requests

- `GET /requests` and `GET /requests/{id}` show a caller the requests
  they made and those of a policy that names them among its approvers
  (its permission, its group, or its approver rule asked about the
  principal). Others are `404`, as out of scope. The new model permission
  `approvals.view_every_approvalrequest` shows every request. Approving,
  rejecting and cancelling keep their own rules, unchanged.

### Parties and approvers

- `Guard.parties` may be a coroutine function, awaited at each decision
  and at the run (`Guard.aparties_of`); `parties_of` stays for plain
  functions and refuses a coroutine one. With `maker_fields`, the command
  carries its maker, so a parties function sees who acts too.
- `approvers_of(request)` gives the active users who may decide a
  request now: holders of its permission (granted directly or through a
  group, then `has_perm`), its group's members, or the superusers and
  staff its approver rule lets through; never the maker, a party, or one
  who decided. The docs give the durable handler that mails them.

### Rules of the project's own

- No setting. The docs give the `select_routes` recipe, tested, that asks
  staff and a step-up on submit and cancel, and staff on reject.

## Consequences

- Staff send a held change twice: the first call answers
  `approval_pending` with the request's id, the second, once approved,
  makes the change. A client polls `GET /requests/{id}`, or is told by
  the project. An approval not used before the request expires is never
  used.
- The approval is spent before the admin route makes the change. A call
  that fails after that (a database error, a field that would grant more
  through another backend) needs a new approval.
- A change that differs from the approved one, because the user's groups
  changed meanwhile, submits a new request rather than apply part of one.
- A password set after approval is not the one the first call sent: the
  approval is of setting one.
- Viewers who hold only `approvals.view_approvalrequest` and are neither
  maker nor approver no longer read others' requests. Upgrading projects
  give `approvals.view_every_approvalrequest` to auditors
  ([upgrading.md](../upgrading.md#23-to-24)); `migrate` creates it.
- A request whose policy changed its permission in a deploy is shown to
  the holders of the permission its policy names now.
- `approvers_of` finds what auth stores; a backend of the project's own
  that grants permissions is not asked for its holders.

## References

1. PCI DSS v4.0.1, requirement 8.2.4, as Microsoft maps it:
   <https://learn.microsoft.com/en-us/entra/standards/pci-requirement-8>
2. NIST SP 800-53 Rev. 5, AC-2 Account Management and AC-5 Separation
   of Duties: <https://csrc.nist.gov/pubs/sp/800/53/r5/upd1/final>;
   AC-5 summarized: <https://www.upguard.com/compliance/nist-sp-800-53/ac/ac-5>
3. Secureframe, "SOX ITGCs": <https://secureframe.com/blog/sox-itgc>
4. Okta, manage access request tasks:
   <https://help.okta.com/en-us/content/topics/identity-governance/access-requests/manage-tasks.htm>
5. AWS, temporary elevated access management with IAM Identity Center:
   <https://aws.amazon.com/blogs/security/temporary-elevated-access-management-with-iam-identity-center/>;
   TEAM's approver guide:
   <https://aws-samples.github.io/iam-identity-center-team/docs/guides/approver.html>
6. Teleport, role access requests:
   <https://goteleport.com/docs/identity-governance/access-requests/role-requests/>
7. Microsoft Entra PIM, approve or deny requests for Microsoft Entra
   roles:
   <https://learn.microsoft.com/en-us/entra/id-governance/privileged-identity-management/pim-approval-workflow>
8. Okta, access request notifications:
   <https://help.okta.com/oie/en-us/content/topics/identity-governance/notifications.htm>

## Amendment (2026-11-16): submit, reject, and cancel as acts

[ADR 0163](0163-authorize-admin-changes-and-requests-steps-as-acts.md)
gives submitting, rejecting, and cancelling built-in actions of their
own, each opt-in by a setting (`SUBMIT_ACTION`, `REJECT_ACTION`,
`CANCEL_ACTION`), as approving has. The rules of the project's own stay
a `select_routes` recipe; rejecting asks for an authorization only where
`REJECT_ACTION` is on. With `AUTH.ADMIN_ACTION` on, a held change is
checked against its authorization before it is submitted for approval,
and spends it only once the change is made.
