# Authorizing one act, with its arguments

A step-up (`StepUp(max_age=...)`) opens every guarded route of a login for
a while, with any arguments, as often as the client likes. An action
authorization opens one act, with the arguments the user was shown, once.
The user confirms "End Alice's membership of Acme", and the server ends
that membership, and nothing else: a copy of the session gains nothing
from the user's approval but that act.

The design is [ADR 0144](../adr/0144-authorize-one-act-not-a-window-of-time.md):
a stored row, spent once, and a code over what was confirmed, as
[transactions](step-up.md#transactions-authorized-for-one-amount-and-payee)
are bound, for any act.

On this page you learn how to:

- declare an act, register it, and guard its route;
- authorize a `@use_case`, on one route and not another;
- ask for an authorization, confirm it, and send it with the act;
- choose what confirming proves, and when the act spends it;
- confirm another way, from an app of your own;
- authorize an act in a test.

## When to use it, and when not to

- **Use it when** the user confirms one act, with the arguments shown,
  once: ending a membership, closing an account, deciding a request.
- **Do not use it** when any guarded act may go ahead for a few minutes:
  a [step-up](step-up.md#stepping-up) asks for one code for all of them.
  Do not use it to get another person's agreement: that is
  [approvals](../contrib/approvals.md), whose steps an authorization can
  guard ([held for approval](#held-for-approval)).

## A first example

Declare the act: its purpose, the command its service takes, what it is
on, and what the user is shown. Decorate the service, register the
action in your domain's `ready()`, and ask for it on the route. The act
is of an organizations domain, as `startdomain` writes one
([part 1 of the tutorial](../tutorial/1-first-resource.md)):
`OrganizationsUnitOfWork` is its unit of work on the database, with the
repositories `memberships` and `organizations`, and `memberships` is the
`ResourceRouter` of its memberships, in `routes.py`. [Part 7 of the
tutorial](../tutorial/7-four-eyes.md) authorizes an act in a project, end
to end: a clerk's approval of a transfer, with a code.

```python
from uuid import UUID

from domainkit.contrib.auth import StepUp
from domainkit.contrib.auth.actions import (
    Action,
    Shown,
    register_action,
    requires_authorization,
)
from domainkit.ddd import Command
from domainkit.http.access import HasPermissions


class EndMembership(Command):
    """End someone's membership of an organization."""

    organization_id: UUID
    membership_id: UUID


async def show(command: EndMembership) -> Shown:
    async with OrganizationsUnitOfWork() as uow:
        membership = await uow.memberships.get(command.membership_id)
        organization = await uow.organizations.get(command.organization_id)
    return Shown(
        "End a membership",
        {"Member": membership.display_name, "Organization": organization.name},
    )


END_MEMBERSHIP = Action(
    "organizations.end_membership",
    command=EndMembership,
    subject=lambda command: f"organizations.membership:{command.membership_id}",
    show=show,
)


@requires_authorization(END_MEMBERSHIP)
async def end_membership(command: EndMembership, uow: OrganizationsUnitOfWork) -> None:
    async with uow:
        membership = await uow.memberships.get(command.membership_id)
        membership.end()
        await uow.commit()


# In OrganizationsConfig.ready():
register_action(END_MEMBERSHIP)

memberships.command_route(
    EndMembership,
    service=end_membership,
    method="DELETE",
    rule=HasPermissions("organizations.end_membership")
    & StepUp(for_="organizations.end_membership"),
)
```

Mount the routes that confirm, next to the one that logs in:
`account_router()` mounts them (`authorizations`), or
`router.include_router(actions.router, prefix="/auth")`. The round trip,
for a web client:

```http
DELETE /organizations/7/memberships/42                       401 authorization_required
POST   /organizations/7/memberships/42/authorization         201 {"id": "9f0c...", "purpose": "organizations.end_membership",
                                                                  "shown": {...}, "expires_at": "...", "methods": ["otp"]}
POST   /auth/authorizations/9f0c.../confirm {"code": "123456"} 200 {"authorization": "9f0c....mYd3...", "expires_at": "..."}
DELETE /organizations/7/memberships/42
       Action-Authorization: 9f0c....mYd3...                 204
```

The same request again with the same header is `403
action_not_authorized`: the authorization was spent.

## Declaring the act

| `Action` argument | Meaning |
| --- | --- |
| `purpose` | A lower-case name of at most 100 characters (`organizations.end_membership`); one action per purpose. |
| `command` | The class the service takes, or the `@use_case` whose command it is (*new in 2.5*). The digest is computed from it, read back through pydantic as JSON. |
| `subject` | `command -> "<type>:<id>"`: what the act is on, an internal id of at most 200 characters (letters, digits, and `_ . : / -`), kept for the trail. |
| `show` | `command -> Shown`, a coroutine or a plain function: what the user is shown, from the command and the stored state, in the request's language. `(command, uow) -> Shown` reads in the unit of work the act runs in (*new in 2.5*, [below](#a-use-case-as-the-act)). |
| `ignore` | Fields of the command that change on every call (a time, a fresh id), left out of the digest. |
| `ttl` | How long its authorizations work, in place of `AUTH.AUTHORIZATIONS.TTL`: more than 0, 30 minutes at most. `AUTHORIZATIONS.ACTIONS` may name another for the act ([below](#each-act-its-own-methods-and-lifetime)). |
| `methods` | What confirming must prove, in place of `AUTH.AUTHORIZATIONS.METHODS`: `otp`, `pwd`, and `phone`, holding `otp` or `phone`. `AUTHORIZATIONS.ACTIONS` may name others for the act. |
| `binds` | `(command, uow) -> value`, a coroutine or a plain function: what the act decides on, read from the stored record in the act's unit of work (an aggregate's `version`, the fields it decides on). The authorization holds while it reads the same (*new in 2.7*, [below](#the-record-it-decides-on)). |
| `require_if_match` | `True`: asking, and the act, need `If-Match` naming the version the page read (`428 precondition_required` without). `False` by default (*new in 2.8*, [below](#the-version-the-page-read)). |

`Shown(title, details)` is what a screen shows: a title of at most 64
printable characters, and at most six labels and values of at most 64
each, in order (a mapping, or pairs). Anything longer is a `ValueError`:
shorten what you show, or lay a list over lines with `laid_out`
([What a screen shows](#what-a-screen-shows)). It is built when the authorization is asked for,
and kept: every screen shows the kept words, never words a page sent.

`register_action(action)` in `ready()` registers it, a hook point of
auth's ([ADR 0140](../adr/0140-auth-asks-other-apps-through-hooks.md)).
Two actions of one purpose are refused.

## What a screen shows

*New in 2.7* ([ADR 0173](../adr/0173-show-an-act-whole-or-refuse-it.md)).
The phone is the one screen a stolen session does not write, and the
person confirms what it shows: the act must be there whole. A screen
holds a title and six lines of 64 characters. `laid_out(details)`, in
`domainkit.contrib.auth.screens`, puts an act's labelled values on those
lines: a text on one line, a list over as many as it takes, the next
lines numbered. `named(name, id)` names what the act is on: its name, cut
to fit, and the end of its id.

```python
from domainkit.contrib.auth.actions import Shown
from domainkit.contrib.auth.screens import laid_out, named


async def show_grant(command: GrantKeys, uow: AccountsUnitOfWork) -> Shown:
    member = await uow.members.get(command.member_id)
    details = {"Member": named(member.name, member.id), "Adds": command.keys}
    return Shown("Grant permissions", laid_out(details))
```

```text
Grant permissions
Member    Ada Lovelace (…123456789abc)
Adds      accounts.add_user, accounts.change_user, accounts.view_user
Adds (2)  accounts.delete_user
```

- **What the act is on is never too much to show.** The act binds its
  whole id; its name is a hint, which a person may have changed (a
  user's address). `named` shows the id's last 12 characters, the last
  group of a UUID (`6f1c9c1e-8a2b-4c3d-9e0f-123456789abc` shows as
  `…123456789abc`), and the name in the 48 characters left, cut with `…`
  where it does not fit. A user whose name was erased shows as
  `(erased)`, which no username, address, or number can be; the id tells
  two users apart either way.
- **Two different texts never show alike**, nor two different lists of
  texts, laid out whole. A list's items are joined with `, `; an item that
  holds a comma or a double quote, has spaces around it, or is empty is
  quoted (`"readers, admins"`). A character that is not printable is
  escaped as Python writes it (a new line as `\n`, a zero-width space as
  `\u200b`), and a backslash is doubled, so a new line and a backslash
  followed by `n` never show alike. Counted (`whole=False`), a list's
  last line ends with words, "and 4 more", that an item could spell.
- **Values are text or JSON.** A dict, a number, `True`, or `None` shows
  as compact JSON on one line; a list or a tuple goes over lines item by
  item, an item that is not text as compact JSON (`{"a":1}, ["b"], 3`). A
  value JSON has not (bytes, a set, a `Decimal`) is a `TypeError`: give
  its text. A text and the value it spells show alike: `1` and `"1"`,
  `true` and `"true"`, as in 2.6.
- **A line holds 64 characters, counted as code points**, not as the
  width a phone draws: a character with combining marks counts each
  mark, and a wide character counts once.
- What the act does that does not fit whole is refused when the act is
  asked for: `TooMuchToShow`, `422 too_much_to_show`. A text or an item
  longer than a line, a label twice, or more than six lines. The person
  makes the act in smaller steps, each shown whole.
- `laid_out(details, whole=False)` cuts and counts instead: a text cut
  ends with `…`, a list's last line says how many more ("auditors,
  clerks and 4 more"), and a last line ("…") how many values are not
  shown. Use it only for an act that does nothing the screen leaves out,
  such as stopping something.

**The framework's own acts** (`auth.admin_change`, and approvals'
`approvals.submit`, `approvals.approve`, `approvals.reject`, and
`approvals.cancel`) lay out their screens so. A list goes over as many
lines as it takes ("Adds", "Adds (2)"); flags and fields are named as a
person reads them ("Superuser: yes", "Email", "Display name"); a user or
a group shows by name and id. With `AUTH["AUTHORIZATIONS"]["WHOLE"]` on,
what an act sets, adds, or takes away that does not fit whole is
refused, never its subject's name. Where a screen would count keys (a
group added, "readers (…9abc) 3 perms"; what a user made active again
holds), the keys `AUTH["AUTHORIZATIONS"]["NAMED_PERMISSIONS"]` names
show by name on a line of their own, "Grants", and first in a list of
keys (since 2.8); by default, `TENANCY.ALL_TENANTS_PERMISSION` where it
is set. Keys granted directly or through a group are named; a grant on a
single object is counted, whatever its key. A change of the admin API that
approvals would hold is refused at its twin when the approver's screen,
a line longer, could not show it whole, and a request that its approvers
could not be shown whole is refused when it is submitted, rather than
left waiting. Rejecting and cancelling are never refused for what the
request shows: they stop it. Their own reason is refused past a line
(since 2.8), as an impersonation's is. Off, the default in 2.x, what does
not fit is cut and counted, never left out unsaid.

```python
AUTH = {"ADMIN_ACTION": True, "AUTHORIZATIONS": {"WHOLE": True}}
```

### Text a person typed

*New in 2.8* ([ADR 0180](../adr/0180-refuse-links-a-person-typed-for-a-screen-and-a-reason-past-a-line.md)).
Part of a screen is text the person asking typed: a reason, a name
given, a note. A stolen session writes it, and the phone shows it to its
owner, or to an approver: "Locked out? Call +1 555 010 0100 to confirm".
Mark such text `Typed` where the screen is built; with
`AUTH["AUTHORIZATIONS"]["TYPED_LINKS"]` set to `"refused"`, `laid_out`
refuses one that holds a link, an email address, or a number to call,
when the act is asked for, before an authorization is stored:
`LinkNotShown`, `422 link_not_shown`. `"shown"`, the default, shows it
as typed, as 2.7 did.

```python
from domainkit.contrib.auth.actions import Shown
from domainkit.contrib.auth.screens import Typed, laid_out, named

details = {"Member": named(member.name, member.id), "Note": Typed(command.note)}
Shown("End a membership", laid_out(details))
```

```python
AUTH = {"ADMIN_ACTION": True, "AUTHORIZATIONS": {"TYPED_LINKS": "refused"}}
```

- **What the framework marks**: an impersonation's reason, on the admin
  API's screen and as a held impersonation's "Stated reason"; a group's
  new name; each text a change sets on a user but the fields that
  identify them (an email or a mobile number set is what is set); a
  rejection's and a cancellation's reason. The admin API checks a group's
  new name at the change too, with or without `ADMIN_ACTION`: approvers'
  screens show it later. A request whose approval would refuse what it
  typed is refused when it is submitted, with `APPROVE_ACTION` on.
- **A group added** (since 2.10): the admin API's act and approvals'
  `auth.set_groups` show each group a grant adds in a few words
  (`group_item`, "readers (…9abc) 3 perms"), its stored name typed; a
  return's "Groups", which it gives back, are read as typed too. A name
  that holds a link, whole, as a screen prints it, or as a line may cut
  it, is refused where the group is granted (`422 link_not_shown`), until
  the group is renamed: one given before 2.8, while `TYPED_LINKS` was
  `"shown"`, or with a top-level domain inside it, which 2.9's check let
  through (`acme.finance.readers`). A request held before is still
  rejected or cancelled. The `on_typed_text` checks get the name as
  stored; the id and the counts beside it are not read. "Takes away"
  shows names unmarked: taking a user out of a group is never refused
  for the group's name.
- **What it never marks**: what the act is on (`named`), which never
  locks its user or group. A group's name is checked where it is typed,
  as a screen may show it cut, and where a grant shows it; a user's
  username, address, or number is an identifier, set as one, and never
  checked. A text field sent as it
  is stored (a form saved whole) changes nothing: it is not checked, and
  with `TYPED_LINKS` refused it is left off the screen, so a stored link
  is never shown through it (`"shown"` keeps 2.7's screen). Rejecting and cancelling
  check their own reason, never the request's, so a request stored before
  the setting can always be stopped.
- **A guard's `show`** marks what its maker typed:
  `guard(..., show=lambda c: [("Payee", c.payee), ("Memo", Typed(c.memo))])`.
  Arguments shown by name are not marked: an IBAN or an amount would read
  as a number.
- **What is checked is what the phone shows too.** A line cut to 63
  characters and `…`, an escaped character, or a list's first items
  "and 4 more" may read as a link the text did not hold: `evil.corporate`
  cut to `evil.co…`. Each line that shows typed text is checked as shown.
- **`holds_link(text)`** reads text as a person reads it: full-width forms
  as their letters, characters drawn as nothing (zero-width spaces)
  dropped, the dots that look like one (`。`, `．`, a middle dot, a bullet)
  as a dot, `example[.]com` and `ann[at]example` as what they spell
  (since 2.8.1 also `example (dot) com`, `example<dot>com`, `https : //`,
  and white space or new lines around them), and since 2.8.1 a name spaced
  out, in the text as typed: `example . com` (white space or new lines on
  both sides of the dot, but before a capital, which starts a sentence:
  `Rejected . No receipt`), and before `com` alone, `example .com`,
  `example. com` and `example dot com`. Other words follow a full stop,
  a dot, or `dot` in prose, and many are top-level domains (`vs. net`,
  `the .NET service`, `a dot net developer`, `Done. Info sent`). Since
  2.10, `Com` after a spaced dot is read as `com` (`evil . Com`, `evil.
  Com`), and an Arabic-Indic zero, which reads as a dot between two
  letters, is one with white space on either side too (`evil ٠ com`),
  but where it touches a digit (`٢٠٢٦`). A cut of
  the text, which the screen may show, is read without these: `i.e.
  company` cut to `i.e. com…` holds none. It
  finds a scheme (`https://`, `tel:`, `mailto:`), an at sign before a
  letter or a digit, an IP address, a name with a top-level domain
  IANA lists or one kept for special use (`evil.example`, also with
  Cyrillic letters that read as Latin), `www.` anything, or seven digits
  or more written as one number (`+1 (555) 010-0100`). Since 2.10, a
  top-level domain ends a name where a phone's linker ends a word: at
  its end, or before a dot, a middle dot, or a hyphen (`evil.com.xy`,
  `evil.com-login`, `evil.com·xy` hold `evil.com`); an underscore joins
  a word (`evil.com_login`). When unsure, it
  holds one. Not a date; nor an amount (with two digits of cents,
  `1.234.567,89`; in groups of three by commas, `1,250,000`, or beside a
  currency, `12 500 000 EUR`), nor a code (its letters touching its
  digits, `INC0012345`, or joined by a hyphen to two runs of fewer than
  seven digits, `CVE-2024-12345`), unless the number holds a plus sign or
  a bracket, or the text a word that asks for a call (`call`, `tel`,
  `ext`, `WhatsApp`, and their Arabic words, read through look-alike
  letters) or a telephone's sign: `call 555 010 0123 EUR` and
  `support-555-010-0123` hold one. `v1.2`, `Q3 report`, `readers`,
  `2026-11-17`, and `invoice.pdf` hold none.
- **Known false positives** read as links to a reader too, and are
  written otherwise: a file or a group named with a top-level domain after
  a dot (`notes.md`, `team.legal`, `Q3.report`), an account number or a
  postal code in groups (`GB29 NWBK 6016 1331 9268 19`, `100-0001`,
  `01310-100`), seven digits or more alone or beside a currency
  (`build 1234567`, `EUR 1250000`), digits in threes by dots or spaces
  with no cents (`1.234.567`, `612 345 678`: numbers to call in Spain), a
  code with a run of seven digits (`JIRA-1234567`), an amount in a
  text that asks for a call, `com` after `dot` (`the dot com era`), a
  top-level domain in lower case after a dot spaced on both sides (`fine
  . me too`), and one after `(dot)` (`the key (dot) is broken`). Since
  2.10, a file named with a top-level domain before its extension
  (`report.final.docx`, `libssl.so.3`), which a phone links too, with a
  common word such as `final`, `new`, `dev`, `app`, `prod`, `team`,
  `office` or `name` inside a dotted name (`budget.new.xlsx`,
  `cairo.office.staff`, `user.name.first`), `v1` read as `vi`
  (`schema.v1.json`), and an Arabic-Indic zero spaced out between two
  words, the second a top-level domain (`الفروع ٠ قطر`, `الطلبات ٠
  عمان`, `المستخدمون ٠ شبكة`, `تمت إضافة ٠ موقع`): write the zero as
  `0`. Also since 2.10, a text longer than 10,000 characters, as given
  or as read (full-width and compatibility forms spelled out), holds
  one, unread (`READ_AT_MOST`): reading it takes bounded work, and no
  screen shows as much. `shows_link` says such a text shows one.
- **What it does not read** (it is a heuristic, not a parser): a name
  split by spaces alone (`evil com`); another top-level domain than
  `com` after `dot`, `(.)`, or a dot with white space on one side
  (`evil dot io`, `evil. net`): many are words that follow a full stop
  or `dot` in prose (`a dot in the name`, `vs. net`, `Done. info sent`);
  a number in hexadecimal (`0xC0A80101`, which a
  browser takes as an address, but which error codes are written as); an
  IPv6 address; and digits written as words. Where typed text must never
  carry a hint of a place to go, check it in `on_typed_text` your own
  way, or do not show it.
- **`shows_link(text, length=LINE_LENGTH)`** (since 2.10) reads stored
  text a screen shows as `laid_out` reads typed text on a line: whole,
  with the spaced readings, and as the line shows it (`fitted(text,
  length)`: escaped, cut to `length` characters and `…`), without them.
  Reading the cut with them makes prose a name: `approved. commercial`
  cut to `approved. com…`. A project hides or refuses a stored name, a
  title, or a note that `shows_link` says a screen would show as a link.
- **`numbers=False`** (since 2.10), on `holds_link` and `shows_link`, for
  a field whose digits are a licence's or a file's number
  (`No. 123 456 789`, `2026/0045`): seven digits or more are a number to
  call only as an amount or a code is, with a plus sign, a bracket, or
  beside a word that asks for a call. Links and addresses are read as
  ever.
- **`on_typed_text`** runs a project's own check on each typed text, as
  typed, whatever the setting: raise to refuse the act. One text may be
  checked more than once (at a twin's two screens, then at the act), so a
  check reads and has no side effect. `check_typed(text)` runs the checks
  where text is typed and shown later, each cut a line may show
  included: from the twentieth character (to the sixty-third, a line's
  length, since 2.10). Since 2.10 it also reads the text whole as a
  screen prints it: a character it does not print (a word joiner, a soft
  hyphen, a zero-width space, a bidirectional control) shows escaped,
  and ends a name before it (`evil.com` and a word joiner).

```python
from domainkit.apps import DomainConfig
from domainkit.contrib.auth.screens import on_typed_text
from domainkit.ddd import BusinessRuleViolation


def no_shouting(text: str) -> None:
    if text.isupper():
        raise BusinessRuleViolation("Write it in lower case.")


class AccountsConfig(DomainConfig):
    def ready(self) -> None:
        on_typed_text(no_shouting)
```

## A use case as the act

*New in 2.5* ([ADR 0161](../adr/0161-authorize-a-use-case-as-a-copy-and-read-what-is-shown-in-its-unit-of-work.md)).
In the simple layout, a use case is a `@use_case` in `controllers.py`,
which may not import the unit of work on the database (`models.py`).
The action lives beside it all the same: its `command` is the use case,
and its `show` takes the unit of work the act runs in, and reads
through its port:

```python
# controllers.py
from domainkit.contrib.auth.actions import Action, Shown
from domainkit.ddd import use_case


@use_case
async def end_membership(uow: OrganizationsUnitOfWork, membership_id: UUID) -> None:
    """End someone's membership of an organization."""
    membership = await uow.memberships.get(MembershipId(membership_id))
    membership.end()


async def show_ending(command, uow: OrganizationsUnitOfWork) -> Shown:
    membership = await uow.memberships.get(MembershipId(command.membership_id))
    return Shown("End a membership", {"Member": membership.display_name})


END_MEMBERSHIP = Action(
    "organizations.end_membership",
    command=end_membership,
    subject=lambda command: f"organizations.membership:{command.membership_id}",
    show=show_ending,
)
```

`requires_authorization(END_MEMBERSHIP)` given a use case returns a copy
whose `run`, and so its call, spends the authorization. The use case
itself stays as it was. Give the copy to the route that asks, and the use
case to the routes that run it without an act, such as the phone's,
where the user's own device is the proof:

```python
# routes.py
memberships.use_case(
    requires_authorization(controllers.END_MEMBERSHIP)(controllers.end_membership),
    method="DELETE",
    rule=StepUp(for_=controllers.END_MEMBERSHIP),
)
phone_memberships.use_case(controllers.end_membership, method="DELETE")
```

- **The twin lends the route's unit of work.** A `show` with a
  parameter named `uow` is given it open, as one whose `commit` is
  refused, and it is left without a commit: `show` only reads. A block
  `async with uow:` in it enters nothing. A `ResourceRouter`'s twin
  gives its route's; a route of your own names the class, and a new one
  is made: `request_action_authorization(request, END_MEMBERSHIP,
  command, unit_of_work=OrganizationsUnitOfWork)`. Without it, asking is
  refused with `ImproperlyConfigured`.
- **The route that asks runs the copy.** A `ResourceRouter` refuses,
  when it declares the route, one that asks `StepUp(for_=...)` and runs
  the use case itself, or a service that spends another act's
  authorization: it would run the act with arguments nobody confirmed. A
  service left undecorated warns (`DomainkitDeprecationWarning`): it
  may call one that is decorated, as in 2.4, and 3.0 refuses it.
- **One command.** A use case is guarded only for its own command, not
  for a class it derives from (`ImproperlyConfigured`).
- **A `show` of the command alone**, or with parameters of its own
  (`language="en"`), is called as before, and opens what it reads
  itself.

## The record it decides on

*New in 2.7* ([ADR 0174](../adr/0174-bind-an-act-to-the-record-it-decides-on-and-keep-a-held-acts-authorization.md)).
The digest covers the command: the ids it names, never what is stored
under them. An approval confirmed for a document as it read when the
screen showed it would decide on whatever is stored when the act lands:
another file uploaded meanwhile, a text rewritten and sent again. An
action's `binds` reads what the act decides on, and the authorization
holds only while it reads the same:

```python
async def bound_document(command, uow: DocumentsUnitOfWork) -> int:
    # Locked until the act commits (PostgreSQL); see "Races" below.
    document = await uow.documents.get_for_update(DocumentId(command.document_id))
    return document.version


APPROVE_DOCUMENT = Action(
    "documents.approve",
    command=approve_document,
    subject=lambda command: f"documents.document:{command.document_id}",
    show=show_document,
    binds=bound_document,
)
```

- **Read where `show` reads, then again where the act runs.** Asking
  reads it in the act's unit of work, the one `show(command, uow)` is
  lent, first: what is bound is what the screen was built from. The act
  reads it again when its service opens its unit of work (`async with
  uow`), before the service reads anything. A service that commits with
  no block open reads it as it commits. `spend_authorization(uow, ...)`
  reads it in `uow`, open: call it before the act changes what it binds.
- **One committed transaction.** A service whose act binds reads and
  decides in one block of its unit of work, and commits it once:
  - Each block opens its own transaction, as everywhere, and is read
    first, until one has committed. A block that rolled back (a run that
    `uow.run(...)` retries after a lost race, a failure the service
    caught) committed nothing: the next block is read again, and refused
    if the record changed.
  - The first commit makes the act, whatever it wrote, and spends the
    authorization. A block opened after it, or a second commit, raises
    `ImproperlyConfigured`, before it does anything: the act was made,
    and the client gets `500 internal_error` all the same. Read what was
    saved in the same block, before its commit, or answer from what the
    act decided.
  - A block opened while another of the act's is open, inside it (a
    helper's) or by another task (under `asyncio.gather`), raises
    `ImproperlyConfigured` before it opens anything, and the act then
    commits nothing: the open block's commit raises too, and it rolls
    back.
  - Work that must persist apart from the act (an audit row, a notice)
    belongs to a unit of work of its own class, or after the act: the
    authorization's own trail and spend work that way already. An act
    that binds nothing opens its unit of work as before 2.7.
- **Only the act's unit of work.** A decision the service makes in
  another unit of work, one it opens itself, is not read against the
  screen: decide in the one the act is given.
- **On another database** than auth's, the authorization is spent just
  before the act, as for any act there; one block still decides.
- **The digest covers it.** The row keeps the SHA-256 of the value, as
  canonical JSON, and the act's digest is of the command and that: the
  phone signs the record as it was shown, and the trail's digest names
  it. A value the row was changed to fails the code.
- **Another value refuses the act**: `409 action_record_changed`, and
  the authorization is voided (the trail: `record_changed`). Ask again:
  the screen shows the record as it is now. On another database than
  auth's, the authorization was spent just before the act, as for any
  act there.
- **A record gone**: a `binds` that raises `NotFound` voids the
  authorization too, which could never be spent rightly, and the act
  answers the `NotFound` (`404`). Any other error of `binds` is answered
  as it is, and voids nothing.
- **What to bind.** An aggregate's `version`, which every stored change
  raises, binds every field at once. A record without one binds the
  fields the act decides on (`{"sha256": file.sha256, "number":
  file.number}`), or a digest of them; any value pydantic writes as JSON.
  A set is read sorted, as the command's are, so a twin and an act on
  two workers agree. Bind what the user was shown, and what the act
  reads to decide.
- **Races.** Between the check and the act's commit, another request may
  change the record. What holds it off depends on the database:
  - **PostgreSQL**: `binds` reads it with `get_for_update`, as above. The
    row stays locked until the act's transaction ends: a writer waits,
    and the service, reading it any way, decides on what was bound.
  - **SQLite has no row locks**: `get_for_update` locks nothing there,
    and another connection may change the record meanwhile. What remains
    is the aggregate's `version`, for an act that writes the record: the
    repository hands the service the aggregate `binds` loaded, and the
    save of a version that no longer holds fails. An act that does not
    write the record has no guard against such a change on SQLite.
  - **A race the version catches** answers `409 concurrent_update`, as
    any lost race does: nothing commits, and the authorization is not
    spent, nor voided. Sent again (or run again by `uow.run`), `binds`
    reads the changed record: `409 action_record_changed`, voided.
  - **A service that reads it again with `get_for_update`**, after a
    `binds` that read it without a lock, loads the newest row and
    decides on it, past the check: lock it in `binds` instead.
- **Mistakes refused**: a `binds` that is not callable
  (`ImproperlyConfigured` when declared), that answers what JSON cannot
  hold (bytes that are not UTF-8, a lone surrogate, NaN, an infinity),
  or that commits; asking without the act's unit of work
  (`unit_of_work=`); a service that takes none, or never opens it.

## The version the page read

*New in 2.8* ([ADR 0178](../adr/0178-bind-an-act-to-the-version-its-page-read.md)).
`binds` holds the record as the twin reads it, and the page read it
before: a file replaced in between, under the same name, is bound as
replaced, and the phone shows the same lines for both. The page knows
which version it showed, its `ETag`. Sent as `If-Match`, the twin and
the act hold to it:

```http
GET  /documents/42                           200 ETag: W/"3-5d41402abc4b2a76"
POST /documents/42/approve/authorization
     If-Match: W/"3-5d41402abc4b2a76"        412 precondition_failed (now version 4)
GET  /documents/42                           200 ETag: W/"4-0f8e2a6c91d3b745"
POST /documents/42/approve/authorization
     If-Match: W/"4-0f8e2a6c91d3b745"        201 {"id": "9f0c...", ...}
POST /documents/42/approve
     If-Match: W/"4-0f8e2a6c91d3b745"
     Action-Authorization: 9f0c....mYd3...   200
```

- **The twin holds it as its act does.** On a `ResourceRouter`'s act
  on one aggregate, after the route's rules, the reach check, and the
  rules on the aggregate, an aggregate not at the version `If-Match`
  names is `412 precondition_failed`: nothing is stored, and the phone
  is never asked. The unit of work the twin lends to `show` and `binds`
  is held to it too: what they load through it at another version is
  `412`, before anything is stored.
- **One tag.** A page reads one version, so a twin takes one tag: a
  list of several, even of one version, however many, or several
  `If-Match` lines, which are one list, is `422`. A tag that names no
  version, or a malformed value, matches nothing (`412`). Tags are
  compared by the version they carry, weak or strong
  ([resources.md](../resources.md#versions-and-conditional-requests)).
  `*`, or an empty value, names no version, and counts as none.
- **The digest covers it**, beside the arguments and what `binds` read.
  The act's `If-Match` must name the same version, however written
  (`W/"4-…"` or `"4"`). Another, several, `*`, or none is `403
  action_not_authorized`, and voids the authorization, as other
  arguments do.
- **At the act**, `If-Match` holds the write as before (`412`), and
  `binds` reads the record again (`409 action_record_changed`). A
  `binds` that loads it at a version the authorization does not hold
  voids it, and the act answers `412`: it will never be at that version
  again.
- **Without `If-Match` at the twin**, the act is bound as in 2.7: an
  `If-Match` sent to the act alone holds its write and nothing else, and
  an authorization stored before 2.8 holds. A twin on the collection
  reads no `If-Match`, as its act reads none.
- **Approvals' twins** (approve, reject, cancel) pass it on too: an
  approval asked with the request's `ETag` is made only with the same
  `If-Match`.
- **`Action(require_if_match=True)`** asks for it. The twin without
  `If-Match`, with `*`, or with an empty one, is `428
  precondition_required`, before anything is stored. The act without
  one is `428` too, and voids nothing: send it again with the tag. An
  authorization asked without one is refused (`403`, voided). Only for
  an act on one aggregate: a route on the collection whose rule is
  given the action is refused when it is declared
  (`ImproperlyConfigured`).

**Only the aggregate the path names.** `If-Match` names a version of the
aggregate the act's path names, and nothing else. A `binds` that reads
another record (a document's file, kept as an aggregate of its own) binds
it as the twin reads it, as in 2.7: a file replaced between the page and
the twin is bound as replaced. Put the act on that record's own path, or
let the page send the version of it that it read, in the act's body,
which the digest covers, and have `binds` refuse another, reading it
locked as above:

```python
class ApproveDocument(Command):
    document_id: UUID
    file_id: UUID
    file_version: int  # The version of the file the page showed.


async def bound_file(command: ApproveDocument, uow: DocumentsUnitOfWork) -> int:
    file = await uow.files.get_for_update(FileId(command.file_id))
    if file.version != command.file_version:
        raise PreconditionFailed("The file changed since you read it.")
    return file.version
```

At the twin, that is `412` before anything is stored; at the act, it
voids an authorization asked with `If-Match`.

**A route of your own** reads `If-Match` itself, as its act already must
to hold its write: the framework does not know which record your route
names. Its twin holds the unit of work that `show` and `binds` read in
to the version, lends it, and passes the header on:

```python
from typing import Annotated

from fastapi import Header, Request

from domainkit.contrib.auth import AuthenticatedUser
from domainkit.contrib.auth.actions import (
    ActionAuthorizationOut,
    request_action_authorization,
)
from domainkit.resources import asked_versions


@router.post("/documents/{document_id}/approve/authorization", status_code=201)
async def ask_to_approve(
    document_id: UUID,
    request: Request,
    user: AuthenticatedUser,
    if_match: Annotated[str | None, Header()] = None,
) -> ActionAuthorizationOut:
    uow = DocumentsUnitOfWork()
    with uow.expect(Document, DocumentId(document_id), asked_versions(if_match)):
        asked = await request_action_authorization(
            request,
            APPROVE_DOCUMENT,
            ApproveDocument(document_id=document_id),
            unit_of_work=lambda: uow,
            if_match=if_match,
        )
    return ActionAuthorizationOut.of(asked)
```

- `asked_versions(if_match)` reads the header as a twin does: none for
  no value, an empty one, or `*`; `422` for several tags.
  `request_action_authorization` takes several `If-Match` lines of the
  request as one list, so they are `422` too.
- `binds` (or `show`) loads the record through `uow`, so its version is
  checked there. **A twin whose `show` and `binds` checked nothing is
  refused** (`ImproperlyConfigured`, `500`), before anything is stored:
  a tag nobody compared with the record must not reach the phone. Load
  the record through `uow` first if they do not.
- The act's rule, `StepUp(for_=...)`, reads the act's own `If-Match`.
  Its route holds its unit of work to it, as any conditional write does
  ([A route of your own](../resources.md#versions-and-conditional-requests)).
  **An act whose unit of work is not held to the version its
  authorization holds is refused** (`ImproperlyConfigured`, `500`),
  before it spends anything or reads the record.
- A rule of your own that asks (`AsksAuthorization`) is given the
  header when its `ask_authorization` takes `if_match`;
  `StepUp(for_=...)` does. A twin says in OpenAPI that the act is bound
  to its `If-Match` only where its rule is given it.

## The route asks: `StepUp(for_=...)`

`StepUp(for_=...)` takes the `Action`, or its purpose, in place of
`max_age`; never both, and no `methods`: the action names them. It is an
`ActionStepUp`, a rule like any other, combined with `&`.

- No user, or staff acting as the user (an impersonation): refused, as
  `StepUp` refuses.
- No `Action-Authorization` header: `401 authorization_required`. The
  `WWW-Authenticate` header says so as RFC 9470 writes it, with the
  purpose; the body names the purpose too, as `errors[0].msg`.
- A header that does not hold: `403 action_not_authorized`, which never
  says why: unknown, another user's or another login's, for another
  purpose, unconfirmed, spent, expired, or a code that does not cover the
  row.
- Otherwise the rule allows, after one query by id, and keeps the
  authorization on the request for the act. It writes nothing: a rule may
  be asked more than once, beside other rules, and a later one may refuse.

A `ResourceRouter` gives each command route that asks a twin, `POST
<the route's path>/authorization`, which builds the command as the route
does and answers `201` with the authorization: its `id`, `purpose`,
`shown`, `expires_at`, and `methods`, what confirming it must prove
(*new in 2.5*). A page reads `methods` to offer the ways that may
confirm it, a code, the phone, or a passkey, without asking `GET
/auth/authorizations/{id}` again. Where routes of one path ask by
several methods, each twin's path names its method,
`<path>/authorization/delete`
([resources.md](../resources.md#authorizations-for-one-act)). A route of
your own gets a twin of your own, that builds the command and calls
`request_action_authorization`:

```python
from fastapi import Request

from domainkit.contrib.auth import AuthenticatedUser
from domainkit.contrib.auth.actions import (
    ActionAuthorizationOut,
    request_action_authorization,
)


@router.post("/memberships/{membership_id}/end/authorization", status_code=201)
async def ask_to_end(
    membership_id: UUID, request: Request, user: AuthenticatedUser
) -> ActionAuthorizationOut:
    command = EndMembership(organization_id=ORGANIZATION, membership_id=membership_id)
    asked = await request_action_authorization(request, END_MEMBERSHIP, command)
    return ActionAuthorizationOut.of(asked)
```

Asking needs credentials without scopes, which can be told apart from the
user's others: a login token, a browser session, or a JSON Web Token login
(`400 step_up_unsupported` otherwise). It needs a user with a way to
confirm what the act asks: a confirmed authenticator app for a code, or,
with [devices](devices.md#confirming-one-act), a bound device or a
passkey (`409 no_second_factor` otherwise). A login holds at most ten
unconfirmed authorizations: asking for an eleventh ends the oldest.

`check` refuses `StepUp(for_=...)` on a WebSocket or an event stream
(`auth.E111`): a connection is asked again and again, and has no act to
spend it on.

## Confirming it

`GET /auth/authorizations/{id}` shows an authorization to the login that
asked: `{"id", "purpose", "shown", "expires_at", "confirmed", "methods"}`.
Another login, even the user's, gets `404`.

`POST /auth/authorizations/{id}/confirm` takes `{"code"}`, and
`"password"` where the act asks `pwd`, as a transaction is confirmed: wrong
codes count against `AUTH.MFA.RATE`, wrong passwords against
`AUTH.LOGIN_RATE`. It answers `{"authorization": "<id>.<code>",
"expires_at"}`, for the act's `Action-Authorization` header.

What confirming must prove is the action's `methods`, or
`AUTH.AUTHORIZATIONS.METHODS` (`["otp"]`). A bound device's key (`hwk`)
and a passkey (`mfa`) stand for `otp`, as for a step-up. `phone` is a
confirmation on a bound device's own screen: the phone's approval, or the
app's own act. A code never proves it, nor does a passkey; `["phone"]`
takes those two only. A code where the act asks the phone is `409
authorization_needs_other_factors`.

Only the phone has a screen the session does not control. A code proves
the user is there, and binds the confirmation to this authorization; what
the user read was on the page.

### On the phone

With [sign-in approvals](ciba.md) installed, the user's phone confirms
an authorization of the session's: the page asks it, the phone shows the
act as the server keeps it and signs its digest, and the page redeems
the authorization ([Confirming one act](ciba.md#confirming-one-act)):

```http
POST /auth/app-sign-in/authorizations/9f0c...            200 {"request": "kq3D1...", "expires_in": 120,
                                                              "interval": 5, "match_code": "47"}
POST /auth/app-sign-in/authorizations/token {"request"}  400 authorization_pending, then
                                                         200 {"authorization": "9f0c....mYd3...", "expires_at": "..."}
```

- The request is counted and bounded as the phone's step-up is
  (`START_RATE` per user, `MAX_PENDING`, `NUMBER_MATCHING`, `409
  phone_unavailable`), and only the session that asked redeems it, or
  withdraws it (`POST /auth/app-sign-in/authorizations/cancel
  {"request"}`, since 2.5): unanswered, the phone no longer lists it,
  and the authorization stays to be confirmed; approved, it stays to be
  redeemed. A user's unanswered asks of the last `REQUEST_TTL`,
  withdrawn ones too, are at most twice `MAX_PENDING`.
- The phone lists it with an `authorization` object: the purpose, the
  kept title and details, what confirming it must prove (`methods`),
  and the digest, read from the row.
- It approves with the digest, in a request its device signs: another
  digest denies it (`409 authorization_changed`). The row keeps the
  device, its key, the bytes it signed, its signature, and when.
- It proves `hwk` and `phone`, for every user with a bound device: an
  act whose methods are `["phone"]` takes it, and asking needs no
  authenticator app.

### The mobile app's own act, and a passkey

With `domainkit.contrib.devices` installed, two more ways confirm an
authorization ([Confirming one act](devices.md#confirming-one-act)):

- **The mobile app's own act**: `POST /auth/devices/authorizations/{id}`
  `{"digest"}`, signed by one of the user's bound devices, for an act the
  app makes itself and shows itself. The authorization must be the app's
  own login's, the login that asked and spends it: another session's is
  `404`, and goes to the phone. A digest other than the act's is `409
  authorization_changed`. It proves `hwk` and `phone`, and the device's
  key, what it signed, and its signature are kept on the row, as the
  phone's approval is.
- **A passkey**: `POST /auth/passkeys/authorizations/{id}/options`
  answers a challenge made for this authorization, linked to its row, and
  `POST /auth/passkeys/authorizations/{id}` takes the assertion. Only the
  session that asked uses either; the challenge works once, for this
  authorization alone. It proves `hwk` and `mfa`, never `phone`.

Each answers `{"authorization": "<id>.<code>", "expires_at"}`, as a code
does.

## The act spends it

The decorated service takes the authorization the rule kept, computes the
digest and the subject again from the command it is given, and spends it:

- **With the act**, when the service's unit of work is on auth's
  database: the spend joins its transaction and commits with its first
  commit, with `ActionAuthorizationUsed` among its events. It is spent if
  and only if the act commits, so an act refused on the way (`409
  concurrent_update`, a business rule) can be sent again, until the
  authorization expires.
- **Before the act**, otherwise: in a transaction of its own, just before
  the service runs. At most once: an act that then fails needs a new
  authorization.

Around it:

- **Other arguments, or another subject**, void the authorization, in a
  transaction of its own, and the act is refused (`403
  action_not_authorized`). Fields of `ignore` may differ.
- **An uploaded file** (a `StoredUpload`, as `upload_route` gives one)
  counts by its content: its type, size, file name and SHA-256, never
  the name the storage gave it, which is new on every request (*New in
  2.5*; [resources.md](../resources.md#files)). Another file is other
  arguments.
- **A decorated service called with no authorization for its purpose**
  (from another route, a batch, a task) raises `AuthorizationRequired`
  (401): the act is the user's own. Code that acts for the system calls a
  service of its own.
- **`spend_authorization(uow, action, command)`** does the same in code
  that is not a service: in the open transaction of `uow` on auth's
  database, else now, alone. Say so once, where the action is declared,
  with `spent_in_code(action)` (since 2.5): `check` then knows something
  spends it.
- **An authorization the rule accepted that no service spent** is voided
  when a response that succeeded starts, and logged on the security log
  (`auth.authorization_unspent`, with the route). Under `DEBUG` and in
  tests, the request fails (`500 authorization_unspent`), so a forgotten
  decorator shows in your tests. A response that failed (a body that does
  not validate, another rule's refusal) leaves it: no act ran.

`check` refuses a route that asks for a purpose no registered action has
(`auth.E109`), or whose action nothing spends (`auth.E110`): no service
decorated `requires_authorization`, and no code that said so with
`spent_in_code(action)`. `check` takes the declaration at its word: code
that declares an action and never spends it passes. The request still
does not: an authorization a rule accepted and nothing spent is voided
when the response starts, and logged (`auth.authorization_unspent`);
under `DEBUG` and in tests, the request fails with `500
authorization_unspent`, so the project's tests show it.

```python
from domainkit.contrib.auth.actions import spend_authorization, spent_in_code

spent_in_code(END_MEMBERSHIP)  # Once, beside the action.


async def end_membership_now(command: EndMembership) -> None:
    async with OrganizationsUnitOfWork() as uow:
        await spend_authorization(uow, END_MEMBERSHIP, command)
        membership = await uow.memberships.get(command.membership_id)
        membership.end()
        await uow.commit()
```

## How it is bound

- **The row**: `domainkit_authorizations`, on auth's database. Its id is
  a random nonce; it keeps the user, the login's digest, the purpose, the
  subject, the act's digest, what is shown, when it was asked and expires,
  when it was confirmed and with what (`amr`), the confirming device with
  its key, what it signed, and its signature, when it was spent, and the
  links to the phone's request and to a passkey's challenge.
- **The digest**: `domainkit.ddd.fingerprint(purpose, arguments)`, the
  SHA-256 of `{"action": <the purpose>, "arguments": ...}` as canonical
  JSON: the function approvals computes its fingerprints with. For an
  action that `binds`, the SHA-256 of `{"action", "arguments", "binds"}`,
  where `binds` is the SHA-256 of what it read, which the row keeps
  (`bound`). Asked with `If-Match` (since 2.8), the SHA-256 of
  `{"action", "arguments", "binds", "if_match"}`, where `if_match` lists
  the versions it names, sorted, and `binds` is null for an action that
  binds nothing.
- **The code**: an HMAC-SHA256, under a key derived from `SECRET_KEY` for
  `auth.authorization` (its fallbacks verify), of a JSON array of
  `domainkit.authorization.v1`, the id, the user, the login's digest, the
  purpose, the subject, the digest, the SHA-256 of what was shown, and the
  expiry. It is never stored, and a row changed after its confirmation
  fails it.
- **Once**: one `UPDATE ... WHERE consumed_at IS NULL` spends it. Of two
  acts at once, one wins.
- **The login**: only the login that asked spends it, sees it, and
  confirms it with a code. Another login of the same user cannot.
- **Expiry**: `AUTH.AUTHORIZATIONS.TTL` (5 minutes, 30 at most), from
  asking to spending; an act's own entry in `AUTHORIZATIONS.ACTIONS`,
  or its `ttl`, takes its place. An act held for approval may be kept
  longer ([below](#held-for-approval)).

## Held for approval

*New in 2.7.* A change [approvals](../contrib/approvals.md) holds for a
second person (`403 approval_pending`, a guard `by_maker` such as
`ADMIN_APPROVALS`'s) is made, once approved, by its maker's same call. Its
authorization was confirmed and checked, and nothing spent it, but it
expired with its `TTL`, five minutes, long before the approver came, so
the maker confirmed the change a second time.

With `AUTH.AUTHORIZATIONS.HELD_TTL` set (`timedelta(days=1)`, a week at
most), approvals keeps it instead, for that request. The maker's call,
once the request is approved, spends it as any act does, once.

- **For that request, while it waits.** The authorization works while
  the request is pending, or approved and not yet carried out, and
  within its time; at most `HELD_TTL` past the authorization's own
  expiry. Once the request is rejected, withdrawn, expired, failed, or
  carried out, the hold is over, and the authorization with it, its own
  five minutes left or not: it never submits another request. The row
  keeps what it is held for (`held_for`, `approvals.request:<id>`) and
  until when (`held_until`).
- **Only what an act took, and did not spend**: the authorization the
  held call carried, checked against that change. Its code still covers
  its own expiry, and the hold counts only within `HELD_TTL` of it, so a
  lowered setting shortens the holds made before.
- **Where it holds**: the admin API's changes (`ADMIN_APPROVALS`), and a
  service of the project's decorated `requires_authorization` outside
  `requires_approval(by_maker=True)`, its act on auth's database. On
  another database the authorization is spent before the act, and a
  guard outside `requires_authorization` refuses before the act takes
  it: as in 2.6, the maker confirms again.
- **Confirming is as short as ever.** `TTL` still bounds the time from
  asking to confirming, as OWASP's transaction authorization asks; only
  a confirmed authorization of an act that waits is kept.
- **`None`, the default**, keeps 2.6's behaviour: the maker confirms
  again, once approved, with a new authorization.
- **It serves that request alone** (since 2.8, [ADR
  0179](../adr/0179-bind-the-admin-acts-groups-by-id-and-keep-a-held-confirmation-to-its-request.md)).
  The call made again builds its command anew. Where that changed since
  (a user made active again who was put in a group meanwhile), it is
  another request: approvals passes over the requests it may not serve,
  and refuses a call that would wait on, or submit, any request but the
  one the authorization is kept for (`403 action_not_authorized`, the
  trail's `held_elsewhere`); a new confirmation, which shows the change
  as it is now, submits it. In 2.7 the call submitted it, and once it was
  approved, the authorization kept for the first request, which waited
  still, made it (user report A17). Nothing is written: the
  authorization still serves its own request.
- **The request is asked at the rule.** Whether it still waits is read
  when the route's rule reads the authorization, before the act. A
  request rejected in that moment, before the maker's call reaches
  approvals, lets that call submit one request more; the authorization
  is held for the first only, so the new one needs a confirmation of its
  own once approved.
- Code that holds an act of its own for approval says so with
  `hold_authorizations(until, held_for="<app>.<kind>:<id>")`, from
  `domainkit.contrib.auth.actions`, in the request of the held call (it
  is written as the response starts), and registers an `on_held_act`
  hook, `async (held_for) -> bool | None`, that answers whether it still
  waits (None for another app's). A hold no hook answers for is over.
  `held_for` holds 1 to 200 characters (`ValueError` otherwise). Since
  2.8 it passes over what `held_elsewhere(held_for)` says the call's
  authorization may not serve, and before the call waits on, or submits,
  what it holds, it calls `refuse_held_elsewhere(held_for)` (None for one
  it is about to submit): an authorization kept for something else is
  refused there.

## The trail, privacy, and the purge

- `auth.action_authorized` (`ActionAuthorized`) when one is confirmed, and
  `auth.action_authorization_used` (`ActionAuthorizationUsed`) when one is
  spent: the user, the authorization, the purpose, the subject, the
  digest, the methods, and the device. The digest lets an auditor match an
  act to its authorization.
- Refusals are `auth.authorization`, with `invalid_authorization`,
  `invalid_code`, `changed`, `record_changed`, `used`, `unspent`, or
  `held_elsewhere` (since 2.8).
- Neither the arguments nor what was shown reach the trail: either may be
  personal data.
- Authorizations past their expiry are purged hourly and by
  `cleartokens`. Deleting or erasing a user deletes theirs, and `privacy
  export` lists them: the purpose, the subject, and the times.

## Approvals: authorizing each approval

[Approvals](../contrib/approvals.md) asks its approvers for a step-up, if
anything. With `APPROVALS["APPROVE_ACTION"]` on, approving a request asks
for an authorization of approvals' own action, `approvals.approve`, in
place of the step-up window:

- its arguments are the decision (`ApproveRequest`): the request's id,
  the approver, and the fingerprint they state. Its subject is
  `approvals.request:<id>`;
- what is shown is built from the stored request: "Approve a request",
  the action, and what the request's guard shows (its `show`), or its
  arguments by name, laid out on the screen
  ([What a screen shows](#what-a-screen-shows));
- the approve route's twin, `POST /requests/{id}/approve/authorization`,
  asks for one, and refuses what approving would refuse: the maker, a
  request no longer pending, a fingerprint that is not the request's;
- `approve_request` spends it, with the approval's commit where the
  requests share auth's database.

The other approve rules still apply; one that holds a `StepUp` is
refused by `check` (`approvals.E001`). See
[Approving with an authorization](../contrib/approvals.md#approving-with-an-authorization).

## The framework's own acts: the admin API and approvals

Since 2.5 ([ADR 0163](../adr/0163-authorize-admin-changes-and-requests-steps-as-acts.md)), the framework's back-office acts have built-in
actions too, each off by default. Turned on, each route asks for an
authorization of its act in place of a step-up window, and has a twin
that asks for one, so a project needs neither a window on those routes
nor the phone's free-text step-up
([deprecated](ciba.md#the-pages-words-and-a-window-deprecated)): the
phone confirms each act as it confirms any other.

| Setting | Action | The act, and what is shown | Twins |
| --- | --- | --- | --- |
| `AUTH["ADMIN_ACTION"]` | `auth.admin_change` | A change of the admin API: its kind, the user or group, and what it sets, adds, and takes away | `<the route's path>/authorization` on each changing route; `.../authorization/patch` and `.../authorization/delete` where a `PATCH` and a `DELETE` share a path ([the admin API](routes.md#each-change-authorized-as-one-act)) |
| `APPROVALS["SUBMIT_ACTION"]` | `approvals.submit` | Submitting a command: the action, and the arguments as stored | `POST /requests/authorization` |
| `APPROVALS["APPROVE_ACTION"]` | `approvals.approve` | Approving a request: the action, and the arguments | `POST /requests/{id}/approve/authorization` |
| `APPROVALS["REJECT_ACTION"]` | `approvals.reject` | Rejecting a request: the action, the reason, and the arguments | `POST /requests/{id}/reject/authorization` |
| `APPROVALS["CANCEL_ACTION"]` | `approvals.cancel` | Withdrawing a request: the action, the reason, and the arguments | `POST /requests/{id}/cancel/authorization` |

The admin API spends its act in its own code, with the change's
transaction, after the `on_admin_change` checks, and checks that it binds
the change before them. A change held for approval keeps its
authorization for the maker's call once approved, with `HELD_TTL` set
([above](#held-for-approval)). Setting a password, with the history on,
is the exception: it spends its authorization first, before any check
(since 2.7), whatever `HELD_TTL` says. The act binds only that a
password is set, so each confirmation is one guess at the user's
history, and a call that waits for approval spends its confirmation
too: confirm again to try again
([Each change, authorized as one act](routes.md#each-change-authorized-as-one-act)). Approvals spends each in its services, as
`approve_request` does ([Submitting, rejecting, and cancelling with an
authorization](../contrib/approvals.md#submitting-rejecting-and-cancelling-with-an-authorization)).
The project's own code takes those steps without one, through the
functions the services run, `record_submission`, `record_approval`,
`record_rejection`, and `record_cancellation` (since 2.6,
[From the project's own code](../contrib/approvals.md#from-the-projects-own-code)).

### Each act its own methods and lifetime

*New in 2.7.* The framework's acts are declared without `methods` or
`ttl`, so they take `AUTH.AUTHORIZATIONS.METHODS` and `TTL`: the same as
every act of the project. `METHODS` defaults to `["otp"]`, which a passkey
proves too; a project whose back office must confirm on the phone had to
ask the phone of every act. `AUTHORIZATIONS.ACTIONS` names, per act by
its purpose, its own `METHODS` and `TTL`, in place of the action's and of
the two settings:

```python
AUTH = {
    "ADMIN_ACTION": True,
    "AUTHORIZATIONS": {
        "ACTIONS": {
            "auth.admin_change": {"METHODS": ["phone"]},
            "approvals.approve": {"METHODS": ["phone"], "TTL": timedelta(minutes=10)},
        },
    },
}
```

It takes any registered act, a project's own too, where one deployment
asks more than another. An entry's `METHODS` replace an action's own,
even stricter ones: the deployment's word about one act is the last. Name
in it no less than the act needs. `check` refuses a purpose no registered action
has (`auth.E112`): a name mistyped, or an act whose setting is off,
would hold for nothing. Methods that no installed way to confirm proves
are `auth.E108`, as for an action's own.

## For other apps: confirming another way

An app that confirms its own way (the phone of
[sign-in approvals](ciba.md), a passkey of [devices](devices.md)) uses
`domainkit.contrib.auth.integration`:

| Name | What it does |
| --- | --- |
| `on_action_confirmation(ActionConfirmation(name, methods, available))` | Registers a way to confirm, in `ready()`: what it proves (`frozenset({"hwk", "phone"})`), and `available(user_id)`, whether a user has it now. Asking needs one the user has; `check` refuses an act none proves (`auth.E108`). Auth registers the code. |
| `action_authorization(authorization_id, user_id, *, login=None)` | The authorization, to show it: its purpose, `shown`, digest, expiry, user, and login's digest. `AuthorizationNotFound` (404) for none, another user's, or another login's. |
| `link_action_authorization(authorization_id, *, user_id, sign_in_request_id=None, passkey_challenge=None)` | Links what will confirm it: the phone's request, a passkey's challenge. |
| `linked_action_authorization(sign_in_request_id, user_id, *, login=None)` | The authorization a phone's request was linked to, as `action_authorization` answers it. |
| `confirm_action_with(authorization_id, methods, *, user_id, login=None, digest=None, device_id=None, algorithm=None, public_key=None, signature=None, signed=None, signed_at=None)` | Confirms it with what the app proved, and returns `<id>.<code>`. `digest` must be the act's (`409 authorization_changed`); `methods` must prove what it asks (`409 authorization_needs_other_factors`). The device's key, what it signed, and its signature are kept on the row, and with `domainkit.contrib.evidence` installed, on the evidence ledger, which outlives the row. |
| `confirmed_action_authorization(authorization_id, user_id, *, login=None)` | `<id>.<code>` of a confirmed authorization, unspent and live, for an app that confirms in one request and hands it over in another (the phone's token route). It is never stored: computed again from the row. |
| `login_digest(credentials)` | The login's digest, as the row and the phone's requests keep it. |
| `action_for(purpose)` | The registered `Action`; its `required()` is what confirming must prove. |
| `register_action(action)` | Registers an `Action`, in `ready()`. |

## Options

| `AUTH` key | Default | What it does |
| --- | --- | --- |
| `AUTHORIZATIONS.TTL` | 5 minutes | How long an authorization works, from asking to spending; 30 minutes at most. |
| `AUTHORIZATIONS.METHODS` | `["otp"]` | What confirming one proves, unless its action names others: `otp`, `pwd`, `phone`. |
| `AUTHORIZATIONS.WHOLE` | `False` | The framework's acts show what they do whole, or are refused (`422 too_much_to_show`); off, what does not fit is cut and counted. |
| `AUTHORIZATIONS.TYPED_LINKS` | `"shown"` | `"refused"`: text a person typed for the act that holds a link, an email address, or a number to call is refused (`422 link_not_shown`). |
| `AUTHORIZATIONS.HELD_TTL` | `None` | How long past its expiry an authorization is kept while its act waits for approval; a week at most. |
| `AUTHORIZATIONS.ACTIONS` | `{}` | Per act, by purpose, its own `TTL` and `METHODS`. |
| `MFA.RATE` | `"10/hour"` | Wrong codes per user, confirmations included. |
| `LOGIN_RATE` | `"10/minute"` | Wrong passwords, where the act asks `pwd`. |

[The settings tables](../auth.md#settings) give the full rows.

## Errors and refusals

| Status and code | When |
| --- | --- |
| `401 authorization_required` | The route asks `StepUp(for_=...)` and the request sent no `Action-Authorization`; a decorated service called with none (`AuthorizationRequired`). |
| `403 action_not_authorized` | Any authorization that does not hold for this act, these arguments, and this login. |
| `400 step_up_unsupported` | Asking with credentials limited to scopes, or that cannot be told apart. |
| `409 no_second_factor` | Asking, for a user with no way to confirm what the act asks. |
| `422 too_much_to_show` | Asking, for an act its screen cannot show whole (`TooMuchToShow`): a `show` that lays it out whole, or a framework act under `AUTHORIZATIONS.WHOLE`. |
| `422 link_not_shown` | Asking, for an act whose screen shows typed text that holds a link, an email address, or a number to call (`LinkNotShown`), under `AUTHORIZATIONS.TYPED_LINKS` refused. |
| `409 authorization_confirmed`, `409 authorization_expired` | Confirming twice, or one ended; confirming too late. |
| `409 authorization_needs_other_factors` | A code or a passkey where the act asks the phone; the phone asked for an act that asks the password. |
| `409 authorization_changed` | The phone, or another app (the app's own act), confirms a digest other than the act's. |
| `409 action_record_changed` | The action `binds` the record, and it reads otherwise than when the authorization was asked for; the authorization is voided. |
| `412 precondition_failed` | The twin, or the act, sent `If-Match`, and the record is not at the version it names; at the act, a `binds` that loads it so voids an authorization asked with `If-Match`. |
| `428 precondition_required` | The action asks for `If-Match` (`require_if_match`), and the twin or the act sent none, an empty one, or `*`. |
| `422 validation_error` | A twin's `If-Match` lists several tags: a page reads one version. |
| `404 authorization_not_found` | Another user's or another login's authorization. |
| `401 invalid_code`, `400 password_required`, `400 wrong_password`, `429` | As for a step-up. |
| `500 authorization_unspent` | Under `DEBUG` and in tests: a rule accepted one, and no service spent it. |

## Checks

| Id | What it refuses |
| --- | --- |
| `auth.E108` | An action whose methods no installed way to confirm proves (`["phone"]` with neither the phone's app nor devices). |
| `auth.E109` | A route that asks `StepUp(for_=...)` for a purpose no registered action has. |
| `auth.E110` | A route whose action nothing spends: no service decorated `requires_authorization`, and no code that said so with `spent_in_code(action)`. |
| `auth.E111` | `StepUp(for_=...)` on a WebSocket or an event stream. |
| `auth.E112` | `AUTH.AUTHORIZATIONS.ACTIONS` naming a purpose no registered action has. |

## Testing

*New in 2.5.* `domainkit.testing` authorizes an act through the
application's own routes, as `confirm_sca` confirms a transaction
([Moving money](../testing.md#moving-money-second-factors-approvals-the-trail-key-services)):

```python
from domainkit.testing import authorize_act, enrol_totp

app = enrol_totp(as_alice, PASSWORD)
act = f"/api/organizations/7/memberships/{membership_id}"
authorization = authorize_act(as_alice, act, app)  # the twin, then a code
as_alice.delete(act, headers={"Action-Authorization": authorization})
```

- `authorize_act(client, url, app=None, *, json=None, password=None,
  prefix="/auth", if_match=None)` asks `POST <url>/authorization` with
  the act's body, and `If-Match` if given (*new in 2.8*), and confirms
  the answer.
- `confirm_authorization(client, authorization_id, app=None, *,
  password=None, prefix="/auth")` confirms one asked elsewhere.
- With `app`, a code of the authenticator confirms it, and `password`
  where the act asks it. Without, auth confirms it in the test as
  proving the act's `methods`: what the phone or a passkey proves, for
  an act a code cannot confirm. Only under `ENVIRONMENT = "test"`; the
  client must be entered (the `client` fixture is).
- Asking is not skipped: the user needs a way to confirm what the act
  asks (`409 no_second_factor` otherwise), an authenticator app
  (`enrol_totp`), or for the phone a bound device.

## Common mistakes

- **The guarded copy on a route that does not ask.** Every call is
  refused `401 authorization_required`, and there is no twin to ask at.
  Put `StepUp(for_=...)` on the route that runs the copy.
- **The guarded copy where the use case should run unguarded**, or the
  reverse: the phone's route and tasks take the use case itself.
- **A `show` that puts more on the phone than the user needs to decide.**
  It may be personal data, and the user reads less of it.
- **An `ignore` list that unbinds what matters.** Leave out only what
  changes on every call. Never an uploaded file: it would authorize any.
- **A service that commits twice.** It spends with the first commit.
- **Spending in a route rather than in the service.** A rule that spent
  would burn the authorization when a later rule refuses, or the act fails.

## See also

- [Stepping up](step-up.md): the window, for acts whose arguments do not
  matter, and transactions authorized for one amount and payee.
- [Sign-in approvals](ciba.md): the user's phone.
- [Approvals](../contrib/approvals.md): someone else agrees, later; with
  `APPROVE_ACTION`, each approval is authorized as one act.
