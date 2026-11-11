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

- **An easier way for a mobile app.** The framework already lets a phone
  scan a code a page shows, and approve what it asks, for sign-ins
  ([ADR 0128](0128-approve-sign-ins-from-the-users-mobile-app.md)).
- **Nobody was told.** NIST SP 800-63B-4 asks that the subscriber be
  notified when an authenticator is added, by a channel other than the
  one that added it [1]. A device bound without the user's knowledge was
  seen only by a user who looked at their device list.

### How others link a new device

The products that link a second device to an account, and the standards
for it, agree on one thing: the device the user already trusts starts it.

- **Signal** [2]: the new device shows a QR code that carries a
  temporary address and its public key. The primary phone scans it, after
  the phone's own unlock or biometrics, and sends the account to it.
- **WhatsApp** [3]: the same, from *Linked devices* on the primary phone.
  WhatsApp added a second way, where the new device asks with the phone
  number and the primary phone types a code. In December 2025, the
  *GhostPairing* campaign used exactly that way to take over accounts:
  the attacker asks with the victim's number, and a fake page persuades
  the victim to type the code [4].
- **Pushes the user did not ask for get approved.** In Uber's 2022
  breach, an attacker sent a contractor push after push, then asked them
  over WhatsApp, posing as IT, to approve one [5]. CISA ranks number
  matching only as an interim measure against it, behind phishing-resistant
  methods [6]; it does not stop a user who is talked into typing the
  number [7]. Microsoft requires number matching for every Authenticator
  push since February 2023, and shows the application and the location
  with it [8, 9]. Apple shows a map of where a new sign-in comes from on
  the trusted device, before it shows the code [10].
- **FIDO's cross-device sign-in** [11] has the phone scan a QR code, and
  checks that the two devices are close, over Bluetooth, so a code relayed
  from far away does not work.
- **Banks** that bind a phone to an account bind a new one from the old
  one, and then hold the new one back from risky actions for a cooling-off
  period, often 12 hours [12, 13, 14].

## Decision

### The new phone shows a code; a bound phone scans it

`POST /app-sign-in/device` `{"name", "public_key"}`:

- The request is signed by the key it asks to bind (`X-Device-Time`,
  `X-Device-Signature`): the asker holds the key.
- It names no account, and nothing is sent to anyone. The answer is a QR
  code, `qr`, for the new phone to show, and a handle, `request`, for it
  to keep. The code can be scanned for `QR_TTL`.
- The user opens the app on a phone they bound, and scans the code
  (`POST /app-sign-in/requests/claim`, signed by that device). The request
  becomes theirs: whose the new phone is, is the scanner's to say. It is
  shown with `kind` `device`, the name the new phone gave, the address it
  asked from, and whether the two phones are on one network
  (`same_network`).
- The scanning phone approves or denies it, as it approves a sign-in
  (`/approve`, `/deny`, signed by that device). No number is typed:
  scanning the code is the proof that the user holds both phones.
- `QR_SAME_NETWORK` applies as to every QR code: `"warn"` (the default)
  tells the app, which shows it; `"require"` refuses a scan from another
  network, the nearest this framework comes to FIDO's proximity check.

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

### Considered, and not done

- **The new phone names the account, and a push asks the bound phone**,
  with the number the new phone shows typed on the old one. This was
  this ADR's first decision. It let anyone who knew a user's address send
  their phone a request, as often as the start rate allowed, and a bound
  phone is a stronger credential than a login: a slip, or a user talked
  into typing the number, gave an attacker the account. It is the shape
  GhostPairing used [4], and the one push fatigue feeds on [5, 7].
- **The account's password on the new phone too.** It would add a factor
  a phishing page must also take, and send a user who forgot it through a
  reset first; scanning already needs the user's unlocked, bound phone in
  hand.
- **A cooling-off period** for a newly bound device, as banks keep. Left
  for its own decision: what it holds back is a project's to choose.

## Consequences

- **A second phone is bound without moving a key between phones,** and
  without a request reaching a phone that did not ask for it. The old way
  (`POST /devices`, signed) stays.
- **A user who scans a code an attacker shows them binds the attacker's
  phone.** The phone sees the name, the address, and whether it is on the
  same network, and the notice mail follows. The app must show all of
  them before the user approves. A project that can require the same
  network sets `QR_SAME_NETWORK = "require"`.
- **The new phone does not need the account's identifier,** so a mobile
  app asks nothing of the user on the new phone but to show the code.
- **Projects with messaging and mail installed send one more mail** per
  bound device. A project that overrides mail templates may add
  `devices/device_bound`.

## References

1. NIST SP 800-63B-4, *Digital Identity Guidelines: Authentication and
   Authenticator Management*, binding an additional authenticator, and
   authenticator events: <https://pages.nist.gov/800-63-4/sp800-63b.html>,
   <https://pages.nist.gov/800-63-4/sp800-63b/events>
2. Signal, *A synchronized start for linked devices*:
   <https://signal.org/blog/a-synchronized-start-for-linked-devices/>;
   Signal Support, *Linked Devices*:
   <https://support.signal.org/hc/en-us/articles/360007320551>
3. Engadget, *WhatsApp now lets you use your account on multiple phones*:
   <https://www.engadget.com/whatsapp-now-lets-you-use-your-account-on-multiple-phones-163158921.html>;
   Tech Advisor, *How to link other phones to your WhatsApp account*:
   <https://www.techadvisor.com/article/743525/how-to-use-whatsapp-on-two-phones.html>
4. Gen Digital, *GhostPairing WhatsApp attack*:
   <https://www.gendigital.com/blog/insights/research/ghostpairing-whatsapp-attack>;
   Malwarebytes, *The ghosts of WhatsApp: how GhostPairing hijacks accounts*:
   <https://www.malwarebytes.com/blog/news/2025/12/the-ghosts-of-whatsapp-how-ghostpairing-hijacks-accounts>;
   Security Affairs, *GhostPairing campaign abuses WhatsApp device linking*:
   <https://securityaffairs.com/185814/hacking/ghostpairing-campaign-abuses-whatsapp-device-linking-to-hijack-accounts.html>
5. *Uber's 2022 breach and MFA fatigue*:
   <https://techearl.com/uber-2022-breach-mfa-fatigue>
6. CISA, *Implementing number matching in MFA applications* (fact sheet,
   October 2022), as summarized in [7].
7. Authsignal, *Push authentication best practices, and why number
   matching alone is not enough*:
   <https://authsignal.com/blog/articles/push-authentication-best-practices-and-why-number-matching-alone-is-not-enough>
8. Microsoft Learn, *Use additional context in Microsoft Authenticator
   notifications*:
   <https://learn.microsoft.com/en-us/entra/identity/authentication/how-to-mfa-additional-context>
9. Dark Reading, *Microsoft Authenticator to enforce number matching*:
   <https://darkreading.com/cloud-security/microsoft-authenticator-to-enforce-number-matching>
10. Apple Support, *Get a verification code and sign in with two-factor
    authentication*: <https://support.apple.com/en-us/HT204974>
11. Thales, *FIDO2 hybrid transport*:
    <https://thalesdocs.com/oip/fido2-sdk/fido2-sdk-hybrid-transport/>
12. Citibank Hong Kong, *Citi Mobile app enhanced security*:
    <https://www.citibank.com.hk/english/ways-to-bank/enhanced-security/>
13. DBS Hong Kong, *Device binding in DBS Card+*:
    <https://dbs.com.hk/personal/support/digital-cardplus-device-binding.html>
14. KAF Digital Bank, *12 hours cooling period*:
    <https://kaf.helpjuice.com/en_US/12-hours-cooling-period-kaf-digital-bank-mobile-application-protocol>
