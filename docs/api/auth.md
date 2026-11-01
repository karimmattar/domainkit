# Public API: users and permissions

`domainkit.contrib.auth` and its modules ([auth.md](../auth.md)). Part of
the [public API](../api.md), under its compatibility policy.

## `domainkit.contrib.auth`

Users, groups, and permissions for REST APIs (Django's `contrib.auth`).

- `AbstractBaseUser`: Credentials, status, groups, and permissions of a user.
- `AbstractUser`: A user with a username, an email, names, and a staff flag (Django's).
- `AnonymousUser`: Whoever sends a request without credentials: no id, no permissions.
- `AnyUser`: A user that logged in, or the anonymous one.
- `AuthSettings`: Users, hashing, validation, and backends; every key is optional.
- `AuthToken`: A stored token: its digest (the `id`), owner, kind, and lifetime.
- `AuthUnitOfWork`: One transaction on the auth tables, wherever `auth` is routed.
- `AuthenticatedUser`: The request's user; anonymous callers get 401.
- `AuthenticationFailed`: Credentials were presented and do not hold (401, unless a subclass says).
- `BaseAuthentication`: Recognizes no credentials and issues no tokens; override what you need.
- `BaseBackend`: A backend that knows no one and grants nothing; override what you need.
- `Credentials`: Who sent a request, and with what (`request.auth`).
- `CurrentUser`: The request's user, or `AnonymousUser`: the core's dependency, typed so.
- `Group`: A named set of permissions; every member has them all.
- `GroupId`: NewType creates simple unique types with almost zero runtime overhead.
- `GroupNameTaken`: Another group has that name.
- `GroupNotFound`: No group has that id.
- `InvalidEmail`: The email address is not one plain address mail can go to.
- `InvalidGroupName`: The group name is empty or too long.
- `InvalidPassword`: The password breaks the configured rules (`PASSWORD_VALIDATORS`).
- `InvalidUserField`: A value does not fit the type of the user field it is for.
- `InvalidUsername`: The username is empty, too long, or uses characters it may not.
- `IsAdminUser`: Staff users (`is_staff`), with credentials that are not limited to scopes.
- `IsSuperuser`: Active superusers, with credentials that are not limited to scopes.
- `IssuedTokens`: What logging in or refreshing returns (OAuth 2's token response).
- `JWTAuthentication`: Signed access tokens (JSON Web Tokens) and opaque refresh tokens.
- `JWTSettings`: `AUTH["JWT"]`: signed access tokens (`JWTAuthentication`).
- `ModelBackend`: Passwords from the user table; permissions of users and their groups.
- `PasswordHash`: A stored password: a hasher's `<algorithm>$...`, or unusable (`!...`).
- `Permission`: Permission to do something to one content type: `<domain>.<codename>`.
- `Proof`: A step-up: when the user authenticated again, and with what.
- `StepUp`: Users who authenticated again, with a second factor, within `max_age`.
- `StepUpRequired`: The rule needs a step-up the credentials do not hold (401).
- `TokenAuthentication`: Opaque tokens: `Authorization: Bearer <AUTH.TOKEN_PREFIX>...`.
- `TokenKind`: What a token may be used for.
- `UnknownGroup`: No group has that name.
- `UnknownPermission`: No permission has that key (`<domain>.<codename>`).
- `UnknownUserField`: The user model has no such field, or it changes another way.
- `User`: The default user; `AUTH.USER_MODEL` swaps in a project's own.
- `UserId`: NewType creates simple unique types with almost zero runtime overhead.
- `UserNotFound`: No user has that id.
- `UsernameTaken`: Another user has that username.
- `acheck_password`: `check_password` on a hashing thread; raises `PasswordChecksBusy`.
- `acurrent_user`: The core's `acurrent_user`, typed as this app's users: the same function.
- `amake_password`: `make_password` on a hashing thread; raises `PasswordChecksBusy`.
- `authenticate`: The user the credentials prove, by the first backend that accepts them.
- `check_password`: Whether `password` matches `encoded`.
- `current_user`: The core's `current_user`, typed as this app's users: the same function.
- `get_all_permissions`: Keys of every permission any backend grants `user` (on `obj`).
- `get_backends`: The configured backends, in order.
- `get_user`: The user with `user_id`, from the first backend that has it.
- `get_user_model`: The user class (Django's `get_user_model`).
- `has_perm`: Whether `user` has `perm` (on `obj`).
- `has_perms`: Whether `user` has every one of `perms` (on `obj`).
- `make_password`: Hash `password` with `hasher` (default: the preferred one).
- `password_help_texts`: What each configured rule asks of a password, for API clients.
- `validate_password`: Raise `InvalidPassword` listing every rule `password` breaks.

## `domainkit.contrib.auth.admin`

The admin API: users, groups, and permissions over REST (Django's admin).

- `router`: The routes: the admin API: users, groups, and permissions over REST (Django's admin).

## `domainkit.contrib.auth.api`

Endpoints to log in and out, refresh tokens, and change passwords.

- `password_holds`: Whether `password` is the stored password of `user`.
- `router`: The routes: endpoints to log in and out, refresh tokens, and change passwords.

## `domainkit.contrib.auth.authentication`

How requests prove who sent them (Django REST framework's authentication).

- `AuthenticationFailed`: Credentials were presented and do not hold (401, unless a subclass says).
- `BaseAuthentication`: Recognizes no credentials and issues no tokens; override what you need.
- `Credentials`: Who sent a request, and with what (`request.auth`).
- `JWTAuthentication`: Signed access tokens (JSON Web Tokens) and opaque refresh tokens.
- `Proof`: A step-up: when the user authenticated again, and with what.
- `TokenAuthentication`: Opaque tokens: `Authorization: Bearer <AUTH.TOKEN_PREFIX>...`.
- `active_user`: The active user `user_id`, loaded with groups and permissions.

## `domainkit.contrib.auth.backends`

Who a user is, and what they may do (Django's authentication backends).

- `BaseBackend`: A backend that knows no one and grants nothing; override what you need.
- `ModelBackend`: Passwords from the user table; permissions of users and their groups.
- `authenticate`: The user the credentials prove, by the first backend that accepts them.
- `get_all_permissions`: Keys of every permission any backend grants `user` (on `obj`).
- `get_user`: The user with `user_id`, from the first backend that has it.
- `has_perm`: Whether `user` has `perm` (on `obj`).
- `has_perms`: Whether `user` has every one of `perms` (on `obj`).

## `domainkit.contrib.auth.certificates`

Client certificates (mutual TLS): a certificate someone checked names its user.

- `ClientCertificateAuthentication`: Users named by a client certificate the server or a trusted proxy checked.

## `domainkit.contrib.auth.cookies`

Browser sessions: an opaque token in an HttpOnly cookie, guarded against CSRF.

- `CookieAuthentication`: Browser sessions: the session cookie, checked against CSRF.
- `router`: The routes: browser sessions: an opaque token in an HttpOnly cookie, guarded against CSRF.

## `domainkit.contrib.auth.devices`

Device proofs: a client that logged in keeps a count of its own (SEC-r1-2).

- `DEVICE_COOKIE`: The proof's cookie, `__Host-` prefixed where the prefix holds.
- `MAX_DEVICES`: Devices a user holds at once; a new one past it ends the oldest.
- `PROVEN_CEILING`: Proven attempts an account takes from all its devices, in `LOGIN_RATE` units.
- `device_key`: The count of wrong passwords sent with `device`'s proof.
- `hand_out`: Set a proof for `user`'s account, once the whole login held.
- `proven_device`: The device the request proves for `username`'s account, if any.
- `proven_limits`: The counts a proven attempt takes: its device's, and the account's ceiling.

## `domainkit.contrib.auth.hashers`

Password hashing (Django's `hashers`, with the same stored formats).

- `Argon2PasswordHasher`: Argon2id (RFC 9106), the Password Hashing Competition's winner.
- `FastPBKDF2PasswordHasher`: One PBKDF2 iteration: for tests only, where hashing time adds up.
- `PBKDF2PasswordHasher`: PBKDF2 with HMAC-SHA256 (the default; NIST SP 800-132), from the stdlib.
- `PBKDF2SHA1PasswordHasher`: PBKDF2 with HMAC-SHA1, to check hashes moved from older systems.
- `PasswordHasher`: One hashing algorithm; `algorithm` starts every hash it makes.
- `ScryptPasswordHasher`: scrypt (RFC 7914): memory-hard, from the stdlib.

## `domainkit.contrib.auth.mfa`

Logins with a second factor: an authenticator app (TOTP), and recovery codes.

- `router`: The routes: logins with a second factor: an authenticator app (TOTP), and recovery codes.

## `domainkit.contrib.auth.middleware`

`AuthenticationMiddleware`: who sent each request, before anything counts it.

- `AuthenticationMiddleware`: Set `scope["user"]`, `scope["auth"]`, and the request's actor.

## `domainkit.contrib.auth.models`

Users, groups, and permissions: the domain model of `domainkit.contrib.auth`.

- `AbstractBaseUser`: Credentials, status, groups, and permissions of a user.
- `AbstractUser`: A user with a username, an email, names, and a staff flag (Django's).
- `AnonymousUser`: Whoever sends a request without credentials: no id, no permissions.
- `AnyUser`: A user that logged in, or the anonymous one.
- `Group`: A named set of permissions; every member has them all.
- `GroupId`: NewType creates simple unique types with almost zero runtime overhead.
- `PasswordHash`: A stored password: a hasher's `<algorithm>$...`, or unusable (`!...`).
- `Permission`: Permission to do something to one content type: `<domain>.<codename>`.
- `User`: The default user; `AUTH.USER_MODEL` swaps in a project's own.
- `UserId`: NewType creates simple unique types with almost zero runtime overhead.

## `domainkit.contrib.auth.personal_tokens`

Personal tokens: users list, create, and revoke their own.

- `router`: The routes: personal tokens: users list, create, and revoke their own.

## `domainkit.contrib.auth.principals`

Principals: who credentials name, kept between requests (ADR 0058).

- `invalidate`: Drop the principals that rest on `targets` in this process; how many.
- `invalidate_all`: Drop every principal of this process; how many.
- `invalidate_everywhere`: Drop `targets` here, and stamp them for every other process.

## `domainkit.contrib.auth.recovery`

Password resets and email verification, by links sent by email.

- `router`: The routes: password resets and email verification, by links sent by email.

## `domainkit.contrib.auth.sca`

Transactions authorized for one amount and one payee (PSD2's dynamic linking).

- `InvalidTransaction`: The amount, payee, or purpose cannot be authorized as given.
- `ScaChallenge`: A transaction waiting for its user's authorization, or holding it.
- `TransactionNotAuthorized`: No authorization holds for this transaction: get one, then try again (403).
- `consume_authorization`: Spend `authorization` on this transaction, once; return what it was for.
- `request_authorization`: Store a transaction for `user_id` to confirm, for `AUTH.SCA.TTL`.
- `router`: The routes: transactions authorized for one amount and one payee (PSD2's dynamic linking).

## `domainkit.contrib.auth.signup`

Signing up: anyone may ask for an account, if `AUTH.SIGNUP_ENABLED` says so.

- `router`: The routes: signing up: anyone may ask for an account, if `AUTH.SIGNUP_ENABLED` says so.

## `domainkit.contrib.auth.stepup`

Stepping up: authenticating again, with a second factor, for one act.

- `router`: The routes: stepping up: authenticating again, with a second factor, for one act.

## `domainkit.contrib.auth.testing`

Users and groups for tests, stored as the application stores them.

- `create_group`: Store a group granted `permissions` (keys); `group-<hex>` unless named.
- `create_user`: Store a user granted `permissions` (keys), in `groups` (names).

## `domainkit.contrib.auth.validators`

Rules for new passwords (Django's `password_validation`).

- `CommonPasswordValidator`: Not on a list of common passwords (default: Django's 20,000).
- `LettersAndDigitsValidator`: At least one letter and one digit, as PCI DSS v4.0 asks (8.3.6).
- `MinimumLengthValidator`: At least `min_length` characters (default 8).
- `NumericPasswordValidator`: Not only digits.
- `UserAttributeSimilarityValidator`: Not too much like the user's username, names, or email.
