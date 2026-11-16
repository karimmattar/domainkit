# 0155. Choose number matching and the same network per page

- Status: Accepted
- Date: 2026-11-16
- Amends: [0128](0128-approve-sign-ins-from-the-users-mobile-app.md)

## Context

A project that runs an identity service on the framework reported that
ciba's policy is global (user report A8). `NUMBER_MATCHING` and
`QR_SAME_NETWORK` are single settings (`contrib/ciba/settings.py`), so a
project cannot ask the phone for the page's number on its admin
dashboard and not in its consumer app, nor refuse a phone on another
network for the dashboard's QR codes alone. A QR sign-in never asks a
number at all (`services.start_qr`). The report proposed a policy per
client or audience.

2.4 already gave the project's pages two hooks (user report A8, the part
closed first): `on_start(request)` may refuse a push or QR start, or
name it with a binding message, and `on_approve(request, user, sign_in)`
may refuse a phone's approval.

### What others do

- **Microsoft Authenticator** asks the number for every push since 8 May
  2023, and "users can't opt out of number matching". It relaxes one
  case: when the prompt shows on the device that started the sign-in,
  Approve and Deny suffice, since "the prompt only shows on the device
  that initiated the sign in" [1].
- **Okta Verify** sets the number challenge on the authenticator: never,
  for high-risk sign-ins only, or for every push [2]. The user selects,
  on the phone, the number that matches the one the sign-in page shows
  [3].
- **CISA** calls push bombing a way in, and number matching "one of the
  best interim mitigation[s]" where phishing-resistant MFA cannot be had.
  It works by "requiring access to the login screen to approve requests"
  [4].
- **RFC 10027** (the cross-device flows BCP) lists request binding with
  out-of-band data, verified "by the user or by the Authorization
  Device", and warns that an attacker may phish that data first. It
  calls proximity the strongest defence, FIDO's Bluetooth proof "the
  RECOMMENDED approach", and lists trusted networks among the controls
  that prevent an attack [5].
- **OpenID CIBA Core** registers per client its delivery mode, its
  notification endpoint, its request signing algorithm, and whether it
  sends a `user_code` (section 4). Its interlock is the
  `binding_message`, "displayed on both the consumption device and the
  authentication device"; nothing asks the user to type a number [6].
- **QR sign-ins.** WhatsApp links a computer by its QR code, or by an
  eight-character code the computer shows and the user types on the
  phone [7]. FIDO's hybrid transport, now the Proximity Exchange
  Protocol, makes the phone send a Bluetooth advertisement that matches
  the QR code: "Without a proof of proximity a web site could, for
  example, display a QR code", and a relayed code fails because the
  attacker's computer is far away [8]. The USENIX Security 2025 study of
  QR logins finds flaws in how codes are made, bound and reused, and
  asks users to hide their codes [9].

### What a page's policy can do, and what it cannot

- **It is decided from the start's request alone.** An answer that
  depended on the user (a number for staff, none for customers) would
  tell whoever starts a push which identifiers are staff: the decoys of
  ADR 0128 exist so that the start's answer says nothing of the user.
- **Whoever starts a sign-in chooses the page.** An `Origin`, a path or a
  header says which page asks, and an attacker sends the one that asks
  least. Microsoft relaxes the number only where the relaxing fact is
  proved (the prompt is on the device that asked); a page's `Origin` is
  not proved. A page that asks less weakens every user's push sign-ins,
  unless something that the attacker does not choose (the user, at
  approval) keeps the stronger policy: `on_approve` sees the request,
  and whether it asked the number.
- **A number on a QR sign-in does not stop a live relay.** Whoever relays
  the code shows the number too (RFC 10027's limit on out-of-band data).
  It makes the phone's user read the page that started the sign-in,
  which a QR code that reached them as a picture, or as a universal link
  they tapped (`QR_FORMAT`), does not need.
- **Same network compares addresses.** A NAT can join strangers, and a
  phone on mobile data never shares the laptop's address. `"require"`
  suits a dashboard used from an office's network, not a public app.

### Options

Where the policy comes from:

| Option | What it costs |
| --- | --- |
| A settings map per page, keyed by `Origin` | A second way to name a page beside `on_start`; each environment's origins in settings; no path or header. |
| Registration metadata per OIDC client, as CIBA's | A client's page has no address the provider sees, so the same network cannot apply; a number needs a response field client libraries do not read. CIBA's interlock is the binding message, which every client sends (`REQUIRE_BINDING_MESSAGE`). |
| A new hook point for the policy | Two hooks deciding what a page's start is. |
| **`on_start` answers a `SignInPolicy`** | One place names, refuses, and sets the policy of a page's start; a message alone still works; a field left None keeps the setting. |

Several hooks answering:

| Option | What it costs |
| --- | --- |
| The first, or the last, answer wins | The order of `INSTALLED_DOMAINS` decides what the phone asks. |
| **The strictest given wins** | A hook relaxes a setting only where no other asks more. |

Where a QR code's same-network policy waits for its claim:

| Option | What it costs |
| --- | --- |
| The setting, read at the claim (as before) | Never per page. |
| A flag in the QR code | The code is a secret found by its digest; a flag in it is obscure, and each later field adds to it. |
| `on_approve`, comparing `claimed_ip` and `requester_ip` | Possible already, after the phone claimed it; the page is known only by its binding message. |
| **A nullable `same_network` column** | One migration. None keeps the setting: rows from before, and use cases that pass none. |

Number matching on QR sign-ins:

| Option | What it costs |
| --- | --- |
| Follow `NUMBER_MATCHING` (on by default) | A breaking change: an app that approves a scanned code without a number would deny it (`wrong_match_code`). |
| **`QR_NUMBER_MATCHING`, off by default** | QR sign-ins stay as they were; one switch asks every page's QR codes. |

Choosing the number or typing it: a phone that offers three numbers lets
a tired tap win one time in three; typing two digits wins one time in a
hundred, and one wrong try denies the request (ADR 0128). The phone is
never told the number, so it has nothing to offer: it types it, for a QR
sign-in as for a push.

## Decision

- **`SignInPolicy(binding_message, number_matching, same_network)`**, in
  `domainkit.contrib.ciba.hooks`. An `on_start` hook may return one in
  place of a message (a string is still a message). A field left None
  keeps the setting: `NUMBER_MATCHING` for a push, `QR_NUMBER_MATCHING`
  for a QR code, `QR_SAME_NETWORK` for its claim.
- **Several hooks:** the first message given is kept; the number is asked
  if a hook asks it, else not if one turns it off; the same network is
  required if a hook requires it, else warned if one says so.
- **A policy the start cannot apply** (a message the phone cannot show,
  `number_matching` that is not a bool, `same_network` other than
  `"warn"` or `"require"`, or another answer) is the project's mistake:
  `ImproperlyConfigured`, a 500, and nothing is stored.
- **`QR_NUMBER_MATCHING = False`**, a new setting: when on, the project's
  QR sign-ins show a number too.
- **`start_qr(..., match=False, same_network=None)`.** With `match`, the
  page gets two digits (`match_code`), the phone that claims the code is
  told `match_required`, and approves with them; a wrong number denies
  the request, as for a push. `same_network` is kept in the new nullable
  column `domainkit_ciba_requests.same_network`, and `claim` reads it, or
  the setting when it is None.
- **Not here:** a policy per user at the start (it would tell who the
  user is), per OIDC client (above), for a signed-in session's asks
  (acts, transactions: no page starts them; they follow
  `NUMBER_MATCHING`), or for a new phone's request to be bound (ADR
  0137, unchanged).
- The docs say that anyone can start from any page, and show an
  `on_approve` hook that keeps the number for staff.

## Consequences

- A project asks the number and the same network of its dashboard and
  not of its shop, with one hook and no routes of its own.
- A page that asks less weakens every user's push sign-ins, since anyone
  can start from it. That is the project's choice to make with
  `on_approve`; the framework keeps the policy on the request, where
  the hook sees it.
- ciba's table gains a nullable column: `makemigrations` and `migrate`
  before the new code serves requests ([Upgrading](../upgrading.md)).
- A number on a QR code adds a step to the sign-in and does not stop a
  live relay. Proximity, from passkeys' hybrid flow, stays the
  phishing-resistant choice; the same network is the proxy this flow
  has.
- A request keeps the policy it started with: a changed setting reaches
  only those whose page gave none.

## References

1. Microsoft, [How number matching works in MFA push notifications for Authenticator](https://learn.microsoft.com/en-us/entra/identity/authentication/how-to-mfa-number-match).
2. Okta, [Okta Verify: Push authenticator settings](https://help.okta.com/oie/en-us/content/topics/identity-engine/authenticators/ov-push-settings.htm).
3. Okta, [Number Challenge for Okta Verify](https://support.okta.com/help/s/article/Number-Challenge-for-Okta-Verify).
4. CISA, [Implementing Number Matching in MFA Applications](https://www.cisa.gov/sites/default/files/publications/fact-sheet-implement-number-matching-in-mfa-applications-508c.pdf) (October 2022).
5. IETF, [RFC 10027, Cross-Device Flows: Security Best Current Practice](https://datatracker.ietf.org/doc/draft-ietf-oauth-cross-device-security/), sections 6.1.1, 6.1.8 and 6.1.17.
6. OpenID Foundation, [Client-Initiated Backchannel Authentication Core 1.0](https://openid.net/specs/openid-client-initiated-backchannel-authentication-core-1_0.html), sections 4 and 7.1.
7. Android Central, [WhatsApp's new login method on the web uses a one-time code](https://www.androidcentral.com/apps-software/whatsapp-link-with-phone-number).
8. FIDO Alliance, [Proximity Exchange Protocol](https://fidoalliance.org/specs/hybrid/proximity-exchange-protocol-v1.0-wd-20260717.html), section 5.4.
9. Zhang et al., [Demystifying the (In)Security of QR Code-based Login in Real-world Deployments](https://www.usenix.org/conference/usenixsecurity25/presentation/zhang-xin), USENIX Security 2025.
