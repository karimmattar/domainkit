# Users, groups, backends, and passwords

## Users and groups

`User` has the fields of Django's user, and is an aggregate like any other:
it records events, and repositories load it with its groups and
permissions.

| Field | Type | Notes |
| --- | --- | --- |
| `id` | UUID | |
| `username` | text, unique | 1 to 150 letters, digits, and `@ . + - _`; NFKC-normalized. Names that start with `erased-`, in any case, are kept for erased accounts. |
| `password` | `PasswordHash` | Never a raw password; see below. |
| `email`, `first_name`, `last_name` | text | The email is one plain address or empty, with its domain lower-cased (see below). |
| `email_verified` | bool | Whether the user proved they read mail at `email`; a new address clears it. |
| `is_active` | bool | Inactive users cannot log in and have no permissions. |
| `is_staff` | bool | May use the admin API (with its permissions). |
| `is_superuser` | bool | Has every permission without grants. |
| `last_login`, `date_joined` | UTC timestamps | |
| `groups`, `user_permissions` | sets | Grants, through link tables. |

An email address is one address alone, in ASCII, checked as
`domainkit.contrib.mail` checks the addresses it sends to:
`user@example.com`, never `Name <user@example.com>`, a comment, or a list,
and an internationalized domain in its `xn--` form. Creating a user,
changing the email (the admin API, `update`), signing up, and asking for
a reset all refuse anything else (`InvalidEmail`, a 422 `invalid_email`),
so the address mail goes to is the whole stored value, and no display
name reaches a mail's text. `validate_email(text)` checks one. Addresses
compare by their ASCII letters in any case and by nothing else
(`same_email`): `find_by_email` never takes a look-alike, such as one
with the KELVIN SIGN, which SQL's `lower()` makes a `k` on PostgreSQL,
for another's address. Rows stored before these checks keep their
address; the worker skips one that cannot take mail (below).

A `Group` has a unique name and a set of permissions; members have them
all. `AnonymousUser` stands for requests without credentials: no id, no
permissions.

```python
from domainkit.contrib.auth import AuthUnitOfWork, authenticate, has_perm

async with AuthUnitOfWork() as uow:
    alice = await uow.users.create_user("alice", password, email="a@example.com")
    editors = await uow.groups.create("editors")
    editors.grant(await uow.permissions.get("shop.publish_product"))
    alice.join(editors)
    await uow.commit()

user = await authenticate(username="alice", password=password)
if user is not None and await has_perm(user, "shop.publish_product"):
    ...
```

`user.has_perm(key)` answers from the loaded user, without a query;
`await has_perm(user, key, obj)` also asks every backend, which is how
object permissions work. Both say yes to an active superuser and no to an
inactive user. `has_perms`, `get_all_permissions`, and `user.has_domain_perms(label)`
(Django's `has_module_perms`) do what their names say.

`UserRepository.create_user` and `create_superuser` hash the password they
are given and refuse a taken username (`UsernameTaken`, a 409), also when
two requests race for it and the unique constraint refuses the second; they
do not validate it. Call `validate_password(password, user)` where people choose
passwords. `set_password(user, None)` makes a password unusable.
`remove(user)` deletes the user with every credential of theirs, in the
same transaction: tokens, sessions, refresh tokens, client certificates,
and the second factor's secret and recovery codes
(`forget_credentials(user_id)`, which privacy erasure uses too). A user
model that is `SoftDeletable` keeps the row, marked deleted: `remove`
forgets the credentials all the same and deactivates the user
(`UserStatusChanged`), since logging in and every request read
`is_active`, not `deleted_at`; `restore` brings the row back inactive,
for staff to activate. A bulk `delete_rows` bypasses it, and leaves them.

Changes are recorded as events, never with passwords or hashes in them:
`UserCreated`, `PasswordChanged`, `UserStatusChanged`, `UserJoinedGroup`,
`UserLeftGroup`, `UserPermissionGranted`, `UserPermissionRevoked`,
`GroupCreated`, `GroupRenamed`, `GroupPermissionGranted`,
`GroupPermissionRevoked`, and `RefreshTokenReused` (a used JWT refresh
token came back, so its login was ended; it is also logged as a security
warning). With `domainkit.contrib.audit` installed, each
becomes a row of the audit trail (`auth.user_permission_granted`). The
username in `UserCreated` and `UserDeleted` is personal data: the trail keeps
it as a pseudonym of the user's id, which `auditforget <user id>` makes
unlinkable (see [contrib.md](../contrib.md)).

## A custom user model

Swap the user for one of your own, as with `AUTH_USER_MODEL`: subclass
`AbstractUser` (or `AbstractBaseUser`, which has no username, names, or
staff flag), give it a table, and name both in the settings. Do it before
the first `migrate` that creates users.

```python
# accounts/domain/model.py
class Account(AbstractUser):
    phone: str = ""
    risk_flag: bool = False
    REQUIRED_FIELDS = ("email", "phone")  # what createsuperuser asks for
    EDITABLE_FIELDS = AbstractUser.EDITABLE_FIELDS | {"phone"}  # staff may change


# accounts/infrastructure/orm.py
accounts = Table(
    "accounts_users",
    metadata,
    *abstract_user_columns(),
    Column("phone", String(32), nullable=False),
    Column("risk_flag", Boolean, nullable=False),
    info={"domain": "accounts"},
)
```

Staff see and change only the fields a model names. `EDITABLE_FIELDS`
lists what the admin API may change (`email`, `first_name`, and
`last_name` on `AbstractUser`, nothing on `AbstractBaseUser`), and
`READ_ONLY_FIELDS` what it only shows (`date_joined`). A new field, such
as `risk_flag` above, stays out of the admin API until you list it.
Startup refuses names the model lacks, and protected fields (credentials,
status flags, grants, the username) in `EDITABLE_FIELDS`. A model may add
to `PROTECTED_FIELDS`, never take away: `PROTECTED_FLOOR` (`id`,
`version`, `password`, `last_login`, `date_joined`, `email_verified`,
`is_active`, `is_staff`, `is_superuser`, `groups`, `user_permissions`)
stays protected whatever the model says, since those change through their
own methods, which the admin API guards and which record their own
events. A user's `repr` shows its id and status flags only, never names or
addresses, and `request.auth`'s shows the user's id, the scheme, and the
scopes.

```python
AUTH = {
    "USER_MODEL": "accounts.domain.model.Account",
    "USER_TABLE": "accounts.infrastructure.orm.accounts",
}
```

The model's links to groups and permissions get tables named after its
table (`accounts_users_groups`, `accounts_users_permissions`), and the
default `domainkit_users` tables are then neither migrated nor dropped.
Startup refuses a table without a column for one of the model's fields, or
without `info={"domain": ...}` naming the installed domain that defines it:
migrations and `DATABASE_ROUTING` follow that domain.

Users who log in with their email subclass `AbstractBaseUser`:

```python
class Member(AbstractBaseUser):
    USERNAME_FIELD = "email"
    EMAIL_FIELD = "email"
    email: str


members = Table(
    "members",
    metadata,
    *abstract_base_user_columns(),
    Column("email", String(254), nullable=False, unique=True),
    info={"domain": "members"},
)

user = await authenticate(email="boss@example.org", password=password)
```

`get_user_model()` returns the class in use, and `get_user_table()` its
table.

### Users' personal data

`AbstractUser` marks `username`, `email`, `first_name`, and `last_name`
`Personal(of="id")`, so `domainkit.contrib.privacy` exports them
(`privacy export --subject <user id>`) and erases them. Mark the personal
fields of a custom model too (a `Member`'s `email` above), and give it an
`erase_personal_data` method if they cannot be erased by default
([privacy.md](../privacy.md)).

Erasing a user (`privacy erase --subject <user id>`) runs
`AbstractUser.erase_personal_data`:

- the username becomes `erased-` and 32 hex digits: an HMAC-SHA256 of the
  name (folded as lookups fold it) under a key derived from `SECRET_KEY`.
  It stays unique, and the old name stays taken: creating a user or
  signing up with it, in any case, is refused as a taken username, so
  nobody else can become the person erased. The key's fallbacks are
  checked too; a name erased under a key dropped from
  `SECRET_KEY_FALLBACKS` is free again. Names that start with `erased-`
  are kept for this: signing up, creating a user (with any user model:
  the repository checks), and `validate_username` refuse them
  (`InvalidUsername`), so no one can pass for an erased account. A name
  stored before that, which only starts with `erased-`, is erased like
  any other; one already of the erased form is left as it is, so erasing
  twice changes nothing;
- the email and names become empty, and the address unverified;
- the account ends in the same commit: it is deactivated and its password
  made unusable (both audited). Right after, every token of the user
  (logins, browser sessions, personal tokens, client certificates) and
  their second factor (secret and recovery codes) are deleted.

The row stays, with its id, so what refers to the user still does.
Erasing again changes nothing.

Fixtures (`dumpdata`, `loaddata`) never hold tokens, sessions,
certificates, TOTP secrets, or recovery codes: their tables are state
(`info={"fixtures": False}`). A fixture of users still holds password
hashes: keep it as safe as the database. Auth's tables are guarded:
`loaddata` writes users, groups, permissions, or their links only with
`--include-guarded`, and `flush` keeps them, tokens and second factors
included, unless it is given
([reference.md](../reference.md#data-fixtures-flush-and-seeds)).

## Backends

`AUTH.BACKENDS` lists backend classes, asked in order. `authenticate`
returns the first user a backend accepts; `has_perm` is true when any
backend grants the permission. A backend that raises `PermissionDenied`
ends either search with "no". `ModelBackend` checks passwords against the
user table, costs an unknown username as much time as a wrong password,
and grants what users and their groups were given.

A backend subclasses `BaseBackend` and overrides what it needs:
`authenticate(credentials)`, `get_user(user_id)`,
`get_all_permissions(user, obj)`, and `has_perm(user, perm, obj)`.
Object permissions:

```python
class OwnerBackend(BaseBackend):
    async def has_perm(self, user, perm, obj=None):
        return (
            perm == "billing.change_invoice"
            and getattr(obj, "owner_id", None) == user.id
        )


AUTH = {
    "BACKENDS": [
        "domainkit.contrib.auth.backends.ModelBackend",
        "billing.auth.OwnerBackend",
    ]
}
```

### Grants on single objects

`domainkit.contrib.objectpermissions` stores grants of one permission on
one object, to a user or to a group (django-guardian's model). Install it
after auth, add its backend after `ModelBackend`, then `makemigrations`
and `migrate`:

```python
INSTALLED_DOMAINS = [
    ...,
    "domainkit.contrib.auth",
    "domainkit.contrib.objectpermissions",
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
    revoke_permission,
    visible_scope,
)

await grant_permission(actor, "catalog.change_product", product, user=ada.id)
await grant_permission(actor, "catalog.view_product", product, group=editors.id)
await revoke_permission(actor, "catalog.change_product", product, user=ada.id)

# Rules that look at objects now see the grants:
products = ResourceRouter(
    ...,
    rule=AllOf(IsAuthenticated(), HasObjectPermissions("catalog.change_product")),
    scope=visible_scope("view", Product),
)
```

- Granting and revoking stay within what the actor holds: they need the
  permission itself, on the model or on the object, and
  `objectpermissions.add_objectpermission` to grant
  (`delete_objectpermission` to revoke); superusers have both. No one
  grants to themselves, and a permission of another model is refused
  (`InvalidGrant`). Granting twice changes nothing.
- Each grant and revocation is an event (`ObjectPermissionGranted`,
  `ObjectPermissionRevoked`) naming who gave what to whom, so the audit
  trail records it.
- `visible_scope("view", Product)` is a resource's `scope`: callers who
  hold `catalog.view_product` see every product; others see those granted
  to them or to one of their groups, filtered in SQL with an `EXISTS` on
  the grants, so pages, counts, and cursors never see the rest; anonymous
  and inactive callers see none, and neither do credentials limited to
  scopes that leave the permission out. `visible_to(user, "view",
  Product, request=request)` gives the same criteria (`VisibleTo`) for
  queries of your own; without `request`, scopes are not consulted.
- In memory, `VisibleTo` reads the grants it is given:
  `visible_scope("view", Product, grants=InMemoryObjectPermissions(...))`
  in tests with fake repositories.
- Grants are stored in the database of the objects they are about, in
  `domainkit_object_permissions`, which every database hosting a domain
  gets. Ids may be UUIDs, integers, or text, each in a column of its type.
  User and group ids are not foreign keys. Deleting a user (a
  repository's `remove`, or the admin API) deletes the grants they held
  in the user's database, in the same transaction; grants kept in another
  database stay, unused and never reused.
- Grants go with their object: when the ORM deletes an object's row (a
  repository's `remove`), the grants on it are deleted in the same
  transaction, so an object stored later with the same id (a slug, or an
  integer the database hands out again) starts without them. A soft
  delete keeps them, for a restore. Bulk deletes (`delete_rows`) bypass
  the ORM and leave grants behind: remove such objects one by one, or
  delete their grants in the same transaction. `flush` keeps the grants
  table (it is guarded), but deletes the grants on the objects of every
  table it empties.

## Passwords

A user holds a `PasswordHash`, which only the hashers make, so a raw
password can never be stored by mistake, and hashes never appear in logs.
Hashing is slow on purpose; `authenticate` and the repositories run it on
the process's hashing threads (`HASHING_THREADS`), so the event loop never
waits. At most `HASHING_QUEUE` calls wait for a thread. When the queue is
full, a login, password change, or user creation answers
`503 password_checks_busy` at once, rather than queueing work the process
cannot finish. Its `Retry-After` is the time the queue takes to drain, from
how long hashes have taken, jittered up to half again so refused clients do
not all return together (at least 1 s, at most 60 s). A call keeps its place
until its hash is done, even when its client gives up, so the work in flight
never exceeds the limits. `HASHING_RESERVED` places of the queue (1 by
default) are kept for signed-in users changing their password and for staff
setting one, so a flood of logins cannot lock them out. Unknown usernames are hashed too,
so response times do not reveal which exist. Raise the limits on processes
with cores to spare; `LOGIN_RATE` limits each client and username, these
limit the process. They keep the rest of the API responsive during a login
flood, but logins themselves may then be refused: stopping a flood from
many addresses takes measures before hashing, such as locking an account
after repeated failures.

`AUTH.PASSWORD_HASHERS` lists hasher classes. The first hashes new
passwords; every one listed checks passwords it once hashed. When a
password checks against an older or weaker hash, the login stores a new
hash made with the first hasher. An Argon2 hash is older when its
variant (`argon2i`, `argon2d`), version, length, or costs differ from
today's, or its salt holds fewer than 128 bits. A wrong password against
an older hash takes at least as long as against a current one: the first
hasher hashes it, as a right one is hashed again, so response times do
not tell which accounts still have a weak, migrated hash (with PBKDF2
first, a wrong password against a `pbkdf2_sha1` hash of 20,000 iterations
answered in 0.006 s before 0.10, against 0.56 s for a current hash).

| Hasher | Notes |
| --- | --- |
| `PBKDF2PasswordHasher` | Default: PBKDF2-HMAC-SHA256, 1,500,000 iterations, from the standard library. |
| `PBKDF2SHA1PasswordHasher` | Checks hashes moved from older systems. |
| `Argon2PasswordHasher` | Argon2id; the `argon2` extra. Put it first to hash new passwords with it. |
| `ScryptPasswordHasher` | scrypt, from the standard library. |
| `FastPBKDF2PasswordHasher` | One iteration, for tests only; staging and production refuse it. |

The hashes have Django's format, so a project moving from Django keeps its
users' passwords: copy the hashes into `password`.

`AUTH.PASSWORD_VALIDATORS` lists the rules new passwords must follow, each
`{"NAME": "<dotted path>", "OPTIONS": {...}}`; `validate_password` runs them
all and raises `InvalidPassword` (a 422) with every rule broken, and
`password_help_texts()` describes them for clients.

| Validator | Options | Refuses |
| --- | --- | --- |
| `UserAttributeSimilarityValidator` | `user_attributes`, `max_similarity` (0.7) | Passwords like the username, names, or email, or parts of them. |
| `MinimumLengthValidator` | `min_length` (8) | Shorter passwords. |
| `CommonPasswordValidator` | `password_list_path` | Django's list of 20,000 common passwords, or your own (one per line, gzipped or not). |
| `NumericPasswordValidator` | | Passwords made only of digits. |

A validator of your own is a class with `validate(password, user)`, raising
`PasswordValidationError`, and `help_text()`.

In tests, hash with `FastPBKDF2PasswordHasher`:

```python
# config/settings/test.py
AUTH = {"PASSWORD_HASHERS": ["domainkit.contrib.auth.hashers.FastPBKDF2PasswordHasher"]}
```

