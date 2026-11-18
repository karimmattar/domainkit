"""The other side: a service that receives the webhooks, and trusts only signed ones.

It runs elsewhere, on any framework; this one is a FastAPI application with
domainkit's ``verify`` (the Standard Webhooks libraries verify the same
signatures). It refuses a delivery whose signature does not hold, or whose
time is more than five minutes off (a replay), and keeps each delivery
once: the sender delivers at least once, under the same ``webhook-id``
however often it tries.
"""

import json
from typing import Any

from domainkit.contrib.webhooks import InvalidSignature, verify
from fastapi import FastAPI, Request, Response, status


class Receiver:
    """A receiving endpoint, at ``POST /webhooks`` of ``app``.

    ``secret`` is the subscription's, shown once when it was made; keep it
    as any secret is kept. ``events`` holds each delivery by its id: a real
    receiver keeps them in its database, the id unique, and acts on each
    the first time only.
    """

    def __init__(self, secret: str) -> None:
        """An endpoint that verifies with ``secret``.

        Raises:
            ValueError: ``secret`` is empty. It would verify no delivery:
                ``verify`` refuses it, and every delivery would fail.
        """
        if not secret:
            raise ValueError("A receiver needs its subscription's secret.")
        self.secret = secret
        self.events: dict[str, Any] = {}
        self.app = FastAPI()
        self.app.add_api_route("/webhooks", self.receive, methods=["POST"])

    async def receive(self, request: Request) -> Response:
        """Take a delivery: 204 if it is signed, 400 if it is not."""
        body = await request.body()
        try:
            delivery_id = verify(self.secret, request.headers, body)
        except InvalidSignature:
            return Response(status_code=status.HTTP_400_BAD_REQUEST)
        self.events.setdefault(delivery_id, json.loads(body))
        return Response(status_code=status.HTTP_204_NO_CONTENT)
