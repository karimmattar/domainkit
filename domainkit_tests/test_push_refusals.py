"""How push services' refusals are read (user reports N1 and N2).

FCM answers ``400 INVALID_ARGUMENT`` both to a token that is not one of
its own and to a message it cannot take; its guidance is to delete the
token in the first case only. The answers here are those of FCM's
reference (error codes, managing tokens). A refusal that would come
again is not tried again, and the provider's code and the fields it
named are kept, never their values.
"""

from collections.abc import Sequence
from typing import Any, ClassVar

import httpx2
import pytest

from domainkit.contrib.messaging import Worker
from domainkit.contrib.messaging.registry import get_task
from domainkit.contrib.messaging.tables import DEAD
from domainkit.contrib.notifications import (
    NotificationFailed,
    NotificationRefused,
    PushBackend,
    PushMessage,
    send_push,
)
from domainkit.contrib.notifications.backends import PushTokenGone
from domainkit.contrib.notifications.delivery import PUSH_TASK, SMS_TASK
from domainkit.contrib.notifications.push import (
    ApnsPushBackend,
    FcmPushBackend,
    HuaweiPushBackend,
    Refusal,
)
from domainkit_tests.test_notifications import (  # noqa: F401 - fixtures.
    Install,
    empty_outboxes,
    immediate_retries,
    install,
    outbox_rows,
    tables,
)

pytestmark = pytest.mark.anyio

MESSAGE = PushMessage(token="a" * 64, title="Sign in", body="")
FCM = FcmPushBackend({"PROJECT_ID": "acme"})
APNS = ApnsPushBackend(
    {"TEAM_ID": "T", "KEY_ID": "K", "PRIVATE_KEY": "unused", "TOPIC": "com.acme"}
)
HUAWEI = HuaweiPushBackend({"APP_ID": "1", "APP_SECRET": "unused"})


def fcm(status: int, error: dict[str, Any]) -> Refusal:
    """FCM's reading of ``status`` with ``error``."""
    return FCM.refusal(httpx2.Response(status, json={"error": error}))


def test_fcm_reads_the_two_invalid_arguments_of_its_reference() -> None:
    """Its own error and no field: the token. A field named: the message."""
    token = fcm(
        400,
        {
            "code": 400,
            "message": "The registration token is not a valid FCM registration token",
            "status": "INVALID_ARGUMENT",
            "details": [
                {
                    "@type": "type.googleapis.com/google.firebase.fcm.v1.FcmError",
                    "errorCode": "INVALID_ARGUMENT",
                }
            ],
        },
    )
    payload = fcm(
        400,
        {
            "code": 400,
            "message": "Invalid value at 'message.data[0].value' (TYPE_STRING), 12",
            "status": "INVALID_ARGUMENT",
            "details": [
                {
                    "@type": "type.googleapis.com/google.rpc.BadRequest",
                    "fieldViolations": [
                        {
                            "field": "message.data[0].value",
                            "description": "Invalid value at 'message.data[0].value' "
                            "(TYPE_STRING), 12",
                        }
                    ],
                }
            ],
        },
    )

    assert token == Refusal("INVALID_ARGUMENT", gone=True)
    assert payload == Refusal(
        "INVALID_ARGUMENT", lasting=True, fields=("message.data[0].value",)
    )


@pytest.mark.parametrize(
    ("status", "error", "expected"),
    [
        (  # The token alone is named.
            400,
            {
                "status": "INVALID_ARGUMENT",
                "details": [{"fieldViolations": [{"field": "message.token"}]}],
            },
            Refusal("INVALID_ARGUMENT", gone=True, fields=("message.token",)),
        ),
        (
            404,
            {"status": "NOT_FOUND", "details": [{"errorCode": "UNREGISTERED"}]},
            Refusal("UNREGISTERED", gone=True),
        ),
        (  # A token of another sender: no use resending, nor forgetting it.
            403,
            {
                "status": "PERMISSION_DENIED",
                "details": [{"errorCode": "SENDER_ID_MISMATCH"}],
            },
            Refusal("SENDER_ID_MISMATCH", lasting=True),
        ),
        (  # No UNREGISTERED: a wrong PROJECT_ID may answer so; tried again.
            404,
            {"status": "NOT_FOUND"},
            Refusal("NOT_FOUND"),
        ),
        (  # Its own error is missing: no token gone, but a bad request.
            400,
            {"status": "INVALID_ARGUMENT"},
            Refusal("INVALID_ARGUMENT", lasting=True),
        ),
        (
            401,
            {
                "status": "UNAUTHENTICATED",
                "details": [{"errorCode": "THIRD_PARTY_AUTH_ERROR"}],
            },
            Refusal("THIRD_PARTY_AUTH_ERROR"),
        ),
        (
            429,
            {
                "status": "RESOURCE_EXHAUSTED",
                "details": [{"errorCode": "QUOTA_EXCEEDED"}],
            },
            Refusal("QUOTA_EXCEEDED"),
        ),
        (503, {"status": "UNAVAILABLE"}, Refusal("UNAVAILABLE")),
        (500, {}, Refusal("500")),
    ],
)
def test_fcm_reads_its_other_answers(
    status: int, error: dict[str, Any], expected: Refusal
) -> None:
    """Gone, lasting, or tried again, by FCM's own code."""
    assert fcm(status, error) == expected


@pytest.mark.parametrize(
    ("status", "reason", "expected"),
    [
        (410, "Unregistered", Refusal("Unregistered", gone=True)),
        (400, "BadDeviceToken", Refusal("BadDeviceToken", gone=True, lasting=True)),
        (400, "BadTopic", Refusal("BadTopic", lasting=True)),
        (413, "PayloadTooLarge", Refusal("PayloadTooLarge", lasting=True)),
        (403, "ExpiredProviderToken", Refusal("ExpiredProviderToken")),
        (429, "TooManyRequests", Refusal("TooManyRequests")),
        (503, None, Refusal("503")),
    ],
)
def test_apns_reads_apples_reasons(
    status: int, reason: str | None, expected: Refusal
) -> None:
    """A lost token is gone; a request Apple will refuse again is lasting."""
    body = {} if reason is None else {"reason": reason}
    assert APNS.refusal(httpx2.Response(status, json=body)) == expected


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        ("80300007", Refusal("80300007", gone=True)),
        ("80100003", Refusal("80100003", lasting=True)),
        ("80300008", Refusal("80300008", lasting=True)),
        ("80200003", Refusal("80200003")),
    ],
)
def test_push_kit_reads_its_codes(code: str, expected: Refusal) -> None:
    """Tokens it does not know are gone; a message it cannot take is lasting."""
    assert HUAWEI.refusal(httpx2.Response(200, json={"code": code})) == expected


def test_a_refusal_becomes_what_the_worker_needs() -> None:
    """Gone, lasting, or tried again; the code and fields kept, no value."""
    gone = FCM.failure(MESSAGE, Refusal("UNREGISTERED", gone=True))
    lasting = FCM.failure(
        MESSAGE,
        Refusal("INVALID_ARGUMENT", lasting=True, fields=("message.data[0].value",)),
    )
    again = FCM.failure(MESSAGE, Refusal("UNAVAILABLE"))

    assert isinstance(gone, PushTokenGone)
    assert gone.token == MESSAGE.token
    assert type(lasting) is NotificationRefused
    assert str(lasting) == (
        "FCM refused the notification: INVALID_ARGUMENT (message.data[0].value)."
    )
    assert (lasting.code, lasting.fields) == (
        "INVALID_ARGUMENT",
        ("message.data[0].value",),
    )
    assert type(again) is NotificationFailed
    assert (str(again), again.code, again.fields) == (
        "FCM refused the notification: UNAVAILABLE.",
        "UNAVAILABLE",
        (),
    )


class LenientFcm(FcmPushBackend):
    """A project's reading of one case, the rest left to FCM's."""

    def refusal(self, response: httpx2.Response) -> Refusal:
        """A token of another sender is one this project forgets."""
        read = super().refusal(response)
        if read.code == "SENDER_ID_MISMATCH":
            return Refusal(read.code, gone=True)
        return read


def test_a_subclass_reads_one_case_its_own_way() -> None:
    """``refusal`` is the seam: ``send`` and its request stay FCM's."""
    answer = httpx2.Response(
        403,
        json={
            "error": {
                "status": "PERMISSION_DENIED",
                "details": [{"errorCode": "SENDER_ID_MISMATCH"}],
            }
        },
    )
    lenient = LenientFcm({"PROJECT_ID": "acme"})

    assert isinstance(lenient.failure(MESSAGE, lenient.refusal(answer)), PushTokenGone)
    assert lenient.refusal(httpx2.Response(503, json={})) == Refusal("503")


class RefusingService(PushBackend):
    """A push service that refuses every message for good."""

    calls: ClassVar[int] = 0

    async def send_messages(self, messages: Sequence[PushMessage]) -> int:
        """Refuse, as a payload the service cannot take."""
        RefusingService.calls += 1
        raise NotificationRefused(
            "Push refused: INVALID_ARGUMENT.", code="INVALID_ARGUMENT"
        )


async def test_a_lasting_refusal_is_dead_lettered_at_once(tables: Install) -> None:  # noqa: F811
    """One call, not ``MAX_ATTEMPTS``; a refusal that may pass is tried again."""
    tables(PUSH_BACKEND="domainkit_tests.test_push_refusals.RefusingService")
    RefusingService.calls = 0
    await send_push("device-token", "Sign in", "")

    await Worker().run_once()

    (row,) = await outbox_rows()
    assert (row["status"], row["attempts"], RefusingService.calls) == (DEAD, 1, 1)
    assert row["last_error"] == "NotificationRefused: Push refused: INVALID_ARGUMENT."
    for name in (PUSH_TASK, SMS_TASK):
        spec = get_task(name)
        assert spec is not None
        assert spec.dont_retry_on == (NotificationRefused,)
