# Identity providers: OpenID Connect, OAuth 2, SAML, LDAP

`domainkit.contrib.identity` logs users in through another party's
accounts: Google, Microsoft Entra ID, Apple, GitHub, any OpenID Connect
provider (Keycloak, Okta, Auth0, AWS Cognito, Huawei IAM), a SAML 2.0
identity provider, or an LDAP or Active Directory server. A login through
a provider ends as a password login does: with this application's tokens,
or with the second factor's challenge when the user has one.

## Installing it

Install the extras you need (`identity` for OpenID Connect and OAuth 2,
`saml`, `ldap`), add the domain after `domainkit.contrib.auth`, migrate,
and mount the routes under auth's:

```python
INSTALLED_DOMAINS = [..., "domainkit.contrib.auth", "domainkit.contrib.identity"]
```

```python
from domainkit.contrib.identity.api import router as identity_router

router.include_router(identity_router, prefix="/auth")
```

The identity tables must live on the same database as auth's: the
application refuses to start when [routing](../databases.md) sends them
elsewhere. Identities are linked to users by the provider's subject,
never by email.

## Settings

`IDENTITY = {...}`; every key is optional.

| Key | Default | Meaning |
| --- | --- | --- |
| `PROVIDERS` | `{}` | OpenID Connect and OAuth 2 providers, by the name routes use (below). |
| `SAML` | `{}` | SAML 2.0 identity providers, by name (below). |
| `LDAP` | `None` | The directory `LDAPBackend` checks passwords against (below). |
| `SIGNUP` | `False` | Make an account for a new identity whose email the provider verified. |
| `FLOW_TTL` | 10 minutes | How long a login started at a provider may take to come back. |
| `TICKET_TTL` | 1 minute (at most 10) | How long a ticket from a SAML response may wait to be redeemed. |
| `FRONTEND_URL` | `None` | Your page a SAML response sends the browser to, with `?ticket=` or `?error=`. |

A provider in `PROVIDERS`:

| Key | Meaning |
| --- | --- |
| `KIND` | `oidc` (the default) for any OpenID Connect provider by its `ISSUER`; `google`, `microsoft`, `apple`, and `github` know their own endpoints. |
| `CLIENT_ID`, `CLIENT_SECRET` | This application's client at the provider. Apple's secret is made from `APPLE_*` instead. |
| `REDIRECT_URI` | Your page the provider sends the browser back to, with the code. |
| `ISSUER` | The OpenID Connect issuer (`oidc`): its discovery document is read. |
| `TENANT` | Microsoft: the Entra ID tenant whose accounts may log in. |
| `SCOPES` | `("openid", "email", "profile")`. |
| `APPLE_TEAM_ID`, `APPLE_KEY_ID`, `APPLE_PRIVATE_KEY` | Apple's ES256 key, which signs the client secret. |

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
| `POST /identity/{name}/login` | anyone | `{"authorization_url", "binding"}`: send the browser there, and keep the binding (in `sessionStorage`, say) for the callback. |
| `POST /identity/{name}/link` | a user | The same, to link an identity to the user: `{"password"}`, or a step-up in the last ten minutes; never as an impersonation (`reauthentication_required`, 401). |
| `POST /identity/{name}/callback` | anyone | `{"code", "state"}` the provider sent your page, and the `binding` its start answered: `200` with tokens, `202` with the second factor's challenge, or `{"linked": "<name>"}` for a link. |
| `POST /identity/ticket` | anyone | `{"ticket"}` a SAML response gave, from the browser that started the login (its cookie): tokens or a challenge, once. |
| `GET /identity/linked` | a user | The user's identities: provider, email, when linked. |
| `DELETE /identity/linked/{name}` | a user | `204`: unlinked, unless it is the last way in. |

Every authorization URL carries PKCE (S256), a state, and (OpenID
Connect) a nonce, kept server-side and used once: a state works only at
its provider and only within `FLOW_TTL`. A flow is finished only in the
browser that started it: the callback must send the start's `binding`
(a signature of its state, which a page that did not start the flow
never holds), so a link or login of the attacker's own, finished in the
victim's browser, is refused. An ID token is believed only
when signed by a key of the provider's JWKS with an asymmetric
algorithm, issued by the provider, for this client, unexpired, and with
the flow's nonce. GitHub, which speaks OAuth 2 alone, is asked for the
user and their verified primary email. Discovery documents and keys are
kept an hour; a key id not seen before reads the keys again, once, so a
provider's key rotation never refuses a login.

A login comes back to one of three ends:

- the identity is linked: its user logs in, unless inactive;
- it is not, `SIGNUP` is on, and the provider verified its email, which
  no account has: an account is made with an unusable password, its
  email verified, and the identity linked to it (`UserSignedUp`);
- otherwise it is refused: `identity_email_taken` (409) when an account
  has the email, since logging in to it would hand it to whoever controls
  the provider's account (its owner logs in and links the identity
  instead); `identity_not_linked` (422) else, and when the user model
  has signup fields a provider does not give.

A linked identity is a way into the account, so starting a link asks the
user to authenticate again, as adding a passkey does: whoever held a
stolen access token could otherwise link their own identity and keep the
account. Linking and unlinking are audited (`identity.identity_linked`,
`identity.identity_unlinked`). A link refuses an identity linked to
another account (`identity_taken`, 409); unlinking refuses to leave a user without a
usable password and without another identity (`identity_last_way_in`,
422). A provider that refuses, cannot be reached, or answers what does
not hold is `identity_provider_error` (502); its reason is logged, never
sent. Deleting a user (`UserDeleted`) unlinks their identities.

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

`GET /identity/saml/{name}/login` sends the browser to `IDP_SSO_URL`
with an AuthnRequest (HTTP-Redirect binding), whose id and RelayState are
a flow's. The provider posts its response to `/identity/saml/{name}/acs`,
which believes only the element signed with `IDP_CERTIFICATE` (RSA or
ECDSA, SHA-256 or stronger), so an unsigned assertion slipped beside a
signed one (signature wrapping) is never read. The assertion must name
the provider as issuer, `ENTITY_ID` as audience, `ACS_URL` as recipient,
answer this request (`InResponseTo`), and be within its times. The email
is `EMAIL_ATTRIBUTE`'s value, or the NameID when its format is an email
address. Encrypted assertions are not read.

`/identity/saml/{name}/login` sets a cookie (`__Host-dk_identity`,
`HttpOnly`, `SameSite=None`) binding the login to the browser; the ACS and
the ticket's redemption refuse a login another browser started, so the
page and the API must share a site for it to be sent. The browser is
then sent (303) to `FRONTEND_URL` with a one-time
`?ticket=`, which the page trades for tokens at `/identity/ticket`
within `TICKET_TTL`, with credentials (cookies), or with `?error=`: `saml_refused` for a response
that does not hold, or a problem code above. Tokens never travel in a
URL. `/identity/saml/{name}/metadata` describes this service provider
for the identity provider. SAML logs in; it does not link.

## LDAP and Active Directory

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

`/auth/login` then also checks a password against the directory: the
reader (`BIND_DN`) finds the one entry `USER_FILTER` names, the username
escaped, and a bind as that entry with the password proves it. An empty
password is refused before any bind, since a directory takes one as an
anonymous bind. On the first login the user is made here, with an
unusable local password, and linked by the entry's DN; each login keeps
the email (`EMAIL_ATTRIBUTE`, `mail`) and the groups `GROUPS` maps in
step, leaving other groups alone. An account with the same username that
the directory did not make is never taken over. `START_TLS` upgrades an
`ldap://` connection; staging and production refuse `ldap://` without it
(`security.E020`). Over TLS the directory's certificate is checked, and
its name: against the system's certificates, or the file
`CA_CERTIFICATES` names. `TIMEOUT` (10 seconds) bounds each step, and a
directory that cannot be reached, or proves no certificate, refuses the
password and is logged.

## Captcha and impersonation

A captcha on login, signup, and password reset is
`AUTH.CAPTCHA` ([Logging in](logging-in.md#captcha)); staff acting as a
user is `AUTH.IMPERSONATION_TTL` ([Routes](routes.md#impersonation)).
