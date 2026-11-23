# 0180. Refuse links a person typed for a screen, and a reason past a line

- Status: Accepted; amended 2026-11-18 and 2026-11-23 (at the end)
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
  asked for, before an authorization is stored. Each line that shows it
  is checked as shown too: cut to 63 characters and `…`, escaped, or the
  first of a list's items "and 4 more" (`evil.corporate` cut reads
  `evil.co…`). `check_typed(text)` runs the same checks where text is
  typed and shown later, with each cut a screen may make of it, past its
  twentieth character. A check may run more than once for one text, and
  has no side effect.
- **What the framework marks**: an impersonation's reason, on the admin
  API's screen and as the "Stated reason" of a held impersonation; a
  group's new name (`group_create`, `group_rename`); each text a change
  sets on a user but its identifiers (the username, the email,
  `IDENTIFIER_FIELDS`, `AUTH.IDENTIFIERS`: an address set is what is set);
  such a text sent as stored (a form saved whole) changes nothing, is not
  marked, and with `TYPED_LINKS` refused is left off the screen, though
  the act binds it (`"shown"` keeps 2.7's screen); each such text that a
  held `auth.set_staff` or `auth.reactivate` holds beside its grant
  (`values`, ADR 0179), which holds no value the user has already: the
  approver sees only what changes, and a form saved whole is held as the
  admin API's act takes it; a rejection's and a cancellation's reason. What an act is on (`named`) is never marked: a
  group's name is checked where it is typed, a user's username or address
  is an identifier, never checked. The admin API checks a group's new
  name at the change too, with or without `ADMIN_ACTION`.
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
  seven digits or more written as one number; when unsure, it holds one,
  as a false positive costs less than a number reaching the phone. Not a
  date; nor an amount (two digits of cents, apart by another mark than its
  groups'; groups of three by commas; or groups of three beside a
  currency's code in capitals or its sign), nor a code (letters touching
  its digits, or joined by a hyphen or an underscore to two runs of fewer
  than seven digits: `INC0012345`, `CVE-2024-12345`), unless the number
  holds a plus sign or a bracket, or the text a word that asks for a call
  (`call`, `tel`, `ext`, `WhatsApp`, and Arabic's words for them, read
  through UTS #39's look-alike letters) or a telephone's sign. The
  package keeps IANA's list as published and the look-alike letters
  derived from `confusables.txt`, with Unicode's notice;
  `.github/scripts/screen_data.py` rebuilds both, and each release runs
  it (`docs/versioning.md`). The package's license reads `MIT AND
  Unicode-3.0`.
- **Under `WHOLE`**, a rejection's or a cancellation's reason longer than
  a line, as shown, is refused at its twin (`422 too_much_to_show`). The
  rest of a decision's screen is still cut and counted.

## Consequences

- **With `TYPED_LINKS` refused**, no text a person typed for the
  framework's acts reaches a phone holding a link, an address, or a number
  to call. The project's four hooks and twin dependencies go.
- **The rule stops what reads as a link, not every way to spell one.**
  "evil . com", a number spelled in words or with letters for digits, a
  code a letter touches (`x5550100123`) pass. So does an amount's shape
  beside a word that asks for a call in another language (`llame`,
  `appelez`, `anrufen`) or misspelled (`cal`, `fone`, `whats app`):
  `5,550,100,123` reads as an amount, and no phone writes its number in
  threes by commas. They read oddly on a confirmation screen, and the
  guard is opt-in; a project with a stricter rule registers it on
  `on_typed_text`.
- **False positives** are text that is a link to a reader too: a file
  whose extension is a top-level domain (`notes.md`, `setup.zip`), a word
  joined to the next by a dot (`Q3.report`), a group name like
  `team.legal`, an account number or a postal code in groups (`GB29 NWBK
  6016 1331 9268 19`, `100-0001`, `01310-100`), seven digits or more
  alone or beside a currency (`build 1234567`, `EUR 1250000`), a code
  with a run of seven digits (`JIRA-1234567`, `user_1234567`), an amount
  in a text that asks for a call. Digits grouped in threes by dots or
  spaces with no cents (`1.234.567`, `612.345.678`, `612 345 678`) stay
  refused: Spain writes its numbers to call so, and nothing tells the two
  apart. The person writes the amount with its cents or its currency.
- **A group's long name** is refused where a cut of it, as a screen may
  show it, reads as a link, though the whole does not: checking each cut
  past the twentieth character costs a long dotted name.
- **The list of top-level domains is a copy**, version on its first
  line. A release refreshes it from IANA (`screen_data.py`); a domain
  delegated since passes until then.
- **Off, nothing changes**: `"shown"` keeps 2.7's screens, and the setting
  is read only where a screen holds typed text. `WHOLE` refuses a long
  reason where 2.7 cut it: a client of the reject and cancel twins sends
  64 characters or fewer.
- **Not covered**: CIBA's binding message and SCA's payee, which a client
  sends as the transaction's own words and the person confirms as such;
  a guard's arguments shown by name, unmarked (an IBAN or an amount there
  would read as numbers); a project's own screens, until it marks them.
- **New names**: `Typed`, `LinkNotShown`, `holds_link`, `check_typed`,
  `on_typed_text`, `typed_field` (in `domainkit.contrib.auth.integration`),
  `AUTH.AUTHORIZATIONS.TYPED_LINKS`, and the problem code
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

## Amendment (2026-11-18): names spaced out, read in the text as typed

The 2.8 audit found `TYPED_LINKS = "refused"` letting a name spaced out
through (A28-SEC-05). `holds_link` now reads more ways of writing a name
not to be a link:

- Bracketed, as before, now with `(dot)` and `<dot>`, `https : //`, and
  white space or new lines around them; `(.)` with white space around
  it before `com` only (`a period (.) at the end` is prose).
- Spaced out: a dot with white space or new lines on both sides, but
  before a capital, which starts a sentence (`Rejected . No receipt`);
  and, before `com` only, a dot with white space on one side, or `dot`.
  Other words follow a full stop or `dot` in prose, and many top-level
  domains are words (`vs. net`, the `.NET` service, `a dot net
  developer`, `Done. Info sent`).
- The spaced-out readings apply to the text as typed, never to a cut of
  it or to a line as shown: a cut ends a word, and `i.e. company` cut to
  `i.e. com…` refused common prose in the first cut of this change (the
  lead review). The cuts are still read for the rest, as this record
  decided.

The docs list what it still does not read: it is a heuristic.

## Amendment (2026-11-23): a group's stored name, a name inside a name, a screen's reading

A project's notes on 2.9 (user report A17) found five gaps in what this
record and its first amendment decided:

- **A group added shows its name unread.** `group_item` ("readers
  (…9abc) 3 perms") gave plain text to approvals' `auth.set_groups`, the
  admin API's act of a change of groups, and a project's own screens.
  Neither `TYPED_LINKS` nor `on_typed_text` read the name: one stored
  before 2.8, which took any name, reached the approver's phone as it was.
- **A top-level domain inside a name** held none: only a name's last
  label was read, so `evil.com.xy`, `evil.com-login` and `evil.com·xy`
  passed. Android's linker [11] takes a host as labels and a known
  top-level domain, ended at a word's boundary, which a dot or a hyphen
  is and an underscore is not; a phone links `evil.com` in each.
- **A screen's cut has no public reading.** A project reading stored
  text as a screen shows it (`holds_link(fitted(text))`) read the cut
  with the spaced readings, which the first amendment keeps to the text
  as typed: `approved. commercial` cut to `approved. com…` held one.
- **An Arabic-Indic zero spaced out** (`evil ٠ com`, drawn as `evil ·
  com`) was no dot, so the spaced readings never met it.
- **Digits that are never a number to call.** A field holding a
  licence's number (`No. 123 456 789`) reads as one; a project could not
  read its digits as a number only beside a word that asks for a call.

### Options

**How a group's stored name is read.**

| Option | What it costs |
| --- | --- |
| `group_item` returns its item `Typed` | The id's end and the counts are read as typed: "(…1234) 100 perms" holds a number to call, and a hook gets the item, not the name. |
| `group_item` checks the name itself | It raises where the screen is built: rejecting or cancelling a request held before, which drop the request's marks, would be refused too. |
| A check that lists the stored names that hold one | The project runs it; a name renamed since passes until it is run again. |
| **A `Typed` that carries the name (`TypedName`), which `laid_out` reads alone** | One more class, not public. A line of such items is not read again as shown: the name is read where it is checked, as the item shows it. |

**A public reading of a screen's line.**

| Option | What it costs |
| --- | --- |
| `holds_link(text, spaced=False)` public | Each caller pairs two calls, and may read the cut with the spaced readings, or skip the whole. |
| **`shows_link(text, length=LINE_LENGTH)`: the whole with the spaced readings, the line as shown without** | One name more; it reads what `laid_out` reads of a `Typed` text on a line. |

**Digits that are never a number to call.**

| Option | What it costs |
| --- | --- |
| `asks_for_a_call(text)` public | The project still reads links and addresses only with `holds_link`, which reads the number too. |
| **`holds_link(text, numbers=False)`, and `shows_link`'s** | A keyword more; the reading of a word that asks for a call stays the module's to change. |

**The misses the first amendment named.** `Com` after a spaced dot
(`evil . Com`, `evil. Com`) is no word in English or Arabic prose: read.
`dot`, or a dot with white space on one side, before another top-level
domain (`evil dot io`, `evil. net`), and a name split by a space alone
(`evil com`) stay unread: many top-level domains are words after a full
stop or `dot` (`a dot in the name`, `on the dot at nine`, `vs. net`), and
`com` is a word in Portuguese and Catalan.

### Decision

- **`group_item` marks the group's name** (`TypedName`, a `Typed` that
  holds the name and how the item shows it). `laid_out` runs the
  `on_typed_text` checks on the name as stored, and with `TYPED_LINKS`
  refused refuses it where it holds a link, whole as typed, or as the
  item shows it (escaped, cut and `…`) and as a line may cut that
  further, as `check_typed` reads a name, without the spaced readings
  for these. The id and the counts are not read. Rejecting and
  cancelling drop the mark, as for any of a request's typed text. A
  return's "Groups", which it gives back, are read as typed too.
- **A top-level domain ends a name where a linker ends a word**: at the
  name's end, or before a dot (a middle dot is one) or a hyphen. A
  label that starts with one before a hyphen counts (`com-login`); an
  IDN's A-label counts whole, as no other top-level domain holds a
  hyphen; an underscore joins a word.
- **`shows_link(text, length=LINE_LENGTH, *, numbers=True)`** reads the
  text whole with `holds_link`, and `fitted(text, length)` without the
  spaced readings.
- **`numbers=False`** on `holds_link` and `shows_link` reads seven digits
  or more as a number to call only as an amount or a code is read: with
  a plus sign, a bracket, or beside a word that asks for a call.
- **An Arabic-Indic zero** is a dot between two letters with white space
  on either side too, when it touches no digit; the spaced readings then
  meet it.
- **`check_typed` reads a name as a screen prints it** too, whole: a
  character it does not print shows escaped, a backslash and its code,
  which ends a name before it (a word joiner, a soft hyphen, a zero-width
  space, a bidirectional control after `evil.com`), however short the
  name (the lead review).
- **Bounded work**: a run of white space is read as one space once the
  spaced readings have read it, so the bracketed readings read a long run
  once; a label past 63 characters (RFC 1035 [12]) is not read for a
  top-level domain; a text longer than 10,000 characters, as given or as
  read (`READ_AT_MOST`), more than any screen shows, holds a link,
  unread: the bound refuses, never lets a link through; `check_typed`
  reads the cuts a line can hold, to its sixty-third character.

### Consequences

- **Behaviour changes, under `TYPED_LINKS` refused**: a group whose
  stored name holds a link is refused where a screen grants it, until it
  is renamed; the rename is checked as any new name is. That includes
  names 2.9's own check accepted under `"refused"`, with a top-level
  domain inside them (`acme.finance.readers`, `cairo.office.staff`): a
  project reads its stored names before it deploys. Text that holds a
  name inside a name is refused where 2.9 showed it.
- **False positives** grow by a file named with a top-level domain before
  its extension (`report.final.docx`, `libssl.so.3`), which a phone links
  too; by a dotted name with a common word that is a top-level domain
  inside it (`final`, `new`, `dev`, `app`, `prod`, `team`, `office`,
  `name`), and `v1` read as `vi` (`schema.v1.json`); and by an
  Arabic-Indic zero spaced out between two words, the second a top-level
  domain (`الفروع ٠ قطر`, `الطلبات ٠ عمان`, `المستخدمون ٠ شبكة`): the
  zero is written `0`. A corpus of notes in English and Arabic
  (versions, files, abbreviations, decimals, dates) pins what still
  passes.
- **Speed**: `holds_link` reads each label of a dotted run where it read
  the last; its time stays linear in the text's length. On 100,000
  characters, dotted labels that each read the 256 ways `READINGS`
  allows take about half a second (2.9: a tenth), and a run of white
  space about 30 milliseconds (2.9: over a minute). A text the size of a
  request body holds a link at once, unread. The framework's own typed
  text is shorter (a reason 500 characters, a group's name 150); a
  project's user field that holds more is refused at the admin API's
  act under `TYPED_LINKS` refused.
- **Not covered**: "Takes away" shows a group's name unmarked
  (`list_item`): taking a user out of a group is never refused for the
  group's name. A project's own item that shows a name late in it,
  after its own words, may be cut by a line before the name's twentieth
  character: that cut is not read.
- **New names**: `shows_link`, and `numbers` on `holds_link`.

### References

11. Android, `android.util.Patterns` source, `STRICT_HOST_NAME`,
    `WORD_BOUNDARY`, `AUTOLINK_WEB_URL`:
    <https://android.googlesource.com/platform/frameworks/base/+/master/core/java/android/util/Patterns.java>
12. RFC 1035, *Domain names: implementation and specification*, 2.3.4
    (labels of 63 octets or less): <https://www.rfc-editor.org/rfc/rfc1035#section-2.3.4>
