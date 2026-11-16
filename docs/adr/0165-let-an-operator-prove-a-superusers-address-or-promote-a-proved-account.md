# 0165. Let an operator prove a superuser's address, or promote a proved account

- Status: Accepted
- Date: 2026-11-16

## Context

A project that runs an identity service on the framework reported (user
report A12): "An account an operator makes can never bind a phone in
strict mode."

- **`createsuperuser` proves nothing it sets.** The address and the number
  it is given are stored with `email_verified` and
  `mobile_number_verified` false, as staff-made values are.
- **Since 2.4, codes go only where a user proved it**
  ([ADR 0141](0141-codes-that-open-an-account-go-only-where-it-was-proved.md)):
  password resets, and the enrollment codes that bind a first phone
  (`send_code`, `reaches_proved`).
- **With `CIBA.APPROVE_LOGINS = "always"`, nobody logs in without a bound
  phone**, and a first phone binds only with an enrollment code. ADR
  0141's way out for a staff-made account, "they log in and prove it",
  is closed: logging in needs the phone the account cannot bind.
- So the first superuser of a project in strict mode, made with the
  command the docs name, could never log in. `AUTH.CODES_TO_UNPROVED`
  would let it, and would text codes to every unproved number of every
  account again.

The project's way around it: a command of its own that makes an account
which signed up in the app (its address proved) a superuser, audited
with the operator's name. Its runbook: sign up in the app, bind the
phone, then run the command.

The report proposed `createsuperuser --promote <identifier>` for an
existing account whose identifier is proved, or a `createsuperuser` that
sends a verification code and asks for it; and a `check` warning in the
phone modes where no superuser could bind.

### What others do

- **Django's `createsuperuser`** creates a user and does nothing to one
  that exists; promoting one goes through the shell [1].
- **Mastodon**: `tootctl accounts modify USERNAME --role Owner` gives an
  existing account a role. `tootctl accounts create --confirmed` "skip[s]
  sending the confirmation email and activate[s] the account
  immediately", on the operator's word [2].
- **Discourse**: `rake admin:create` finds an existing account by its
  address, asks whether to grant it admin, and then marks its address
  confirmed [3].
- **MediaWiki**: `createAndPromote.php --force` grants rights to an
  account that exists [4]. **Zulip**: `manage.py change_user_role` [5].
- **NIST SP 800-63B-4**, 4.2: an address used for recovery other than one
  validated at proofing "SHALL be established only after the subscriber
  provides the correct confirmation code" sent to it [6]. **OWASP ASVS
  5.0**, V6.6.1: codes by phone "only when the phone number has
  previously been validated" [7].
- **Django's database checks** "are not run by default because they do
  more than static code analysis as regular checks do"; `check --database`
  asks for them [8].

### Options

| Option | What it costs |
| --- | --- |
| `createsuperuser --proved`: mark what the operator typed as proved (Mastodon, Discourse) | One flag. A typo in the address hands the superuser's password resets to whoever owns the typo: the hole ADR 0141 closed, opened for the account that matters most. |
| **`createsuperuser --verify`: send a code to each address and number, now, and ask for it at the prompt** | The operator reads the mail or the text, as any user proves an address (NIST 4.2). Mail and texts go at once, not through the outbox: no worker may run yet. Interactive only. |
| **`createsuperuser --promote IDENTIFIER`: make an existing account a superuser** | No new way to prove anything: the account proved its address at signup, in the app, as every user does. An address or a number names the account only once proved, or a value Mallory put on her account, unproved, would name hers; a code the operator types proves the operator reads it, not that the account is theirs. |
| A `check` that reads the database in the phone modes | `check` reads settings and code, and runs in CI and deployment pipelines before any database exists; Django keeps such checks opt-in. A new `check --database` is a mechanism of its own, for one finding. |
| **The command says so where it leaves a superuser no code reaches** | One line on stderr, at the moment the operator can act, in every mode: no password reset reaches such a superuser either. |
| `AUTH.CODES_TO_UNPROVED` for the first superuser | Exists already, and texts every unproved number again. |

## Decision

1. **`createsuperuser --verify`** proves the new superuser's email address
   and mobile number. The command issues an `email_verification` (or
   `mobile_verification`) code for each, mails it from
   `auth/email_verification_code` (or texts it) at once, in the user's
   language, and asks for it at the prompt, `AUTH.CODE_ATTEMPTS` times at
   most. The right code marks the value proved (`EmailVerified`,
   `MobileNumberVerified`, with the operator as the actor). It cannot run
   with `--noinput`. A code that does not come back leaves the superuser
   made, and unproved; the command says how to try again.
2. **`createsuperuser --promote IDENTIFIER`** makes an existing, active
   user a superuser, and staff where the model has `is_staff`. The value
   is read once NFKC folds look-alike characters (a full-width `＋` is a
   `+`). What names the user:
   - its id, in any form `UUID` reads (32 hex digits too): a value that
     reads as an id is never a username;
   - an email address or a mobile number in international form the user
     **proved**, in its address or number field, whatever field another
     user holds it in. A username written as an address
     (`ceo@example.com`, which `validate_username` allows) never names
     its account by that address, even where usernames are tried first;
   - its username, or another field of `AUTH.IDENTIFIERS`, as the other
     operator commands take them (`match_for_staff`), for a value written
     as none of these.

   Digits of any script with phone separators, and a `+` or not, that are
   no number in international form (`0612345678`, `0033…`, `06.12.…`)
   are refused: they name no number, and would name a username written
   so. The operator gives the number in international form, or the id.
   A user model with no `mobile_number` refuses a number.

   **A value nobody proved names no one, with or without `--verify`.** A
   code proves that the operator reads the value, not that the account
   holding it is the operator's: Mallory, who put the operator's number on
   her account unproved, would be promoted once the operator typed the
   code from their own phone. The one exception is a superuser already,
   holding the value unproved, with `--verify`: a superuser made by an
   operator before 2.5, proving its address. Otherwise the command says
   to name the user by its username or its id, or by a value it proved.

   Before anything changes, the command prints the user (its id,
   username, address and number, each proved or not, and when it
   joined), and asks `[y/N]`. With `--noinput` it asks nothing, and goes
   on only for a user named by its username, its id, or a value it
   proved. With `--verify` it proves the user's unproved values by codes,
   as above, and promotes only once every code came back.
   `UserStatusChanged` is recorded with the actor `operator:<name>`. In
   staging and production the operator must be named (`--operator` or
   `DOMAINKIT_OPERATOR`), as for `loaddata`.
3. **The command tells the operator** on stderr, after making or
   promoting a superuser that proved neither an address nor a number,
   unless `CODES_TO_UNPROVED` is set: no code reaches it, to reset its
   password or enroll a phone, until it proves one, and the command line
   that does, naming the superuser by its username, or by its id where it
   has none (or one written as an address or a number). The address is
   the model's `EMAIL_FIELD`. A code that cannot be sent, or does not come
   back, stops working, and the command says how to try again; the apps
   that send the codes are checked before the user is made.
4. **No `check` reads the database.** The note above is said where the
   superuser is made. A project in strict mode makes its first superuser
   with `--verify`, or signs up in the app and runs `--promote`.

Neither the admin API's guard on making superusers (`on_make_superuser`,
which approvals registers) nor a password is asked: the command is the
operator's, on the server, and `createsuperuser` already makes
superusers without approvals ([contrib/approvals](../contrib/approvals.md)).

## Consequences

- **The first superuser of a project in strict mode can log in**: made
  with `--verify`, or promoted after signing up in the app, it gets its
  enrollment code. The project's own command is no longer needed.
- **`createsuperuser` writes one more line to stderr** where it leaves a
  superuser no code reaches. Stdout is unchanged.
- **`--verify` needs mail (or notifications, for a number) configured on
  the machine the operator runs it on**, as the worker needs them.
- **A superuser whose address staff change later** is unproved again
  (ADR 0153); nothing checks for it. `createsuperuser --promote <its
  username or id> --verify` proves the new one.
- **A review of the first version found two ways to promote Mallory**: by
  `--promote <the operator's number> --verify` where her account held
  that number unproved, and by an address that was her username. Both are
  closed by naming users only by what they proved (Decision 2). A second
  review found more: the operator's number written otherwise
  (`0612345678`, a full-width `+`, Arabic-Indic digits), or the
  operator's id as 32 hex digits, named a username Mallory had chosen so.
  Those values are now folded, read as ids or numbers, or refused, never
  read as a username.
- ADR 0141's consequence for staff-made accounts stands for other users:
  they log in and prove their address, or get a password from staff.

## References

1. Django, *django-admin and manage.py*, `createsuperuser`:
   <https://docs.djangoproject.com/en/5.2/ref/django-admin/#createsuperuser>
2. Mastodon, *Using the admin CLI*, `tootctl accounts create` and
   `tootctl accounts modify`: <https://docs.joinmastodon.org/admin/tootctl/>
3. Discourse, `lib/tasks/admin.rake`:
   <https://github.com/discourse/discourse/blob/main/lib/tasks/admin.rake>
4. MediaWiki, *Manual:createAndPromote.php*:
   <https://www.mediawiki.org/wiki/Manual:CreateAndPromote.php>
5. Zulip, *Management commands*:
   <https://zulip.readthedocs.io/en/latest/production/management-commands.html>
6. NIST SP 800-63B-4, *Authentication and Authenticator Management*,
   section 4.2: <https://pages.nist.gov/800-63-4/sp800-63b.html>
7. OWASP ASVS 5.0, V6 Authentication, requirement 6.6.1:
   <https://github.com/OWASP/ASVS/blob/master/5.0/en/0x15-V6-Authentication.md>
8. Django, *System check framework*, the `database` tag:
   <https://docs.djangoproject.com/en/5.2/ref/checks/>
