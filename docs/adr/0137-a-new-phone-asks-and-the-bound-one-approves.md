# 0137. A new phone asks, and the bound one approves

- Status: Accepted
- Date: 2026-11-11

## Context

Since 2.3, in the phone modes, a user who bound a device binds another
only by a request one of their devices signs
([ADR 0134](0134-bind-another-device-from-a-bound-one.md)). The one way to
do it was for the old phone to send the new phone's public key itself
(`POST /devices`): the app has to move a key from one phone to the other,
by a QR code for example.

Two things were left open:

- **An easier way for a mobile app.** The framework already sends a
  request to the user's phone and waits for its answer, for sign-ins,
  step-ups, and payments ([ADR 0128](0128-approve-sign-ins-from-the-users-mobile-app.md)).
  WhatsApp links a device both ways: the primary phone scans a code, or
  types a number the new device shows.
- **Nobody was told.** NIST SP 800-63B-4 (4.1.2.1) asks that the
  subscriber be notified when an authenticator is added, by a channel
  other than the one that added it. A device bound without the user's
  knowledge was seen only by a user who looked at their device list.

## Decision

### The new phone asks with its key

`POST /app-sign-in/device` `{"identifier", "name", "public_key"}`:

- The request is signed by the key it asks to bind (`X-Device-Time`,
  `X-Device-Signature`): the asker holds the key.
- The user's bound phone gets a request of kind `device`, with the name
  the new phone gave and where it asks from. With `NUMBER_MATCHING`, the
  new phone shows two digits for the old one to type.
- The answer is the same whoever the identifier names. A user with no
  bound phone, or no user, gets a request nobody can approve: a first
  phone enrolls instead (`/devices/enroll`).

### The bound phone approves, as it approves a sign-in

The request is listed, approved, and denied by the routes that answer
every other request, each signed by a bound device. That is the
signature ADR 0134 asks for.

### Only the key that asked redeems

`POST /app-sign-in/device/token` `{"request", "public_key"}`, signed by
the key again:

- The request keeps the SHA-256 of the key it was made for, and redeems
  only for that key. The handle alone binds nothing.
- Once approved, the key is bound under the name the phone approved,
  within `DEVICES.MAX_DEVICES`, and the new phone is logged in: a login
  of its own device, through every login gate.

No table changes: the digest goes where a step-up's request keeps the
credentials that asked.

### The user is told when a device is bound

With `DEVICES.BOUND_NOTICE` (on by default), every `DeviceBound` queues a
mail to the account's address, from the template `devices/device_bound`:
the device's name, and what to do if the user did not bind it. It needs
`domainkit.contrib.messaging` and `domainkit.contrib.mail`; a user with
no address gets none.

## Consequences

- **A second phone is bound without moving a key between phones.** The
  old way (`POST /devices`, signed) stays.
- **A request a user is tricked into approving binds an attacker's
  phone,** as a sign-in they are tricked into approving logs one in. The
  request names the device and its address, number matching makes the
  user read the new phone's screen, and the notice follows.
- **Projects with messaging and mail installed send one more mail** per
  bound device. A project that overrides mail templates may add
  `devices/device_bound`.
