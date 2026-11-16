# Users, groups, backends, and passwords

A user is a person or a program that logs in to your service. A group
is a named set of permissions, and every member of a group has them
all. A backend answers two questions: "who do these credentials prove?"
and "may this user do that?". This page shows how to make users and
groups, how to give them permissions, and how passwords are stored and
checked.

On this page you learn how to:

- make users and groups, and give them permissions;
- check a password and a permission in code;
- swap the user for a model of your own;
- write a backend, and grant a permission on one object;
- choose how passwords are hashed and which passwords are allowed.

## A first example

A user is an [aggregate](../learning.md#words) like any other. You make
and change users in an `AuthUnitOfWork`, which is the
[unit of work](../learning.md#words) of the auth tables:

```python
import asyncio

from domainkit.contrib.auth import AuthUnitOfWork, authenticate, has_perm


async def main() -> None:
    async with AuthUnitOfWork() as uow:
        alice = await uow.users.create_user(
            "alice", "correct horse battery", email="alice@example.com"
        )
        editors = await uow.groups.create("editors")
        editors.grant(await uow.permissions.get("shop.publish_product"))
        alice.join(editors)
        await uow.commit()

    user = await authenticate(username="alice", password="correct horse battery")
    print(user)
    print(await has_perm(user, "shop.publish_product"))
    print(await has_perm(user, "shop.delete_product"))
    print(await authenticate(username="alice", password="wrong"))


asyncio.run(main())
```

```text
alice
True
False
None
```

`authenticate` returns the user, or `None` when the password is wrong.
`has_perm` is true because Alice is a member of `editors`, and `editors`
has `shop.publish_product`.

## Users and groups

`User` has the fields of Django's user. Its repository loads it with its
groups and permissions.

| Field | Type | Notes |
| --- | --- | --- |
| `id` | `UserId`, a UUID | |
| `username` | text, unique when set | Optional since 1.8. 1 to 150 letters, digits, and `@ . + - _`, NFKC-normalized. Names that start with `erased-`, in any case, are kept for erased accounts. |
| `password` | `PasswordHash` | Never a raw password; see [Passwords](#passwords). |
| `email`, `first_name`, `last_name` | text | The email is one plain address or empty, with its domain in lower case. |
| `email_verified` | bool | Whether the user proved that they read mail at `email`. An address staff set clears it; one the user changes to is proved first ([Changing an address or a number](#changing-an-address-or-a-number)). |
| `mobile_number` | text, unique when set | A number in international form (E.164), such as `+33612345678`. Spaces, dots, dashes, and brackets are dropped. |
| `mobile_number_verified` | bool | Whether the user proved that they receive texts at `mobile_number`. A number staff set clears it; one the user changes to is proved first. |
| `is_active` | bool | An inactive user cannot log in and has no permissions. |
| `is_staff` | bool | May use the [admin API](routes.md#the-admin-api), with its permissions. |
| `is_superuser` | bool | Has every permission, without grants. |
| `last_login`, `date_joined` | UTC timestamps | |
| `groups`, `user_permissions` | sets | Grants, kept in link tables. |

The other names of the model:

| Name | What it is |
| --- | --- |
| `Group` | A unique `name` (1 to 150 characters) and a set of `permissions`. `Group.create(name)`, `rename(name)`, `grant(*permissions)`, `revoke(*permissions)`, `permission_keys()`. |
| `Permission` | One permission of one content type: `codename`, `name`, `content_type`, and its `key`, such as `shop.add_product`. |
| `AnonymousUser` | A request without credentials: no `id`, not active, no permissions. `is_anonymous` is `True`. |
| `AnyUser` | The type `AbstractBaseUser \| AnonymousUser`: a user who logged in, or the anonymous one. |
| `PasswordHash` | A stored password: a hasher's `<algorithm>$...`, or an unusable one that starts with `!`. |
| `UserId`, `GroupId` | `NewType`s of `UUID`. |

A user changes through its methods, and each records an
[event](../learning.md#words): `set_password(hash)`,
`set_unusable_password()`, `set_active(value)`, `set_superuser(value)`,
`set_staff(value)`, `join(group)`, `leave(group)`, `grant(*permissions)`,
`revoke(*permissions)`, `verify_email()`, `verify_mobile_number()`,
`change_email(address)`, `change_mobile_number(number)`, and
`update(**fields)`. `get_username()`, `get_full_name()`,
`get_short_name()`, and `has_usable_password()` read it.

A user needs at least one identifier: a username, an email address, or a
mobile number (`422 identifier_required`). The repository asks more: a new
user needs one of the fields that `AUTH.IDENTIFIERS` names, which is the
username alone by default. Where `email` is one of them, two users cannot
have one address (`409 email_taken`). `get_username()` returns the
username, or, without one, the email address or the mobile number. Events,
the audit trail, and the admin API name a user by it.

```python
from domainkit.contrib.auth import PasswordHash, User

user = User.create(password=PasswordHash.unusable(), mobile_number="+33 6 12 34 56 78")
print(user.username, user.mobile_number, user.get_username())
```

```text
None +33612345678 +33612345678
```

### The repositories

`AuthUnitOfWork` has one repository or store for each kind of row. These
are the methods you use most:

| Call | What it does |
| --- | --- |
| `uow.users.create_user(username, password=None, **fields)` | Hashes the password and adds the user. Without a password, the password is unusable. It does not run the validators: call `validate_password` first. |
| `uow.users.create_superuser(username, password=None, **fields)` | The same, with `is_superuser`, and `is_staff` if the model has it. |
| `uow.users.get(user_id)`, `get_by_natural_key(username)` | One user, by id or by username. |
| `uow.users.match_for_staff(value)` | The user an operator names, and the field that matched (`StaffMatch(user, field)`): by `find_by_identifier` first; then, where `AUTH.IDENTIFIERS` leaves usernames out, by username, for a value that is neither an email address nor a mobile number. The commands use it. |
| `uow.users.find_for_staff(value)` | The user of `match_for_staff`, alone. |
| `uow.users.find_by_email(email)` | Every user with that address, in any case of its ASCII letters. |
| `uow.users.set_password(user, password)` | Hashes and stores a new password; `None` makes it unusable. Refuses a password of the user's history (`PASSWORD_HISTORY`). |
| `uow.users.password_hash(user_id)` | The stored hash, read from the table. |
| `uow.users.remove(user)` | Deletes the user and every credential of theirs (below). |
| `uow.users.page(limit=..., after=..., search=...)` | A page of users by username, after the username `after`. `search` matches part of the username, or of the email address or mobile number where `AUTH.IDENTIFIERS` names them, in any case. Users with no username sort last, where `after`, a username, cannot reach them: page those with the admin API (`GET /users`) and its cursors. |
| `uow.groups.create(name)`, `get_by_name(name)`, `get_many_by_name(names)`, `rename(group, name)` | Groups. |
| `uow.permissions.get(key)`, `get_many(keys)`, `all()`, `keys()` | Permissions, by key. |
| `uow.tokens.issue(user_id, ttl=...)`, `revoke_user(user_id)` | Tokens; see [Authenticating requests](authentication.md). |
| `uow.pending_identifiers.get(user_id, field)` | The new address (`"email"`) or number (`"mobile_number"`) the user holds until its code proves it, while it lasts ([Changing an address or a number](#changing-an-address-or-a-number)). |

`uow.users.find_by_identifier(value)` finds the user that an email
address, a mobile number, or a username names, by the fields of
`AUTH.IDENTIFIERS` ([Logging in with an email address or a mobile
number](logging-in.md#logging-in-with-an-email-address-or-a-mobile-number)).
`create_user(None, password, email=...)` makes a user without a username.

`create_user` refuses a username that a user has already, with
`UsernameTaken` (409). It compares the name exactly, after NFKC, so
`Alice` and `alice` can be two users. Signing up is stricter: it refuses
a name taken in any case. A second request that races for the same name
fails on the unique column with the same 409.

### Checking permissions

You check a permission in two ways:

```python
from domainkit.contrib.auth import get_all_permissions, has_perm, has_perms

user.has_perm("shop.publish_product")  # from the loaded user, no query
await has_perm(user, "shop.publish_product")  # asks every backend
await has_perm(user, "shop.change_product", product)  # on one object
await has_perms(user, ["shop.view_product", "shop.change_product"])
await get_all_permissions(user)  # frozenset of keys
```

`user.has_perm(key)` answers from the user as it was loaded, without a
query. `await has_perm(user, key, obj)` also asks every backend, which is
how [object permissions](#grants-on-single-objects) work. Both say yes to
an active superuser, and no to an inactive user.

An active superuser has every permission, on every object, before any
backend is asked: no backend narrows a superuser, and a backend that
raises `PermissionDenied` does not either. `get_all_permissions` agrees
with `has_perm`: for an active superuser it lists every permission, on
an object too, with whatever the backends add. Before 2.4,
`get_all_permissions(superuser, obj)` listed only what the backends
granted on the object, so a "what may I do" route under-reported a
superuser. Nothing else changed about what a superuser may do.

Within one request, a backend that reads grants from the database
answers each user and object once (*new in 2.4*). An access rule that
asks `HasObjectPermissions("shop.change_product")`, then a use case that
asks `has_perm(user, "shop.view_product", product)`, cost one query, not
two. Every request with credentials gets its own memo from
`AuthenticationMiddleware`, and it ends with the request:

- **A commit drops it.** Each unit of work that commits in the request
  empties the memo, so a grant, a revocation, or a deleted object is seen
  by the next check. A grant another request commits meanwhile is seen
  by this one's next commit, or by the next request.
- **Each answer is for one user, with their groups, and one object.**
  The same object asked about for another user, or for the same user
  with other groups, is asked again.
- **What outlasts a request keeps nothing.** WebSockets keep no memo, nor
  event streams once they stream, nor a task started in the request that
  is still running after it.
- **Outside a request nothing changes**: each call asks the backends.
  `with memoised_permissions():` gives a task or a command the same
  memo for a block, and `forget_permissions()` empties it after a change
  made without a unit of work.

Django keeps a user's model permissions on the user object for as long
as it lives (`_perm_cache`), and django-guardian keeps a user's object
permissions in an `ObjectPermissionChecker`; this is the same idea,
bounded by the request, and emptied by commits.

`user.has_perms(keys)`, `user.get_all_permissions()`,
`user.get_user_permissions()`, `user.get_group_permissions()`, and
`user.has_domain_perms("shop")` read the loaded user too.
`has_domain_perms` is Django's `has_module_perms`.

Two more functions read through the backends: `get_user(user_id)`
returns the user from the first backend that has it, and
`get_backends()` returns the configured backends, in order.

### Email addresses

An email address is one address alone, in ASCII, as
`domainkit.contrib.mail` checks the addresses it sends to:
`user@example.com`. A display name (`Name <user@example.com>`), a
comment, or a list is refused, and an internationalized domain must be
in its `xn--` form. Creating a user, changing the email (the admin API,
`update`), signing up, and asking for a reset all refuse anything else
with `InvalidEmail` (422 `invalid_email`):

```text
Enter one email address, such as user@example.com, without a name.
```

A request body's `email` that is not shaped as an address at all
(`not-an-email`) is refused as `Enter a valid email address.`, in the
request's language, rather than with the pattern that checks it (since
2.5); OpenAPI still shows the pattern.

So the address that mail goes to is the whole stored value, and no
display name reaches the text of a mail. Addresses compare by their
ASCII letters in any case, and by nothing else: `find_by_email` never
takes a look-alike for another address. (On PostgreSQL, SQL's `lower()`
turns the KELVIN SIGN into `k`; domainkit does not.) Rows stored before
these checks keep their address, and the worker skips a user whose
address cannot take mail.

### Removing a user

`uow.users.remove(user)` deletes the user and, in the same transaction,
every credential of theirs: tokens, browser sessions, refresh tokens,
client certificates, device proofs, the second factor's secret and
recovery codes, step-ups, transaction authorizations, the history of
their passwords, and a new address or number they held unproved.
`uow.users.forget_credentials(user_id)` deletes the credentials alone;
privacy erasure uses it too.

A user model that is `SoftDeletable` keeps the row, marked deleted.
`remove` forgets the credentials all the same and deactivates the user
(`UserStatusChanged`), because logging in and every request read
`is_active`, not `deleted_at`. `restore` brings the row back inactive,
for staff to activate. A bulk `delete_rows` bypasses all this, and
leaves the credentials.

### Events

Changes are recorded as events, never with passwords or hashes in them:

| Event | When |
| --- | --- |
| `UserCreated`, `UserUpdated`, `UserDeleted` | A user is made, has fields changed (the event names the fields, not the values), or is deleted. |
| `EmailChanged`, `MobileNumberChanged` | The email address or the mobile number changed, after `UserUpdated`: each names the one it replaced ([Telling the old address](#telling-the-old-address)). A change the user asked for is recorded when its code comes back. |
| `PasswordChanged`, `UserStatusChanged` | A new password; `is_active`, `is_staff`, or `is_superuser` changed. |
| `UserJoinedGroup`, `UserLeftGroup` | Group membership. |
| `UserPermissionGranted`, `UserPermissionRevoked` | Direct grants. |
| `GroupCreated`, `GroupRenamed`, `GroupDeleted`, `GroupPermissionGranted`, `GroupPermissionRevoked` | Groups. |
| `UserLoggedIn`, `UserLoggedOut`, `TokenCreated`, `TokenRevoked` | Logins and tokens. |
| `RefreshTokenReused` | A used JSON Web Token refresh token came back, so its login was ended. It is also logged as a security warning. |
| `UserImpersonated`, `SteppedUp`, `EmailVerified`, `UserSignedUp` | See [Protecting routes](routes.md#impersonation), [Stepping up](step-up.md), and [Logging in](logging-in.md). |
| `EmailReleased`, `MobileNumberReleased` | The user lost an address or a number they never proved: a signup proved it ([Signing up](logging-in.md#signing-up)). |

The events live in `domainkit.contrib.auth.events`. `UserDeleted`,
`UserSignedUp`, and `SteppedUp` are also exported from
`domainkit.contrib.auth.integration`, for domains that react to them.
With `domainkit.contrib.audit` installed, each event becomes a row of the
audit trail, such as `auth.user_permission_granted`. The username in
`UserCreated` and `UserDeleted` is personal data: the trail keeps it as
a pseudonym of the user's id, which `audit forget <user id>` makes
unlinkable (see [contrib.md](../contrib.md)).

Each event names who caused it in `actor`: the user, staff who changed
them through the admin API, staff impersonating them
(`actor.impersonating` is the user's id), an operator's command, or the
system ([Who acted](../resources.md#the-domain-behind-a-resource)).
`event.actor.is_self(event.user_id)` holds only for the user's own acts.

### Changing an address or a number

A user changes their own address or number by proving the new one first.
`POST /auth/email-verification` with `{"email": "ann@new.example"}`, or
`POST /auth/mobile-verification` with `{"mobile_number": "+33698765432"}`,
holds the new value apart, and the worker sends the verification code to
it. The account keeps the address or number it has: that one still logs
in, and still receives reset and enrollment codes if it was proved. When
the code comes back at `/email-verification/code` (or
`/mobile-verification/code`), the new value replaces the old one, proved,
and the codes sent to the old one stop working
([Changing your address or number](logging-in.md#changing-your-address-or-number),
[ADR 0153](../adr/0153-hold-a-new-address-or-number-until-its-code-proves-it.md)).

| Who changes it | How | What happens |
| --- | --- | --- |
| The user | The verification routes, with the new value | Held until its code comes back, as long as its last code works (`AUTH.CODE_TTL`); then `change_email` or `change_mobile_number` records `UserUpdated`, `EmailChanged` or `MobileNumberChanged`, and `EmailVerified` or `MobileNumberVerified`. |
| Staff | The admin API (`PATCH /users/{id}`), or `update` in code | At once, unproved: `UserUpdated` and `EmailChanged` or `MobileNumberChanged`. A value the user held for that field is dropped, and the codes sent to the old value stop working. |

A held value reserves nothing: whoever proves it first gets it. When its
code comes back, an account that proved it keeps it
(`409 mobile_number_taken`, or `409 email_taken` where addresses
identify users), and one that holds it unproved gives it up, as to a
signup (`EmailReleased`, `MobileNumberReleased`). A held value is kept in
`domainkit_pending_identifiers`, never in fixtures, and goes with the
user's credentials when they are deleted or erased.

`change_email(address)` and `change_mobile_number(number)` are for a
value a code reached. A route of your own that changed the user's
address with `update` replaced it at once: send the address and the
number to the verification routes instead, and keep `update` for the
other fields.

### Telling the old address

When an email address or a mobile number changes, tell the old one: if
someone else made the change, the owner hears of it where they still
read (OWASP's [Authentication Cheat Sheet](https://cheatsheetseries.owasp.org/cheatsheets/Authentication_Cheat_Sheet.html),
ASVS 5.0 V6.3.7). `update`, and the swap of a change the user proved
(`change_email`, `change_mobile_number`), record `EmailChanged` or
`MobileNumberChanged` after `UserUpdated`, with:

- `previous`: the address or number it replaced; `None` when the user
  had none.
- `previous_verified`: whether the user had proved it. One never proved
  may be someone else's
  ([ADR 0141](../adr/0141-codes-that-open-an-account-go-only-where-it-was-proved.md)).
- `actor`: who made the change.

```python
from domainkit.contrib.auth.integration import EmailChanged
from domainkit.contrib.mail import send_mail
from domainkit.ddd import handles


@handles(EmailChanged, durable=True, name="accounts.tell_the_old_address")
async def tell_the_old_address(event: EmailChanged) -> None:
    if event.previous is None or not event.previous_verified:
        return
    by_user = event.actor is not None and event.actor.is_self(event.user_id)
    who = "You" if by_user else "Our support team"
    await send_mail(
        "Your email address was changed",
        f"{who} changed the email address of your account. "
        "If you did not ask for this, contact us.",
        [event.previous],
    )
```

`MobileNumberChanged` is the same, with `send_sms`. The new address is
the user's own: read it from them, as it may change again before the
handler runs. Neither event is recorded when a change is refused
(`IdentifierRequired`), when privacy erases the user, nor when a signup
takes an address they never proved (`EmailReleased`). Nor is either
recorded when a user asks for a change: only once its code comes back.

`previous` is marked `Personal`. The audit trail keeps a pseudonym of
it; the outbox keeps it until the handler's message has run, and
`privacy erase` finds it there; live events leave it out. A project that
exposes the event as a webhook picks its `payload`
([Privacy](../privacy.md#what-erasure-reaches-and-when)).
`UserUpdated` stays the names of the fields: a handler of every change
would otherwise hold old names and custom fields too
([ADR 0147](../adr/0147-name-who-caused-each-event-and-tell-the-old-address.md)).

## A custom user model

You can replace the user with a class of your own, before the first
`migrate` that creates users. One command writes it:

```bash
python manage.py startdomain accounts --user
```

It writes `accounts/models.py`, with a `User` that subclasses
`AbstractUser` and its table. It installs the domain, and names both in
`AUTH`. To write the model by hand, subclass `AbstractUser`, or
`AbstractBaseUser`, which has no username, names, or staff flag. Give it
a table, and name both in the settings:

```python
# accounts/models.py
from typing import Annotated

from sqlalchemy import Boolean, Column, String, Table

from domainkit.contrib.auth import AbstractUser
from domainkit.contrib.auth.tables import abstract_user_columns
from domainkit.db import metadata
from domainkit.ddd import MaxLength, Pattern


class Account(AbstractUser):
    phone: Annotated[str, MaxLength(32), Pattern(r"\+[0-9]{8,15}")] = ""
    risk_flag: bool = False
    REQUIRED_FIELDS = ("email", "phone")  # createsuperuser and /signup ask for these
    EDITABLE_FIELDS = AbstractUser.EDITABLE_FIELDS | {"phone"}  # staff may change it


accounts = Table(
    "accounts_users",
    metadata,
    *abstract_user_columns(),
    Column("phone", String(32), nullable=False),
    Column("risk_flag", Boolean, nullable=False),
    info={"domain": "accounts"},
)
```

```python
# config/settings/base.py
AUTH = {
    "USER_MODEL": "accounts.models.Account",
    "USER_TABLE": "accounts.models.accounts",
}
```

Keep the model and the table in `models.py` (or `infrastructure/`). They
build on `domainkit.contrib.auth`, which the domain layer may not import.
Settings files other than the base one extend `AUTH`
(`AUTH = {**AUTH, ...}`); otherwise the user model is lost there.

- **Markers hold everywhere.** A field's [markers](../validation.md)
  hold wherever the user is made or changed: at signup, in the admin API,
  in `createsuperuser`, and in a script. Over HTTP the answer is a 422
  that names the field. So `phone` above needs no extra code.
- **`REQUIRED_FIELDS`** are what `createsuperuser` asks for. Those other
  than the username and the email (`signup_fields()`) are what
  `POST /signup` takes under `fields` ([Signing up](logging-in.md#signing-up)).
  Startup refuses a name the model lacks, or one that changes through its
  own methods.
- **`EDITABLE_FIELDS`** lists what the admin API may change: `email`,
  `first_name`, and `last_name` on `AbstractUser`, nothing on
  `AbstractBaseUser`. `READ_ONLY_FIELDS` lists what it only shows
  (`date_joined` and `email_verified`). A new field, such as `risk_flag`
  above, stays out of the admin API until you list it.
- **`PROTECTED_FIELDS`** can only grow. `PROTECTED_FLOOR` (`id`,
  `version`, `password`, `last_login`, `date_joined`, `email_verified`,
  `is_active`, `is_staff`, `is_superuser`, `groups`, `user_permissions`)
  stays protected whatever the model says, because those fields change
  through their own methods, which the admin API guards and which record
  their own events. Startup refuses a protected field in
  `EDITABLE_FIELDS`.
- **`clean_field(name, value)`** tidies a value before it is stored, when
  a user is made and when one is changed. Override it to trim or
  normalize a field.
- **`LANGUAGE_FIELD`** (*new in 2.4*) names the field that holds the
  user's language, such as `language: str = ""` with
  `LANGUAGE_FIELD = "language"`. Codes and pushes sent to the user are
  written in it, when `LANGUAGES` lists it; without one, in the language
  of the request that asked for them, or `LANGUAGE_CODE`
  ([The language of codes](logging-in.md#the-language-of-codes)).
  Startup refuses a name the model lacks. It is `None` by default: no
  column is added for you.

The model's links to groups and permissions get tables named after its
table (`accounts_users_groups`, `accounts_users_permissions`). Startup
refuses a table without a column for one of the model's fields, or
without `info={"domain": ...}` naming the installed domain that defines
it: migrations and `DATABASE_ROUTING` follow that domain.

Users who log in with their email subclass `AbstractBaseUser`, and use
`abstract_base_user_columns()`:

```python
from typing import Annotated

from sqlalchemy import Column, String, Table

from domainkit.contrib.auth import AbstractBaseUser, authenticate
from domainkit.contrib.auth.tables import abstract_base_user_columns
from domainkit.db import metadata
from domainkit.ddd import Personal


class Member(AbstractBaseUser):
    USERNAME_FIELD = "email"
    EMAIL_FIELD = "email"
    email: Annotated[str, Personal(of="id")]


members = Table(
    "members",
    metadata,
    *abstract_base_user_columns(),
    Column("email", String(254), nullable=False, unique=True),
    info={"domain": "members"},
)

user = await authenticate(email="boss@example.org", password="correct horse battery")
```

`get_user_model()` returns the class in use. `get_user_table()`, in
`domainkit.contrib.auth.users`, returns its table. A user's `repr` shows
its id and status flags only, never names or addresses:

```text
User(id=UUID('01cb93fe-e476-466d-aebc-cc2f2cf64072'), is_active=True, is_superuser=False)
```

### Users' personal data

`AbstractUser` marks `username`, `email`, `first_name`, and `last_name`
as `Personal(of="id")`. So `domainkit.contrib.privacy` exports them
(`privacy export --subject <user id>`) and erases them. Mark the personal
fields of a custom model too (the `Member`'s `email` above). Give the
model an `erase_personal_data` method if the default way of erasing does
not fit ([privacy.md](../privacy.md)).

Erasing a user (`privacy erase --subject <user id>`) runs
`AbstractUser.erase_personal_data`:

- **The username becomes `erased-` and 32 hex digits**: an HMAC-SHA256 of
  the name (folded as lookups fold it), under a key derived from
  `SECRET_KEY`. It stays unique, and the old name stays taken: creating a
  user or signing up with it, in any case, is refused as a taken
  username. So nobody else can become the person erased. The fallback
  keys are checked too; a name erased under a key dropped from
  `SECRET_KEY_FALLBACKS` is free again.
- **Names that start with `erased-` are kept for erasure.** Signing up,
  creating a user (with any user model), and `validate_username` refuse
  them with `InvalidUsername`, so no one can pass for an erased account.
  A stored name that only starts with `erased-` is erased like any other;
  one already of the erased form is left as it is.
- **The email and the names become empty**, and the address unverified.
- **The account ends in the same commit.** It is deactivated, and its
  password made unusable; both are audited. Right after, every token of
  the user (logins, browser sessions, personal tokens, client
  certificates) and their second factor are deleted.

The row stays, with its id, so what refers to the user still does.
Erasing again changes nothing.

Fixtures (`dumpdata`, `loaddata`) never hold tokens, sessions,
certificates, TOTP secrets, or recovery codes: their tables are state
(`info={"fixtures": False}`). A fixture of users still holds password
hashes, so keep it as safe as the database. Auth's tables are guarded:
`dumpdata` writes them, but `loaddata` writes users, groups, permissions,
or their links only with `--include-guarded`, and `flush` keeps them,
tokens and second factors included, unless it is given that option. A
dumped user loads back as it was, its password hash included
([reference/commands.md](../reference/commands.md#data-fixtures-flush-and-seeds)).

## Backends

`AUTH.BACKENDS` lists backend classes, asked in order:

- `authenticate(**credentials)` returns the first user a backend accepts;
- `has_perm` is true when any backend grants the permission;
- a backend that raises `PermissionDenied` ends either search with "no";
- an active superuser has every permission, on every object, before any
  backend is asked ([Checking permissions](#checking-permissions)).

`ModelBackend`, the default, checks passwords against the user table and
grants what users and their groups were given. An unknown username costs
it as much time as a wrong password, so response times do not tell which
usernames exist.

A backend subclasses `BaseBackend` and overrides what it needs. Each
method is async:

| Method | Answers |
| --- | --- |
| `authenticate(credentials)` | The user that the mapping `credentials` proves, or `None`. |
| `get_user(user_id)` | The user this backend vouches for, or `None`. |
| `get_all_permissions(user, obj=None)` | The permission keys it grants. |
| `has_perm(user, perm, obj=None)` | Whether it grants `perm` (on `obj`). |
| `object_grants(holder)` | The grants it holds on single objects for a user or a group (below). |

A backend that grants on objects:

```python
from domainkit.contrib.auth import BaseBackend


class OwnerBackend(BaseBackend):
    async def has_perm(self, user, perm, obj=None) -> bool:
        return (
            perm == "billing.change_invoice"
            and getattr(obj, "owner_id", None) == user.id
        )

    async def object_grants(self, holder):
        return frozenset()  # see below


AUTH = {
    "BACKENDS": [
        "domainkit.contrib.auth.backends.ModelBackend",
        "billing.auth.OwnerBackend",
    ]
}
```

The admin API needs to know what each backend grants on single objects,
so that staff cannot change a user who holds more than they do. A
backend says it in `object_grants(holder)`: a frozenset of
`ObjectGrant(permission, content_type, object_id)` for a user (with their
groups' grants, active or not) or for a group. `object_grants(holder)` in
`domainkit.contrib.auth.backends` asks every backend.

- A backend that grants on no object answers with an empty set.
- A backend that cannot list what it grants (it grants by the object's
  own fields, as `OwnerBackend` does) leaves the method out. The admin
  API then lets only superusers change users and groups, because becoming
  an owner hands out what the owner holds. `check` warns with
  `auth.W007`, and `backends_without_object_grants()` lists such
  backends.

So the `OwnerBackend` above should leave `object_grants` out, unless the
invoices it guards can never change owner through the admin API.

A backend whose answers come from the database, and not from the
object's fields, can keep them for the request
([Checking permissions](#checking-permissions)) with
`memoised(key, compute)` from `domainkit.contrib.auth.backends`. The key
names all that the answer depends on, starting with the backend's class;
`compute` is called once per request and key, and on every call outside
a request. At most `MAX_MEMOISED` (1,024) answers are kept per request.

```python
from domainkit.contrib.auth.backends import BaseBackend, memoised


class TeamBackend(BaseBackend):
    async def get_all_permissions(self, user, obj=None):
        if obj is None or not user.is_active:
            return frozenset()
        key = (TeamBackend, user.id, type(obj).__name__, obj.id)
        return await memoised(key, lambda: team_grants(user.id, obj))
```

`OwnerBackend` above must not: an invoice's owner can change within a
request, and its answer with it.

The [LDAP backend](identity.md#ldap-and-active-directory) checks
passwords against a directory. `MembershipBackend` grants what a user
holds as a member of the tenant being served, such as a role's keys
(`TENANCY.MEMBER_PERMISSIONS`, [tenancy](../tenancy.md#members-permissions)).
Such a backend sets `tenant_scoped = True`: `has_perm` counts its keys in
the tenant, and `get_all_permissions(user)`, which bounds the admin API
and token scopes, leaves them out (`tenant_scoped=True` adds them).

### Grants on single objects

`domainkit.contrib.objectpermissions` stores grants of one permission on
one object, to a user or to a group (django-guardian's model). Install it
after auth, add its backend after `ModelBackend`, then run
`makemigrations` and `migrate`:

```python
INSTALLED_DOMAINS = [
    "domainkit.contrib.contenttypes",
    "domainkit.contrib.auth",
    "domainkit.contrib.objectpermissions",
    "catalog",
]
AUTH = {
    "BACKENDS": [
        "domainkit.contrib.auth.backends.ModelBackend",
        "domainkit.contrib.objectpermissions.ObjectPermissionBackend",
    ]
}
```

```python
from domainkit.contrib.objectpermissions import (
    grant_permission,
    grants_on,
    revoke_permission,
    visible_scope,
)
from domainkit.http.access import AllOf, HasObjectPermissions, IsAuthenticated

await grant_permission(actor, "catalog.change_product", product, user=ada.id)
await grant_permission(actor, "catalog.view_product", product, group=editors.id)
await revoke_permission(actor, "catalog.change_product", product, user=ada.id)
print(await grants_on(product))  # every grant on it

# Rules that look at objects now see the grants:
products = ResourceRouter(
    "/products",
    model=Product,
    unit_of_work=CatalogUnitOfWork,
    repository=lambda uow: uow.products,
    rule=AllOf(IsAuthenticated(), HasObjectPermissions("catalog.change_product")),
    scope=visible_scope("view", Product),
)
```

- **Granting stays within what the actor holds.** `grant_permission` and
  `revoke_permission` need the permission itself, on the model or on the
  object, and `objectpermissions.add_objectpermission` to grant
  (`delete_objectpermission` to revoke). Superusers have both. No one
  grants to themselves, and a permission of another model is refused
  with `InvalidGrant`, unless a domain made it
  [grantable on the model](#permissions-of-another-model). Granting twice
  changes nothing. `revoke_permission` takes the permission back however
  it was granted to that user or group, alone or with a role. Each takes
  an optional `uow`, an `ObjectPermissionsUnitOfWork`, to join your
  transaction.
- **Each grant and revocation is an event** (`ObjectPermissionGranted`,
  `ObjectPermissionRevoked`) that names who gave what to whom, with the
  grant's expiry and role, so the audit trail records it.
- **One query per object and request.** `ObjectPermissionBackend` reads
  every permission a user holds on an object, through them or their
  groups, in one query, and keeps the answer for the request
  ([Checking permissions](#checking-permissions)). Granting or revoking
  in the request commits, which drops it.
- **`visible_scope("view", Product)`** is a resource's `scope`. Callers
  who hold `catalog.view_product` see every product. Others see those
  granted to them or to one of their groups, filtered in SQL with an
  `EXISTS` on the grants, so pages, counts, and cursors never see the
  rest. Anonymous and inactive callers see none, and neither do
  credentials limited to scopes that leave the permission out.
  `visible_to(user, "view", Product, request=request)` gives the same
  criteria (`VisibleTo`) for queries of your own; without `request`,
  scopes are not checked.
- **In tests**, `VisibleTo` reads the grants it is given:
  `visible_scope("view", Product, grants=InMemoryObjectPermissions(...))`
  works with fake repositories. `Grants` is the protocol it reads.
- **Where grants live.** Grants are stored in the database of the objects
  they are about, in `domainkit_object_permissions`, which every database
  that hosts a domain gets. Ids may be UUIDs, integers, or text, each in a
  column of its type. User and group ids are not foreign keys. Deleting a
  user (a repository's `remove`, or the admin API) deletes the grants
  they held in the user's database, in the same transaction. Grants kept
  in another database stay, unused and never reused.
- **Grants go with their object.** When the ORM deletes an object's row
  (a repository's `remove`), the grants on it are deleted in the same
  transaction. So an object stored later with the same id starts without
  them. A soft delete keeps them, for a restore. Bulk deletes
  (`delete_rows`) bypass the ORM and leave grants behind: remove such
  objects one by one, or delete their grants in the same transaction.
  `flush` keeps the grants table (it is guarded), but deletes the grants
  on the objects of every table it empties.

The other names of the domain: `ObjectPermission` (one grant, with its
`ObjectPermissionId`), `ObjectPermissionRepository` (find grants with
`about(...)`), and `ObjectPermissionsUnitOfWork`.

#### Grants that expire

A grant may end at a moment you give (since 2.4,
[ADR 0151](../adr/0151-object-grants-that-expire-roles-and-owners-a-caller-administers.md)):

```python
from datetime import timedelta

from domainkit.clock import now

await grant_permission(
    actor,
    "catalog.change_product",
    product,
    user=ada.id,
    expires_at=now() + timedelta(days=7),
)
```

From `expires_at` on, the grant counts nowhere: not in `has_perm`, not in
`visible_scope` and `visible_to` (pages and counts alike), not in the
admin API's bounds on staff, and not in `grants_on`. Each reads the
clock, so no job has to run first. The expiry must be in the future, with
its time zone; otherwise `InvalidGrant`. Granting again sets the expiry
you give: the same one changes nothing, another one (or `None`, for no
end) renews the grant under its id and records `ObjectPermissionGranted`
again. That also brings back a grant past its expiry.

Expired grants stay in the table until deleted. `python manage.py
cleargrants`, or `clear_expired_grants()`, deletes them in every database
that holds grants; run it from a scheduled job, as `cleartokens`.

#### Roles

A role is a named set of permissions, granted and taken back together:

```python
from domainkit.contrib.objectpermissions import ObjectRole, grant_role, revoke_role

EDITOR = ObjectRole("editor", ["catalog.view_product", "catalog.change_product"])

await grant_role(actor, EDITOR, product, user=ada.id, expires_at=friday)
await revoke_role(actor, EDITOR, product, user=ada.id)
```

`grant_role` makes one grant per permission, named after the role
(`ObjectPermission.role`), in one transaction: all of them, or none. The
actor must hold each permission, as for `grant_permission`. Checks read
the grants, so a role costs nothing more when a request is checked.
`revoke_role` takes back the grants named after the role; the same
permission granted alone, or with another role, stays. Roles live in your
code, not in the database. Change a role's permissions, and grants made
before keep what they had: `grant_role` adds what is new, and
`revoke_role` takes back all the role's grants, those the role no longer
names included.

#### Permissions of another model

A permission belongs to one model, and is granted on that model's
objects: `catalog.change_product` on a product. Some permissions are
about another object: "Ada may add members to this company" is
`memberships.add_membership`, on one `Company`. A domain says where such
a permission applies, in its `ready()` (since 2.4):

```python
from domainkit.apps import DomainConfig
from domainkit.contrib.objectpermissions import grantable_on


class CompaniesConfig(DomainConfig):
    def ready(self) -> None:
        grantable_on(Company, "memberships.add_membership")
```

Then `grant_permission(actor, "memberships.add_membership", company,
user=ada.id)` is a grant, `has_perm(ada, "memberships.add_membership",
company)` answers yes, and `visible_to(ada, "memberships.add_membership",
Company)` lists the companies she may add members to. Holders of the
permission on the model may do it everywhere, as with any permission.
Without `grantable_on`, such a grant is refused with `InvalidGrant`, as
before, and on every other model it stays refused.

## Passwords

A user holds a `PasswordHash`, which only the hashers make. So a raw
password can never be stored by mistake, and hashes never appear in
logs. You hash and check passwords with these functions:

```python
from domainkit.contrib.auth import check_password, make_password

encoded = make_password("correct horse battery")
print(encoded[:30] + "...")
print(
    check_password("correct horse battery", encoded), check_password("wrong", encoded)
)
```

```text
pbkdf2_sha256$1500000$baPjmHBT...
True False
```

`amake_password` and `acheck_password` do the same on a hashing thread,
for async code. `password_holds(user, password)`, in
`domainkit.contrib.auth.api`, reads the user's hash from the table and
checks it.

### Hashing threads and the queue

Hashing is slow on purpose: about 0.4 seconds for the default hasher.
`authenticate` and the repositories run it on the process's hashing
threads (`HASHING_THREADS`, 1 by default), so the event loop never
waits.

- **The queue is short.** At most `HASHING_QUEUE` calls (8) wait for a
  thread. When the queue is full, a login, a password change, or a new
  user answers `503 password_checks_busy` at once, instead of queueing
  work that the process cannot finish.
- **`Retry-After` says when to come back.** It is the time the queue
  takes to drain, from how long hashes have taken, plus up to half again
  at random, so refused clients do not all return together. It is at
  least 1 second and at most 60 seconds.
- **A call keeps its place** until its hash is done, even when its client
  gives up. So the work in flight never exceeds the limits.
- **Some places are reserved.** `HASHING_RESERVED` places of the queue
  (1) are kept for signed-in users who change their password and for
  staff who set one, so a flood of logins cannot lock them out.
- **Unknown usernames are hashed too**, so response times do not tell
  which usernames exist.

Raise the limits on processes with cores to spare. `LOGIN_RATE` limits
each client and each username; these settings limit the process. They
keep the rest of the API responsive during a flood of logins, but logins
themselves may then be refused.

### Hashers

`AUTH.PASSWORD_HASHERS` lists hasher classes. The first one hashes new
passwords. Every one listed checks the passwords it once hashed.

| Hasher | Notes |
| --- | --- |
| `PBKDF2PasswordHasher` | The default: PBKDF2-HMAC-SHA256, 1,500,000 iterations, from the standard library. |
| `PBKDF2SHA1PasswordHasher` | Checks hashes moved from older systems. |
| `Argon2PasswordHasher` | Argon2id; needs the `argon2` extra. Put it first to hash new passwords with it. |
| `ScryptPasswordHasher` | scrypt, from the standard library. |
| `FastPBKDF2PasswordHasher` | One iteration, for tests only. Staging and production refuse it (`auth.E004`). |

When a password checks against an older or weaker hash, the login stores
a new hash made with the first hasher. An Argon2 hash is older when its
variant (`argon2i`, `argon2d`), version, length, or costs differ from
today's, or its salt holds fewer than 128 bits. A wrong password against
an older hash takes as long as against a current one, so response times
do not tell which accounts still have a weak hash.

The hashes have Django's format. A project that moves from Django keeps
its users' passwords: copy the hashes into `password`.

A hasher of your own subclasses `PasswordHasher` (or one of the
hashers). For example, more PBKDF2 iterations; each password gets the new
cost at its next login:

```python
from domainkit.contrib.auth.hashers import PBKDF2PasswordHasher


class StrongerPBKDF2(PBKDF2PasswordHasher):
    iterations = 2_000_000


AUTH = {"PASSWORD_HASHERS": ["accounts.hashers.StrongerPBKDF2"]}
```

### Validators

`AUTH.PASSWORD_VALIDATORS` lists the rules that new passwords must
follow. Each entry has a `"NAME"`, the dotted path of a class, and
optional `"OPTIONS"`, the keyword arguments of the class:

```python
AUTH = {
    "PASSWORD_VALIDATORS": [
        {"NAME": "domainkit.contrib.auth.validators.UserAttributeSimilarityValidator"},
        {
            "NAME": "domainkit.contrib.auth.validators.MinimumLengthValidator",
            "OPTIONS": {"min_length": 12},
        },
        {"NAME": "domainkit.contrib.auth.validators.CommonPasswordValidator"},
        {"NAME": "domainkit.contrib.auth.validators.NumericPasswordValidator"},
    ],
}
```

`validate_password(password, user)` runs them all and raises
`InvalidPassword` (422 `invalid_password`) with every rule broken.
`await avalidate_password(password, user)` does the same in async code,
and awaits the rules that are `async`, such as the
[breached-password check](#breached-passwords). Each message is
translated into the request's language. `password_help_texts()`
describes the rules, for the page where users choose a password:

```python
from domainkit.contrib.auth import (
    InvalidPassword,
    password_help_texts,
    validate_password,
)

for line in password_help_texts():
    print(line)
try:
    validate_password("12345", user=None)
except InvalidPassword as exc:
    print(exc.errors)
```

```text
Your password can't be too similar to your other personal information.
Your password must contain at least 8 characters.
Your password can't be a commonly used password.
Your password can't be entirely numeric.
['This password is too short. It must contain at least 8 characters.', 'This password is too common.', 'This password is entirely numeric.']
```

| Validator | Options | Refuses |
| --- | --- | --- |
| `UserAttributeSimilarityValidator` | `user_attributes` (`username`, `first_name`, `last_name`, `email`), `max_similarity` (0.7, at least 0.1) | Passwords like the username, the names, or the email, or parts of them. |
| `MinimumLengthValidator` | `min_length` (8) | Shorter passwords. |
| `CommonPasswordValidator` | `password_list_path` | Django's list of 20,000 common passwords, or your own (one per line, gzipped or not). |
| `NumericPasswordValidator` | | Passwords made only of digits. |
| `LettersAndDigitsValidator` | | Passwords without at least one letter and one digit, of any script ([PCI DSS](pci-dss.md)). |
| `BreachedPasswordValidator` | a `BreachedPasswordSettings` | Passwords seen in data breaches ([Breached passwords](#breached-passwords)). `AUTH.BREACHED_PASSWORDS` turns it on and gives its settings: you do not list it. |

The rules' refusals and descriptions are in the shipped catalogs, in
English and Arabic (*new in 2.5*). A rule whose text holds a value
translates its text first, then fills the value in, written as the
language writes numbers: `MinimumLengthValidator` translates `This
password is too short. It must contain at least {min_length}
characters.` (with its singular), and `UserAttributeSimilarityValidator`
`The password is too similar to the {label}.`. The label is the
attribute's name, spaced (`first name`), translated in the `field`
context; the catalogs name `username`, `first name`, `last name`,
`email`, and `mobile number`, and your catalog adds your own attributes:

```po
msgctxt "field"
msgid "national id"
msgstr "رقم الهوية الوطنية"
```

`PASSWORD_VALIDATORS = []` checks nothing (but `BREACHED_PASSWORDS`,
when it is on). A validator of your own is a class with
`validate(password, user)`, which raises `PasswordValidationError` (from
`domainkit.contrib.auth`), and `help_text()`. The validators collect
only that error; any other exception goes up to the caller. Each message
is translated as it is collected, by its exact text; a rule whose text
holds a value translates it itself, then fills the value in, as
`MinimumLengthValidator` does.

```python
from domainkit.contrib.auth import PasswordValidationError


class NoShopNameValidator:
    def validate(self, password: str, user: object | None = None) -> None:
        if "shop" in password.lower():
            raise PasswordValidationError("This password contains the shop's name.")

    def help_text(self) -> str:
        return "Your password can't contain the shop's name."
```

`validate` may be `async def`, for a rule that reads a table or asks
another service:

```python
from accounts.uow import AccountsUnitOfWork
from domainkit.contrib.auth import PasswordValidationError


class BannedPasswordValidator:
    async def validate(self, password: str, user: object | None = None) -> None:
        async with AccountsUnitOfWork() as uow:
            banned = await uow.banned_passwords.contains(password.lower())
        if banned:
            raise PasswordValidationError("The security team banned this password.")

    def help_text(self) -> str:
        return "Your password can't be one the security team banned."
```

`avalidate_password` awaits it. `validate_password` cannot, so it
refuses to run with an `async` rule configured (`ImproperlyConfigured`),
rather than take the password unchecked: call `avalidate_password` from
async code, or `run_async(avalidate_password(...))` from a command.

`create_user` and the repositories hash a password; they do not validate
it. The routes that take a password (`/auth/password`, resets, signup,
the admin API, a change at the first login or at a phone's enrollment)
and the commands (`createsuperuser`, `changepassword`) call
`avalidate_password` for you. `AUTH.PASSWORD_HISTORY` also refuses a
password the user had before ([PCI DSS](pci-dss.md)).

### Breached passwords

NIST SP 800-63B asks that a new password be compared with passwords
known to be compromised, and refused with the reason when it is one.
`CommonPasswordValidator` holds 20,000 of them. `AUTH.BREACHED_PASSWORDS`
adds the hundreds of millions that Have I Been Pwned has seen in data
breaches, through its Pwned Passwords range API:

```python
AUTH = {"BREACHED_PASSWORDS": {"ENABLED": True}}
```

The check runs after `PASSWORD_VALIDATORS`, wherever passwords are
chosen, and refuses a password seen `MIN_COUNT` times or more: "This
password has appeared in a data breach: choose another one."

- **The password never leaves the service, nor its hash.** The check
  hashes the password with SHA-1 and sends the first five characters of
  the hash, `GET <URL>21BD1`. The API answers every hash ending it knows
  under that prefix, padded to 800 to 1,000 lines (`Add-Padding: true`)
  so the answer's size does not tell the prefix, and the ending is
  compared here. domainkit's logs name the API's origin, never the
  prefix.
- **It goes through the outgoing client** (`domainkit.http.client`, the
  `httpclient` extra), so the call is bounded by `TIMEOUT` (2 seconds,
  retries included) and by the request's deadline, and after
  `HTTP_CLIENT.BREAKER_FAILURES` failures in a row it fails at once for
  `BREAKER_COOLDOWN` seconds instead of making each person wait.
- **It fails open by default.** When the API does not answer in time,
  or answers what does not read as hashes, the password is let through
  with a warning in the log; the other rules, the 20,000 common
  passwords among them, still apply. Keep `CommonPasswordValidator` in
  `PASSWORD_VALIDATORS` for that reason. `FAIL_CLOSED = True` refuses the
  password instead (`503 breached_passwords_unavailable`): signups and
  resets then stop while the API is down ([ADR 0148](../adr/0148-check-new-passwords-against-breaches-and-let-rules-be-async.md)).
- **`URL` can name a mirror** that serves the hashes you downloaded from
  Have I Been Pwned on the same paths, so no call leaves your network.

The routes that check a password inside their transaction (a reset, a
signup's code, a change at the first login) hold their database
connection for the check, `TIMEOUT` at most.

In tests, fake the API with `domainkit.http.client.stub`, or leave the
check off in the test settings: tests never call it on their own.

## Users in tests

Hash with `FastPBKDF2PasswordHasher` in tests: the default hasher takes
about 0.4 seconds a password.

```python
# config/settings/test.py
AUTH = {"PASSWORD_HASHERS": ["domainkit.contrib.auth.hashers.FastPBKDF2PasswordHasher"]}
```

`domainkit.contrib.auth.testing` stores users and groups as the
application does, so their events are published:

```python
from domainkit.contrib.auth.testing import create_group, create_user


async def test_clerks_read(async_client) -> None:
    clerks = await create_group("billing.view_invoice", name="clerks")
    ann = await create_user(groups=[clerks.name], email="ann@example.com")
    client = await async_client.authenticated_as(ann)
    assert (await client.get("/api/v1/billing/invoices")).status_code == 200
```

- `create_user(*permissions, password=None, groups=(), superuser=False, **fields)`
  grants the permission keys, joins the groups by name, and names the
  user `user-<hex>` unless you give a username. Where the username does
  not identify users, and you give no field that does, the first field of
  `AUTH.IDENTIFIERS` gets a value: `user-<hex>@example.com` for `email`.
  With `password`, a test can log in through the API.
- `create_group(*permissions, name=None)` names the group `group-<hex>`
  unless you give a name.

In a sync test, run them in the client's event loop:
`client.run(create_user, "billing.view_invoice")`. To issue a token
directly, see [the hub page](../auth.md#tests).

## Options

| `AUTH` key | Default | What it does |
| --- | --- | --- |
| `USER_MODEL` | `"domainkit.contrib.auth.models.User"` | The user class. |
| `USER_TABLE` | `None` | The table of a custom user. |
| `BACKENDS` | `ModelBackend` | Backends, asked in order. |
| `PASSWORD_HASHERS` | PBKDF2, PBKDF2-SHA1, Argon2, scrypt | The first hashes new passwords. |
| `PASSWORD_VALIDATORS` | the four rules above | `[]` checks nothing. |
| `BREACHED_PASSWORDS` | off | New passwords checked against data breaches ([Breached passwords](#breached-passwords)). |
| `HASHING_THREADS`, `HASHING_QUEUE`, `HASHING_RESERVED` | 1, 8, 1 | Threads, waiting calls, and reserved places. |
| `PASSWORD_HISTORY` | `0` | Old passwords a new one may not repeat. |

[The settings tables](../auth.md#settings) give every key, its bounds,
and the full defaults.

## Errors and refusals

| Exception | HTTP | Code | When |
| --- | --- | --- | --- |
| `UsernameTaken` | 409 | `username_taken` | Another user has the username. |
| `InvalidUsername` | 422 | `invalid_username` | Empty, too long, other characters, or starts with `erased-`. |
| `InvalidEmail` | 422 | `invalid_email` | Not one plain address. |
| `InvalidMobileNumber` | 422 | `invalid_mobile_number` | Not a number in international form. |
| `IdentifierRequired` | 422 | `identifier_required` | No username, address, or number; or, for a new user, none of `AUTH.IDENTIFIERS`. |
| `EmailTaken`, `MobileNumberTaken` | 409 | `email_taken`, `mobile_number_taken` | Another user has the address (where addresses identify users), or the number. |
| `InvalidPassword` | 422 | `invalid_password` | A validator, or the history, refuses it; `errors` lists each rule. |
| `GroupNameTaken` | 409 | `group_name_taken` | Another group has the name. |
| `InvalidGroupName` | 422 | `invalid_group_name` | Empty, or more than 150 characters. |
| `UnknownPermission` | 404 | `unknown_permission` | No permission has the key. |
| `UnknownGroup` | 404 | `unknown_group` | No group has the name. |
| `UserNotFound`, `GroupNotFound` | 404 | `user_not_found`, `group_not_found` | No user or group has the id. |
| `UnknownUserField` | 422 | `unknown_user_field` | The model has no such field, or the field changes another way. |
| `InvalidUserField` | 422 | `invalid_user_field` | A value does not fit the field's type or markers. |
| `PasswordChecksBusy` | 503 | `password_checks_busy` | The hashing queue is full; see `Retry-After`. |
| `BreachedPasswordsUnavailable` | 503 | `breached_passwords_unavailable` | The breached-password check could not answer, and `BREACHED_PASSWORDS.FAIL_CLOSED` is set. |

## Common mistakes

- **Creating users with a password nobody validated.** `create_user`
  only hashes. Call `await avalidate_password(password, user)` where
  people choose passwords.
- **Calling `validate_password` with an `async` rule configured.** It
  raises `ImproperlyConfigured`: await `avalidate_password` instead.
- **Swapping the user model after the first `migrate`.** Do it first.
- **Forgetting `AUTH = {**AUTH, ...}` in other settings files.** A plain
  `AUTH = {...}` there drops `USER_MODEL`.
- **Leaving `FastPBKDF2PasswordHasher` in deployed settings.** Staging
  and production refuse to start with it.
- **Writing a backend that grants on objects without `object_grants`.**
  The admin API then lets only superusers change users and groups.

## See also

- [Installing auth](install.md): the tables and the permissions of your
  classes.
- [Authenticating requests](authentication.md): how a request proves
  who sent it.
- [Logging in](logging-in.md): passwords at login, resets, and signup.
- [Protecting routes](routes.md): rules that check permissions, and the
  admin API.
- [PCI DSS v4.0's passwords](pci-dss.md): the preset for card data.
