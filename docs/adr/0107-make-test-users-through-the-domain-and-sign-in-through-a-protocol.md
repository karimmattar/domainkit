# 0107. Make test users through the domain, and sign in as them through a protocol

- Status: Accepted
- Date: 2026-10-28

## Context

The 0.9.0 audit's TEST-08 compared `domainkit.testing` with pytest-django,
DRF's test tools, Litestar's, and factory_boy (the list is in
`docs/testing.md`). Up to 0.11 a test could sign in only as a new user
`client_for(...)` made, which it never saw: a test that needed a user as
an object's owner, a group's member, or a login reached for
`AuthUnitOfWork` itself, and then had no way to sign that user in. The
options for making users:

- factory classes (`UserFactory(Factory[User])`), as factory_boy's
  `DjangoModelFactory`: but a user needs async work around the object
  (hashing a password, reading permissions and groups by key), which
  `Factory`'s sync `build` cannot do, and a second way to define
  factories would need `post_generation` first;
- async functions over auth's own repositories (`create_user`,
  `create_group`), which hash, grant, and commit as the application does,
  so events and principal stamps follow.

For signing in as a stored user: extend `TokenIssuer`, the protocol
`client_for` finds auth through, with a second method; or add a second
protocol. `TokenIssuer` is `runtime_checkable`: an installed domain that
implements only today's method would stop matching it, and `client_for`
would stop finding it.

## Decision

- `domainkit.contrib.auth.testing` holds `create_user(*permissions,
  password=None, groups=(), superuser=False, **fields)` and
  `create_group(*permissions, name=None)`, async, through `AuthUnitOfWork`.
  They live in auth, not in `domainkit.testing`: the core imports no
  contrib app (ADR 0080).
- A second protocol, `StoredUserTokenIssuer` (`issue_test_token_for(user_id)`),
  which `AuthConfig` implements; `token_for(user)` and the clients'
  `authenticated_as(user)` ask the first installed domain that does.
  `TokenIssuer` is unchanged.
- `TestClient.run(function, ...)` awaits async setup in the sync client's
  loop, with the caller's context, so sync tests use the same functions.
- The plugin gains a `settings` fixture (`SettingsOverrides`), stacking
  `override_settings` until the test ends.

## Consequences

- One way to make a user in tests, whatever the test's kind; factories of
  a project's own aggregates still use `Factory`, and take a user's id.
- Another provider of users implements `StoredUserTokenIssuer` to let
  `authenticated_as` sign its users in.
- `create_user` and `create_group` are public names: changing their
  arguments now takes a deprecation (`docs/versioning.md`).
- What would change this: `Factory` with async hooks (`post_generation`),
  after which a `UserFactory` could be built on these functions.
