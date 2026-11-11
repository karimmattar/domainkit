# 0134. Bind another device from a bound one

- Status: Accepted
- Date: 2026-11-10

## Context

In the phone modes (`CIBA.APPROVE_LOGINS`), the user's bound phone is
their identity: it approves the web's sign-ins, and since 2.2 only it
lists and ends logins, unbinds devices, and manages the second factors
([ADR 0130](0130-show-where-each-login-came-from-and-manage-logins-from-the-phone.md),
[ADR 0132](0132-manage-the-second-factors-from-the-phone.md)).

An audit of 2.2.1 found that a web session whose thief knows the password
could still bind a device of its own (`POST /devices`): the route asked
for the password, not for the phone. The audit ran it:

1. the web session bound a second key (`201`);
2. that device listed the logins (`200`) and ended the others (`204`);
3. the user's phone was signed out (`401`).

The thief's device then counted as the user's phone. Three more routes
that add or remove a way into the account, or a way to reach the phone,
had the same gap:

- push tokens: a web session could register its own, or remove the
  phone's, so that sign-in pushes stop reaching it;
- personal tokens: one made from a web session outlasts the phone ending
  the other logins;
- linking and unlinking an identity provider's account.

What others do:

- **NIST SP 800-63B-4, 4.1.2.1** asks that binding an additional
  authenticator need authentication at the highest level the account
  has, and that the subscriber be notified.
- **Banks under PSD2** enroll a new device through strong authentication
  on the one already registered.
- **WhatsApp and Signal** link a device only from the primary phone,
  which scans the new device's code.

Two more faults showed in the same code:

- Binding and unbinding a device each wrote in two transactions (the
  device, then its logins): a failure between them left a login marked
  for a device that was gone, or a device whose login was not marked.
- Two binds at once could both pass `DEVICES.MAX_DEVICES`.

## Decision

### A bound device binds the next one

`POST /devices` runs auth's `on_manage_logins` checks first, as unbinding
does. In the phone modes, a user who bound a device binds another only by
a request one of their devices signs (`403 phone_required` otherwise).
The new phone shows its public key, as a QR code for example; the old
phone sends it, signed, and the new phone logs in with its key
(`POST /devices/login`).

A bind that another device signed does not mark the signer's login as the
new device's. A user with no bound device binds the first one as before,
and so does every user outside the phone modes.

### The other ways in are the phone's too

The same check runs before:

- `POST /devices/push-tokens` and `DELETE /devices/push-tokens/{id}`;
- `POST /auth/tokens` (a personal token);
- `POST /identity/{name}/link` and `DELETE /identity/linked/{name}`.

Reading them (`GET`) is not checked.

### A device and its logins are written together

Binding writes the device and the mark on its login in one transaction,
under the user's lock, so two binds count each other. Unbinding removes
the device and ends its logins in one. `attach_login_device` and
`end_device_logins` take the caller's unit of work (`uow=`).

### `check` warns of a phone a password change signs out

`ciba.W001`: `APPROVE_LOGINS` is on and
`AUTH.CREDENTIAL_CHANGE_DEVICE_LOGINS` is `"end"`. The default stays
`"end"` in 2.x ([ADR 0131](0131-choose-what-a-password-change-does-to-the-devices-logins.md)).

## Consequences

- **A stolen web session adds no phone, push token, personal token, or
  linked identity,** whatever its thief knows.
- **An app that binds a second phone changes how:** the old phone sends
  the new key. An app that registered push tokens, made personal tokens,
  or linked identities from an unsigned request, for a user with a bound
  device, signs those requests.
- **A user who lost every phone** recovers through support
  (`unbinddevices`), as before.
- **The guard is still added route by route.** Declaring it per route,
  with a startup check that refuses a route that declares nothing, is
  planned for 2.3.
