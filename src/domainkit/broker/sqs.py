"""Amazon SQS (``sqs://<region>``) as a work queue, through its JSON API.

Each queue is an SQS queue, ``<PREFIX>-<queue>``, made by ``declare``
(its visibility timeout ``REDELIVER_AFTER``) or found by name. A message
taken (``ReceiveMessage``, waiting up to a second) is hidden from other
consumers until it is answered: deleted (``DeleteMessage``), or made
visible again at once (``ChangeMessageVisibility`` to 0) to requeue it;
unanswered within ``REDELIVER_AFTER``, it is handed out again. Bodies are
sent as base64, the message id as an attribute.

Calls go through ``domainkit.http.client`` (the ``cloud`` extra), signed
with SigV4: credentials from the URL (``sqs://KEY:SECRET@eu-west-1``)
or the environment (variables, a web identity, a container or instance
role). ``?endpoint=https://...`` names another endpoint (a VPC
endpoint, or ElasticMQ in development).
"""

import base64
import binascii
import json
from collections.abc import Iterable, Mapping, Sequence
from typing import Any, Final
from urllib.parse import parse_qs, unquote, urlsplit

from domainkit.broker.base import BrokerRefused, BrokerUnavailable
from domainkit.broker.pull import Fetched, PullBroker, Puller
from domainkit.conf import BrokerConfig

__all__ = ["SqsBroker"]

TARGET: Final = "AmazonSQS"
CONTENT_TYPE: Final = "application/x-amz-json-1.0"
RETENTION: Final = "1209600"
"""Seconds SQS keeps a message: 14 days, its most."""


class SqsBroker(PullBroker):
    """Work queues on Amazon SQS (see the module)."""

    def __init__(self, config: BrokerConfig) -> None:
        """Call the region's endpoint, or the URL's ``endpoint``."""
        super().__init__()
        from domainkit.cloud.aws import credential_source
        from domainkit.http.client import HttpClient

        self.config = config
        self.publish_timeout = config.PUBLISH_TIMEOUT
        parts = urlsplit(config.URL.get_secret_value())
        self.region = parts.hostname or ""
        endpoint = parse_qs(parts.query).get("endpoint", [""])[0]
        self.endpoint = (endpoint or f"https://sqs.{self.region}.amazonaws.com").rstrip(
            "/"
        )
        self.credentials = credential_source(
            self.region,
            unquote(parts.username) if parts.username else None,
            unquote(parts.password) if parts.password else None,
        )
        self.client = HttpClient(timeout=config.PUBLISH_TIMEOUT + 2, propagate=False)
        self.urls: dict[str, str] = {}

    def name(self, queue: str) -> str:
        """The SQS queue of ``queue``: SQS names take no dots."""
        return f"{self.config.PREFIX}-{queue}".replace(".", "_")

    async def call(self, action: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        """One signed call; its answer.

        Raises:
            BrokerRefused: SQS refused it (its error type is named).
            BrokerUnavailable: SQS could not be reached, or failed.
        """
        import httpx2

        from domainkit.cloud.aws import signed_request

        try:
            response = await signed_request(
                self.client,
                "POST",
                f"{self.endpoint}/",
                body=json.dumps(dict(payload)).encode(),
                headers={
                    "Content-Type": CONTENT_TYPE,
                    "X-Amz-Target": f"{TARGET}.{action}",
                },
                region=self.region,
                service="sqs",
                credentials=self.credentials,
            )
        except httpx2.HTTPError as exc:
            raise BrokerUnavailable(f"SQS could not be reached ({action}).") from exc
        try:
            answer = response.json() if response.content else {}
        except ValueError:
            answer = None
        if not isinstance(answer, dict):
            raise BrokerUnavailable(f"SQS answered {action} without JSON.")
        if response.status_code == 200:
            return answer
        kind = str(answer.get("__type", "")).rpartition("#")[2][:64] or "an error"
        if response.status_code >= 500:
            raise BrokerUnavailable(f"SQS failed {action}: {kind}.")
        raise BrokerRefused(f"SQS refused {action}: {kind}.")

    async def url(self, queue: str) -> str:
        """The queue's URL, made (or found) on first use."""
        if queue not in self.urls:
            answer = await self.call(
                "CreateQueue",
                {
                    "QueueName": self.name(queue),
                    "Attributes": {
                        "VisibilityTimeout": str(int(self.config.REDELIVER_AFTER)),
                        "MessageRetentionPeriod": RETENTION,
                    },
                },
            )
            url = answer.get("QueueUrl")
            if not isinstance(url, str):
                raise BrokerUnavailable("SQS answered CreateQueue without a URL.")
            self.urls[queue] = url
        return self.urls[queue]

    async def declare(self, queues: Iterable[str]) -> None:
        """Make the queues (``CreateQueue`` finds one that exists)."""
        for queue in queues:
            await self.url(queue)

    async def publish(self, queue: str, body: bytes, *, message_id: str) -> None:
        """``SendMessage``: the body in base64, the id as an attribute."""
        await self.call(
            "SendMessage",
            {
                "QueueUrl": await self.url(queue),
                "MessageBody": base64.b64encode(body).decode(),
                "MessageAttributes": {
                    "message_id": {"DataType": "String", "StringValue": message_id}
                },
            },
        )

    async def puller(self, queues: Sequence[str]) -> Puller:
        """A puller of ``queues``, in turn."""
        return _SqsPuller(self, [(queue, await self.url(queue)) for queue in queues])

    async def check(self) -> None:
        """``ListQueues`` of the prefix: SQS answers, the credentials hold."""
        await self.call(
            "ListQueues", {"QueueNamePrefix": str(self.config.PREFIX), "MaxResults": 1}
        )


class _SqsPuller(Puller):
    """Receives from each queue in turn."""

    def __init__(self, broker: SqsBroker, queues: list[tuple[str, str]]) -> None:
        """Ask ``queues``, (queue, URL) pairs, of ``broker``."""
        self.broker = broker
        self.queues = queues
        self.turn = 0

    async def fetch(self, limit: int) -> Sequence[Fetched]:
        """The next queue's messages, waiting a second for one."""
        for _ in range(len(self.queues)):
            queue, url = self.queues[self.turn]
            self.turn = (self.turn + 1) % len(self.queues)
            answer = await self.broker.call(
                "ReceiveMessage",
                {
                    "QueueUrl": url,
                    "MaxNumberOfMessages": min(limit, 10),
                    "WaitTimeSeconds": 1,
                    "MessageAttributeNames": ["message_id"],
                },
            )
            if found := [
                self._fetched(queue, url, message)
                for message in answer.get("Messages") or []
            ]:
                return found
        return []

    def _fetched(self, queue: str, url: str, message: Mapping[str, Any]) -> Fetched:
        """An SQS message as a message: its body decoded, its id read."""
        try:
            body = base64.b64decode(str(message.get("Body", "")), validate=True)
        except (binascii.Error, ValueError):
            body = str(message.get("Body", "")).encode()
        attributes = message.get("MessageAttributes") or {}
        message_id = (attributes.get("message_id") or {}).get("StringValue", "")
        return Fetched(
            queue, body, str(message_id), (url, message.get("ReceiptHandle"))
        )

    async def ack(self, message: Fetched) -> None:
        """``DeleteMessage``."""
        url, receipt = message.handle
        await self.broker.call(
            "DeleteMessage", {"QueueUrl": url, "ReceiptHandle": receipt}
        )

    async def requeue(self, message: Fetched) -> None:
        """Visible again at once."""
        url, receipt = message.handle
        await self.broker.call(
            "ChangeMessageVisibility",
            {"QueueUrl": url, "ReceiptHandle": receipt, "VisibilityTimeout": 0},
        )
