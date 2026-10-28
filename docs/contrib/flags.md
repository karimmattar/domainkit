# Flags: feature flags and dynamic settings

A flag turns a feature on for everyone, for no one, or for some users, in
the database, without a deployment:

- `everyone`: `True` is on for all, `False` off for all; unset (`None`), the
  rules below decide;
- `users`: user ids it is on for;
- `groups`: group names it is on for;
- `percentage`: a share of the other users, 0 to 100. A user's place comes
  from a hash of the flag's name and the user's id, so they stay in or out
  while the percentage stays, and stay in as it grows.

Anonymous callers get a flag only when `everyone` is on. A flag nobody
created is off.

```python
from fastapi import Depends

from domainkit.contrib.flags import RequireFlag, flag_enabled
from domainkit.http.access import Public

if await flag_enabled("new_checkout", user=user):
    ...


@router.get("/beta", dependencies=[Depends(RequireFlag("beta")), Public])
async def beta() -> ...: ...
```

`flag_enabled` takes the request's user (`domainkit.contrib.auth`), or any
object with an `id` and, optionally, `groups` with names. `RequireFlag`
answers 404 while the flag is off for the caller, so the route looks
absent. It is not an access rule: the route still needs `Require(...)` or
`Public`.

A dynamic setting is a value staff change at runtime, typed and validated
by pydantic. Declare it in code, with its default:

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

`get()` returns the stored value, or the default when none is stored. A
stored value the type no longer accepts (the declaration changed) gives the
default, and a warning in the log.

The audit trail records each change of a setting with its value, for
good. Declare a setting that holds a secret, or personal data, with
`secret=True`: its changes are recorded as `{"key": ..., "value": null,
"secret": true}`, and the staff API and `flags settings` show no value
(`null`, `********`). The value is still stored as it is, in the table
and the flags' cache entry, and `flags setting KEY VALUE` leaves it in the
shell's history, so keep credentials such as provider API keys in settings
or a secret store rather than here. Changes of a flag record
its rules, with its users as a count, not their ids.

Change both from the command line, or through the staff API:

```shell
python manage.py flags                                    # every flag
python manage.py flags set beta --percentage 10 --groups staff
python manage.py flags set beta --everyone on            # on, off, or rules
python manage.py flags delete beta
python manage.py flags settings                           # every dynamic setting
python manage.py flags setting checkout.max_items 25      # the value, as JSON
python manage.py flags setting checkout.max_items --reset
```

```python
from domainkit.contrib.flags.admin import router as flags_router

router.include_router(flags_router, prefix="/admin/flags")
```

| Endpoint | Permission |
| --- | --- |
| `GET /flags` | `flags.view_flag` |
| `POST /flags` | `flags.add_flag`; a taken name is 409 |
| `PUT /flags/{name}` | `flags.change_flag` |
| `DELETE /flags/{name}` | `flags.delete_flag` |
| `GET /settings` | `flags.view_dynamicsetting`: values, defaults, descriptions, and JSON schemas |
| `PUT /settings/{key}` | `flags.change_dynamicsetting`; a value the type refuses is 422 |
| `DELETE /settings/{key}` | `flags.change_dynamicsetting`: back to the default |

Every endpoint also needs a staff user (`is_staff`) or a superuser. Each
change, from the API or the command, is a domain event (`FlagChanged`,
`FlagDeleted`, `SettingChanged`, `SettingReset`), so the audit trail records
who changed what, and `auditlog --aggregate name=beta` shows a flag's
history.

Every flag and stored setting is read in one query and cached as one entry
in `FLAGS.CACHE`. Once a change commits, the process that made it reads
them again and replaces the entry (`Cache.refresh`), so this process, and
every process sharing a Redis cache, see it at once, and none of them
misses: with 20 replicas reading as a change lands, the flags are read
once, where deleting the entry, as 0.11 did, had all 20 read them at
once. Each change is counted in the cache (`domainkit.flags.version`): one
whose read raced another change's reads again, so neither is lost; after
three such races, or a read that fails, the entry is deleted and the next
read loads it, as before (PERF-17). Processes with their own memory cache
see a change when their entry expires, within `FLAGS.CACHE_TTL`; so does a
change made behind the store's back (SQL, `loaddata`), or one a read that
began before it overwrote.

| `FLAGS` key | Default | Meaning |
| --- | --- | --- |
| `CACHE` | `"default"` | The cache alias holding the flags and settings. |
| `CACHE_TTL` | `30.0` | Seconds a cached copy lives: how long other processes with a memory cache may see an old value. |
