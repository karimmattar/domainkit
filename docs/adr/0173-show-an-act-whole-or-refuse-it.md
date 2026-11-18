# 0173. Show an act whole, or refuse it

- Status: Accepted; amended by [0180](0180-refuse-links-a-person-typed-for-a-screen-and-a-reason-past-a-line.md)
- Date: 2026-11-17
- Amends: [0144](0144-authorize-one-act-not-a-window-of-time.md),
  [0163](0163-authorize-admin-changes-and-requests-steps-as-acts.md)

## Context

ADR 0144 gives every act a screen: `Shown`, a title and six labelled
lines of 64 printable characters, built by the server and kept with the
authorization. The phone shows it, and the phone is the one screen a
stolen session does not write. The framework's own acts filled that
screen by cutting:

- **`auth.admin_change`** (ADR 0163) put a value on one line, cut at 64
  characters with `…`; a list of groups or permissions on one line,
  "auditors, clerks and 4 more"; and six lines in all, the last counting
  the values left out. Flags and fields were labelled by their names
  (`is_staff: true`).
- **Approvals' screens** (`details_of`) put each argument on one line as
  compact JSON, cut with `…`, and never counted a list's items. A user
  was named by `user_id`. A guard could not say what its request shows.

The project that runs an identity service on the framework reported both
(user reports A22 and A23). A change padded with values already stored
pushed the one that mattered off the screen: "2 more values" hid a new
mobile number among a user's unchanged names. Approving a grant of 30
permission keys showed `added: ["accounts.add_user","accounts.change_user","accounts.delete_us…`:
three of 30 grants, no count, and a user by id. A project could not
refuse either at the twin without rebuilding the act. Its workaround:
twin dependencies that read the body again and refuse what the
framework's own `fitted` and `fit` would cut or count (`422
too_much_to_show`), and held grants refused at the twin when the
approver's screen would not show them whole.

### What a person can confirm

- **OWASP's Transaction Authorization Cheat Sheet** [1], 1.1: "What You
  See Is What You Sign". The user "must verify all significant
  transaction data" as part of the authorization.
- **PSD2's dynamic linking** [2], Article 5: the payer "is made aware of
  the amount of the payment transaction and of the payee" (5(1)(a)); for
  a batch, the code is "specific to the total amount of the batch of
  payment transactions and to the specified payees" (5(3)(b)). The EBA
  answered that it links "to every single payee included in that batch"
  [3]: a list approved at once is approved item by item, not as a count.
- **WebAuthn Level 1's `txAuthSimple`** [4]: "The authenticator MUST
  display the prompt", and "MAY insert line breaks if needed"; what it
  returns is the prompt as displayed. A text goes over lines; it is not
  cut.
- **FIDO UAF 1.2** [5]: a text transaction is at most 200 characters, and
  the server builds the content for the display each authenticator
  declares. The screen's size is a fact the server lays out for.
- **CIBA** [6]: devices "may have limited display abilities", and the
  binding message is meant "for visual inspection by the end-user", so it
  should be short. A message a device cannot take is refused
  (`invalid_binding_message`), not cut.
- **NIST SP 800-63B-4** [7], 3.1.3: a push that only asks for approval is
  "no longer considered acceptable because it increases the likelihood
  that the subscriber would approve an authentication request without
  actually comparing". What a person is asked to check must be short
  enough to check: a screen of thirty lines, or a count, is approved
  unread.
- **Django** [8] names a field that has no verbose name after its
  attribute, its underscores spaces: a label people read, from the name
  the code has.

### Options

**What a screen does with what does not fit.**

| Option | What it costs |
| --- | --- |
| Cut and count (2.6) | The person confirms a count, "2 more values", "and 4 more". Padding hides the value that matters. |
| More lines: raise `Shown`'s six | A phone app built for six lines receives more, a change to what clients take in a minor release; and a long screen is scrolled, not read. Six lines of 64 stay the contract. |
| **A list over as many of the six lines as it takes; what does not fit whole refused (`TooMuchToShow`, 422)** | A large change is made in smaller steps, each confirmed: thirty permissions are about five confirmations. |
| A hook at the twin, given the act and its `Shown`, that may refuse | Each project writes the same rule; approvals' screens need a hook of their own. The rule belongs where the screen is built. A project's other checks (the project's refusal of links) stay twin dependencies. |

**Whether refusing is the default.**

| Option | What it costs |
| --- | --- |
| Refuse by default in 2.7 | A long impersonation reason, a staff user created with names in one call, a ledger posting whose lines are JSON longer than a line (with `APPROVE_ACTION` on): refused where 2.6 answered. Working back offices break in a minor release. |
| **A setting, `AUTH.AUTHORIZATIONS.WHOLE`, off in 2.x; lists over lines and counted by default** | The default still counts what does not fit, never leaves it out unsaid, and now counts a list's items too. A project that confirms on the phone turns the setting on. |

**Where a request no approver could see whole is refused.**

| Option | What it costs |
| --- | --- |
| At the approve twin only | The request waits for an approval nobody can give, until it expires. |
| **At submission too, with `APPROVE_ACTION` on** | One more screen built per submission: one read of the user for the admin API's held changes. |

**How a request names the user it is about.**

| Option | What it costs |
| --- | --- |
| Copy the name into the request's arguments | Personal data kept where erasure does not reach (the project's note); the fingerprint would change with a rename. |
| **The guard's `show(command)`, run when a screen is built** | One read per screen; the request keeps the id alone. |

## Decision

- **`laid_out(details, *, whole=True, more=None)`**, in the new public
  module `domainkit.contrib.auth.screens`, lays out labelled values on a
  screen's lines. A text is one line. A list's items are joined with `, `
  while they fit, then go on to the next line, labelled with its number
  ("Adds", "Adds (2)", in the request's language), whatever its items:
  a text as itself, anything else as compact JSON. A dict, a number,
  `True`, `None` is one line of compact JSON; a value JSON has not is a
  `TypeError`. A text item that holds a comma or a double quote, spaces
  around it, or nothing is quoted, so two items never read as one. A
  character that is not printable is escaped as Python writes it, and a
  backslash is doubled: laid out whole, two different texts never show
  alike, nor two different lists of texts (a text and the JSON it spells
  do, `1` and `"1"`, as in 2.6; counted, "and 4 more" is words an item
  could spell). With
  `whole`, a text, an item, or a label longer than a line, a label twice,
  or more than six lines raise `TooMuchToShow` (`422 too_much_to_show`).
  Otherwise what does not fit is cut and counted: a text ends with `…`, a
  list's last line says how many more, and a last line ("…") how many
  values are not shown. Every value gets a line before a list gets more.
  `Shown` itself is unchanged.
- **What the act is on shows by name and id** (`named(name, id)`):
  "ann@example.com (…123456789abc)". The act binds the id, and a name can
  be changed to read like another's (a user's address, through a change
  that is not held), so the name alone never names the subject, and a
  long one is never a reason to refuse: a username or a group name of 150
  characters must not lock its user or group. The whole id, 36
  characters, would leave the name 25 and cut ordinary addresses, so the
  screen shows its last 12 characters, the UUID's last group: the
  authorization binds the full id, and what is shown of it is a check,
  as the name is. Users and groups have UUIDs of version 4 (`uuid4()`),
  random but for 6 bits, none of them in the last group; in a UUID of
  version 7, the leading 48 bits are a time that users made together
  share, and the last group is random still. So the last group stays
  distinctive (48 random bits) whichever version a project's ids take.
  The name has the 48 characters left. A name erased shows as
  `(erased)`, which no username (letters, digits, `@.+-_`), address, or
  number can be.
- **`AUTH.AUTHORIZATIONS.WHOLE`** (off): the framework's acts show what
  they set, add, and take away whole, or are refused when asked for:
  `auth.admin_change`, `approvals.submit`, and `approvals.approve`. With
  `APPROVE_ACTION` on, a change of the admin API that approvals would
  hold is refused at its twin when the approver's screen, a line longer
  (the action), could not show it whole: auth's new `on_admin_screen`
  hook runs as the twin builds the staff member's screen, before an
  authorization is stored, and approvals registers its check there.
  Submitting a request whose approval could not be shown whole is
  refused too, however it is submitted (`POST /requests`,
  `submit_command`, a guarded service called directly, the admin API's
  held change without its act), so no request waits for an approval
  nobody could confirm. `approvals.reject`
  and `approvals.cancel` are never refused for what they show: they stop
  a request, and do nothing the screen leaves out (ADR 0154 keeps
  stopping a request from waiting).
- **`auth.admin_change`'s screen** lays out its values so: a list over
  lines; the flags by name, "Superuser", "Staff", "Active", yes or no; the
  built-in user's fields by label ("Email", "Mobile number", "First
  name", "Last name"), another field by its name, underscores as spaces
  and a capital first ("Display name"). The order is 2.6's: the user or
  group, the flags, the reason, the lists, then the fields, so that
  counting keeps what grants access.
- **Approvals' screens** (`details_of`) lay out each argument so: a list
  goes over lines, item by item, an object or a list among its items as
  compact JSON; another value is one line. **`guard(show=...)`** (and
  `Guard.show`) says what a request shows in place of its arguments: a
  function of the command, plain or `async`, returning labelled values;
  a value JSON has not (a `Decimal`, a `UUID`) shows as its text. The
  built-in holds of users (`auth.grant_superuser`, and every hold
  `ADMIN_APPROVALS` names, through `ADMIN_SHOWS`, where a hold of a group
  gives its own) show "User" by the user's first identifier and id, read
  when shown, then "Adds", "Takes away", or "Stated reason": a
  rejection's or a cancellation's own "Reason" shows beside it.

## Consequences

- **With `WHOLE` on, the phone shows everything the act does**, or the
  act is refused before anything is stored: no authorization, no
  request, and for a held change of the admin API, no confirmation that
  fails afterwards. Staff make a large grant in several smaller ones, each
  confirmed, and approved, whole: the project's twin dependencies and its
  check of held grants go.
- **Off, nothing is left out unsaid**: a list shows its first items over
  the lines left and how many more, where approvals showed cut JSON. The
  count is still a count: the setting is what closes padding.
- **The labels change** ("Staff: no" for `is_staff: false`), and a user
  or a group shows with its id. They are words for people, in the
  request's language, never a contract; a client or a test that matched
  them reads the new ones. An empty list shows as nothing, where 2.6
  wrote `[]`; a backslash shows doubled.
- **A line holds 64 code points**, not 64 columns: combining marks count
  one each, a wide character once. A text and the value it spells show
  alike (`1` and `"1"`, `true` and `"true"`), as in 2.6.
- **A guard whose arguments do not fit has no screen under `WHOLE`.**
  Accounting's guards show their arguments by name, and a posting's
  `lines` are JSON objects longer than a line: off, they show one by one
  and are counted; with `WHOLE` and `APPROVE_ACTION` on, a large posting
  is refused when it is submitted. A project gives such a guard of its
  own a `show`; the ledger's guards need one in a later release.
- **A user's name on the approver's phone** is personal data on the
  approver's own device, as the admin API's screen is (ADR 0163). It is
  read when shown and kept with the authorization only, whose rows live
  minutes and go with the user.
- **New names**: `domainkit.contrib.auth.screens` (`laid_out`, `named`,
  `fitted`, `LINES`, `LINE_LENGTH`, `TooMuchToShow`),
  `AUTH.AUTHORIZATIONS.WHOLE`, auth's `on_admin_screen`, the `show`
  argument of `guard()` and `Guard`, and the problem code
  `too_much_to_show`. No table changes. A later major release may turn
  `WHOLE` on by default.

## References

1. OWASP Cheat Sheet Series, *Transaction Authorization Cheat Sheet*:
   <https://cheatsheetseries.owasp.org/cheatsheets/Transaction_Authorization_Cheat_Sheet.html>
2. Commission Delegated Regulation (EU) 2018/389, Article 5, *Dynamic
   linking*: <https://www.legislation.gov.uk/eur/2018/389/article/5>
3. EBA Single Rulebook Q&A 2018_4435, dynamic linking of batch payments:
   <https://www.eba.europa.eu/single-rule-book-qa/qna/view/publicId/2018_4435>
4. W3C, *Web Authentication, Level 1*, 10.2, the simple transaction
   authorization extension: <https://www.w3.org/TR/2019/REC-webauthn-1-20190304/#sctn-simple-txauth-extension>
5. FIDO Alliance, *FIDO UAF Protocol Specification* 1.2, 3.5.1, the
   `Transaction` dictionary: <https://fidoalliance.org/specs/fido-uaf-v1.2-ps-20201020/fido-uaf-protocol-v1.2-ps-20201020.html>
6. OpenID Connect Client-Initiated Backchannel Authentication Flow, Core
   1.0, `binding_message`:
   <https://openid.net/specs/openid-client-initiated-backchannel-authentication-core-1_0.html>
7. NIST SP 800-63B-4, 3.1.3, *Out-of-Band Devices*:
   <https://pages.nist.gov/800-63-4/sp800-63b/authenticators/>
8. Django, *Model field reference*, `verbose_name`:
   <https://docs.djangoproject.com/en/5.2/ref/models/fields/#verbose-name>
