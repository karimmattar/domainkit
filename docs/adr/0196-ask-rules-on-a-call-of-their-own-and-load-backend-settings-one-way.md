# 0196. Ask rules on a call of their own, and load backend settings one way

- Status: Accepted
- Date: 2026-11-18

## Context

The architecture audit of 2.8.0 (A28-ARCH) found two seams where code
depended on a concrete thing where an interface would do.

- **Access rules read a Starlette connection** (ARCH-08).
  `BasePermission.has_permission(request: HTTPConnection, user)` is asked
  on a route, and also where no request exists. Approvals asks a guard's
  `maker_rule` when an approved command runs in the worker, and a
  policy's `approver_rule` in `approvers_of`. It asked them on one
  connection the whole process shared, `_RUN`, whose scope held no
  `query_string`, no `app`, and no `user`. A rule written for routes, which
  reads `request.query_params`, `request.app`, or `request.user`, raised
  there instead of answering. The run sat in a `try` that caught only
  `DomainError`, so the worker tried it again until it dead-lettered it,
  and `approvers_of` raised for every caller. `request.state` was shared
  too: what one call kept, every later call saw.
- **Settings that name backend classes are loaded by hand** (ARCH-09).
  `utils.delivery.load_backend` imports, checks the class, and makes it.
  Five more loaders did the same with their own checks:
  `RATE_LIMIT_BACKEND` (no check at all, imported by five code paths),
  `AUTH.BACKENDS`, `AUTH.PASSWORD_HASHERS`, `AUTH.AUTHENTICATION_CLASSES`,
  and `AUTH.PASSWORD_VALIDATORS` (no check of the class). A class without
  `hit` passed the settings and `check` as `RATE_LIMIT_BACKEND`, and failed
  on the first request. Names that installed packages offer
  (`domainkit.backends` entry points) worked for caches, brokers,
  storages, search, scanning, mail, and notifications, but not for the
  auth settings or `RATE_LIMIT_BACKEND`. The auth loaders kept their
  instances in `functools.cache` keyed by the paths, which nothing resets.

Constraints: 2.9 is a minor release. A rule written today, annotated
`request: HTTPConnection`, keeps working and keeps passing a project's
type checks. What loads today keeps loading. The core's layering (ADR
0188) puts `domainkit.utils` below the settings: it cannot read them.
The request path may not gain a lookup.

How others do it:

- Django REST framework's permissions take the request and the view
  (`has_permission(self, request, view)`), and are asked inside a view
  only [1].
- Django keeps loaded hashers in an `lru_cache`, and empties it from a
  `setting_changed` receiver when a test overrides `PASSWORD_HASHERS` [2].
  domainkit has no such signal: `override_settings` puts another settings
  object in place, and `registry.settings` already keeps app settings
  for as long as that object is the one loaded.
- A method that overrides another may take a more general argument, not a
  more specific one [3]. A protocol marked `runtime_checkable` answers
  `issubclass` only when its members are methods [4].

### Options weighed for the rules

1. **Only fill the shared connection's scope.** It ends the errors of
   `query_string` and `app`, not the shared state, and a project still
   cannot tell what a rule sees offline.
2. **`BasePermission`'s hooks take a protocol in place of
   `HTTPConnection`.** A project's rule that annotates `request:
   HTTPConnection` would then take a narrower argument than the method it
   overrides: its type check fails. Breaking.
3. **A second hook for offline calls** (`has_offline_permission`). Two
   hooks to keep in step, and every rule written so far answers only one.
4. **Run the rules through an ASGI application in memory.** Heavy for a
   worker, and still a request that never happened.
5. **A narrow protocol that `HTTPConnection` meets, and an offline
   connection that meets both** (chosen).

### Options weighed for the loaders

1. **Import and check the class when the settings load** (a validator of
   `Settings`). The default `RATE_LIMIT_BACKEND` lives in
   `domainkit.http.ratelimit`: every process, every command, and every
   `override_settings` would import the HTTP stack, which loading the
   settings and `setup()` do not do today, and the settings would load
   the serving layer.
2. **One instance of `RATE_LIMIT_BACKEND` per settings object, shared by
   every counter.** The global limit, each application's throttles,
   auth's counts of refused credentials and of attempts, and the worker's
   task rates would share one `MemoryBackend` of 100,000 callers. A flood
   of new addresses on the global limit would push login attempts out of
   it, and their counts would start again from zero. Two applications in
   one process, as a project's tests build, would count together.
3. **A handler in `domainkit.utils`.** The layering forbids it to read
   the settings; it would need the settings object passed in by every
   caller.
4. **One handler beside the settings** (chosen), with the import and the
   check in `domainkit.utils`, where `load_backend` uses them too.

## Decision

### Rules asked outside a request

- `domainkit.http.access.RuleContext` is a protocol of what a rule may
  read of the call it is asked about: `scope` (its `method`, `path`,
  `auth`, and `user`), `user`, and `state`. `HTTPConnection` meets it, so
  every request and WebSocket is one. A rule that annotates `request:
  RuleContext` says it reads no more, and answers offline as on a route;
  `BasePermission`'s hooks keep taking an `HTTPConnection`.
  `scopes_of` takes a `RuleContext`.
- `domainkit.http.access.OfflineContext(user, *, method="POST",
  path="/", scopes=None)` is an `HTTPConnection` of no client: no headers,
  query, cookies, or session data; `user` in the scope; `auth` None, or
  credentials limited to `scopes`; an `app` of nothing but an empty
  `state`; and a `state` of its own. Code that asks rules outside a
  request (a task, a command) makes one per call.
- Approvals asks a guard's `maker_rule` and a policy's `approver_rule` on
  a new `OfflineContext` each time, as `POST /domainkit.approvals.execute`.
  What a rule raises is told apart:
  - A transient error goes up, so the run is tried again as before, and
    `approvers_of` raises it: a rule that could not answer has not
    refused, and an approved command is not lost to a blip.
    `domainkit.http.errors.transient` says which: `OSError` (timeouts and
    lost connections among them), `ServiceBusy`, an `UpstreamError` that
    may not happen again, and what
    `database_refusal` already answers with 503 (a pool out of
    connections, a statement cancelled or lost to another transaction, a
    server out of reach or a connection SQLAlchemy invalidated). It reuses
    that classification rather than keeping a second list. An
    `UpstreamError` is transient when it comes from a timeout, a failed
    connection, a 5xx answer, or a host's open circuit (`CircuitOpen`,
    `CallTimeout`). It is not where trying again cannot change it:
    `ResponseTooLarge` (the answer stays as long) and `WriteOnceIgnored`
    (the store ignores `If-None-Match`), by their pinned codes, so the
    serving layer imports no HTTP client for them; one that is, or comes
    from, a `DomainError` (the guard's `UnsafeURL`: a private address) or
    `ImproperlyConfigured` (`RendererNotConfigured`); and one that comes
    from a 4xx answer other than 408 and 429, read on the error's chain.
  - A maker rule's `DomainError` fails the request with its own code, as in
    2.8: it is the rule's answer, not a mistake.
  - Any other error is a mistake in the rule, and refuses: the maker may
    not make the command (`maker_not_allowed`, the request fails once),
    and the user is not an approver. It is logged as a warning by the
    rule's class and the error's, never the error's text, a line per rule
    a while (`ThrottledLog`).

### Settings that name backend classes

- `domainkit.utils.module_loading.backend_class(setting, path, base)`
  imports a class by dotted path (`module:Class` too) or by the name an
  installed package offers, and checks that it is a `base`: a class, or a
  `runtime_checkable` protocol of methods. `utils.delivery.load_backend`
  uses it.
- `domainkit.conf.backends.BackendHandler[T](setting, base, read=None)`
  stands for one setting. `read` returns one path, or a sequence of paths,
  each alone or with its keyword arguments; by default
  `settings.<setting>`. `backend_class()` and `create()` load the one
  class a setting names; `instances()` makes one instance of each class a
  setting names, kept while the settings object is the one loaded, as
  `registry.settings` keeps app settings, and made again under a lock so
  loops on several threads get the same ones. `load(value)` makes them
  for a value that is not the setting's, for checks. It lives beside the
  settings, not in `domainkit.utils` as the audit proposed: `utils` is
  the bottom layer (ADR 0188) and cannot read the settings object whose
  lifetime the instances follow; the import and the check, which need no
  settings, stay in `utils`.
- `RATE_LIMIT_BACKEND` is `domainkit.http.ratelimit.rate_limit_backend`.
  Its `Backend` protocol is `runtime_checkable`: the class must have
  `hit`. Each counter keeps its own backend, made with `create()` where
  it was made before: once per application for the global limit and for
  throttles, once per middleware for refused credentials, once per
  application for auth's attempts, and once per worker for task rates.
  Not one per settings object, as for auth's classes: a backend holds
  counts, and one `MemoryBackend` shared by every counter would let a
  flood of new addresses on the global limit push login attempts out of
  it, and would count two applications of one process, or a project's
  tests under one settings object, together.
- `Settings` refuses a `RATE_LIMIT_BACKEND` of no form that could load
  (`BACKEND_PATH`). `check` and the application's start refuse one that
  does not import, or names a class without `hit` (`backends.E001`):
  staging and production do not start, and elsewhere it is logged, and
  the first request is refused with what is wrong. The class is not
  imported when the settings load: the default one lives in
  `domainkit.http.ratelimit`, so every process, every command, and every
  `override_settings` would import the HTTP stack, which neither loading
  the settings nor `setup()` does, and the settings would load the
  serving layer.
- `AUTH.BACKENDS`, `AUTH.PASSWORD_HASHERS`, `AUTH.AUTHENTICATION_CLASSES`,
  and `AUTH.PASSWORD_VALIDATORS` use handlers: they take names that
  packages offer, and their instances are made once per settings object
  rather than once per value for the process. `PasswordValidator` is
  `runtime_checkable`: a validator class needs `validate` and `help_text`.
  Auth's `setup()` loads the backends, the authentication classes, and the
  validators, as before.
- The handlers of caches, storages, brokers, ledgers, and connections are
  left as they are: they key their instances by alias and event loop.

## Consequences

- A project's maker and approver rules that read the request answer
  offline as on a route. One that needs a request (a header, a step-up)
  refuses there, as the docs said; one with a mistake refuses and is
  logged, and no longer dead-letters the run or breaks `approvers_of`.
  One that meets a timeout, an outgoing call that may succeed later, or
  a database out of reach is tried again, as before, and one that refuses
  with a `DomainError` names its code, as before. A mistake that raises
  an `OSError` (a file not found) is taken for a transient error and
  tried again until it is dead-lettered. So is an `UpstreamError` that
  names its 4xx answer only in its message, as an identity provider's
  refusal of a code does (`ProviderError`): nothing on it says which.
- A rule meant for both places can say so by annotating `RuleContext`.
  The built-in rules keep their annotations, so a project's subclass of
  one passes its type check as before.
- Newly refused, each a configuration that could not have worked where it
  was used: a `RATE_LIMIT_BACKEND` of no form that loads (with the
  settings), one that does not import or has no `hit`, even where nothing
  counts with it (`backends.E001`), and a password validator class without
  `validate` or `help_text` (at `setup()`). docs/upgrading.md says so.
- An error that a misconfigured auth class raised as `TypeError` (a class
  that wants arguments) is now `ImproperlyConfigured`, naming the setting.
- `override_settings` gives auth's classes new instances, so a test that
  replaced PyJWT no longer needs to empty a cache. A project's tests that
  never change the settings keep one set, as before.
- Each counter of rate limits counts alone, as before: a backend that
  wants one count for all of them shares its state itself, as
  `RedisBackend` does in Redis.
- Other backend settings (identity providers, SMS, search) can move to a
  handler later; ARCH-02's per-call clients are not changed here.

## References

1. Django REST framework, *Permissions*:
   <https://www.django-rest-framework.org/api-guide/permissions/>
2. Django, `django/contrib/auth/hashers.py` (`get_hashers`,
   `reset_hashers`):
   <https://github.com/django/django/blob/main/django/contrib/auth/hashers.py>
3. mypy, *Incompatible overrides*:
   <https://mypy.readthedocs.io/en/stable/common_issues.html#incompatible-overrides>
4. Python typing specification, *Protocols*, "`@runtime_checkable`
   decorator and narrowing types by `isinstance()`":
   <https://typing.python.org/en/latest/spec/protocol.html#runtime-checkable-decorator-and-narrowing-types-by-isinstance>
