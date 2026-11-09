# 0131. Choose what a password change does to the devices' logins

- Status: Accepted
- Date: 2026-11-09

[ADR 0132](0132-manage-the-second-factors-from-the-phone.md) renames the
setting `AUTH.CREDENTIAL_CHANGE_DEVICE_LOGINS`, and widens it to turning
the second factor on, before any release carried this name.

## Context

A password change (`POST /auth/password`) ends every other login of the
user, and a reset by code ends all of them. In the phone-as-identity modes
([ADR 0128](0128-approve-sign-ins-from-the-users-mobile-app.md),
[ADR 0130](0130-show-where-each-login-came-from-and-manage-logins-from-the-phone.md)),
that includes the phone's login. A stolen web session whose thief knows
the password, or anyone who can read the user's mail, can then sign the
phone out: the one place the user sees and ends logins.

Others do this differently:

- **Google** signs the user out everywhere on a change or a reset, except
  the devices the user verifies sign-ins with.
- **OWASP ASVS 4.0.3, 3.3.3** asks that the user be given the option to
  end all other sessions after a change, a reset included.
- **Keycloak** asks with a "Sign out from other devices" checkbox,
  unchecked by default since 26.3; Clerk and Facebook ask too.

Two more gaps showed while looking:

1. **A phone's login is not always marked as the phone's.** A device's
   own login records its `device_id` (ADR 0130), but the app's password
   login, signed by the device, and the login the app bound its key with,
   do not.
2. **Any login of the user unbinds a device** (`DELETE /devices/{id}`),
   and its logins stay. A stolen web session could unbind the phone.

Constraints:

- **Boundaries:** auth must not import devices or ciba.
- **The project owner's decision:** a setting of three values: keep the
  bound devices' logged in, log them out too, or let the request say.

## Decision

### A setting decides, or lets the request decide

`AUTH.PASSWORD_CHANGE_DEVICE_LOGINS`:

| Value | A password change or reset |
| --- | --- |
| `"end"` (default) | ends every other login, the devices' too, as before. |
| `"keep"` | ends every other login but the bound devices'. |
| `"ask"` | does as the request's `keep_device_logins` says: `true` (the default) keeps them, `false` ends them. |

A device's login is a token row with a `device_id`. The other logins, and
the personal tokens, end as before in every mode: ending them is what a
password change is for.

It holds for the user's own change, `POST /auth/password`, and the reset
by code, `POST /auth/password-reset/code`. Staff setting a password, the
`changepassword` command, and a password changed at login still end every
login: those are an operator's acts, often after an incident.

`keep_device_logins` sent while the setting is `"keep"` or `"end"` is
refused (`422 device_logins_not_asked`): a client asking to sign the
phone out must not be kept in silently.

### Ending the devices' logins on request is managing logins

Under `"ask"`, `keep_device_logins: false` ends logins that are not the
request's own: auth's `on_manage_logins` checks run first. In the phone
modes, only a request the phone signs may ask it (`403 phone_required`).
A thief who knows the password changes it, and ends every web session,
but the phone stays.

### The phone's logins are marked as the phone's

- A login whose request a bound device of the user signed records that
  device: the devices app tells auth, through `auth.integration`, which
  device signed the request (`note_login_device`).
- Binding a device marks the login that bound it as that device's
  (`attach_login_device`): the app logged in, then bound its key.

### Unbinding a device ends its logins, and is the phone's to do

- `DELETE /devices/{id}` ends the logins marked as that device's
  (`end_device_logins`).
- It runs the `on_manage_logins` checks first: in the phone modes, one of
  the user's devices signs it. A user who lost every phone recovers
  through support (`unbinddevices`), as before.

## Consequences

- **Projects choose.** The default keeps 2.1's behaviour; the phone modes'
  docs recommend `"keep"`, Google's behaviour.
- **A thief with the password cannot sign the phone out** under `"keep"`
  or `"ask"`, nor unbind it in the phone modes.
- **A client may offer the choice** under `"ask"`, as ASVS asks.
- **Unbinding a device logs it out.** A device unbound from the web, in
  modes where that is allowed, stops working at once.
- **`DELETE /devices/{id}` in the phone modes needs the phone's
  signature,** a change for a web page that unbinds devices.
