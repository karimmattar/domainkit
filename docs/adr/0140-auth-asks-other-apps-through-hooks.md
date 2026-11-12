# 0140. Auth asks other apps through hooks

- Status: Accepted
- Date: 2026-11-11

## Context

Two pairs of apps imported each other, the audit of 2.2.1 found (A2):

| Pair | One way | The other way |
| --- | --- | --- |
| auth and audit | Audit's API is guarded by auth's rules, and its settings read auth's. | Auth wrote refused attempts to the trail: `throttling.audit_refusal` imported `audit.record`, inside the function. |
| auth and approvals | Approvals guards making superusers with auth's units of work and rules. | Auth's admin API asked approvals before it made a superuser: it imported `approvals.refuse_if_guarded`, inside the function. |

Each import sat inside a function, behind `registry.is_installed`, so no
import failed. The cycle was still there: neither app of a pair could be
read, tested or changed without the other, and auth, which every other
app builds on, knew two of them by name. A third app that wanted to hear
of refusals, or to hold superusers back, had no way in but a third
import in auth.

The framework has a way for an app to offer a point others hang on:
`HookPoint` (`on_stage`, `on_login_gate`, `on_manage_logins`). An app
registers in its `ready()`, and the offering app calls what is there.

## Decision

- **Auth imports no app that imports auth.** What it needs from one, it
  asks through a hook point it owns.
- `auth.integration.on_refusal`: hooks are told of each refused attempt,
  with its action and the reason's code, never what was tried.
  `audit_refusal` calls them. Audit registers one, when auth is
  installed, that writes the trail as before.
- `auth.gates.on_make_superuser`: checks run before the admin API
  creates a superuser or makes a user one; a check raises to refuse.
  Approvals registers one, when auth is installed, that asks
  `refuse_if_guarded(GRANT_SUPERUSER)` as before.
- A test fails when two contrib apps import each other, at any depth,
  inside functions too (`test_contrib_boundaries.py`). Its list of
  allowed pairs is empty.

## Consequences

- Nothing changes for a project: the same refusals reach the trail, and
  the admin API refuses a superuser under the same settings.
- Another app can record refusals (a SIEM feed) or hold superusers back
  without a change in auth.
- Two public names more in auth, with their types (`RefusalHook`,
  `SuperuserCheck`).
- The apps still depend one way: audit and approvals on auth. That is
  the direction the rest of the framework has.
