# Public API: users and permissions

`domainkit.contrib.auth`, `domainkit.contrib.identity`, `domainkit.contrib.devices`,
and their modules
([auth.md](../auth.md), [identity](../auth/identity.md),
[devices](../auth/devices.md)). Part of
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
- `EmailTaken`: Another user has that email address, and it identifies users.
- `Group`: A named set of permissions; every member has them all.
- `GroupId`: A group's id: a UUID typed apart from other ids.
- `GroupNameTaken`: Another group has that name.
- `GroupNotFound`: No group has that id.
- `IdentifierRequired`: A user needs at least one identifier: a username, an email, or a number.
- `InvalidEmail`: The email address is not one plain address mail can go to.
- `InvalidGroupName`: The group name is empty or too long.
- `InvalidMobileNumber`: The mobile number is not one in international form (E.164).
- `InvalidPassword`: The password breaks the configured rules (`PASSWORD_VALIDATORS`).
- `InvalidUserField`: A value does not fit the type of the user field it is for.
- `InvalidUsername`: The username is empty, too long, or uses characters it may not.
- `IsAdminUser`: Staff users (`is_staff`), with credentials that are not limited to scopes.
- `IsSuperuser`: Active superusers, with credentials that are not limited to scopes.
- `IssuedTokens`: What logging in or refreshing returns (OAuth 2's token response).
- `JWTAuthentication`: Signed access tokens (JSON Web Tokens) and opaque refresh tokens.
- `JWTSettings`: `AUTH["JWT"]`: signed access tokens (`JWTAuthentication`).
- `MobileNumberTaken`: Another user has that mobile number.
- `ModelBackend`: Passwords from the user table; permissions of users and their groups.
- `PasswordHash`: A stored password: a hasher's `<algorithm>$...`, or unusable (`!...`).
- `PasswordValidationError`: A password breaks one rule; the message says which.
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
- `UserId`: A user's id: a UUID typed apart from other ids.
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
- `normalize_mobile_number`: `text` as `+33612345678`: spaces, dots, dashes, and brackets dropped.
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
- `ObjectGrant`: `permission` granted on one object: its content type and its id.
- `authenticate`: The user the credentials prove, by the first backend that accepts them.
- `backends_without_object_grants`: The configured backends that do not say what they grant on objects.
- `get_all_permissions`: Keys of every permission any backend grants `user` (on `obj`).
- `get_user`: The user with `user_id`, from the first backend that has it.
- `has_perm`: Whether `user` has `perm` (on `obj`).
- `has_perms`: Whether `user` has every one of `perms` (on `obj`).
- `object_grants`: What every backend grants `holder` (a user or a group) on single objects.

## `domainkit.contrib.auth.captcha`

A captcha on the routes bots try: login, signup, password reset.

- `CaptchaUnavailable`: The captcha provider could not check the token: 503.
- `captcha`: A dependency that checks the captcha of `action`, if it is `ON`.

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

## `domainkit.contrib.auth.gates`

Login gates: checks every login passes before it is issued (ADR 0128).

- `LoginGate`: A check of a login: return to let it through, raise to refuse it.
- `LoginNotGated`: A login was issued without passing the gates: a route forgot them (500).
- `check_gates_passed`: Refuse the login of `user` if gates exist and it did not pass them.
- `gates_passed`: Let the login of `user_id` through, in the block, with no gate run.
- `on_login_gate`: Register a gate every login passes (`on_login_gate(gate)` in `ready()`).
- `pass_login_gates`: Run every gate on the login of `user`; record that it passed.

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
- `GroupId`: A group's id: a UUID typed apart from other ids.
- `PasswordHash`: A stored password: a hasher's `<algorithm>$...`, or unusable (`!...`).
- `Permission`: Permission to do something to one content type: `<domain>.<codename>`.
- `User`: The default user; `AUTH.USER_MODEL` swaps in a project's own.
- `UserId`: A user's id: a UUID typed apart from other ids.

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

## `domainkit.contrib.auth.tables`

The columns a custom user table starts with (`AUTH.USER_TABLE`).

- `abstract_base_user_columns`: New columns for the fields of `AbstractBaseUser`.
- `abstract_user_columns`: New columns for the fields of `AbstractUser`.

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

## `domainkit.contrib.identity`

Logging in through identity providers: OpenID Connect, OAuth 2, SAML 2.0, LDAP.

- `Claims`: Who the provider says logged in: its subject, and an email if it gave one.
- `EmailTaken`: An account has the identity's email: log in to it, then link the identity.
- `IdentityTaken`: The identity is linked to another account already.
- `IdentityUnitOfWork`: The auth unit of work, with the identity tables (on the same database).
- `LastWayIn`: Unlinking would leave the user no way to log in.
- `LinkedIdentity`: A provider's account a user logs in with.
- `NoAccount`: The identity is linked to no account, and none may be made for it.
- `ProviderError`: The provider refused, could not be reached, or answered what does not hold (502).
- `UnknownProvider`: No identity provider has that name (404).

## `domainkit.contrib.identity.api`

The routes of logging in and linking through identity providers.

- `router`: The routes: providers, login, link, callback, ticket, and linked identities.

## `domainkit.contrib.identity.binding`

A login or link started in one browser is finished in that browser only.

- `COOKIE`: The cookie SAML's start sets: the binding of the browser's flow.
- `TICKET_SEPARATOR`: Between a SAML ticket and the flow it ends, in what the page redeems.
- `binding`: The binding of the flow `flow`: what its browser holds.
- `bound`: Whether `given` is the binding of `flow`, compared in constant time.

## `domainkit.contrib.identity.events`

What happens to a user's identities at providers (audited as `identity.*`).

- `IdentityLinked`: A user linked their identity at a provider: a new way into the account.
- `IdentityUnlinked`: A user unlinked their identity at a provider.

## `domainkit.contrib.identity.exceptions`

What an identity provider, or a login through one, may refuse.

- `EmailTaken`: An account has the identity's email: log in to it, then link the identity.
- `IdentityTaken`: The identity is linked to another account already.
- `LastWayIn`: Unlinking would leave the user no way to log in.
- `NoAccount`: The identity is linked to no account, and none may be made for it.
- `ProviderError`: The provider refused, could not be reached, or answered what does not hold (502).
- `UnknownProvider`: No identity provider has that name (404).

## `domainkit.contrib.identity.ldap`

Logging in with a directory's password: LDAP or Active Directory (`domainkit[ldap]`).

- `LDAPBackend`: Passwords checked by the directory `IDENTITY.LDAP` names.

## `domainkit.contrib.identity.providers`

OpenID Connect and OAuth 2 providers: where to send the browser, and what came back.

- `AppleProvider`: Sign in with Apple: its client secret is a JWT this application signs.
- `Claims`: Who the provider says logged in: its subject, and an email if it gave one.
- `GitHubProvider`: GitHub accounts (OAuth 2): the user, and their verified primary email.
- `OIDCProvider`: An OpenID Connect provider, found by its issuer's discovery document.
- `Provider`: A provider of identities; subclasses speak its protocol.
- `make_provider`: The provider `config` describes.

## `domainkit.contrib.identity.saml`

SAML 2.0 single sign-on, as a service provider (`domainkit[saml]`).

- `router`: The routes: login, the assertion consumer service, and metadata.

## `domainkit.contrib.identity.services`

Logging in and linking through identity providers.

- `begin`: Start a login (a link, for `user_id`) at `name`: the browser goes there.
- `complete`: Finish what `state` started: the flow, and the user to log in (None: a link).
- `provider`: The provider `name`.
- `unlink`: Unlink the user's identity at `name`.

## `domainkit.contrib.identity.stores`

The identity tables, read and written in an `IdentityUnitOfWork`.

- `Flow`: A login or link sent to a provider; its `verifier` and `nonce` are secret.
- `IdentityUnitOfWork`: The auth unit of work, with the identity tables (on the same database).
- `LinkedIdentity`: A provider's account a user logs in with.

## `domainkit.contrib.identity.tables`

Linked identities, logins on their way back from a provider, and tickets.

- `flows`: Logins and links on their way to a provider and back, by state.
- `identities`: The provider accounts users are linked to, one per provider and subject.
- `tickets`: One-use tickets a browser trades for tokens, kept as digests.

## `domainkit.contrib.devices`

Passkeys (WebAuthn) and bound devices: keys a user's devices hold.

- `BoundDevice`: A user's device: a key it holds, which signs requests.
- `DeviceNotFound`: The user has no device with that id.
- `DeviceSignatureRefused`: The request is not signed by a device of the user, now, once (401).
- `DeviceSigned`: Requests a bound device of the user signed, now, once.
- `DevicesUnitOfWork`: The auth unit of work, with the device tables (on the same database).
- `Passkey`: A user's passkey: a WebAuthn credential's public key and counter.
- `PasskeyNotFound`: The user has no passkey with that id.
- `PasskeyRefused`: A passkey's answer does not hold (401); the reason is logged, not sent.
- `PasskeysOff`: `DEVICES.RP_ID` is not set: no passkeys here (404).
- `ReauthenticationRequired`: A change to how one logs in needs the password, or a recent step-up (401).
- `TooManyKeys`: The user has `MAX_PASSKEYS` (or `MAX_DEVICES`) already: remove one first.
- `UnsupportedKey`: Not a P-256 or Ed25519 public key (SubjectPublicKeyInfo, base64url).

## `domainkit.contrib.devices.api`

The routes of passkeys and bound devices.

- `router`: The routes: passkeys registered, logging in and stepping up; devices bound, listed, unbound, stepping up.

## `domainkit.contrib.devices.binding`

Requests signed by a bound device's key.

- `DeviceSigned`: Requests a bound device of the user signed, now, once.
- `device_key`: A device key's algorithm, and the key as SubjectPublicKeyInfo.
- `signed_message`: What a device signs for a request.

## `domainkit.contrib.devices.events`

What happens to a user's passkeys and devices (audited as `devices.*`).

- `DeviceBound`: A user bound a device's key.
- `DeviceUnbound`: A user unbound a device.
- `PasskeyAdded`: A user registered a passkey.
- `PasskeyRemoved`: A user removed a passkey.

## `domainkit.contrib.devices.exceptions`

What a passkey, or a bound device, may be refused.

- `DeviceNotFound`: The user has no device with that id.
- `DeviceSignatureRefused`: The request is not signed by a device of the user, now, once (401).
- `PasskeyNotFound`: The user has no passkey with that id.
- `PasskeyRefused`: A passkey's answer does not hold (401); the reason is logged, not sent.
- `PasskeysOff`: `DEVICES.RP_ID` is not set: no passkeys here (404).
- `ReauthenticationRequired`: A change to how one logs in needs the password, or a recent step-up (401).
- `TooManyKeys`: The user has `MAX_PASSKEYS` (or `MAX_DEVICES`) already: remove one first.
- `UnsupportedKey`: Not a P-256 or Ed25519 public key (SubjectPublicKeyInfo, base64url).

## `domainkit.contrib.devices.passkeys`

Passkeys: registering one, and logging in or stepping up with it.

- `Assertion`: A passkey's answer to a challenge, base64url as the browser sends it.
- `authenticate`: The active user whose passkey answered a challenge of `purpose`.
- `creation_options`: What `navigator.credentials.create()` needs, as WebAuthn's JSON.
- `reauthenticate`: The user authenticated again: `password`, or a recent step-up.
- `register`: Store the passkey a registration's response proves.
- `request_options`: What `navigator.credentials.get()` needs, as WebAuthn's JSON.

## `domainkit.contrib.devices.settings`

Settings of `domainkit.contrib.devices`: `DEVICES = {...}`.

- `DevicesSettings`: Passkeys and bound devices; every key is optional.

## `domainkit.contrib.devices.stores`

The device tables, read and written in a `DevicesUnitOfWork`.

- `BoundDevice`: A user's device: a key it holds, which signs requests.
- `Challenge`: A challenge handed to a browser: for what, and for whom.
- `DevicesUnitOfWork`: The auth unit of work, with the device tables (on the same database).
- `Passkey`: A user's passkey: a WebAuthn credential's public key and counter.
- `PushToken`: Where a push service reaches one of the user's apps.

## `domainkit.contrib.devices.tables`

Passkeys, their challenges, and bound devices.

- `challenges`: Challenges handed to a browser, until its passkey answers one.
- `devices`: Each user's bound devices: a key the device holds, and its last signed time.
- `passkeys`: Each user's passkeys: a WebAuthn credential's public key and counter.
- `push_tokens`: The tokens push services gave each user's apps, one row per token.

## `domainkit.contrib.devices.push`

Push notifications to a user: to every app of theirs that gave a token.

- `notify_user`: Queue a notification to each of the user's tokens; how many.

## `domainkit.contrib.auth.integration`

What apps built on auth use: log a user in, step them up, hear of them.

- `PASSWORD_MAX_LENGTH`: The longest password auth checks: longer ones are refused before hashing.
- `REAUTH_AGE`: How recent a step-up must be to stand for the password (`reauthenticate`).
- `MfaChallengeOut`: The password holds; send this token and a code to `/mfa/login`.
- `ReauthenticationRequired`: A change to how one logs in needs the password, or a recent step-up (401).
- `StepUpOut`: When the credentials stepped up, and with what (OpenID Connect's names).
- `SteppedUp`: A user authenticated again, for acts that ask for it (`StepUp`).
- `TokenOut`: Tokens (OAuth 2's token response): send `Authorization: Bearer ...`.
- `UserDeleted`: A user was deleted.
- `UserSignedUp`: Someone created their own account (`AUTH.SIGNUP_ENABLED`).
- `challenge`: A challenge, if `user` has a second factor; None if not.
- `check_password_again`: Check the user's password again, counted with their other password checks.
- `gates_passed`: Let the login of `user_id` through, in the block, with no gate run.
- `impersonated`: Whether staff act as the user on `connection` (an impersonation).
- `issue_login`: Record the user's login, and issue them tokens (the first class's).
- `on_login_gate`: Register a gate every login passes (`on_login_gate(gate)` in `ready()`).
- `pass_login_gates`: Run every gate on the login of `user`; record that it passed.
- `proof_of`: The step-up of the connection's credentials, read once a request.
- `reauthenticate`: The user authenticated again: `password`, or a step-up within `REAUTH_AGE`.
- `record_step_up`: Keep `proof` where the credentials keep step-ups, and say so (`SteppedUp`).
- `step_up_answer`: What a step-up answers: when, with what, and a JWT login's new token.
- `stepping_credentials`: The request's credentials, if they can keep a step-up.
- `utc_now`: The current time in UTC.

## `domainkit.contrib.phones`

Phone verification: a code by text message, and the numbers users proved.

- `CodeExpired`: No code works for this number: none sent, too old, or tried too often (422).
- `CodeTooSoon`: A code went to this number moments ago (429 `rate_limited`).
- `InvalidPhone`: Not a phone number in international form, such as `+33612345678` (422).
- `PendingCode`: A code on its way: the number, until when it works, and when to resend.
- `PhoneAlreadyVerified`: The user verified this number already (409).
- `PhoneNotFound`: The user has no verified number with that id (404).
- `PhoneRemoved`: A user removed a verified number.
- `PhoneVerified`: A user proved they receive messages at a number.
- `PhonesSettings`: How codes are made, sent, and tried; every key is optional.
- `PhonesUnitOfWork`: The auth unit of work, with the phone tables (on the same database).
- `TooManyCodes`: `PHONES.MAX_SENDS` codes went to this number, or for this user (429).
- `TooManyPhones`: The user holds `PHONES.MAX_PHONES` numbers already (409).
- `VerifiedPhone`: A number a user proved they receive messages at.
- `WrongCode`: The code is not the one sent (422); a few more tries are left.
- `confirm_code`: Check `code` against the last one sent; keep the number if it matches.
- `normalize_phone`: `text` as `+33612345678`: spaces, dots, dashes, brackets dropped.
- `remove_phone`: Forget the user's verified number `phone_id`.
- `send_code`: Make a code for `phone` and queue the text message holding it.

## `domainkit.contrib.phones.api`

The routes that verify a user's phone numbers.

- `router`: The routes: ask for a code, verify a number, list and remove verified numbers.

## `domainkit.contrib.phones.events`

What happens to a user's phone numbers (audited as `phones.*`).

- `PhoneRemoved`: A user removed a verified number.
- `PhoneVerified`: A user proved they receive messages at a number.

## `domainkit.contrib.phones.exceptions`

What verifying a phone number may be refused.

- `CodeExpired`: No code works for this number: none sent, too old, or tried too often (422).
- `CodeTooSoon`: A code went to this number moments ago (429 `rate_limited`).
- `InvalidPhone`: Not a phone number in international form, such as `+33612345678` (422).
- `PhoneAlreadyVerified`: The user verified this number already (409).
- `PhoneNotFound`: The user has no verified number with that id (404).
- `TooManyCodes`: `PHONES.MAX_SENDS` codes went to this number, or for this user (429).
- `TooManyPhones`: The user holds `PHONES.MAX_PHONES` numbers already (409).
- `WrongCode`: The code is not the one sent (422); a few more tries are left.

## `domainkit.contrib.phones.services`

Send a code to a number, check it, and keep the number as verified.

- `PendingCode`: A code on its way: the number, until when it works, and when to resend.
- `confirm_code`: Check `code` against the last one sent; keep the number if it matches.
- `normalize_phone`: `text` as `+33612345678`: spaces, dots, dashes, brackets dropped.
- `phones_settings`: `PHONES`, validated (the defaults when it is unset).
- `remove_phone`: Forget the user's verified number `phone_id`.
- `send_code`: Make a code for `phone` and queue the text message holding it.

## `domainkit.contrib.phones.settings`

Settings of `domainkit.contrib.phones`: `PHONES = {...}`.

- `PhonesSettings`: How codes are made, sent, and tried; every key is optional.

## `domainkit.contrib.phones.stores`

The phone tables, read and written in a `PhonesUnitOfWork`.

- `PhoneCode`: A code sent to a number: its keyed digest, never the code.
- `PhonesUnitOfWork`: The auth unit of work, with the phone tables (on the same database).
- `VerifiedPhone`: A number a user proved they receive messages at.

## `domainkit.contrib.phones.tables`

Codes sent to phone numbers, and the numbers users verified.

- `phone_codes`: Each code sent, as a keyed digest, until it is used, spent, or old: counted to limit sends.
- `phones`: The numbers each user proved they receive messages at.

## `domainkit.contrib.ciba`

Approve sign-ins from the user's mobile app: OIDC CIBA, push and QR.

- `AccessDenied`: The phone denied the sign-in.
- `ApprovalRequired`: This login must be approved from the user's mobile app (403).
- `AuthorizationPending`: The phone has not answered yet: poll again after the interval.
- `CibaClient`: An OIDC client that may ask users' phones: who, how it proves it, what for.
- `CibaSettings`: How sign-in requests reach the phone, last, and are limited; all optional.
- `CibaUnitOfWork`: The devices unit of work, with the CIBA tables (on the same database).
- `DifferentNetwork`: The phone is not on the address of the page (403, `QR_SAME_NETWORK`).
- `PollRefused`: A poll that gives no tokens (400); `code` says why, as CIBA names it.
- `QrCodeExpired`: The QR code is unknown, was claimed, or expired (422).
- `Requester`: Who started a sign-in, as the phone is shown it: an address, a browser.
- `SignInApproved`: A user approved a sign-in from one of their bound devices.
- `SignInDenied`: A user denied a sign-in.
- `SignInExpired`: The request's time is up before the phone approved it.
- `SignInNotFound`: The user has no sign-in request with that id that waits for an answer (404).
- `SignInReported`: A user denied a sign-in they did not start: someone else knows them.
- `SignInRequest`: A sign-in a phone is asked to approve, as its row keeps it.
- `SignInUnknown`: No request has that handle, or it was redeemed, or is another client's.
- `SlowDown`: A poll came before the interval: wait 5 seconds more from now on.
- `Started`: A request started: what its initiator keeps, and shows on its page.
- `WrongMatchCode`: The number typed is not the page's: the sign-in is denied (422).
- `approve`: Approve the user's request from their device `device_id`.
- `claim`: Make the QR request whose code a phone of `user_id` scanned theirs.
- `deny`: Deny the user's request; with `report`, every other one waiting too.
- `poll`: The approved request `handle` polls for, redeemed now; it works once.
- `start_push`: Ask `user`'s phone to approve a sign-in; queue the push to their apps.
- `start_qr`: A request for whichever phone scans its QR code, within `QR_TTL`.

## `domainkit.contrib.ciba.api`

The routes of sign-ins approved from the phone: the web's, and the phone's.

- `router`: The routes: sign-ins approved from the phone: the web's, and the phone's.

## `domainkit.contrib.ciba.events`

What happens to a user's sign-in requests (audited as `ciba.*`).

- `SignInApproved`: A user approved a sign-in from one of their bound devices.
- `SignInDenied`: A user denied a sign-in.
- `SignInReported`: A user denied a sign-in they did not start: someone else knows them.

## `domainkit.contrib.ciba.exceptions`

What a sign-in request may be refused, and what a poll may answer.

- `AccessDenied`: The phone denied the sign-in.
- `ApprovalRequired`: This login must be approved from the user's mobile app (403).
- `AuthorizationPending`: The phone has not answered yet: poll again after the interval.
- `DifferentNetwork`: The phone is not on the address of the page (403, `QR_SAME_NETWORK`).
- `PollRefused`: A poll that gives no tokens (400); `code` says why, as CIBA names it.
- `QrCodeExpired`: The QR code is unknown, was claimed, or expired (422).
- `SignInExpired`: The request's time is up before the phone approved it.
- `SignInNotFound`: The user has no sign-in request with that id that waits for an answer (404).
- `SignInUnknown`: No request has that handle, or it was redeemed, or is another client's.
- `SlowDown`: A poll came before the interval: wait 5 seconds more from now on.
- `WrongMatchCode`: The number typed is not the page's: the sign-in is denied (422).

## `domainkit.contrib.ciba.gate`

The login gate of `CIBA.APPROVE_LOGINS`: the mobile app is the identity.

- `approved_from_the_phone`: Let the login of `user` through if the app is not their identity, or signs it.

## `domainkit.contrib.ciba.services`

Start a sign-in request, let the phone answer it, and poll it (ADR 0128).

- `PUSH_KIND`: The `type` of a push's data: the app fetches the request it names.
- `Requester`: Who started a sign-in, as the phone is shown it: an address, a browser.
- `Started`: A request started: what its initiator keeps, and shows on its page.
- `approve`: Approve the user's request from their device `device_id`.
- `ciba_settings`: `CIBA`, validated (the defaults when it is unset).
- `claim`: Make the QR request whose code a phone of `user_id` scanned theirs.
- `deny`: Deny the user's request; with `report`, every other one waiting too.
- `poll`: The approved request `handle` polls for, redeemed now; it works once.
- `start_push`: Ask `user`'s phone to approve a sign-in; queue the push to their apps.
- `start_qr`: A request for whichever phone scans its QR code, within `QR_TTL`.

## `domainkit.contrib.ciba.settings`

Settings of `domainkit.contrib.ciba`: `CIBA = {...}`.

- `CibaSettings`: How sign-in requests reach the phone, last, and are limited; all optional.

## `domainkit.contrib.ciba.stores`

The CIBA tables, read and written in a `CibaUnitOfWork`.

- `CibaClient`: An OIDC client that may ask users' phones: who, how it proves it, what for.
- `CibaUnitOfWork`: The devices unit of work, with the CIBA tables (on the same database).
- `SignInRequest`: A sign-in a phone is asked to approve, as its row keeps it.

## `domainkit.contrib.ciba.tables`

CIBA clients, sign-in requests, and the client assertions already used.

- `assertions`: The `jti` of each `private_key_jwt` assertion, until it expires: once.
- `clients`: The OIDC clients that may ask a user's phone (`domainkit ciba clients`).
- `sign_in_requests`: Each sign-in a phone is asked to approve, from its start until purged.
