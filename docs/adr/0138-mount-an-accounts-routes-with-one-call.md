# 0138. Mount an account's routes with one call

- Status: Accepted
- Date: 2026-11-11

## Context

An audit of 2.2.1 counted what a phone-first project sets up by hand:

- **Eight routers**, each imported and mounted: logging in, sessions, the
  second factor, tokens, recovery, step-up, devices, sign-in approvals.
  A router left out fails late: a client gets `404`, or a check refuses
  to start (`ciba.E003`).
- **Four settings in three apps** that only work together:
  `CIBA.APPROVE_LOGINS`, `DEVICES.ENROLLMENT`, `DEVICES.DEVICE_LOGIN`,
  and `AUTH.CREDENTIAL_CHANGE_DEVICE_LOGINS`. Checks name what is missing
  (`ciba.E005`, `ciba.W001`), one start at a time.

Constraints:

- Auth must not import the apps built on it.
- A settings file is read before any app is loaded.
- Some routes must not appear by default: `auth.signup` lets anyone make
  an account, and a few routes refuse to start without apps or settings
  they need (`auth.E102`, `auth.E103`).

## Decision

### `account_router()` mounts what the installed apps name

`domainkit.contrib.auth.routing.account_router(include=(), exclude=())`
returns one router. Each app names its routes in `ready()`
(`on_account_routes`, an `AccountRoutes` with a name, the router's dotted
path, and whether it is mounted by default), so auth imports none of
them.

| Mounted by default | When |
| --- | --- |
| `login`, `mfa`, `tokens`, `step-up`, `sca` | always |
| `sessions` | `CookieAuthentication` is in `AUTH.AUTHENTICATION_CLASSES` |
| `recovery` | messaging and mail are installed |
| `devices`, `sign-in`, `identity`, `phones` | their app is installed |
| `signup`, `saml` | never: a project includes them |

A name no installed app has is refused, with the names there are. Staff's
routes (`auth.admin`) and an OpenID Provider's (`ciba.oidc`) keep their
own prefixes, and are mounted by hand.

### `phone_identity()` gives the settings that go together

`domainkit.contrib.presets.phone_identity(strict=False, auth=None,
devices=None, ciba=None)` returns `AUTH`, `DEVICES` and `CIBA`, with a
project's own keys winning. The module imports no app, so a settings file
may use it. It is a function, not a `PRESET` key like `AUTH.PRESET`: one
app's settings class cannot set another's.

## Consequences

- **A phone-first project mounts one router and calls one function.**
  Mounting each router by hand still works.
- **Public signup stays a decision:** `include=["signup"]`.
- **An app built on auth adds its routes** with `on_account_routes`.
- **The defaults follow what is installed and configured,** so a router
  appears when its app is installed. A project that wants a fixed set
  names it with `exclude`, or mounts by hand.
