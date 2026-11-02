"""``webhooks``: list subscriptions and deliveries, and replay deliveries."""

import uuid
from typing import Any

import httpx2
from sqlalchemy import select

from domainkit.contrib.webhooks.delivery import (
    failed_deliveries,
    webhooks_alias,
)
from domainkit.contrib.webhooks.delivery import (
    replay as replay_deliveries,
)
from domainkit.contrib.webhooks.tables import (
    DELIVERY_STATUSES,
    deliveries,
    subscriptions,
)
from domainkit.db import connections
from domainkit.management import ActionCommand, CommandError, action, option

MAX_LIMIT = 1000


class Command(ActionCommand):
    """Inspect webhook subscriptions and deliveries, and send deliveries again."""

    help = "List webhook subscriptions and deliveries, or replay deliveries."
    common_options = (
        option("--limit", type=int, default=20, help="at most this many (default: 20)"),
    )

    @action(help="list the subscriptions, without their URLs' paths or secrets")
    async def subscriptions(self, *, limit: int) -> None:
        """One line per subscription."""
        await self._subscriptions(_checked(limit))

    @action(
        help="list the newest deliveries",
        options=[
            option("--status", choices=DELIVERY_STATUSES),
            option("--subscription", metavar="ID", help="only this subscription's"),
        ],
    )
    async def deliveries(
        self, *, status: str | None, subscription: str | None, limit: int
    ) -> None:
        """One line per delivery."""
        owner = None if subscription is None else _id(subscription)
        await self._deliveries(_checked(limit), status, owner)

    @action(
        help="send deliveries again: those named, or the most recent failed ones",
        options=[
            option("ids", nargs="*", metavar="DELIVERY", help="the deliveries"),
            option(
                "--failed",
                action="store_true",
                help="the most recent failed deliveries (up to --limit)",
            ),
            option("--subscription", metavar="ID", help="only this subscription's"),
        ],
        operator=True,
    )
    async def replay(
        self, *, ids: list[str], failed: bool, subscription: str | None, limit: int
    ) -> None:
        """Queue deliveries again."""
        owner = None if subscription is None else _id(subscription)
        await self._replay([_id(text) for text in ids], failed, _checked(limit), owner)

    async def _subscriptions(self, limit: int) -> None:
        """One line per subscription, without its URL's path or its secret."""
        statement = (
            select(
                subscriptions.c.id,
                subscriptions.c.url,
                subscriptions.c.active,
                subscriptions.c.event_types,
            )
            .order_by(subscriptions.c.created_at)
            .limit(limit)
        )
        for row in await _rows(statement):
            state = "active" if row.active else "inactive"
            host = httpx2.URL(row.url).host  # Paths and queries may hold tokens.
            self.write(f"{row.id}  {state:8}  {host}  {','.join(row.event_types)}")

    async def _deliveries(
        self, limit: int, status: str | None, owner: uuid.UUID | None
    ) -> None:
        """The newest deliveries, one line each."""
        statement = (
            select(deliveries).order_by(deliveries.c.created_at.desc()).limit(limit)
        )
        if status is not None:
            statement = statement.where(deliveries.c.status == status)
        if owner is not None:
            statement = statement.where(deliveries.c.subscription_id == owner)
        for row in await _rows(statement):
            code = row.last_status_code if row.last_status_code is not None else "-"
            self.write(
                f"{row.id}  {row.status:9}  {row.event_type}  x{row.attempts}  "
                f"{code}  {(row.last_error or '')[:100]}"
            )

    async def _replay(
        self,
        ids: list[uuid.UUID],
        failed: bool,
        limit: int,
        owner: uuid.UUID | None,
    ) -> None:
        """Queue deliveries again."""
        if failed == bool(ids):
            raise CommandError("replay takes delivery ids or --failed, not both.")
        if failed:
            ids = await failed_deliveries(limit, owner)
        async with self.recorded("webhooks.replay", asked=len(ids)) as action:
            replayed = await replay_deliveries(ids)
            action["deliveries"] = len(replayed)
        self.write(f"Queued {len(replayed)} delivery(ies) again.")


def _id(text: str) -> uuid.UUID:
    """Parse an id given on the command line."""
    try:
        return uuid.UUID(text.removeprefix("msg_"))
    except ValueError:
        raise CommandError(f"{text!r} is not an id.") from None


async def _rows(statement: Any) -> list[Any]:  # noqa: ANN401 - a select.
    """Run ``statement`` on the webhooks database."""
    async with connections.engine(webhooks_alias()).connect() as connection:
        return list((await connection.execute(statement)).all())


def _checked(limit: int) -> int:
    """``limit``, if from 1 to ``MAX_LIMIT``.

    Raises:
        CommandError: It is not.
    """
    if not 1 <= limit <= MAX_LIMIT:
        raise CommandError(f"--limit must be from 1 to {MAX_LIMIT}.")
    return limit
