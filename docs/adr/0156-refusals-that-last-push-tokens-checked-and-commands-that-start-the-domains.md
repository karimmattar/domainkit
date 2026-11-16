# 0156. Refusals that last, push tokens checked when registered, and commands that start the domains

- Status: Accepted
- Date: 2026-11-16

## Context

A project that runs domainkit reported three things that #150 left open
(user reports N2 and W1).

1. **A lasting refusal is retried like an outage.** Push backends raise
   `NotificationRefused` since #150, and the push and text tasks do not
   retry it. But no SMS adapter raised it: Twilio, Vonage, SNS, Unifonic
   and Huawei Cloud raised `NotificationFailed` for everything, without
   the provider's code. The mail task had no `dont_retry_on`, and
   `MailDeliveryFailed` had no lasting kind. An invalid number, a
   recipient who replied STOP, or a mailbox that does not exist was tried
   five times, the last tries minutes apart.
2. **A push token is not checked when it is registered.**
   `POST /auth/devices/push-tokens` stores any string under the platform
   named; the first push finds out. In production, a token a phone
   simulator made up failed a superuser's step-up push.
3. **Commands skip the domains' startup.** `BaseCommand.needs_domains`
   exists since #150, and only `runworker` sets it. Which other built-in
   commands fail with a key service that is not `local`?

### What the providers document

- **SMTP** (RFC 5321 [1], 4.2.1): a `5yz` reply is a permanent negative
  completion; "the SMTP client SHOULD NOT repeat the exact request". A
  `4yz` reply is transient: the client should try again. 4.5.3.1.10: a
  `552` to `RCPT` for too many recipients should be treated as temporary.
- **SMS APIs** answer with their own codes, each documented. Twilio [2]:
  `21211` invalid `To`, `21614` not a mobile number, `21610` unsubscribed
  (STOP), `21408` a region the geo permissions block, `21612` a `To`/`From`
  pair it cannot route. Vonage [3]: statuses `3` invalid parameter, `6`
  unroutable, `7` barred number, `12` too long, `33` de-activated; `1`
  throttled, `9` no credit. SNS Publish [4]: `InvalidParameter`,
  `ParameterValueInvalid` (400). Unifonic [5]: `ER-02` to `ER-05`
  (malformed, too long or short, no route), `ER-07` filtered, `ER-13`
  geo-blocked, `ER-14` blacklisted, `ER-99` temporary; statuses `410`,
  `412`, `481`, `482`. Huawei Cloud SMS [6]: `E000503` parameter format,
  `E200028` template variable.
- **Mail APIs.** SES v2 SendEmail [7]: `MessageRejected`,
  `BadRequestException` (content, input) beside `SendingPausedException`
  and `AccountSuspendedException` (account state). SendGrid [8] and
  Mailgun [9]: `400` and `413`. Postmark [10]: codes `300` (invalid
  message), `402`/`403` (JSON), `406` (inactive recipient), `411`
  (attachment); its code `413` is an account not approved, while the HTTP
  status `413` is a request too large.
- **Push token checks.** FCM's `messages:send` takes `validate_only`,
  "testing the request without actually delivering the message" [11];
  Firebase's own integration tests send a dry run to `INVALID_TOKEN` and
  expect `InvalidArgumentError` [12]. Push Kit takes `validate_only`: a
  test message, "only used to verify the validity of the format", not
  pushed [13]. APNs has no call that checks a token without delivering to
  it; Apple's tokens are hex.

### Options

Which refusals last:

| Option | What it costs |
| --- | --- |
| Every `4xx` lasts | Credentials (`401`), quotas (`429`), an account paused, a sender not yet allowed: an operator fixes them within the retries, and the messages would be lost. |
| **Each adapter lists the codes its provider documents for a message it will refuse again (`lasting`); SMTP reads the reply class** | A list per provider, kept with a link to its reference; a code not listed is retried, as before. |

Checking a push token:

| Option | What it costs |
| --- | --- |
| Always check | One call to the service per registration, for every project; an app registers on every start. |
| Check, and fail closed | An outage at FCM stops every app from registering; nothing is gained, since the worker forgets a token gone at its first push. |
| **Opt in (`DEVICES.PUSH_TOKEN_CHECK`), bounded by `TIMEOUT`, failing open** | A token is refused only when its service says it does not know it; an outage costs a warning in the log. |

Which commands start the domains:

| Option | What it costs |
| --- | --- |
| Every command | `migrate` would run the trail's startup check before it made the triggers it looks for; `keys generate` could not make the first key; `audit verify` could not run on the trail it is meant to check. |
| **The commands that run the application's work, or other domains' hooks written for it** | Each other command keeps running without the startup; the commands reference says why for each. |

What fails without the startup: the ring unwraps its keys inside
SQLAlchemy's events (`await_only` in the session's greenlet), so ORM
loads and writes, and `run_sync`, work unloaded. A blind index is
computed as the query is built, outside that greenlet, and raises
`KeysNotLoaded`. The tests ran every candidate with a fake Vault and a
ring not yet unwrapped: `privacy export`/`erase` (built-in hooks only),
`dumpdata --decrypt`, `keys status`, `audit verify` and `messages` passed;
`seed` with a hook that looks up by a blind index, and `privacy
export`/`erase` with such a hook for a person with no sealed row loaded
first, failed.

## Decision

- **`MailRefused`**, a `MailDeliveryFailed`, which the mail task does not
  retry. `SmtpBackend` raises it for a `5xx` reply to the sender, to
  every recipient (but `552`), or to the message; connecting, `STARTTLS`
  and logging in stay retried. Both mail errors keep the code in `code`.
- **`lasting`** on each SMS and mail provider adapter: the codes above.
  `refused(code)` raises `NotificationRefused` or `MailRefused` for them,
  the failure otherwise, and keeps the code. A subclass gives its own.
- **`PushBackend.check_token(token)`**: `PushTokenGone` when the service
  does not know the token, `NotificationFailed` when it cannot tell, and
  nothing by default. FCM and Push Kit send a dry run; APNs checks the
  token's form. **`check_push_token(token, platform, within=...)`**
  answers False only for a token gone; a failure or a timeout counts as
  True, logged without the token.
- **`DEVICES["PUSH_TOKEN_CHECK"] = {"ENABLED": False, "TIMEOUT": 3.0}`**
  (at most 10 seconds): the route asks first, outside the transaction,
  and refuses with `422 push_token_refused`. It needs notifications.
- **`seed`**, **`privacy export`** and **`privacy erase`** start the
  domains, as `runworker` does; `@action(needs_domains=True)` marks one
  action. `shell` does not: it opens for repairs, when a startup check
  refuses, and `await keyring().load()` unwraps the keys there.

## Consequences

- An invalid number, an opted-out recipient, or a mailbox that does not
  exist is dead-lettered after one try, with its code; the dead gauge
  rises at once instead of after five tries. A provider code missing
  from a list is retried, as before; adding it later changes nothing
  else.
- The lists follow the providers' references, read in November 2026;
  no adapter was run against a live service, and the answers in the
  tests are the references', given by stubs.
- With the check on, a registration waits up to `TIMEOUT` for the push
  service, and costs one call to it. Push Kit documents its test message
  as a check of the format: a token it does not know may still pass
  there, and the first push tells, as before.
- `seed` and `privacy export`/`erase` call the key service at start, and
  refuse where a startup check refuses, as the application would. A
  command that commits events still runs in-process handlers without the
  startup; a project whose handlers query by a blind index sets
  `needs_domains` on its own commands, or runs that work in the worker.

## References

1. RFC 5321, Simple Mail Transfer Protocol, 4.2.1 and 4.5.3.1.10: <https://www.rfc-editor.org/rfc/rfc5321>
2. Twilio error dictionary: <https://www.twilio.com/docs/api/errors> (21211, 21408, 21610, 21612, 21614)
3. Vonage SMS API, troubleshooting (status codes): <https://developer.vonage.com/en/messaging/sms/guides/troubleshooting-sms>
4. Amazon SNS Publish, errors: <https://docs.aws.amazon.com/sns/latest/api/API_Publish.html>
5. Unifonic, sending your first SMS (statuses, `ER-04`), and the reasons of non-delivery (`ER-` codes): <https://docs.unifonic.com/articles/products-documentation/sending-your-first-sms-via-unifonic-api>, <https://docs.unifonic.com/docs/understanding-reasons-of-sms-non-delivery>
6. Huawei Cloud Message & SMS, API error codes: <https://support.huaweicloud.com/intl/en-us/api-msgsms/sms_05_0050.html>
7. Amazon SES v2 SendEmail, errors: <https://docs.aws.amazon.com/ses/latest/APIReference-V2/API_SendEmail.html>
8. SendGrid v3 Mail Send: <https://www.twilio.com/docs/sendgrid/api-reference/mail-send/mail-send>
9. Mailgun API overview (status codes): <https://documentation.mailgun.com/docs/mailgun/api-reference/api-overview>
10. Postmark API overview (error codes, HTTP statuses): <https://postmarkapp.com/developer/api/overview>
11. FCM HTTP v1, `projects.messages/send`: <https://firebase.google.com/docs/reference/fcm/rest/v1/projects.messages/send>
12. firebase-admin-go, integration tests of dry runs: <https://github.com/firebase/firebase-admin-go/blob/master/integration/messaging/messaging_test.go>
13. Huawei Push Kit, server API (`validate_only`): <https://developer.huawei.com/consumer/en/doc/HMS-References/push-sendapi>
