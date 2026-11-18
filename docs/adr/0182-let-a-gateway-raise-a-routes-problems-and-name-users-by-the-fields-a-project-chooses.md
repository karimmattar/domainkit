# 0182. Let a gateway raise a route's problems, and name users by the fields a project chooses

- Status: Accepted; amended by [0188](0188-write-down-the-cores-layering-and-hold-the-boundaries-it-draws.md) on 2026-11-18 (at the end)
- Date: 2026-11-17
- Amends: [0003](0003-enforced-layer-rules.md), [0177](0177-let-a-key-reach-every-tenant-name-a-domains-own-keys-and-search-every-word-at-once.md)

## Context

A project reported two data and layout notes (D2) against 2.7.0.

- **A file gateway cannot raise the error a route would.** The
  infrastructure layer may import `domainkit.http.client` (2.4) and
  `domainkit.http.uploads` (2.5), but not `domainkit.http.errors`, where
  `APIError` and `FieldError` live. A gateway that refuses a file with a
  status, a code, and an error under the form's field needs both. The
  project keeps an extra `gateways` layer (`ARCHITECTURE.EXTRA_LAYERS`)
  for its gateways alone.
- **`GET /users/names` names people by `first_name` and `last_name`
  alone** (`NAME_FIELDS` in `contrib/auth/admin.py`). The project's
  people name themselves in the app (`display_name`), so a back office
  would show the same person under another name. The dashboard asks
  `GET /users?id=...` for each page's people instead, each user whole.

Why `domainkit.http.errors` is barred. ADR 0003's 2.4 amendment says
"no web libraries" keeps the serving side out of adapters: FastAPI,
Starlette, and `domainkit.http`'s routing and errors. The module imports
FastAPI's `Request`, `Response` and `RequestValidationError`,
Starlette's routing, SQLAlchemy's errors and the ORM's metadata, and the
settings; it builds every error response and installs the handlers. The
2.5 amendment and ADR 0177 kept it out again: "a gateway raises domain
errors, which the HTTP layer maps". `git log -S` traces the rule:
1.5.0 forbade `domainkit.http` in the infrastructure layer by prefix
(#94); `ALLOWED_MODULES` came with the client (#157), and uploads joined
it (#198), each for an adapter's need.

What the rule already lets in. The rules read the imports a domain's
files write, by name, not the chains behind them; import-linter's layers
contract, by contrast, follows indirect imports too [1].
`domainkit.http.uploads`, allowed since 2.5, imports FastAPI, Starlette
and `domainkit.http.errors` itself, and hands the layer `UploadRefused`,
an `APIError`: a gateway may raise or catch one today. And as a module
lends every name it imports, `from domainkit.http.uploads import
APIError` passes 2.7's check too, though nothing documents it and it
holds only while `uploads` imports the class. `FieldError`, which an
error under a field needs, is not among those names.

Domain errors fit a rule of the business: their status follows their
base class (404, 409, 412, 403, 422, 400). A gateway's refusal of a file
answers about the request's file, with the statuses and codes a route's
upload refusal answers (413 `file_too_large`, 415, 422 under a field).

Constraints:

- 2.8 is a minor release: nothing a project relies on changes, and a
  default that changes behaviour is opt-in.
- The layer rules only add, and the framework's exceptions are reviewed
  one module at a time (ADR 0003).
- 2.7's review established that `/users/names` shows nothing `/users`
  hides: the name is built only from fields `/users` shows
  (`admin_fields()`), never from `get_full_name`, and its search is
  `/users`' own, never a name.

How others do it:

- *Architecture Patterns with Python* keeps HTTP in the entrypoint:
  the Flask app catches the service layer's and the domain's exceptions
  and chooses the status [2].
- Django defines refusals apart from the responses that answer them:
  `SuspiciousOperation` and `BadRequest` live in `django.core.exceptions`
  and reach the handler, which answers 400 [3].
- Django's user model may implement `get_full_name()`, which the admin
  shows beside the username in an object's history; it is optional, and
  the model's own [4]. django-allauth names a user by a callable,
  `ACCOUNT_USER_DISPLAY`, the username by default [5].
- OWASP's API Security Top 10 (API3:2023) asks that a response keep to
  the properties its endpoint needs, cherry-picked, and warns against
  generic methods that serialize a whole object [6].
- Django's system checks refuse, when the service starts, settings that
  cannot work [7].

Options for the gateway's problems:

- **Allow `domainkit.http.errors`** (the project's first proposal). It
  lets adapters read requests and build responses: the serving side 2.4
  kept out, in exchange for two classes.
- **Point to `UploadRefused`.** Already allowed, but no `FieldError`, and
  any other refusal would be named an upload refusal.
- **A refusal of its own below HTTP** (as `UpstreamError` is), which
  `domainkit.http.errors` maps. No Starlette class in the layer, but a
  second coded error beside `APIError`, which code catching `APIError`
  misses, and a second field error.
- **Move the two problems to a module of their own**
  (`domainkit.http.problems`), re-exported by `domainkit.http.errors`
  and `domainkit.http`, and allowed in the infrastructure layer. It
  imports pydantic, Starlette's `HTTPException` (the base `APIError`
  has always had), and `domainkit.exceptions`, nothing that builds a
  response. Under `domainkit.http`, it stays forbidden to the
  application layer.

Options for naming people:

- **The model's own `admin_name()`** (the project's second proposal). A
  method may read any field, as `get_full_name` may: whether a name
  shows only what staff see would rest on each project's method, which
  is why 2.7 refused `get_full_name`. allauth's callable has the same
  shape.
- **A class attribute on the model.** Declarative too, but which name a
  back office shows is the deployment's choice, as which fields
  identify a user is: `AUTH.IDENTIFIERS` is a setting, checked against
  the model (`auth.E011`).
- **`AUTH.NAME_FIELDS`, field names, the first that is set winning.** A
  name read from fields can be checked against what `/users` shows. An
  entry is a field or several joined by a space, so 2.7's full name can
  stay the fallback after a `display_name`.

For a configured field `/users` does not show: a null name alone fails
silently, a back office loses every name and nobody is told; a check
alone does not cover what changes after startup (`admin_fields()` is
read from the model's class when asked, and tests override settings
without starting again).

## Decision

- **`domainkit.http.problems`** holds `APIError` and `FieldError`,
  moved from `domainkit.http.errors`, which imports and re-exports them,
  as `domainkit.http` does, the same classes. `domainkit.http.errors`
  keeps `DomainkitError`, which it imported in 2.7: every domainkit name
  2.7 imported from either still imports (only the standard library's
  `Sequence` and `TYPE_CHECKING` left `domainkit.http.errors`).
  `domainkit.http`'s lazy names and `domainkit.http.uploads` take the
  classes from `problems`, so `domainkit.http.APIError` loads neither
  FastAPI nor SQLAlchemy. `ALLOWED_MODULES["infrastructure"]` adds the
  module, held to the names its `__all__` lists: `from
  domainkit.http.problems import HTTPException` (or `BaseModel`, or
  `DomainkitError`) is refused. The rest of `domainkit.http.errors`
  stays forbidden there; a business refusal stays a domain error. A test
  pins what the module imports, and another the modules importing it
  loads, in a fresh interpreter.
- **`AUTH.NAME_FIELDS`**: None by default, which names a user as 2.7
  does. Otherwise up to 8 entries, each a field or a tuple of up to 8
  fields; a field alone is an entry of one. `/users/names` names a user
  by the first entry whose fields, each stripped and joined by a space
  (empty ones left out), give text; none gives null. Only text is taken:
  `str()` of a date, a flag or a dict is not what `/users` shows
  (`2026-11-17 22:01:00+00:00` against `2026-11-17T22:01:00Z`), and it
  names no one. The username counts as shown: `/users` shows it, as its
  own key.
- **Both guards.** `check` refuses, when the service starts, a field of
  `AUTH.NAME_FIELDS` that cannot name users (`auth.E012`), with a message
  for each case, as each asks something else of the project: a field the
  model lacks; one the admin API never shows (`HIDDEN_FIELDS`: the
  password, `last_login`, the ids and grants), which no setting changes;
  one the model does not show, which its `EDITABLE_FIELDS` or
  `READ_ONLY_FIELDS` would; and one whose type is not text. The route
  still tests each entry against what `/users` shows when it answers,
  and each value for text: an entry with a field not shown gives way to
  the next, and a value that is not text gives nothing. `HIDDEN_FIELDS`
  moves to `contrib/auth/models.py`, so the check reads the same set as
  the route without importing the admin API.
- Search does not change: `/users`' own, never a name.

## Consequences

- A file gateway names its refusal as a route does, and the project's
  extra layer can go. A project that names `domainkit.http.problems` in
  `ARCHITECTURE.FORBIDDEN_LIBRARIES` still forbids it.
- `APIError.__module__` is `domainkit.http.problems`: tracebacks, the
  mail reporter's report, and tracing's `error.type` name it there, so a
  filter or an alert on the old name needs the new one (upgrading.md's
  "2.7 to 2.8").
- Names are stripped by default too: a first or last name of white
  space alone gives no name, where 2.7 answered the spaces.
- An allowed module lends the layer every name it imports:
  `from domainkit.http.uploads import UploadFile` passes the check, as
  in 2.7. `domainkit.http.problems`, which nothing imports yet, is held
  to its `__all__` from the start. Holding `client` and `uploads` to
  theirs would refuse imports that pass in 2.7, so it comes in steps: a
  later 2.x release warns of a name outside their `__all__` (a warning
  of `check`, which `check --deploy` fails on until the project changes
  the import or silences it), and 3.0 refuses it.
- A back office reads people as they name themselves, in one request
  per page. A project that sets `AUTH.NAME_FIELDS` learns of a field
  staff are not shown when the service starts, not from empty columns.
- Serving loads one more module (`domainkit.http.problems`), within the
  benchmarks' tolerance; `/users/names` reads the setting once per user,
  a cached lookup.

## References

1. import-linter, *Layers*:
   <https://import-linter.readthedocs.io/en/stable/contract_types/layers/>
2. Percival and Gregory, *Architecture Patterns with Python*, chapter 4,
   *Our First Use Case: Flask API and Service Layer*:
   <https://www.cosmicpython.com/book/chapter_04_service_layer.html>
3. Django, *Django Exceptions*:
   <https://docs.djangoproject.com/en/5.2/ref/exceptions/>
4. Django, *Customizing authentication in Django*, `get_full_name()`:
   <https://docs.djangoproject.com/en/5.2/topics/auth/customizing/>
5. django-allauth, *Configuration*, `ACCOUNT_USER_DISPLAY`:
   <https://docs.allauth.org/en/latest/account/configuration.html>
6. OWASP API Security Top 10 2023, *API3:2023 Broken Object Property
   Level Authorization*:
   <https://owasp.org/API-Security/editions/2023/en/0xa3-broken-object-property-level-authorization/>
7. Django, *System check framework*:
   <https://docs.djangoproject.com/en/5.2/topics/checks/>

## Amendment (2026-11-18, 2.9)

The step this record planned for a later 2.x is taken
([0188](0188-write-down-the-cores-layering-and-hold-the-boundaries-it-draws.md)): `check` warns (`architecture.W001`) of a name outside the
`__all__` of `domainkit.http.client` or `domainkit.http.uploads`
imported in the infrastructure layer, and of `uploads.serve`. 3.0
refuses them.
