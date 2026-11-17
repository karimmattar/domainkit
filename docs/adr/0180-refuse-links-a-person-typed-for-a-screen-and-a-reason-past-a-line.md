# 0180. Refuse links a person typed for a screen, and a reason past a line

- Status: Accepted
- Date: 2026-11-17
- Amends: [0173](0173-show-an-act-whole-or-refuse-it.md)

## Context

ADR 0173 lays every act out on the phone's six lines, and with
`AUTH.AUTHORIZATIONS.WHOLE` on refuses what does not fit. The phone is
the one screen a stolen session does not write; but part of what it shows
is text the session typed for the act. The project that runs an identity
service on the framework reported two gaps (user report A17):

- **Typed text reaches the phone as typed.** An impersonation's reason, a
  group's new name, a user's names, a rejection's or a cancellation's
  reason: "Locked out? Call +1 555 010 0100 to confirm", "visit
  evil.example". The person reading their own phone, or an approver
  reading theirs, is invited to call or to visit. The project refuses
  such text itself (`422 link_not_shown`), in an `on_admin_change` hook,
  an `on_admin_group_change` hook, and twin dependencies on the reject and
  cancel twins that read the body again.
- **Rejecting and cancelling are never refused for what they show**
  (ADR 0173), so a reason longer than a line is cut on the phone, though
  the maker reads it whole. An impersonation's reason, under `WHOLE`, is
  refused past a line.

### What others do

- **OWASP's Transaction Authorization Cheat Sheet** [1]: what the person
  signs is built on the server (2.3), and "the application shouldn't
  make it easier" for social engineering (1.4).
- **Callback phishing through trusted notifications** [2]: attackers put a
  number to call in a service's own form fields (a name, an address, an
  organization's name), and the service's genuine message carries it. The
  defence named is to check what such fields hold.
- **Twilio Verify** [3] puts a customer's service name into each code it
  sends, and refuses one with "phone numbers, special characters or
  links", or more than four digits.
- **CIBA** [4] asks that a binding message, shown on the authentication
  device, "use a limited set of plain text characters".
- **What a dot is**: UTS #46 [5] reads the full-width and ideographic full
  stops as label separators; UTS #39's confusables [6] list a dozen more
  characters that look like a dot, and letters of other scripts that look
  like Latin ones (Cyrillic `с`, `о`).
- **What a top-level domain is**: IANA's list [7], the names kept for
  special use (`example`, `test`, `local`, `onion`, `alt`) [8], and
  `internal`, which ICANN keeps out of the root for private networks [9].
  Android linked text with a copy of IANA's list, and deprecated it as
  "expected to become out-of-date very quickly" [10]. Its phone pattern
  is "for searching for things that look like they might be phone
  numbers", not for validating them [10].

### Options

**Where the rule lives.**

| Option | What it costs |
| --- | --- |
| Hooks of the project's, per act (2.7) | Each project writes the same rule four times, and reads the body again on the twins it has no hook for. |
| A hook at the twin, given the screen | It sees lines, not where they came from: the user's address beside their id, or an email being set, read as typed links. |
| **A `Typed` marker where the screen is built, which `laid_out` checks; a hook on typed text** | The framework marks what its acts take from a person; a project marks its own (`guard(show=...)`, its own `Shown`). |

**What a screen does with typed text that holds a link.**

| Option | What it costs |
| --- | --- |
| Show it as typed (2.7) | The channel stays open. |
| Show it defanged (`evil[.]com`) | A person still reads a link, and a number to call is still one. |
| Refuse it by default | Back offices whose reasons hold a ticket's link break in a minor release. |
| **A setting, `TYPED_LINKS`: `"shown"` by default, `"refused"` opt-in** | The default is 2.7's; a project that confirms on the phone turns it on, as it turns on `WHOLE`. |

**What is typed.**

| Option | What it costs |
| --- | --- |
| Every text on the screen | A user named by their address, a group stored before the setting: locked, as a long name would be (ADR 0173 refused that). |
| **What the act takes from the person asking** | A name stored is checked where it was typed. Approvers' screens show a group's name later, so the admin API checks a new name with or without its act. |

**How a link is told.**

| Option | What it costs |
| --- | --- |
| Any `word.letters` | Refuses `invoice.pdf`, `Mr.Smith`, `finance.readers`. |
| A library (libphonenumber, a public-suffix list) | A new dependency, which a minor release does not add. |
| **IANA's list, read as a person reads text** | A top-level domain delegated after the copy passes until the copy is refreshed. |

**A reason past a line, under `WHOLE`.**

| Option | What it costs |
| --- | --- |
| Cut it (2.7) | The approver confirms words the maker does not read. |
| Over more lines ("Reason (2)") | A long reason pushes the request's values off the screen: the padding ADR 0173 closed. |
| **Refused at the twin, as an impersonation's reason is** | Clients send 64 characters or fewer. |

## Decision

- **`Typed`**, in `domainkit.contrib.auth.screens`, a `str` subclass,
  marks text a person typed. `laid_out` checks each `Typed` value and list
  item first, whole or not: the `on_typed_text` hooks run on it, and with
  `AUTH.AUTHORIZATIONS.TYPED_LINKS` set to `"refused"` one that holds a
  link is refused (`LinkNotShown`, `422 link_not_shown`), when the act is
  asked for, before an authorization is stored. `check_typed(text)` runs
  the same checks where text is typed and shown later.
- **What the framework marks**: an impersonation's reason, on the admin
  API's screen and as the "Stated reason" of a held impersonation; a
  group's new name (`group_create`, `group_rename`); each text a change
  sets on a user but its identifiers (the username, the email,
  `IDENTIFIER_FIELDS`, `AUTH.IDENTIFIERS`: an address set is what is set);
  a rejection's and a cancellation's reason. What an act is on (`named`)
  is never marked. The admin API checks a group's new name at the change
  too, with or without `ADMIN_ACTION`.
- **Approvals**: submitting and approving check what a guard's `show`
  marks `Typed`; a request no approver could approve so is refused when
  submitted, with `APPROVE_ACTION` on, as for `WHOLE`. Rejecting and
  cancelling check their own reason only: the request's typed text is
  shown, never refused, so a request stored before the setting can always
  be stopped.
- **`holds_link(text)`** reads text as a person does: NFKC, without the
  characters a screen draws as nothing (Unicode's default ignorable
  ones), with UTS #46's label separators and UTS #39's look-alike dots as
  dots, and `example[.]com`, `ann[at]example`, `hxxp[:]//` as the link
  they spell. It holds one where it holds a scheme (`https://`, `tel:`,
  `mailto:`), an at sign before a letter or a digit, four numbers joined
  by dots, a name whose last label is in IANA's list or is special-use
  (read through UTS #39's look-alike letters, and `0` and `1` as `o`, `l`,
  `i` beside letters; an IDN label as its A-label), `www.` anything, or
  seven digits or more written as one number. Not a date, an amount with
  its cents, or a code whose letters touch its digits (`INC0012345`).
  The package keeps IANA's list as published and the look-alike letters
  derived from `confusables.txt`, with Unicode's notice.
- **Under `WHOLE`**, a rejection's or a cancellation's reason longer than
  a line, as shown, is refused at its twin (`422 too_much_to_show`). The
  rest of a decision's screen is still cut and counted.

## Consequences

- **With `TYPED_LINKS` refused**, no text a person typed for the
  framework's acts reaches a phone holding a link, an address, or a number
  to call. The project's four hooks and twin dependencies go.
- **The rule stops what reads as a link, not every way to spell one.**
  "evil . com", a number spelled in words or with letters for digits, a
  code a letter touches (`x5550100123`) pass. They read oddly on a
  confirmation screen; a project with a stricter rule registers it on
  `on_typed_text`.
- **False positives** are text that is a link to a reader too: a file
  whose extension is a top-level domain (`notes.md`, `setup.zip`), a word
  joined to the next by a dot (`Q3.report`), a group name like
  `team.legal`, an account number in groups (`GB29 NWBK 6016 1331 9268
  19`), a seven-digit number alone.
  The person writes it otherwise.
- **The list of top-level domains is a copy**, version on its first
  line. A release refreshes it from IANA; a domain delegated since passes
  until then.
- **Off, nothing changes**: `"shown"` keeps 2.7's screens, and the setting
  is read only where a screen holds typed text. `WHOLE` refuses a long
  reason where 2.7 cut it: a client of the reject and cancel twins sends
  64 characters or fewer.
- **Not covered**: CIBA's binding message and SCA's payee, which a client
  sends as the transaction's own words and the person confirms as such;
  a guard's arguments shown by name, unmarked (an IBAN or an amount there
  would read as numbers); a project's own screens, until it marks them.
- **New names**: `Typed`, `LinkNotShown`, `holds_link`, `check_typed`,
  `on_typed_text`, `AUTH.AUTHORIZATIONS.TYPED_LINKS`, and the problem code
  `link_not_shown`. No table changes.

## References

1. OWASP Cheat Sheet Series, *Transaction Authorization Cheat Sheet*:
   <https://cheatsheetseries.owasp.org/cheatsheets/Transaction_Authorization_Cheat_Sheet.html>
2. Sublime Security, *Surge in callback phishing attacks abusing auto
   notifications*:
   <https://sublime.security/blog/surge-in-callback-phishing-attacks-abusing-auto-notifications-verifications-alerts-receipts-and-more/>
3. Twilio, *Verify Service resource*, `friendly_name`:
   <https://www.twilio.com/docs/verify/api/service>
4. OpenID Connect Client-Initiated Backchannel Authentication Flow, Core
   1.0, 7.1, `binding_message`:
   <https://openid.net/specs/openid-client-initiated-backchannel-authentication-core-1_0.html>
5. Unicode Technical Standard #46, *Unicode IDNA Compatibility
   Processing*, 2.3: <https://www.unicode.org/reports/tr46/>
6. Unicode Technical Standard #39, *Unicode Security Mechanisms*,
   `confusables.txt`:
   <https://www.unicode.org/Public/security/latest/confusables.txt>
7. IANA, *List of top-level domains*:
   <https://data.iana.org/TLD/tlds-alpha-by-domain.txt>
8. IANA, *Special-Use Domain Names* (RFC 6761, RFC 6762, RFC 7686,
   RFC 9476):
   <https://www.iana.org/assignments/special-use-domain-names/special-use-domain-names.xhtml>
9. IANA, *Proposed Top-Level Domain String for Private Use* (`.internal`,
   reserved by ICANN's Board on 2024-07-29):
   <https://www.iana.org/news/2024/proposed-private-use-tld>
10. Android, `android.util.Patterns`, `TOP_LEVEL_DOMAIN_STR` and `PHONE`:
    <https://developer.android.com/reference/android/util/Patterns>
