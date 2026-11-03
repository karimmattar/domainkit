"""Provider adapters for text messages, push notifications, and mail by API."""

import base64
import hashlib
import json
from collections.abc import Callable
from contextvars import ContextVar
from email import message_from_bytes
from typing import Any
from urllib.parse import parse_qs

import httpx2
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa

from domainkit.contrib.mail import EmailMessage, MailDeliveryFailed
from domainkit.contrib.mail import providers as mail_providers
from domainkit.contrib.notifications import (
    NotificationFailed,
    PushMessage,
    SmsMessage,
)
from domainkit.contrib.notifications import push as push_module
from domainkit.contrib.notifications.backends import PushTokenGone
from domainkit.contrib.notifications.push import (
    ApnsPushBackend,
    FcmPushBackend,
    HuaweiPushBackend,
)
from domainkit.contrib.notifications.sms import (
    HuaweiSmsBackend,
    SnsSmsBackend,
    TwilioSmsBackend,
    UnifonicSmsBackend,
    VonageSmsBackend,
    wsse,
)
from domainkit.http import client as client_module

pytestmark = pytest.mark.anyio

PHONE = "+33612345678"
SMS = SmsMessage(to=PHONE, body="Your code is 123456").prepared()
PUSH = PushMessage(
    token="a" * 64, title="Shipped", body="On its way", data={"order": "7"}
)
type Handler = Callable[[httpx2.Request], httpx2.Response]


class Recorder:
    """A provider that answers ``answers`` in turn and keeps every request."""

    def __init__(self, *answers: httpx2.Response | Exception) -> None:
        """Answer with these, the last one again once they run out."""
        self.answers = list(answers)
        self.requests: list[httpx2.Request] = []

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        """Keep the request; answer the next answer."""
        self.requests.append(request)
        answer = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        if isinstance(answer, Exception):
            raise answer
        return answer

    def form(self, index: int = -1) -> dict[str, list[str]]:
        """A request's form, parsed."""
        return parse_qs(self.requests[index].content.decode())

    def json(self, index: int = -1) -> Any:  # noqa: ANN401 - any JSON.
        """A request's JSON body."""
        return json.loads(self.requests[index].content)


@pytest.fixture
def provider(monkeypatch: pytest.MonkeyPatch) -> Callable[..., Recorder]:
    """``provider(*answers)``: answer every call made in the test.

    The stub is the default of the client's context variable, so the
    test's own context, wherever it runs, sees it.
    """

    def make(*answers: httpx2.Response | Exception) -> Recorder:
        recorder = Recorder(*answers)
        monkeypatch.setattr(
            client_module,
            "_stub",
            ContextVar("stubbed", default=httpx2.MockTransport(recorder)),  # noqa: B039
        )
        return recorder

    return make


DOWN = httpx2.ConnectError("refused")


async def test_twilio_posts_the_message_to_the_account(provider: Any) -> None:  # noqa: ANN401
    """Basic auth by the account, the From, and the text."""
    calls = provider(httpx2.Response(201, json={"sid": "SM1"}))
    backend = TwilioSmsBackend(
        {"ACCOUNT_SID": "AC1", "AUTH_TOKEN": "secret", "FROM": "+15005550006"}
    )

    assert await backend.send_messages([SMS]) == 1

    request = calls.requests[0]
    assert request.url.path == "/2010-04-01/Accounts/AC1/Messages.json"
    assert (
        request.headers["authorization"]
        == "Basic " + base64.b64encode(b"AC1:secret").decode()
    )
    assert calls.form() == {
        "To": [PHONE],
        "Body": ["Your code is 123456"],
        "From": ["+15005550006"],
    }


async def test_twilio_refusals_and_messaging_services(provider: Any) -> None:  # noqa: ANN401
    """Twilio's error code; a messaging service picks the sender; one is needed."""
    calls = provider(httpx2.Response(400, json={"code": 21211}), DOWN)
    backend = TwilioSmsBackend(
        {
            "ACCOUNT_SID": "AC1",
            "AUTH_TOKEN": "secret",
            "API_KEY_SID": "SK1",
            "MESSAGING_SERVICE_SID": "MG1",
        }
    )

    with pytest.raises(NotificationFailed, match="error 21211"):
        await backend.send_messages([SMS])
    with pytest.raises(NotificationFailed, match="could not be reached"):
        await backend.send_messages([SMS])
    assert calls.form(0)["MessagingServiceSid"] == ["MG1"]
    assert "From" not in calls.form(0)
    assert calls.requests[0].headers["authorization"].startswith("Basic U0sx")
    with pytest.raises(ValueError, match="FROM or MESSAGING_SERVICE_SID"):
        TwilioSmsBackend({"ACCOUNT_SID": "AC1", "AUTH_TOKEN": "x"})


async def test_vonage_needs_every_part_accepted(provider: Any) -> None:  # noqa: ANN401
    """Status 0 for each part; unicode text marked so; a refused part fails."""
    calls = provider(
        httpx2.Response(200, json={"messages": [{"status": "0"}]}),
        httpx2.Response(200, json={"messages": [{"status": "4"}]}),
    )
    backend = VonageSmsBackend({"API_KEY": "k", "API_SECRET": "s", "FROM": "Acme"})
    arabic = SmsMessage(to=PHONE, body="رمزك 123456", sender="Shop").prepared()

    await backend.send_messages([arabic])
    with pytest.raises(NotificationFailed, match="status 4"):
        await backend.send_messages([SMS])

    first = calls.form(0)
    assert (first["to"], first["type"], first["from"]) == (
        ["33612345678"],
        ["unicode"],
        ["Shop"],
    )
    assert calls.form(1)["type"] == ["text"]


async def test_sns_publishes_signed_with_sigv4(provider: Any) -> None:  # noqa: ANN401
    """Publish, transactional, the sender id; AWS's error code on a refusal."""
    calls = provider(
        httpx2.Response(200, text="<PublishResponse/>"),
        httpx2.Response(400, text="<Error><Code>InvalidParameter</Code></Error>"),
        httpx2.Response(500, text="oops"),
    )
    backend = SnsSmsBackend(
        {
            "REGION": "eu-west-1",
            "ACCESS_KEY_ID": "AKIDEXAMPLE",
            "SECRET_ACCESS_KEY": "secret",
            "SENDER_ID": "Acme",
        }
    )

    await backend.send_messages([SMS])
    with pytest.raises(NotificationFailed, match="InvalidParameter"):
        await backend.send_messages([SMS])
    with pytest.raises(NotificationFailed, match="500"):
        await backend.send_messages([SMS])

    request = calls.requests[0]
    assert request.url.host == "sns.eu-west-1.amazonaws.com"
    assert "/eu-west-1/sns/aws4_request" in request.headers["authorization"]
    form = calls.form(0)
    assert form["Action"] == ["Publish"]
    assert form["MessageAttributes.entry.1.Value.StringValue"] == ["Transactional"]
    assert form["MessageAttributes.entry.2.Value.StringValue"] == ["Acme"]


async def test_sns_without_keys_reads_the_environment(
    monkeypatch: pytest.MonkeyPatch,
    provider: Any,  # noqa: ANN401
) -> None:
    """No keys: the environment's credentials, no sender id."""
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIDENV")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "secret")
    calls = provider(httpx2.Response(200))

    await SnsSmsBackend({"REGION": "us-east-1"}).send_messages([SMS])

    assert "AKIDENV/" in calls.requests[0].headers["authorization"]
    assert len([k for k in calls.form() if k.endswith(".Name")]) == 1


async def test_unifonic_needs_success(provider: Any) -> None:  # noqa: ANN401
    """``success: true``; else its error code."""
    calls = provider(
        httpx2.Response(200, json={"success": True}),
        httpx2.Response(200, json={"success": "false", "errorCode": "ER-02"}),
    )
    backend = UnifonicSmsBackend({"APP_SID": "app", "SENDER_ID": "Acme"})

    await backend.send_messages([SMS])
    with pytest.raises(NotificationFailed, match="ER-02"):
        await backend.send_messages([SMS])
    assert calls.form(0)["Recipient"] == ["33612345678"]


async def test_huawei_sms_signs_with_wsse(provider: Any) -> None:  # noqa: ANN401
    """The template's one variable is the text; code 000000; WSSE's digest holds."""
    calls = provider(
        httpx2.Response(200, json={"code": "000000"}),
        httpx2.Response(200, json={"code": "E000510"}),
        httpx2.Response(502, text="bad gateway"),
    )
    backend = HuaweiSmsBackend(
        {
            "URL": "https://smsapi.example.com:443/",
            "APP_KEY": "key",
            "APP_SECRET": "secret",
            "SENDER": "8823",
            "TEMPLATE_ID": "t1",
            "SIGNATURE": "Acme",
        }
    )

    await backend.send_messages([SMS])
    with pytest.raises(NotificationFailed, match="E000510"):
        await backend.send_messages([SMS])
    with pytest.raises(NotificationFailed, match="502"):
        await backend.send_messages([SMS])

    request = calls.requests[0]
    assert request.url.path == "/sms/batchSendSms/v1"
    assert json.loads(calls.form(0)["templateParas"][0]) == ["Your code is 123456"]
    assert calls.form(0)["signature"] == ["Acme"]
    header = dict(
        part.split("=", 1)
        for part in request.headers["x-wsse"].removeprefix("UsernameToken ").split(",")
    )
    nonce, created = header["Nonce"].strip('"'), header["Created"].strip('"')
    digest = base64.b64encode(
        hashlib.sha256(f"{nonce}{created}secret".encode()).digest()
    ).decode()
    assert header["PasswordDigest"].strip('"') == digest
    assert wsse("k", "s") != wsse("k", "s")  # A fresh nonce each time.
    unsigned = HuaweiSmsBackend(
        {
            "URL": "https://smsapi.example.com",
            "APP_KEY": "key",
            "APP_SECRET": "secret",
            "SENDER": "8823",
            "TEMPLATE_ID": "t1",
        }
    )
    calls.answers = [httpx2.Response(200, json={"code": "000000"})]
    await unsigned.send_messages([SMS])
    assert "signature" not in calls.form()


def service_account_file(tmp_path: Any) -> str:  # noqa: ANN401 - a path.
    """A Google service account's key file."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    path = tmp_path / "fcm.json"
    path.write_text(
        json.dumps(
            {
                "type": "service_account",
                "client_email": "push@acme.iam.gserviceaccount.com",
                "private_key": pem,
                "token_uri": "https://oauth2.googleapis.com/token",
            }
        )
    )
    return str(path)


async def test_fcm_sends_with_the_service_accounts_token(
    tmp_path: Any,  # noqa: ANN401
    provider: Any,  # noqa: ANN401
) -> None:
    """A token for FCM's scope, kept; the message; gone and refused answers."""
    gone = {
        "error": {"status": "NOT_FOUND", "details": [{"errorCode": "UNREGISTERED"}]}
    }
    calls = provider(
        httpx2.Response(200, json={"access_token": "ya29", "expires_in": 3600}),
        httpx2.Response(200, json={"name": "projects/acme/messages/1"}),
        httpx2.Response(400, json=gone),
        httpx2.Response(404, json={}),
        httpx2.Response(400, json={"error": {"status": "INVALID_ARGUMENT"}}),
    )
    backend = FcmPushBackend(
        {"PROJECT_ID": "acme", "CREDENTIALS_FILE": service_account_file(tmp_path)}
    )

    await backend.send_messages([PUSH])
    for _ in range(2):
        with pytest.raises(PushTokenGone):
            await backend.send_messages([PUSH])
    with pytest.raises(NotificationFailed, match="INVALID_ARGUMENT"):
        await backend.send_messages([PUSH])

    send = calls.requests[1]
    assert send.url.path == "/v1/projects/acme/messages:send"
    assert send.headers["authorization"] == "Bearer ya29"
    assert calls.json(1)["message"] == {
        "token": "a" * 64,
        "notification": {"title": "Shipped", "body": "On its way"},
        "data": {"order": "7"},
    }
    assert len(calls.requests) == 5  # One token for the four sends.
    push_module._google_accounts.clear()


async def test_fcm_on_google_cloud_asks_the_metadata_server(provider: Any) -> None:  # noqa: ANN401
    """No key file: the metadata server's account; no data, no data field."""
    calls = provider(
        httpx2.Response(200, json={"access_token": "meta", "expires_in": 3600}),
        httpx2.Response(200, json={}),
    )
    bare = PushMessage(token="t", title="Hi", body="")

    await FcmPushBackend({"PROJECT_ID": "acme"}).send_messages([bare])

    assert calls.requests[0].url.host == "metadata.google.internal"
    assert "data" not in calls.json(1)["message"]
    push_module._google_accounts.clear()


P8 = (
    ec.generate_private_key(ec.SECP256R1())
    .private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    .decode()
)
APNS = {"TEAM_ID": "TEAM1", "KEY_ID": "KEY1", "PRIVATE_KEY": P8, "TOPIC": "com.acme"}


async def test_apns_sends_with_a_provider_token(provider: Any) -> None:  # noqa: ANN401
    """ES256 token, kept; the alert; Apple's reasons for lost tokens are gone."""
    calls = provider(
        httpx2.Response(200),
        httpx2.Response(410, json={"reason": "Unregistered"}),
        httpx2.Response(400, json={"reason": "BadDeviceToken"}),
        httpx2.Response(400, json={"reason": "PayloadTooLarge"}),
        httpx2.Response(500),
    )
    backend = ApnsPushBackend(APNS)
    message = PUSH.prepared()

    await backend.send_messages([message])
    for _ in range(2):
        with pytest.raises(PushTokenGone):
            await backend.send_messages([message])
    with pytest.raises(NotificationFailed, match="PayloadTooLarge"):
        await backend.send_messages([message])
    with pytest.raises(NotificationFailed, match="500"):
        await backend.send_messages([PUSH])

    request = calls.requests[0]
    assert request.url.host == "api.push.apple.com"
    assert request.url.path == f"/3/device/{'a' * 64}"
    assert request.headers["apns-topic"] == "com.acme"
    assert request.headers["apns-id"] == str(message.message_id).upper()
    token = request.headers["authorization"].removeprefix("bearer ")
    assert jwt.get_unverified_header(token)["kid"] == "KEY1"
    assert calls.json(0) == {
        "aps": {"alert": {"title": "Shipped", "body": "On its way"}},
        "order": "7",
    }
    tokens = {r.headers["authorization"] for r in calls.requests}
    assert len(tokens) == 1
    assert "apns-id" not in calls.requests[-1].headers
    push_module._apns_tokens.clear()


async def test_apns_refuses_tokens_that_are_not_apples(provider: Any) -> None:  # noqa: ANN401
    """A token that is not hex never reaches a URL; the sandbox when asked."""
    calls = provider(httpx2.Response(200))
    backend = ApnsPushBackend({**APNS, "SANDBOX": True})

    with pytest.raises(PushTokenGone):
        await backend.send_messages([PushMessage(token="../x", title="t", body="b")])
    await backend.send_messages([PUSH])

    assert [r.url.host for r in calls.requests] == ["api.sandbox.push.apple.com"]
    push_module._apns_tokens.clear()


async def test_huawei_push_sends_with_the_apps_token(provider: Any) -> None:  # noqa: ANN401
    """The app's OAuth token, kept; data as a JSON string; codes read."""
    calls = provider(
        httpx2.Response(200, json={"access_token": "hw", "expires_in": 3600}),
        httpx2.Response(200, json={"code": "80000000"}),
        httpx2.Response(200, json={"code": "80300007"}),
        httpx2.Response(200, json={"code": "80100003"}),
        httpx2.Response(401, json={"access_token": "x"}),
    )
    backend = HuaweiPushBackend({"APP_ID": "1234", "APP_SECRET": "s"})

    await backend.send_messages([PUSH])
    with pytest.raises(PushTokenGone):
        await backend.send_messages([PUSH])
    with pytest.raises(NotificationFailed, match="80100003"):
        await backend.send_messages([PushMessage(token="t", title="t", body="")])

    assert calls.form(0)["grant_type"] == ["client_credentials"]
    assert calls.requests[1].url.path == "/v1/1234/messages:send"
    message = calls.json(1)["message"]
    assert (message["token"], json.loads(message["data"])) == (
        ["a" * 64],
        {"order": "7"},
    )
    assert "data" not in calls.json(3)["message"]
    push_module._huawei_apps.clear()
    with pytest.raises(NotificationFailed, match="could not be reached"):
        await HuaweiPushBackend({"APP_ID": "1234", "APP_SECRET": "s"}).send_messages(
            [PUSH]
        )
    push_module._huawei_apps.clear()


MAIL = EmailMessage(
    subject="Your receipt",
    body="Thanks",
    html="<p>Thanks</p>",
    to=("Ann <ann@example.com>",),
    cc=("bob@example.com",),
    bcc=("audit@example.com",),
    reply_to=("help@acme.example",),
    headers={"X-Order": "7"},
).prepared("Acme <hello@acme.example>")
PLAIN = EmailMessage(subject="Hi", body="Hello", to=("ann@example.com",)).prepared(
    "hello@acme.example"
)


async def test_ses_sends_the_raw_message_signed(provider: Any) -> None:  # noqa: ANN401
    """MIME without Bcc, every recipient in Destination; SES's error type."""
    calls = provider(
        httpx2.Response(200, json={"MessageId": "1"}),
        httpx2.Response(
            400,
            json={"message": "x"},
            headers={"x-amzn-ErrorType": "MessageRejected:http://x"},
        ),
        httpx2.Response(500, json={"__type": "InternalFailure"}),
    )
    backend = mail_providers.SesBackend(
        {
            "REGION": "eu-west-1",
            "ACCESS_KEY_ID": "AKID",
            "SECRET_ACCESS_KEY": "s",
            "CONFIGURATION_SET": "tracking",
        }
    )

    assert await backend.send_messages([MAIL]) == 1
    with pytest.raises(MailDeliveryFailed, match="MessageRejected"):
        await backend.send_messages([MAIL])
    with pytest.raises(MailDeliveryFailed, match="InternalFailure"):
        await backend.send_messages([MAIL])

    request = calls.requests[0]
    assert request.url.path == "/v2/email/outbound-emails"
    assert "/eu-west-1/ses/aws4_request" in request.headers["authorization"]
    body = calls.json(0)
    assert body["Destination"]["BccAddresses"] == ["audit@example.com"]
    assert body["ConfigurationSetName"] == "tracking"
    mime = message_from_bytes(base64.b64decode(body["Content"]["Raw"]["Data"]))
    assert mime["Subject"] == "Your receipt"
    assert mime["Bcc"] is None
    plain = mail_providers.SesBackend(
        {"REGION": "eu-west-1", "ACCESS_KEY_ID": "AKID", "SECRET_ACCESS_KEY": "s"}
    )
    calls.answers = [httpx2.Response(200, json={})]
    await plain.send_messages([PLAIN])
    assert "ConfigurationSetName" not in calls.json()


async def test_sendgrid_sends_the_parts(provider: Any) -> None:  # noqa: ANN401
    """Personalizations with Bcc; text and HTML; reply-to; 202; a refusal."""
    calls = provider(httpx2.Response(202), httpx2.Response(401), DOWN)
    backend = mail_providers.SendGridBackend({"API_KEY": "SG.key"})

    await backend.send_messages([MAIL])
    with pytest.raises(MailDeliveryFailed, match="401"):
        await backend.send_messages([PLAIN])
    with pytest.raises(MailDeliveryFailed, match="could not be reached"):
        await backend.send_messages([PLAIN])

    body = calls.json(0)
    assert body["personalizations"] == [
        {
            "to": [{"email": "ann@example.com", "name": "Ann"}],
            "cc": [{"email": "bob@example.com"}],
            "bcc": [{"email": "audit@example.com"}],
        }
    ]
    assert body["from"] == {"email": "hello@acme.example", "name": "Acme"}
    assert [c["type"] for c in body["content"]] == ["text/plain", "text/html"]
    assert body["reply_to_list"] == [{"email": "help@acme.example"}]
    assert body["headers"]["X-Order"] == "7"
    assert calls.requests[0].headers["authorization"] == "Bearer SG.key"
    assert [c["type"] for c in calls.json(1)["content"]] == ["text/plain"]


async def test_mailgun_sends_a_form(provider: Any) -> None:  # noqa: ANN401
    """Recipients, headers as h:, basic auth as api; a refusal."""
    calls = provider(httpx2.Response(200, json={"id": "<1>"}), httpx2.Response(400))
    backend = mail_providers.MailgunBackend(
        {"API_KEY": "key", "DOMAIN": "mg.acme.example"}
    )

    await backend.send_messages([MAIL])
    with pytest.raises(MailDeliveryFailed, match="400"):
        await backend.send_messages([PLAIN])

    request = calls.requests[0]
    assert request.url.path == "/v3/mg.acme.example/messages"
    assert (
        request.headers["authorization"]
        == "Basic " + base64.b64encode(b"api:key").decode()
    )
    form = calls.form(0)
    assert form["bcc"] == ["audit@example.com"]
    assert form["h:X-Order"] == ["7"]
    assert form["h:Reply-To"] == ["help@acme.example"]
    assert "html" not in calls.form(1)


async def test_postmark_sends_json(provider: Any) -> None:  # noqa: ANN401
    """The token header; the stream; ErrorCode 0, else its code."""
    calls = provider(
        httpx2.Response(200, json={"ErrorCode": 0}),
        httpx2.Response(422, json={"ErrorCode": 300}),
        httpx2.Response(500, text="down"),
    )
    backend = mail_providers.PostmarkBackend({"SERVER_TOKEN": "pm"})

    await backend.send_messages([MAIL])
    with pytest.raises(MailDeliveryFailed, match="error 300"):
        await backend.send_messages([PLAIN])
    with pytest.raises(MailDeliveryFailed, match="error 500"):
        await backend.send_messages([PLAIN])

    body = calls.json(0)
    assert (body["Bcc"], body["ReplyTo"], body["MessageStream"]) == (
        "audit@example.com",
        "help@acme.example",
        "outbound",
    )
    assert {"Name": "X-Order", "Value": "7"} in body["Headers"]
    assert calls.requests[0].headers["x-postmark-server-token"] == "pm"
    assert "HtmlBody" not in calls.json(1)
