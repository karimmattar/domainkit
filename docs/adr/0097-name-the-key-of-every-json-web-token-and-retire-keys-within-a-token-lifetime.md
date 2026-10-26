# 0097. Name the key of every JSON Web Token, and retire keys within a token's lifetime

- Status: Accepted
- Date: 2026-10-26
- Amends: [0016](0016-authenticating-requests.md)

## Context

`JWTAuthentication` ([0016](0016-authenticating-requests.md)) signed and
verified access tokens with one key, `AUTH.JWT.SIGNING_KEY`, and its
tokens carried no `kid` header. A key could not be replaced without
refusing every outstanding access token at once, and nothing could keep
the old key for verification, for a while or for good. Other services
that verify tokens with the public key could not tell which key a token
needs (AUTH-10 of the 0.9.0 audit). Refresh tokens are opaque and stored,
so a client whose access token is refused can refresh, but every client
does so at the same moment, and a service that only verifies cannot.

[0028](0028-one-secret-key-signs-and-derives.md) keeps the JWT keys apart
from `SECRET_KEY`: other services check tokens with a key they share,
which must not be the key every other signature of the service derives
from. So `SECRET_KEY_FALLBACKS`, which keeps old secret keys verifying
until they are removed by hand, does not apply to tokens, and rotating
`SECRET_KEY` never touches them. JWT keys need their own rotation.

Options considered, for each part:

- **How a token names its key.** An id the operator writes can be
  reused for another key, and nothing ties it to the key. Trying every
  key in turn, without an id, costs a verification per key and keeps a
  retired key a way in for as long as it is listed. An id derived from
  the key needs no setting. For a public key, the JWK thumbprint (RFC
  7638, SHA-256 of the key's required members) is the standard id, which
  any service holding the public key computes alike. For an HMAC secret
  the id must not reveal the secret: a plain hash of a weak secret could
  be guessed offline, so the id is a keyed hash, HMAC-SHA256 of a fixed
  label under the secret itself. It says nothing a token's signature, an
  HMAC under the same secret, does not.
- **How long a previous key verifies.** Unbounded, removed by hand (as
  `SECRET_KEY_FALLBACKS`): a forgotten entry verifies forever. Until a
  time the operator writes: bounded only by what is written. Until the
  time the key stopped signing, plus one token lifetime: no token it
  signed before then outlives that, so the window follows from the
  token's own bound.
- **Tokens without `kid`** (all of 0.10's). Refusing them would refuse
  every access token at the upgrade, the very problem. Trying every key
  for them is the unbounded search above. Checking them with the signing
  key alone keeps 0.10's behaviour for exactly the tokens 0.10 issued.

## Decision

- Every token `JWTAuthentication` issues carries `kid`. An HMAC key's
  is the first 16 bytes of HMAC-SHA256 of `domainkit:jwt-key-id` under
  the secret; a public key's is its RFC 7638 thumbprint (of
  `VERIFYING_KEY` for the signing key); both base64url without padding.
- `AUTH.JWT.PREVIOUS_KEYS` lists at most three keys that signed before,
  each with `KEY` (the old HMAC secret, or the old public key in PEM; a
  private key is refused, as a retired key only verifies), `ALGORITHM`
  (the one it signed with; `ALGORITHM` by default), and `RETIRED_AT`
  (aware: when the last process stopped signing with it).
- The header's `kid` picks the key, and with it the one algorithm
  accepted: a token's own `alg` never chooses one, so the RSA public key
  cannot be passed off as an HMAC secret, and `none` never verifies. An
  id the set lacks is refused, without trying another key. A previous
  key verifies until `RETIRED_AT` plus `ACCESS_TOKEN_TTL` and `LEEWAY`,
  on domainkit's clock; then its tokens are refused, unexpired or not.
- A token without `kid` is checked with the signing key alone, never a
  previous one.
- Startup refuses, as `CheckFailed` with every problem at once, a
  previous key that is the signing key or that is listed twice
  (`auth.E008`, compared by id, so the same key in other PEM text is
  found), and a `RETIRED_AT` more than a day ahead (`auth.E009`): a key
  that retires later is still in use, and its window would not be
  bounded by a rollout. Short secrets and keys that do not load for
  their algorithm are refused as the signing key's are.
- Keys are loaded once, at startup, not from PEM for every token; the
  header is read for its `kid` alone, and PyJWT then checks that the key
  it names signed that very header.
- `SECRET_KEY` stays apart, as 0028 decided.

## Consequences

- A key is replaced without refusing a token: set the new one as
  `SIGNING_KEY`, list the old one in `PREVIOUS_KEYS` with `RETIRED_AT`
  at the end of the rollout, deploy, and remove the entry once
  `RETIRED_AT` plus `ACCESS_TOKEN_TTL` has passed. A process that still
  signs with the old key after `RETIRED_AT` issues tokens that are
  refused once the window ends; their clients refresh, as all did before.
- Upgrading from 0.10 refuses nothing: its tokens verify with the
  signing key until they expire. Rotating in the same deploy as the
  upgrade ends them (they name no key, so no previous key checks them):
  rotate once `ACCESS_TOKEN_TTL` has passed since the upgrade.
- The window is computed from the current `ACCESS_TOKEN_TTL`: lowering
  it during a rotation shortens the old key's window too.
- Tokens grow by the header member: 42 characters with an HMAC key's
  id, 70 with a thumbprint. Verifying one makes more Python calls, read
  with a profiler on PyJWT 2.15 (deterministic, unlike timings on a
  shared machine):

  | Algorithm | Token, 0.10 | Token, 0.11 | Calls, 0.10 | Calls, 0.11 |
  | --- | --- | --- | --- | --- |
  | HS256 | 303 | 345 | 497 | 572 |
  | RS256 | 602 | 672 | 782 | 872 |
  | ES256 | 346 | 416 | 535 | 626 |

  PyJWT reads every segment of a token with Python code proportional to
  its length; reading the header with PyJWT's own `get_unverified_header`
  instead cost 385 calls for HS256 and 684 for RS256, as it decodes the
  whole token.
- Another service verifying with the public key picks it by thumbprint.
  One sharing an HMAC secret computes its id as above.
