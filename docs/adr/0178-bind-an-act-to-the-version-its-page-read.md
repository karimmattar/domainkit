# 0178. Bind an act to the version its page read

- Status: Accepted
- Date: 2026-11-17
- Amends: [0174](0174-bind-an-act-to-the-record-it-decides-on-and-keep-a-held-acts-authorization.md)

## Context

Since 2.7 an action may bind the record it decides on (ADR 0174):
`binds(command, uow)` is read when the twin is asked, and again at the
act, and the digest covers what the twin read. So the authorization
holds the record as the twin found it, not as the page showed it. A
project reported it against 2.7 (user report A25), proved by its audit:

- A reviewer opens file A; the company uploads B under the same name;
  the reviewer clicks Approve. The twin binds B, the phone shows the same
  lines for A and B (company, kind, number, file name), and B is
  approved. A template withdrawn, rewritten, and sent again while the
  checker had the first open is published the same way.
- The page knows which version it showed: a resource's answer carries
  its `ETag`, and `If-Match` holds the act's write to that version
  (`412`). But the twin reads no `If-Match`, and the digest does not
  cover it. The project's clients restate the version in each command
  (`read_version`), each act's `show` refuses another (`409`), and
  `binds` holds it until the act lands.

The project proposed either (a) the twin honouring the route's
`If-Match`, `412` before the phone is asked, with the digest covering
it; or (b) `binds(command, uow, request)`, to compare what the client
says it read.

### What others do

- **RFC 9110** [1]: `If-Match` makes a state-changing request
  conditional on the representation the client saw (13.1.1); a list of
  tags is true if any matches; `*` is true if there is a current
  representation; a false condition must not perform the method, and
  `412` says so. Preconditions are evaluated after the server's normal
  checks, so a refusal those make (`401`, `403`, `404`) comes first
  (13.2.1). Comparison is strong (8.8.3.2). domainkit's tags are weak
  and compared by the version they carry (ADR 0025): a representation
  varies by caller and language, the version does not.
- **RFC 6585** [2]: `428 Precondition Required` says the server requires
  the request to be conditional, against the lost update.
- **Google's AIP-154** [3]: a request that names an etag that does not
  match is refused; one without an etag is permitted, and a service may
  require one. **AIP-163** [4]: a request that only validates (as a twin
  only asks) performs the checks the live request would, and fails when
  it would fail.
- **OWASP's Transaction Authorization Cheat Sheet** [5] asks that the
  data be protected against modification between its entry and its
  authorization, and that a change reset the authorization (2.6).

### Options

| Option | What it costs |
| --- | --- |
| (b) `binds(command, uow, request)`, comparing what the client says | Each act's `binds` compares the client's statement with the record itself, as the project's commands do now: the workaround moves into `binds`. Its value depends on the request, so "the record changed" (`409`) and "another statement" read alike. A third parameter for a 2.7 `binds` to be told apart. |
| A field of the command (`read_version`), framework-named | A name every command must reserve, read from the body; `version` is generated and refused there. HTTP already has the header, and resources already check it. |
| **(a) The twin holds `If-Match` as its act does; the digest covers the versions it names; the act's rule reads the act's `If-Match`** | A twin's OpenAPI gains an optional header and `412`. A route of the project's own checks it itself, as its act does. |

Where the act's `If-Match` is covered:

| Option | What it costs |
| --- | --- |
| Always, with or without one at the twin | A 2.7 client that sends `If-Match` to the act alone, as 2.7 read it (every act of a resource with `require_if_match`), is refused. Breaking. |
| A column saying whether the twin held one | A migration, for what the digest already covers. |
| **The digest is computed with the act's versions, and, unless the action requires them, without** | At most two HMACs at the act. An authorization asked with `If-Match` holds only for an act that names the same versions; one asked without holds as in 2.7. |

Where `If-Match` is required:

| Option | What it costs |
| --- | --- |
| A resource's `require_if_match` extends to its twins | A 2.7 client of such a resource asks without `If-Match`: refused. Breaking. |
| **`Action(require_if_match=True)`, off by default** | The act's own word, on a resource's route or one of the project's; `428` at the twin and at the act. |

## Decision

- **A twin of an act on one aggregate reads `If-Match`**, as its act
  does (a `ResourceRouter`'s; at most 4096 characters, `422` past it).
  After the route's rules, the reach check, and the rules on the
  aggregate, the aggregate at none of the versions it names is `412
  precondition_failed`, before the rule is asked. The route's unit of
  work, lent to the rule, is held to the versions (`uow.expect`), so
  what `binds` and `show` load through it is checked again: a change
  made in between is `412` before anything is stored. Versions are read
  as a write reads them: by the number a tag carries, weak or strong,
  quoted or not; a tag that names no version, a malformed value, or more
  than 20 tags match nothing. A twin takes **one tag** (`asked_versions`):
  a page reads one version, and a twin that took `"1", "2"` bound an act
  made on version 2, which no page had read (found in review). Several
  are `422`, as a header out of bounds is (the 4096 characters a route
  reads): not `412`, since no condition was found false, nor a new code.
  `*`, or an empty value, names none. A twin on the collection reads
  none, as its act reads none. A loaded aggregate the expectation does
  not match (an id of another type) is an error, as at the act, never a
  pass.
- **The rule is given it.** An `ask_authorization` that takes
  `if_match` by name is given the header; `StepUp(for_=...)` does and
  passes it on, and so do approvals' approve, reject, and cancel rules,
  whose acts are command routes that hold `If-Match`. A rule of 2.7 is
  asked as before, and still gets the `412`. The twin's OpenAPI says
  the act is bound to the tag only where the rule is given it.
- **The digest covers it.** `request_action_authorization(...,
  if_match=...)` computes the digest with the versions it names, sorted:
  the SHA-256 of `{"action", "arguments", "binds", "if_match"}` as
  canonical JSON, `binds` null for an action that binds nothing.
  Without `If-Match`, or with `*`, the digest is 2.7's, so an
  authorization stored before holds. `Action.digest_of(command, bound,
  versions)` computes it.
- **The act's rule reads the act's `If-Match`.** Its digest is computed
  with those versions, and, unless the action requires `If-Match`,
  without: an authorization asked with versions holds for an act that
  names the same ones, however written (`W/"3-…"` and `"3"`); another
  `If-Match`, several, `*`, an empty one, or none is other arguments:
  `403 action_not_authorized`, voided. One asked without holds as in
  2.7, with or without an `If-Match` at the act, which holds the write
  as before.
- **Fail closed where a step is forgotten** (found in review). Asking
  with `if_match` is refused (`ImproperlyConfigured`) unless the unit
  of work it lends holds an expectation of those versions that a load
  checked (`UnitOfWork.expectations`); a `ResourceRouter`'s twin marks
  its own as checked, having checked the aggregate itself. The act is
  refused, before it spends or reads anything, unless its unit of work
  is held to the versions its authorization holds. Either is the
  route's mistake, not the client's: a `500` names it, and the
  confirmation is kept, where a `403` would burn it and a `412` send the
  client round again for nothing.
- **At the act**, a resource's `If-Match` holds the write (`412`), and
  `binds` is read as before (`409 action_record_changed`, voided). A
  `binds` that loads the record at a version the authorization does not
  hold raises `412`, and voids it: the record will never be at that
  version again.
- **`Action(require_if_match=True)`**: asking without `If-Match`, with
  `*`, or with an empty one, is `428 precondition_required`, before
  anything is stored; the act without one is `428` too, and voids
  nothing; an authorization asked without versions is refused (`403`,
  voided). Off by default. A collection route whose rule is given such
  an action is refused when declared: it would never be authorized.
- **A route of the project's own** reads `If-Match` itself, as its act
  already must to hold its write: its twin holds the unit of work it
  lends to the read to the versions (`uow.expect`), passes it
  (`unit_of_work=lambda: uow`), and passes the header
  (`if_match=`); its act holds its unit of work to them as before. The
  framework cannot know which record a route of the project's names, so
  it does not guess; it refuses what it cannot see checked.
- (b) is not done. A route that compares something else than a version
  passes it in the command, which the digest covers already.

## Consequences

- **The project's attack is refused at the twin** for a page that sends
  the `ETag` it read: `412`, nothing stored, the phone never asked. The
  project drops `read_version`, its `show` checks and its `409`.
- **A client that sent `If-Match` to a twin** (ignored in 2.7) now has
  it checked, and its act must send the same: an upgrading note says
  so. Clients that send none, and authorizations stored before, are
  bound as in 2.7.
- **What a page read is bound only as far as it says.** A session that
  sends B's tag is bound to B; the phone's screen is still what the
  person reads ([ADR 0173](0173-show-an-act-whole-or-refuse-it.md)).
  `require_if_match` makes a client forget nothing, not tell the truth.
- **Versions are numbers** (ADR 0025): an aggregate created again under
  an id it had starts again at its first version. Ids are new for each
  record (`new_id`), so a page's tag names one record's version.
- **Only the aggregate the path names.** `If-Match` names a version of
  it alone. A `binds` that reads another record (a document's file kept
  as an aggregate of its own) binds it as the twin reads it, as in 2.7,
  and A25 survives there. The project puts the act on that record's
  path, or the page sends the version it read of the other record in
  the act's body, which the digest covers, and `binds` reads it with
  `get_for_update` (ADR 0174) and raises `PreconditionFailed` for
  another: `412` at the twin, voided at the act. The docs show it.
- **A rolling deploy**: a 2.7 instance computes 2.7's digest, so it
  refuses (`403`, voided) an authorization a 2.8 instance stored with
  `If-Match`. Clients send `If-Match` to twins once every instance runs
  2.8; the upgrading notes say so, and that the twins' OpenAPI gains
  the header and `412`.
- **New names**: `Action(require_if_match=...)`,
  `request_action_authorization(..., if_match=...)`,
  `Action.digest_of(..., versions)`, `if_match` in
  `ActionStepUp.ask_authorization`, approvals' `ask_authorization`, and
  the `AsksAuthorization` protocol's description, `asked_versions`,
  `UnitOfWork.expectations`, `authorize_act(..., if_match=...)`.
  Nothing is renamed or removed: a minor release (2.8).

## References

1. RFC 9110, *HTTP Semantics*, 13.1.1 `If-Match`, 13.2.1 When to
   Evaluate, 8.8.3.2 Comparison:
   <https://www.rfc-editor.org/rfc/rfc9110#section-13.1.1>
2. RFC 6585, *Additional HTTP Status Codes*, 3. 428 Precondition
   Required: <https://www.rfc-editor.org/rfc/rfc6585#section-3>
3. Google API Improvement Proposals, *AIP-154: Resource freshness
   validation*: <https://google.aip.dev/154>
4. Google API Improvement Proposals, *AIP-163: Change validation*:
   <https://google.aip.dev/163>
5. OWASP Cheat Sheet Series, *Transaction Authorization Cheat Sheet*:
   <https://cheatsheetseries.owasp.org/cheatsheets/Transaction_Authorization_Cheat_Sheet.html>
