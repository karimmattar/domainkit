# Identity providers: OpenID Connect, OAuth 2, SAML, LDAP

An identity provider is another party that proves who a user is:
Google, Microsoft Entra ID, Apple, GitHub, any OpenID Connect provider
(Keycloak, Okta, Auth0, AWS Cognito, Huawei IAM), a SAML 2.0 identity
provider, or an LDAP or Active Directory server.
`domainkit.contrib.identity` lets users log in through them. A login
through a provider ends as a password login does: with this
application's tokens, or with the second factor's challenge when the user
has one.

On this page you learn how to:

- install the domain and name your providers;
- log a user in through a provider, from the browser that started it;
- link and unlink a provider's account for a user who logged in;
- log in through SAML 2.0;
- check passwords against an LDAP or Active Directory server.

## A first example

Install the extras you need (`identity` for OpenID Connect and OAuth 2,
`saml`, `ldap`). Add the domain after `domainkit.contrib.auth`, run
`makemigrations` and `migrate`, and mount the routes under auth's:

```python
INSTALLED_DOMAINS = [
    "domainkit.contrib.contenttypes",
    "domainkit.contrib.auth",
    "domainkit.contrib.identity",
]
IDENTITY = {
    "PROVIDERS": {
        "github": {
            "KIND": "github",
            "CLIENT_ID": env.str("GITHUB_CLIENT_ID"),
            "CLIENT_SECRET": env.str("GITHUB_CLIENT_SECRET"),
            "REDIRECT_URI": "https://app.example.com/login/github",
        },
    },
    "SIGNUP": True,
}
```

```python
from domainkit.contrib.identity.api import router as identity_router

router.include_router(identity_router, prefix="/auth")
```

Your page starts a login, and keeps the `binding` it gets:

```http
POST /auth/identity/github/login
```

```json
{
  "authorization_url": "https://github.com/login/oauth/authorize?client_id=Iv1.shop&redirect_uri=https%3A%2F%2Fapp.example.com%2Flogin%2Fgithub&scope=read%3Auser+user%3Aemail&state=IMd5j9pXmlJjEKW2CZYD8j4DxkjINkdcTvTb4NWhlpE&code_challenge=BD5GUqVCbNIsQ0CNmcd4rZTA7cIuM_dyOmG2j6wQ1Mo&code_challenge_method=S256",
  "binding": "uAAixaWHkxs9Yx2itr9FFwqjiHRYMzC4ueOrW12-ZNk"
}
```

1. The page keeps `binding` (in `sessionStorage`, for example), and sends
   the browser to `authorization_url`.
2. The provider sends the browser back to `REDIRECT_URI` with `code` and
   `state`.
3. The page posts `{"code", "state", "binding"}` to
   `POST /auth/identity/github/callback`, and gets tokens.

A callback whose `binding` is not the one its start answered is refused.
So a login that an attacker started cannot be finished in the victim's
browser:

```json
{"type": "about:blank", "title": "Unprocessable Content", "status": 422, "detail": "That login was started elsewhere; start it again here.", "instance": "/auth/identity/github/callback", "code": "identity_not_linked", "request_id": "a4d51d01c9b318944599d077ec4ac875"}
```

The identity tables must live on the same database as auth's: the
application refuses to start when [routing](../databases.md) sends them
elsewhere. Identities are linked to users by the provider's subject,
never by email.

## Settings

`IDENTITY = {...}`; every key is optional.

| Key | Default | Meaning |
| --- | --- | --- |
| `PROVIDERS` | `{}` | OpenID Connect and OAuth 2 providers, by the name that routes use (below). |
| `SAML` | `{}` | SAML 2.0 identity providers, by name ([SAML 2.0](#saml-20)). |
| `LDAP` | `None` | The directory that `LDAPBackend` checks passwords against ([LDAP](#ldap-and-active-directory)). |
| `SIGNUP` | `False` | Make an account for a new identity whose email the provider verified. |
| `FLOW_TTL` | 10 minutes | How long a login started at a provider may take to come back. |
| `TICKET_TTL` | 1 minute (at most 10) | How long a ticket from a SAML response may wait to be redeemed. |
| `FRONTEND_URL` | `None` | Your page that a SAML response sends the browser to, with `?ticket=` or `?error=`. |

A provider in `PROVIDERS`:

| Key | Default | Meaning |
| --- | --- | --- |
| `KIND` | `"oidc"` | `oidc` for any OpenID Connect provider by its `ISSUER`; `google`, `microsoft`, `apple`, and `github` know their own endpoints. |
| `CLIENT_ID` | required | This application's client at the provider. |
| `CLIENT_SECRET` | `None` | The client's secret. Apple's secret is made from `APPLE_*` instead. |
| `REDIRECT_URI` | required | Your page that the provider sends the browser back to, with the code. |
| `ISSUER` | `None` | The OpenID Connect issuer (`oidc`): its discovery document is read. |
| `TENANT` | `None` | Microsoft: the Entra ID tenant whose accounts may log in. Required for `microsoft`. |
| `SCOPES` | `("openid", "email", "profile")` | The scopes asked of an OpenID Connect provider. GitHub always asks `read:user user:email`. |
| `APPLE_TEAM_ID`, `APPLE_KEY_ID`, `APPLE_PRIVATE_KEY` | `None` | Apple's ES256 key (PEM), which signs the client secret. |

Two providers, one of each kind:

```python
IDENTITY = {
    "PROVIDERS": {
        "google": {
            "KIND": "google",
            "CLIENT_ID": env.str("GOOGLE_CLIENT_ID"),
            "CLIENT_SECRET": env.str("GOOGLE_CLIENT_SECRET"),
            "REDIRECT_URI": "https://app.example.com/login/google",
        },
        "keycloak": {
            "ISSUER": "https://sso.example.com/realms/staff",
            "CLIENT_ID": "billing",
            "CLIENT_SECRET": env.str("KEYCLOAK_SECRET"),
            "REDIRECT_URI": "https://app.example.com/login/keycloak",
        },
    },
    "SIGNUP": True,
}
```

## Logging in and linking

| Endpoint | Who | Answer |
| --- | --- | --- |
| `GET /identity/providers` | anyone | `{"providers": [...], "saml": [...]}`: the names. |
| `POST /identity/{name}/login` | anyone | `{"authorization_url", "binding"}`: send the browser there, and keep the binding for the callback. |
| `POST /identity/{name}/link` | a user, unscoped | `{"password"}`, or a step-up in the last 10 minutes: the same answer, to link an identity to the user. In the [phone modes](ciba.md#managing-logins-from-the-phone), signed by the user's device (`403 phone_required`). |
| `POST /identity/{name}/callback` | anyone | `{"code", "state", "binding"}`: `200` with tokens, `202` with the second factor's challenge, or `{"linked": "<name>"}` for a link. |
| `POST /identity/ticket` | anyone | `{"ticket"}` that a SAML response gave, from the browser that started the login (its cookie): tokens or a challenge, once. |
| `GET /identity/linked` | a user, unscoped | The user's identities: `provider`, `email`, `created_at`. |
| `DELETE /identity/linked/{name}` | a user, unscoped | `204`: unlinked, unless it is the last way in. Signed in the phone modes, as linking is. |

Answers that hold tokens are marked `Cache-Control: no-store`.

### What protects a flow

- **PKCE, state, and nonce.** Every authorization URL carries PKCE (S256)
  and a state, and for OpenID Connect a nonce. They are kept on the
  server and used once. A state works only at its provider, and only
  within `FLOW_TTL`.
- **The browser that started it.** A flow is finished only in the
  browser that started it. The callback must send the start's `binding`:
  a signature of its state under `SECRET_KEY`, which a page that did not
  start the flow never holds. So a login or a link of the attacker's own,
  finished in the victim's browser, is refused (`422 identity_not_linked`).
  Nothing extra is stored. A flow started before `SECRET_KEY` changed is
  started again.
- **The ID token.** It is believed only when it is signed by a key of the
  provider's JWKS with an asymmetric algorithm (RS, PS, or ES; never
  `none` or HMAC), issued by the provider, for this client, unexpired
  (60 seconds of clock difference allowed), and with the flow's nonce.
- **GitHub**, which speaks OAuth 2 alone, is asked for the user and
  their verified primary email.
- **Keys and discovery documents** are kept for an hour. A key id not
  seen before reads the keys again, once, so a provider's key rotation
  never refuses a login.

### Where a login ends

A login comes back to one of three ends:

- **The identity is linked**: its user logs in, unless inactive.
- **It is not linked, `SIGNUP` is on, and the provider verified its
  email, which no account has**: an account is made, with an unusable
  password and its email verified, and the identity is linked to it
  (`UserSignedUp`). The username is the email without other characters,
  or `id-<subject>` when that is taken.
- **Otherwise it is refused**: `409 identity_email_taken` when an account
  has the email, because logging in to it would hand it to whoever
  controls the provider's account. Its owner logs in and links the
  identity instead. `422 identity_not_linked` in every other case,
  including a user model with signup fields that a provider does not
  give.

### Linking needs the password

A linked identity is a way into the account. So starting a link asks the
user to authenticate again, as adding a passkey does. Whoever held a
stolen access token could otherwise link their own identity and keep the
account.

```http
POST /auth/identity/github/link
Authorization: Bearer <a login token>

{"password": "correct horse battery"}
```

- **Without a password**, and without a step-up in the last 10 minutes:
  `401 reauthentication_required` ("Confirm it is you: send your
  password, or step up first.").
- **A wrong password**: `400 wrong_password`, counted against
  `AUTH.LOGIN_RATE` with the user's other password checks.
- **Never as an impersonation**: staff acting as the user get
  `401 reauthentication_required`, password or not.

Then:

- a link refuses an identity linked to another account
  (`409 identity_taken`);
- unlinking refuses to leave a user without a usable password and without
  another identity (`422 identity_last_way_in`);
- linking and unlinking are audited (`identity.identity_linked`,
  `identity.identity_unlinked`; the events `IdentityLinked` and
  `IdentityUnlinked`);
- deleting a user (`UserDeleted`) unlinks their identities, and
  `privacy export` and `privacy erase` list and unlink them too.

A provider that refuses, cannot be reached, or answers what does not
hold is `502 identity_provider_error`. Its reason is logged, never sent.

## SAML 2.0

```python
IDENTITY = {
    "SAML": {
        "okta": {
            "ENTITY_ID": "https://api.example.com/saml",
            "ACS_URL": "https://api.example.com/auth/identity/saml/okta/acs",
            "IDP_ENTITY_ID": "http://www.okta.com/exk...",
            "IDP_SSO_URL": "https://acme.okta.com/app/.../sso/saml",
            "IDP_CERTIFICATE": env.str("OKTA_CERTIFICATE"),
            "EMAIL_ATTRIBUTE": "email",
        },
    },
    "FRONTEND_URL": "https://app.example.com/login/done",
}
```

| `SAML` entry key | Default | Meaning |
| --- | --- | --- |
| `ENTITY_ID` | required | This application's entity id at the provider. |
| `ACS_URL` | required | Where the provider posts its response: `.../identity/saml/<name>/acs`. |
| `IDP_ENTITY_ID` | required | The provider's entity id; the assertion's issuer. |
| `IDP_SSO_URL` | required | The provider's single sign-on URL (HTTP-Redirect binding). |
| `IDP_CERTIFICATE` | required | The provider's signing certificate (PEM): only what it signs is believed. |
| `EMAIL_ATTRIBUTE` | `None` | The attribute that holds the email; `None`: the NameID, when its format is an email address. |

| Endpoint | Answer |
| --- | --- |
| `GET /identity/saml/{name}/login` | `302` to `IDP_SSO_URL` with an AuthnRequest, and the cookie that binds the flow to the browser. |
| `POST /identity/saml/{name}/acs` | The provider's response: `303` to `FRONTEND_URL` with `?ticket=` or `?error=`. |
| `GET /identity/saml/{name}/metadata` | This service provider's metadata, for the identity provider. |

How a SAML login works:

1. `GET /identity/saml/{name}/login` sends the browser to `IDP_SSO_URL`
   with an AuthnRequest, whose id and RelayState are a flow's. It also
   sets the cookie `__Host-dk_identity` (`Secure`, `HttpOnly`,
   `SameSite=None`), which binds the login to this browser for
   `FLOW_TTL`.
2. The provider posts its response to `/identity/saml/{name}/acs`. The
   ACS checks the cookie, so a login that another browser started is
   refused.
3. The browser is sent (`303`) to `FRONTEND_URL` with a one-time
   `?ticket=`, or with `?error=`: `saml_refused` for a response that does
   not hold, or a code of [Where a login ends](#where-a-login-ends).
4. The page trades the ticket for tokens at `POST /identity/ticket`,
   within `TICKET_TTL`, with credentials (cookies). The redemption
   checks the cookie again, then clears it. Tokens never travel in a URL.

The cookie must reach both the ACS and the ticket's redemption, so the
page and the API must share a site. A ticket from another browser, or one
that is unknown, used, or expired, gets `422 identity_not_linked`
("That ticket expired or was used; log in again.").

What the ACS believes:

- **Only the element signed with `IDP_CERTIFICATE`** (RSA or ECDSA,
  SHA-256 or stronger). So an unsigned assertion slipped beside a signed
  one (signature wrapping) is never read.
- **The assertion must** name the provider as issuer, `ENTITY_ID` as
  audience, and `ACS_URL` as recipient; answer this request
  (`InResponseTo`); and be within its times (60 seconds of clock
  difference allowed).
- **The email** is `EMAIL_ATTRIBUTE`'s value, or the NameID when its
  format is an email address. A SAML email counts as verified for
  `SIGNUP`.
- **Encrypted assertions are not read.**

SAML logs in; it does not link. Its identities are kept under the
provider name `saml:<name>`.

## LDAP and Active Directory

`LDAPBackend` checks a password against a directory. Add it to
`AUTH.BACKENDS`, after `ModelBackend`:

```python
AUTH = {
    **AUTH,
    "BACKENDS": [
        "domainkit.contrib.auth.backends.ModelBackend",
        "domainkit.contrib.identity.ldap.LDAPBackend",
    ],
}
IDENTITY = {
    "LDAP": {
        "URL": "ldaps://dc.example.com",
        "BIND_DN": "cn=reader,ou=services,dc=example,dc=com",
        "BIND_PASSWORD": env.str("LDAP_READER_PASSWORD"),
        "USER_SEARCH_BASE": "ou=people,dc=example,dc=com",
        "USER_FILTER": "(sAMAccountName={username})",
        "GROUPS": {"cn=finance,ou=groups,dc=example,dc=com": "finance"},
    }
}
```

| `LDAP` key | Default | Meaning |
| --- | --- | --- |
| `URL` | required | `ldaps://dc.example.com`, or `ldap://` with `START_TLS`. |
| `START_TLS` | `False` | Upgrade an `ldap://` connection to TLS. |
| `CA_CERTIFICATES` | `None` | A file of the certificates that sign the directory's; `None`: the system's. |
| `BIND_DN`, `BIND_PASSWORD` | `None` | The account that finds users; `None`: an anonymous search. |
| `USER_SEARCH_BASE` | required | Where users are found. |
| `USER_FILTER` | `"(uid={username})"` | The filter that names one user; `(sAMAccountName={username})` for Active Directory. |
| `EMAIL_ATTRIBUTE` | `"mail"` | The attribute that holds the email. |
| `GROUPS` | `{}` | A directory group's DN, and the group a member is put in here. |
| `TIMEOUT` | `10.0` | Seconds each step may take (at most 60). |

`/auth/login` then also checks a password against the directory:

1. **The reader** (`BIND_DN`) finds the one entry that `USER_FILTER`
   names. The username is escaped, so a `*` or a `)` in it is only a
   character. No entry, or more than one, refuses the login.
2. **A bind as that entry with the password** proves it. An empty
   password is refused before any bind, because a directory takes one as
   an anonymous bind, which would "succeed".
3. **On the first login** the user is made here, with an unusable local
   password, and linked by the entry's DN. An account here with the same
   username (in any case) that the directory did not make is never taken
   over: the login is refused, and a warning is logged.
4. **Each login keeps the user in step**: the email (`EMAIL_ATTRIBUTE`),
   and membership of the groups that `GROUPS` maps. Other groups are
   left alone. An inactive user is refused.

The connection is always encrypted where deployed, and always checked:

- **TLS.** `START_TLS` upgrades an `ldap://` connection. Staging and
  production refuse to start with `ldap://` and no `START_TLS`
  (`security.E020`):

  ```text
  security.E020: IDENTITY.LDAP reaches the directory without TLS in production
  ```

  Every password a login checks would otherwise cross the network in the
  clear. Use an `ldaps://` URL, or set `START_TLS`.
- **The certificate is verified.** Over TLS, the directory's certificate
  is checked against the system's certificates, or against the file that
  `CA_CERTIFICATES` names, and so is its name. A directory with a
  certificate of a private CA needs `CA_CERTIFICATES`.
- **Failures refuse the login.** `TIMEOUT` bounds each step. A directory
  that cannot be reached, or proves no valid certificate, refuses the
  password, and a warning naming the kind of error is logged. The login
  then gets `401 invalid_credentials`, as a wrong password does.

The directory is called in a thread, so the event loop never waits.

## The Python names

For code of your own, `domainkit.contrib.identity` exports:

| Name | What it is |
| --- | --- |
| `IdentityUnitOfWork` | `AuthUnitOfWork` with `identities`, `flows`, and `tickets`. |
| `LinkedIdentity` | One link: `user_id`, `provider`, `subject`, `email`, `created_at`. |
| `Claims` | Who a provider says logged in: `subject`, `email`, `email_verified`. |
| `ProviderError`, `UnknownProvider`, `NoAccount`, `EmailTaken`, `IdentityTaken`, `LastWayIn` | The errors below. |

```python
from domainkit.contrib.identity import IdentityUnitOfWork

async with IdentityUnitOfWork() as uow:
    for linked in await uow.identities.of_user(user.id):
        print(linked.provider, linked.email, linked.created_at)
```

The submodules hold the rest: `services` (`begin`, `complete`,
`provider`, `unlink`), `providers` (`Provider`, `OIDCProvider`,
`AppleProvider`, `GitHubProvider`, `make_provider`), `binding` (`binding`,
`bound`, `COOKIE`, `TICKET_SEPARATOR`), `events` (`IdentityLinked`,
`IdentityUnlinked`), `stores` (`Flow`), `tables` (`identities`, `flows`,
`tickets`), `ldap` (`LDAPBackend`), and `saml` and `api` (the routers).

## Errors and refusals

| Exception | Status and code | When |
| --- | --- | --- |
| `UnknownProvider` | `404 unknown_identity_provider` | No provider has that name. |
| `NoAccount` | `422 identity_not_linked` | No account for the identity and none may be made; an expired, used, or foreign state; a callback without its binding; a ticket from another browser. |
| `EmailTaken` | `409 identity_email_taken` | An account has the identity's email: log in to it, then link. |
| `IdentityTaken` | `409 identity_taken` | Another account has the identity. |
| `LastWayIn` | `422 identity_last_way_in` | Unlinking would leave no way to log in. |
| `ProviderError` | `502 identity_provider_error` | The provider refused, cannot be reached, or answered what does not hold. |
| | `401 reauthentication_required` | A link without the password or a recent step-up, or under impersonation. |
| | `security.E020` | `ldap://` without `START_TLS`, in staging or production. |

## Common mistakes

- **Not sending `binding` with the callback.** The callback is refused.
  Keep it from the start, in the same browser.
- **Serving the SAML page on another site than the API.** The
  `__Host-dk_identity` cookie must reach the ACS and the ticket route.
- **Expecting a login to take over an account with the same email.** It
  never does. The owner logs in, then links the identity.
- **A directory with a private CA and no `CA_CERTIFICATES`.** Every
  login is refused, and the log names the certificate error.
- **`ldap://` in production.** Startup refuses it; use `ldaps://` or
  `START_TLS`.

## See also

- [Captcha](logging-in.md#captcha) on login, signup, and password reset.
- [Impersonation](routes.md#impersonation): staff acting as a user.
- [Passkeys and bound devices](devices.md): another way in without a
  password.
- [Stepping up](step-up.md): a recent step-up stands for the password
  when linking.
