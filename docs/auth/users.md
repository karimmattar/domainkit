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
| `email_verified` | bool | Whether the user proved that they read mail at `email`. A new address clears it. |
| `mobile_number` | text, unique when set | A number in international form (E.164), such as `+33612345678`. Spaces, dots, dashes, and brackets are dropped. |
| `mobile_number_verified` | bool | Whether the user proved that they receive texts at `mobile_number`. A new number clears it. |
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
`revoke(*permissions)`, `verify_email()`, `verify_mobile_number()`, and
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
| `uow.users.find_for_staff(value)` | The user an operator names: by username, then by `find_by_identifier`. The commands use it. |
| `uow.users.find_by_email(email)` | Every user with that address, in any case of its ASCII letters. |
| `uow.users.set_password(user, password)` | Hashes and stores a new password; `None` makes it unusable. Refuses a password of the user's history (`PASSWORD_HISTORY`). |
| `uow.users.password_hash(user_id)` | The stored hash, read from the table. |
| `uow.users.remove(user)` | Deletes the user and every credential of theirs (below). |
| `uow.users.page(limit=..., after=..., search=...)` | A page of users by username, after the username `after`. `search` matches part of the username, or of the email address or mobile number where `AUTH.IDENTIFIERS` names them, in any case. Users with no username sort last, where `after`, a username, cannot reach them: page those with the admin API (`GET /users`) and its cursors. |
| `uow.groups.create(name)`, `get_by_name(name)`, `get_many_by_name(names)`, `rename(group, name)` | Groups. |
| `uow.permissions.get(key)`, `get_many(keys)`, `all()`, `keys()` | Permissions, by key. |
| `uow.tokens.issue(user_id, ttl=...)`, `revoke_user(user_id)` | Tokens; see [Authenticating requests](authentication.md). |

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
an active superuser, and no to an inactive user. `user.has_perms(keys)`,
`user.get_all_permissions()`, `user.get_user_permissions()`,
`user.get_group_permissions()`, and `user.has_domain_perms("shop")` read
the loaded user too. `has_domain_perms` is Django's `has_module_perms`.

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
recovery codes, step-ups, transaction authorizations, and the history
of their passwords. `uow.users.forget_credentials(user_id)` deletes the
credentials alone; privacy erasure uses it too.

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
- a backend that raises `PermissionDenied` ends either search with "no".

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

The [LDAP backend](identity.md#ldap-and-active-directory) checks
passwords against a directory.

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
  with `InvalidGrant`. Granting twice changes nothing. Each takes an
  optional `uow`, an `ObjectPermissionsUnitOfWork`, to join your
  transaction.
- **Each grant and revocation is an event** (`ObjectPermissionGranted`,
  `ObjectPermissionRevoked`) that names who gave what to whom, so the
  audit trail records it.
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
`password_help_texts()` describes the rules, for the page where users
choose a password:

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

`PASSWORD_VALIDATORS = []` checks nothing. A validator of your own is a
class with `validate(password, user)`, which raises
`PasswordValidationError` (from `domainkit.contrib.auth`),
and `help_text()`. `validate_password` collects only that error; any
other exception goes up to the caller.

```python
from domainkit.contrib.auth import PasswordValidationError


class NoShopNameValidator:
    def validate(self, password: str, user: object | None = None) -> None:
        if "shop" in password.lower():
            raise PasswordValidationError("This password contains the shop's name.")

    def help_text(self) -> str:
        return "Your password can't contain the shop's name."
```

`create_user` and the repositories hash a password; they do not validate
it. The routes that take a password (`/auth/password`, resets, signup,
the admin API) and the commands (`createsuperuser`, `changepassword`)
call `validate_password` for you. `AUTH.PASSWORD_HISTORY` also refuses a
password the user had before ([PCI DSS](pci-dss.md)).

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

## Common mistakes

- **Creating users with a password nobody validated.** `create_user`
  only hashes. Call `validate_password(password, user)` where people
  choose passwords.
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
