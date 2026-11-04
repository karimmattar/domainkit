# Flags: feature flags and dynamic settings

A feature flag turns a feature on for everyone, for no one, or for some
users, without a deployment. A dynamic setting is a typed value, such as
"items per order", that staff change while the service runs. The flags
app keeps both in the database, caches them, and records every change in
the [audit trail](audit.md).

On this page you learn how to:

- check a flag in code, and hide a route behind a flag;
- roll a feature out to some users, some groups, or a percentage;
- declare a dynamic setting with a type and a default, and read it;
- keep a secret setting's value out of the audit trail;
- change flags and settings from the command line or the staff API;
- know how fast a change reaches every process.

## Install

Add the app, then make and apply its migration. It brings two tables:
`domainkit_flags` and `domainkit_dynamic_settings`.

```python
INSTALLED_DOMAINS = [
    "domainkit.contrib.contenttypes",
    "domainkit.contrib.auth",  # for the staff API and the request's user
    "domainkit.contrib.audit",  # to record each change
    "domainkit.contrib.flags",
]
```

```bash
python manage.py makemigrations -m "flags"
python manage.py migrate
```

The flags app needs neither auth nor audit to run. Without auth, there
is no staff API, and you pass the user yourself. Without audit, changes
are not recorded.

## A first example

Turn the flag `beta` on for the group `staff` and for 10% of the other
users, then check it in code:

```bash
python manage.py flags set beta --percentage 10 --groups staff --note "new checkout"
```

```text
beta: 10% of users, users [], groups ['staff']  (new checkout)
```

```python
from types import SimpleNamespace

from domainkit.contrib.flags import flag_enabled

user = SimpleNamespace(id=7, groups=[SimpleNamespace(name="staff")])

print(await flag_enabled("beta", user))
print(await flag_enabled("beta"))  # no user: anonymous
print(await flag_enabled("nothing", user))  # nobody created it
```

```text
True
False
False
```

User 7 is in the group `staff`, so the flag is on for that user.
An anonymous caller gets a flag only when it is on for everyone. A flag
that nobody created is off.

## Who a flag is on for

A `Flag` is an [aggregate](../learning.md#words) whose id is its name.
Its rules decide who gets it, in this order:

| Rule | What it does |
| --- | --- |
| `everyone` | `True`: on for all. `False`: off for all. `None` (the default): the rules below decide. |
| `users` | User ids that the flag is on for. |
| `groups` | Group names that the flag is on for. |
| `percentage` | A share of the other users, 0 to 100. |
| `note` | Free text for people, at most 255 characters. It decides nothing. |

The percentage puts each user in one of 100 buckets. The bucket comes
from a SHA-256 hash of the flag's name and the user's id. So a user
stays in or out while the percentage stays the same, and stays in when
the percentage grows. Each process computes the same bucket.

A name is lower-case letters and digits, then up to 99 more of these or
`_`, `.`, `-`. A percentage outside 0 to 100 raises `InvalidFlag`.

## Checking a flag: `flag_enabled`

`await flag_enabled(name, user=None)` returns `True` or `False`. The
user is the request's user from `domainkit.contrib.auth`, or any object
with an `id` and, if you like, `groups` whose items have a `name` (or are
names). A user whose `is_authenticated` is not `True` counts as
anonymous.

```python
from domainkit.contrib.flags import flag_enabled

if await flag_enabled("new_checkout", user=user):
    ...
```

## A route behind a flag: `RequireFlag`

`RequireFlag(name)` is a FastAPI dependency. It answers 404 while the
flag is off for the caller, so the route looks absent:

```python
from fastapi import Depends

from domainkit.contrib.flags import RequireFlag
from domainkit.http.access import Public


@router.get("/beta", dependencies=[Depends(RequireFlag("beta")), Public])
async def beta() -> dict[str, str]:
    return {"hello": "beta"}
```

The caller is the user that `AuthenticationMiddleware` found. `RequireFlag`
is not an access rule: the route still needs `Require(...)` or `Public`.

## Dynamic settings

Declare a dynamic setting in code, with a key, a type, and a default.
pydantic checks every value against the type:

```python
from typing import Annotated

from pydantic import Field

from domainkit.contrib.flags import dynamic_setting

MAX_ITEMS = dynamic_setting(
    "checkout.max_items",
    Annotated[int, Field(ge=1, le=100)],
    default=10,
    description="Items per order.",
)

limit = await MAX_ITEMS.get()
```

`dynamic_setting` returns a `DynamicSettingDefinition`. Its `get()`
returns the stored value, or the default when none is stored. If a
stored value no longer fits the type (because you changed the
declaration), `get()` returns the default and logs a warning.

The key follows the same rule as a flag's name. A bad key, or a default
that does not fit the type, raises `ImproperlyConfigured` when the module
is imported. Declaring a key again replaces its definition.
`declared_settings()` returns every declared setting, sorted by key.

A stored value is a `DynamicSetting` aggregate, whose id is the key.
Its model has only two permissions: view and change.

### Secret settings

The audit trail keeps every change of a setting, with its value, for
ever. For a value that is a secret or personal data, declare the setting
with `secret=True`:

```python
API_KEY = dynamic_setting("partner.api_key", str, default="", secret=True)
```

Its changes are recorded as `{"key": "partner.api_key", "value": null,
"secret": true}`. The staff API shows `null`, and `flags settings` shows
`********`.

!!! warning
    A secret setting's value is still stored as it is, in the table and
    in the flags' cache entry. `flags setting KEY VALUE` also leaves it in
    the shell's history. Keep credentials, such as a provider's API key,
    in settings or in a secret store, not here.

## From the command line

The `flags` command lists and changes flags and settings. Each change
goes through `FlagsUnitOfWork`, so it is audited and reaches the cache,
the same as a change through the staff API. The actions that change
something take `--operator` ([operators](../reference/commands.md#operators)).

| Action | What it does |
| --- | --- |
| `list` (the default) | Every flag and its rules. |
| `set NAME` | Create a flag, or change only the rules you give: `--everyone on\|off\|rules`, `--percentage N`, `--users ID ...`, `--groups NAME ...`, `--note TEXT`. |
| `delete NAME` | Delete a flag: it is off for everyone again. |
| `settings` | Every declared setting, its value, and its default. |
| `setting KEY VALUE` | Store a value, written as JSON. |
| `setting KEY --reset` | Go back to the default. |

```bash
python manage.py flags set beta --everyone on
python manage.py flags set beta --everyone rules
python manage.py flags setting checkout.max_items 25
python manage.py flags settings
python manage.py flags setting checkout.max_items --reset
python manage.py flags delete beta
```

```text
beta: on for everyone  (new checkout)
beta: 10% of users, users [], groups ['staff']  (new checkout)
checkout.max_items = 25
checkout.max_items = 25 (default 10)
partner.api_key = ******** (secret)
checkout.max_items is back to 10.
Deleted beta.
```

`--everyone rules` sets `everyone` back to `None`, so the other rules
decide again. A refusal is a command error:

```text
CommandError: 'Beta!' is not a flag name: [a-z0-9][a-z0-9_.-]{0,99}.
CommandError: A rollout percentage is 0 to 100.
CommandError: checkout.max_items: Input should be less than or equal to 100.
CommandError: Give a value as JSON, or --reset.
CommandError: No dynamic setting is called 'nope'.
CommandError: No flag is called 'beta'.
```

## The staff API

Mount the staff router next to the auth admin API:

```python
from domainkit.contrib.flags.admin import router as flags_router

router.include_router(flags_router, prefix="/admin/flags")
```

| Route | Permission | What it does |
| --- | --- | --- |
| `GET /flags` | `flags.view_flag` | Every flag, by name. |
| `POST /flags` | `flags.add_flag` | Create a flag (201); a name already used is 409. |
| `PUT /flags/{name}` | `flags.change_flag` | Replace a flag's rules. |
| `DELETE /flags/{name}` | `flags.delete_flag` | Delete a flag (204). |
| `GET /settings` | `flags.view_dynamicsetting` | Every setting: value, default, description, and JSON schema. |
| `PUT /settings/{key}` | `flags.change_dynamicsetting` | Store a value; a value the type refuses is 422. |
| `DELETE /settings/{key}` | `flags.change_dynamicsetting` | Go back to the default (204). |

Every route also needs a staff user (`is_staff`) or a superuser. The
body of `POST /flags` is the rules and a `name`:

```json
{"name": "beta", "everyone": null, "percentage": 10, "users": [], "groups": ["staff"], "note": ""}
```

A body may list at most 1,000 users and 100 groups. `PUT /settings/{key}`
takes `{"value": 25}`.

## Events and the audit trail

Each change, from the API or the command, records a domain event:

| Event | When | Fields |
| --- | --- | --- |
| `FlagChanged` | A flag was created, or its rules changed. | `name`, `everyone`, `percentage`, `users`, `groups` |
| `FlagDeleted` | A flag was deleted. | `name` |
| `SettingChanged` | A setting got a new value. | `key`, `value` (`None` when secret), `secret` |
| `SettingReset` | A setting went back to its default. | `key` |

The audit trail records who changed what. It keeps a flag's `users` as a
count, not as the ids (`Counted`). `python manage.py audit history
--aggregate name=beta` shows a flag's history.

To change flags from your own code, use `FlagsUnitOfWork`. It has two
repositories, `flags` and `settings`:

```python
from domainkit.contrib.flags import Flag, FlagsUnitOfWork

async with FlagsUnitOfWork() as uow:
    flag = await uow.flags.find("beta")
    if flag is None:
        flag = Flag(id="beta")
        await uow.flags.add(flag)
    flag.configure(everyone=True, percentage=0, users=[], groups=[], note="")
    await uow.commit()
```

## How changes reach every process

domainkit reads every flag and every stored setting in one query. It
caches them as one entry in `FLAGS.CACHE`, kept under the count of
changes made so far (the key `domainkit.flags.version`). A read is two
cache reads: the count, then its entry.

When a change commits, the process that made it loads the flags into the
entry of the next count (`Cache.refresh`), then adds one to the count.
So this process, and every process that shares a Redis cache, see the
change at once, and none of them misses the cache. With 20 replicas that
read as a change lands, the flags are read once. Before, deleting the
entry made all 20 read them at once (PERF-17).

A process that read the tables right before a change can only store what
it read under an earlier count. Nobody reads that count after the change
is counted, so a kill switch turned off is never served on again
(SEC-12b-1). If another change was counted in the meantime, its entry is
loaded again. If loading fails, the change is still counted, and the
next read loads the flags.

Processes with their own memory cache see a change when their entry
expires, within `FLAGS.CACHE_TTL`. So does a change made outside the
store, with SQL or `loaddata`.

Flags are shared by every [tenant](../learning.md#words): one count and
one entry for all.

## Settings

Every key is optional. [Settings](../reference/settings.md) lists the
other settings.

| `FLAGS` key | Default | Meaning |
| --- | --- | --- |
| `CACHE` | `"default"` | The cache alias that holds every flag and dynamic setting, as one entry. It must be in `CACHES`. |
| `CACHE_TTL` | `30.0` | Seconds a cached copy lives: how long other processes with a memory cache may see an old value. More than 0. |

## On PostgreSQL and SQLite

The two tables work the same on both. The database checks that a
percentage is from 0 to 100. With several databases, the flags tables
must be on one database that is not part of a shard group:
`DATABASE_ROUTING['flags']` names it.

## Errors and refusals

| Error | When | HTTP |
| --- | --- | --- |
| `InvalidFlag` | A percentage outside 0 to 100. | 422, `invalid_flag` |
| `FlagNotFound` | No flag has that name. | 404, `flag_not_found` |
| `UnknownSetting` | No setting is declared with that key. | 404, `unknown_setting` |
| `InvalidSettingValue` | The value does not fit the type. The message gives the reasons, never the value. | 422, `invalid_setting_value` |
| `ImproperlyConfigured` | At startup: `FLAGS.CACHE` is not in `CACHES`, or the flags database is in a shard group. When you declare a setting: a bad key, or a default that does not fit. | none; the process does not start |

## Common mistakes

- **Using `RequireFlag` as the access rule.** It only hides the route.
  Add `Require(...)` or `Public` too.
- **Storing an API key in a secret setting.** `secret=True` keeps the
  value out of the audit trail and the API, but the table and the cache
  still hold it. Use a secret store.
- **Expecting an instant change with a memory cache and several
  processes.** Each process waits for its entry to expire. Use a shared
  Redis cache, or lower `CACHE_TTL`.
- **Writing a value that is not JSON.** `flags setting greeting hello`
  fails. Write `'"hello"'`.

## See also

- [Audit](audit.md): the history of every change.
- [Caches](../cache.md): the `CACHES` that `FLAGS.CACHE` names.
- [Users and permissions](../auth.md): staff users and model
  permissions.
- [Commands](../reference/commands.md): every command and its options.
