"""``approvals``: list and show approval requests, and expire those whose time is up."""

import json
import uuid

from sqlalchemy import select

from domainkit.contrib.approvals.models import RequestStatus
from domainkit.contrib.approvals.services import expire_requests
from domainkit.contrib.approvals.tables import decisions, requests
from domainkit.contrib.approvals.uow import approvals_alias
from domainkit.db import connections
from domainkit.management import ActionCommand, CommandError, action, option


class Command(ActionCommand):
    """List requests, show one with its decisions, or expire the overdue."""

    help = (
        "List approval requests (pending by default), show one with its "
        "command and decisions, or expire those whose time is up."
    )

    @action(
        name="list",
        help="list the requests in a status, oldest first",
        options=[
            option(
                "--status",
                choices=[status.value for status in RequestStatus],
                default=RequestStatus.PENDING.value,
                help="the requests in this status (default: pending)",
            ),
            option(
                "--limit", type=int, default=50, help="from 1 to 1000 (default: 50)"
            ),
        ],
    )
    async def listed(self, *, status: str, limit: int) -> None:
        """One line per request."""
        await self._list(RequestStatus(status), limit)

    @action(
        help="show a request, its command, and its decisions",
        options=[option("request", nargs="?", help="the request's id")],
    )
    async def show(self, *, request: str | None) -> None:
        """The request, its command, and its decisions."""
        await self._show(_id(request))

    @action(help="expire the requests whose time is up", operator=True)
    async def expire(self) -> None:
        """Expire the overdue requests, and say how many."""
        async with self.recorded("approvals.expire") as recorded:
            recorded["requests"] = expired = await expire_requests()
        self.write(f"Expired {expired} request(s).")

    async def _list(self, status: RequestStatus, limit: int) -> None:
        """One line per request in ``status``, oldest first."""
        if not 1 <= limit <= 1000:
            raise CommandError("--limit is from 1 to 1000.")
        async with connections.engine(approvals_alias()).connect() as connection:
            found = await connection.execute(
                select(requests)
                .where(requests.c.status == status)
                .order_by(requests.c.submitted_at, requests.c.id)
                .limit(limit)
            )
            for row in found:
                self.write(
                    f"{row.id}  {row.action}  {row.policy}  maker {_shown(row.maker)}  "
                    f"expires {row.expires_at.isoformat()}"
                )

    async def _show(self, request_id: uuid.UUID) -> None:
        """The request, its command, and its decisions."""
        async with connections.engine(approvals_alias()).connect() as connection:
            row = (
                await connection.execute(
                    select(requests).where(requests.c.id == request_id)
                )
            ).first()
            if row is None:
                raise CommandError(f"No request is {request_id}.")
            taken = await connection.execute(
                select(decisions)
                .where(decisions.c.request_id == request_id)
                .order_by(decisions.c.decided_at)
            )
            self.write(f"{row.id}  {row.action}  {row.status.value}")
            self.write(
                f"maker {_shown(row.maker)}, policy {row.policy}, needs {row.required}"
            )
            self.write(f"fingerprint {row.fingerprint}")
            self.write(json.dumps(row.arguments, sort_keys=True))
            for each in taken:
                self.write(
                    f"{each.verdict.value}  {_shown(each.approver)}  "
                    f"{each.decided_at.isoformat()}"
                )
            if row.reason:
                self.write(f"reason: {_shown(row.reason)}")


def _id(text: str | None) -> uuid.UUID:
    """``text`` as a request's id.

    Raises:
        CommandError: It is missing, or not one.
    """
    try:
        return uuid.UUID(text or "")
    except ValueError:
        raise CommandError("show needs a request's id.") from None


def _shown(text: str) -> str:
    """``text`` with what a terminal would act on (control characters) escaped."""
    return "".join(c if c.isprintable() else f"\\u{ord(c):04x}" for c in text)
