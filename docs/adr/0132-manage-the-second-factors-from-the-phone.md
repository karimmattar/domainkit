# 0132. Manage the second factors from the phone

- Status: Accepted
- Date: 2026-11-09

## Context

[ADR 0131](0131-choose-what-a-password-change-does-to-the-devices-logins.md)
lets a project keep the phone's login when the password changes. Turning
a second factor on does more than end the phone's login: the user's
second factors, the authenticator app (TOTP) and passkeys, step up a web
session (`/step-up`, `/passkeys/step-up`).

In the phone-as-identity modes
([ADR 0128](0128-approve-sign-ins-from-the-users-mobile-app.md),
[ADR 0130](0130-show-where-each-login-came-from-and-manage-logins-from-the-phone.md)),
a web session steps up by asking the phone. But a stolen web session whose
thief knows the password can:

1. **turn the authenticator app on with the thief's own app,** which ends
   every other login, the phone's too, and hands the thief the recovery
   codes;
2. **register the thief's own passkey.**

Either way, the thief steps up without the phone: the payments and other
acts a step-up guards are theirs.

Others:

- **Keycloak** asks "Sign out from other devices" when a user updates any
  credential: a password, an OTP, or a passkey.
- **OWASP's Multifactor Authentication Cheat Sheet** and its testing guide
  treat changing the factors as a sensitive act: authenticate again, and
  tell the user.
- **OWASP ASVS 4.0.3, 3.3** counts enrolling a new MFA device among the
  attacks that ending sessions answers.

## Decision

### One setting for every change of credentials

ADR 0131's setting covers turning the second factor on as well as a
password change or reset, and is renamed for it:
`AUTH.CREDENTIAL_CHANGE_DEVICE_LOGINS` (`"end"`, `"keep"`, `"ask"`), and the
confirmation (`POST /mfa/totp/confirm`) takes `keep_device_logins`. The
name changes before any release carried it.

Turning the factor off, and new recovery codes, end no login, as before.

### In the phone modes, only the phone manages the second factors

Auth's `on_manage_logins` checks, which already guard listing and ending
logins, also run before:

- `POST /mfa/totp`, `/mfa/totp/confirm`, `/mfa/totp/disable`, and
  `/mfa/recovery-codes`;
- `POST /passkeys/register/options`, `/passkeys/register`, and
  `DELETE /passkeys/{id}`.

With `CIBA.APPROVE_LOGINS`, the ciba app's check asks that one of the
user's devices sign them: else `403 phone_required`. Users with no bound
device, and projects without the phone modes, are unchanged.

## Consequences

- **A thief with the password and a web session cannot add a factor**, nor
  step up without the phone, in the phone modes.
- **A web page in the phone modes no longer turns the authenticator app on
  or off, nor adds passkeys:** the mobile app does, signed.
- **`AUTH.PASSWORD_CHANGE_DEVICE_LOGINS` never shipped:** projects set
  `CREDENTIAL_CHANGE_DEVICE_LOGINS`.
- **`on_manage_logins` means "manage the user's logins and the factors they
  log in with"**: a project's own checks see these requests too.
