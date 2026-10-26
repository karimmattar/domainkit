"""Erasure reaches the copies stores keep: deliveries, kept responses, the outbox.

The proof of DATA-02: one person's data goes into each store the way an
application puts it there (a keyed request that commits an event exposed
to webhooks, a durable handler, and a deferred task), then ``privacy
erase`` runs, and no read path finds it any more.
"""

import asyncio
import io
import json
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any, Final
from uuid import UUID

import pytest
from fastapi import APIRouter
from fastapi.testclient import TestClient
from pydantic import BaseModel
from sqlalchemy import insert, select
from sqlalchemy.ext.asyncio import AsyncSession

from domainkit import clock, setup
from domainkit.apps import DomainConfig, registry
from domainkit.conf import override_settings
from domainkit.contrib.audit.tables import audit_log
from domainkit.contrib.idempotency.tables import idempotency_keys
from domainkit.contrib.messaging import task
from domainkit.contrib.messaging.admin import retry_dead
from domainkit.contrib.messaging.settings import MessagingSettings
from domainkit.contrib.messaging.tables import BLANKED, DEAD, PENDING, messages
from domainkit.contrib.messaging.testing import drain
from domainkit.contrib.privacy import erase_subject, export_subject
from domainkit.contrib.webhooks import delivery as delivery_module
from domainkit.contrib.webhooks import expose, queue_deliveries
from domainkit.contrib.webhooks.admin import read_delivery
from domainkit.contrib.webhooks.delivery import replay
from domainkit.contrib.webhooks.models import Subscription
from domainkit.contrib.webhooks.tables import CANCELLED, FAILED, deliveries
from domainkit.contrib.webhooks.uow import WebhooksUnitOfWork
from domainkit.db import SqlAlchemyRepository, SqlAlchemyUnitOfWork, connections
from domainkit.ddd import DomainEvent, Personal, event_bus
from domainkit.exceptions import ImproperlyConfigured
from domainkit.http import get_asgi_application
from domainkit.management import call_command
from domainkit.management.base import run_async
from domainkit_tests.conftest import ALPHA
from domainkit_tests.sample_domains.patients.models import Patient

AUDIT: Final = "domainkit.contrib.audit"
PRIVACY: Final = "domainkit.contrib.privacy"
MESSAGING: Final = "domainkit.contrib.messaging"
IDEMPOTENCY: Final = "domainkit.contrib.idempotency"
WEBHOOKS: Final = "domainkit.contrib.webhooks"
PATIENTS: Final = "domainkit_tests.sample_domains.patients"
EMAIL: Final = "ann@example.com"
OTHER_EMAIL: Final = "bob@example.com"
KEY: Final = "register-ann"


class PatientRegistered(DomainEvent):
    """A patient registered: the e-mail address is theirs."""

    patient_id: UUID
    email: Annotated[str, Personal(of="patient_id")]


class Patients(SqlAlchemyRepository[Patient, UUID]):
    """Patients, to register them."""

    model = Patient


class PatientsUnitOfWork(SqlAlchemyUnitOfWork):
    """A unit of work over patients."""

    patients: Patients

    def create_repositories(self, session: AsyncSession) -> None:
        """Bind the repository."""
        self.patients = Patients(session)


@task(name="tests.privacy.welcome", max_attempts=1)
async def welcome(
    patient_id: str, email: Annotated[str, Personal(of="patient_id")]
) -> None:
    """Send a welcome mail: it fails, so its message dies holding the address."""
    raise RuntimeError("The mail server is down.")


async def remember(event: PatientRegistered) -> None:
    """A durable handler that fails, so its message dies holding the event."""
    raise RuntimeError("The search index is down.")


class PatientIn(BaseModel):
    """A new patient."""

    name: str
    email: str


router = APIRouter()


@router.post("/patients", status_code=201)
async def register(body: PatientIn) -> dict[str, str]:
    """Register a patient, welcome them, and say who they are."""
    patient = Patient(
        id=uuid.uuid4(), name=body.name, email=body.email, registered_at=clock.now()
    )
    patient.record(PatientRegistered(patient_id=patient.id, email=body.email))
    async with PatientsUnitOfWork() as uow:
        await uow.patients.add(patient)
        uow.defer(welcome, patient_id=str(patient.id), email=body.email)
        await uow.commit()
    return {"id": str(patient.id), "email": body.email}


@dataclass
class Scenario:
    """Ann registered through a keyed request; her copies are in every store."""

    client: TestClient
    subject: str
    response: dict[str, str]
    delivery: UUID
    """Her failed delivery to the subscription."""


def migrate(tmp_path: Path) -> None:
    """Create every installed app's tables."""
    call_command("makemigrations", "-m", "stores", stdout=io.StringIO())
    call_command("migrate", stdout=io.StringIO())


async def subscribe() -> None:
    """Subscribe a receiver that refuses connections to every event."""
    subscription, _ = Subscription.create("http://127.0.0.1:9/hook", "127.0.0.1", "*")
    async with WebhooksUnitOfWork() as uow:
        await uow.subscriptions.add(subscription)
        await uow.commit()


async def rows(table: Any, *where: Any) -> list[Any]:  # noqa: ANN401 - a table.
    """The rows of ``table`` matching ``where``."""
    async with connections.engine().connect() as connection:
        return list((await connection.execute(select(table).where(*where))).all())


@pytest.fixture
def scenario(database_url: str, tmp_path: Path) -> Iterator[Scenario]:
    """Every store holds Ann's address: a delivery, a response, two dead messages."""
    registry.clear()
    with override_settings(
        INSTALLED_DOMAINS=[
            AUDIT,
            PRIVACY,
            MESSAGING,
            IDEMPOTENCY,
            WEBHOOKS,
            PATIENTS,
            ALPHA,
        ],
        DATABASES={"default": {"URL": database_url}},
        MIGRATIONS_DIR=tmp_path / "migrations",
        ROOT_ROUTER=f"{__name__}.router",
        MIDDLEWARE=[
            "domainkit.http.middleware.RequestContextMiddleware",
            "domainkit.http.middleware.RequestBodyLimitMiddleware",
            "domainkit.contrib.idempotency.middleware.IdempotencyMiddleware",
        ],
        MESSAGING={"MAX_ATTEMPTS": 1, "RETRY_BASE_SECONDS": 0.001},
        WEBHOOKS={
            "ALLOWED_PRIVATE_NETWORKS": ["127.0.0.0/8"],
            "MAX_ATTEMPTS": 1,
            "TIMEOUT": 2,
            "CONNECT_TIMEOUT": 1,
        },
        DEBUG=True,
    ):
        setup()
        migrate(tmp_path)
        expose(PatientRegistered, "patient.registered")
        event_bus.subscribe(PatientRegistered, remember, durable=True)
        run_async(subscribe())
        with TestClient(get_asgi_application()) as client:
            response = client.post(
                "/patients",
                json={"name": "Ann", "email": EMAIL},
                headers={"Idempotency-Key": KEY},
            ).json()

            async def run() -> UUID:
                await drain(delayed=True)
                (delivery,) = await rows(deliveries)
                assert (delivery.status, json.loads(delivery.body)["data"]) == (
                    FAILED,
                    {
                        "patient_id": response["id"],
                        "email": EMAIL,
                        "event_id": json.loads(delivery.body)["data"]["event_id"],
                        "occurred_at": json.loads(delivery.body)["data"]["occurred_at"],
                    },
                )
                dead = await rows(messages, messages.c.status == DEAD)
                assert len(dead) == 2
                assert all(EMAIL in json.dumps(row.payload) for row in dead)
                return UUID(str(delivery.id))

            delivery = run_async(run())
            yield Scenario(client, response["id"], response, delivery)
        delivery_module._exposed.clear()
        asyncio.run(connections.close_all())
    registry.clear()


def retry(scenario: Scenario) -> Any:  # noqa: ANN401 - a response.
    """Send Ann's registration again, with the same key."""
    return scenario.client.post(
        "/patients",
        json={"name": "Ann", "email": EMAIL},
        headers={"Idempotency-Key": KEY},
    )


def test_a_kept_response_is_replayed_until_erased(scenario: Scenario) -> None:
    """The stored response named Ann's address; after erasure, a retry gets 409.

    The key stays, so the request still never runs twice.
    """
    replayed = retry(scenario)
    assert replayed.json() == scenario.response

    run_async(erase_subject(scenario.subject))
    again = retry(scenario)
    (key,) = run_async(rows(idempotency_keys))

    assert again.status_code == 409
    assert again.json()["code"] == "idempotency_response_unavailable"
    assert EMAIL not in again.text
    assert (key.body, key.headers, key.status_code) == (None, None, 201)


def test_deliveries_about_an_erased_subject_are_blanked(scenario: Scenario) -> None:
    """Its body is gone from the row and the staff API, and replay sends nothing."""
    run_async(erase_subject(scenario.subject))

    async def read() -> tuple[Any, Any, list[UUID]]:
        (delivery,) = await rows(deliveries)
        shown = await read_delivery(scenario.delivery, actor=None)  # type: ignore[arg-type]
        return delivery, shown, await replay([scenario.delivery])

    delivery, shown, replayed = run_async(read())

    assert (delivery.body, delivery.status) == ("", FAILED)
    assert shown.payload is None
    assert delivery._mapping["subjects"] is None
    assert replayed == []


def test_pending_deliveries_about_an_erased_subject_are_cancelled(
    scenario: Scenario,
) -> None:
    """A delivery still to be sent is cancelled; another person's is left."""

    async def queue() -> None:
        now = clock.now()
        await queue_deliveries(
            "patient.registered",
            {"email": EMAIL},
            key="second",
            occurred_at=now,
            subjects=[scenario.subject],
        )
        await queue_deliveries(
            "patient.registered",
            {"email": OTHER_EMAIL},
            key="third",
            occurred_at=now,
            subjects=["someone-else"],
        )

    run_async(queue())
    run_async(erase_subject(scenario.subject))
    found = run_async(rows(deliveries, deliveries.c.status != FAILED))

    cancelled = next(row for row in found if row.status == CANCELLED)
    pending = next(row for row in found if row.status == "pending")

    assert (len(found), cancelled.body) == (2, "")
    assert json.loads(pending.body)["data"] == {"email": OTHER_EMAIL}
    assert run_async(delivery_module.erase_subject(scenario.subject)) == {
        "deliveries": 0,
        "cancelled": 0,
        "subscriptions": 0,
    }


def test_dead_messages_about_an_erased_subject_are_blanked(scenario: Scenario) -> None:
    """The handler's and the task's arguments are gone, and they are never retried."""
    run_async(erase_subject(scenario.subject))
    dead = run_async(rows(messages, messages.c.status == DEAD))
    retried = run_async(retry_dead(registry.settings(MessagingSettings), None))

    assert [row.payload for row in dead] == [{}, {}]
    assert retried == 0
    assert [row.headers[BLANKED] for row in dead] == [True, True]


def test_pending_messages_are_left_to_run_and_counted(
    scenario: Scenario, caplog: pytest.LogCaptureFixture
) -> None:
    """A queued task about the subject runs as planned; the erasure warns of it."""

    async def queue_and_erase() -> dict[str, int]:
        async with PatientsUnitOfWork() as uow:
            uow.defer(welcome, patient_id=scenario.subject, email=EMAIL)
            await uow.commit()
        await erase_subject(scenario.subject)
        (pending,) = await rows(messages, messages.c.status == PENDING)
        assert pending.payload["email"] == EMAIL
        from domainkit.contrib.messaging.erasure import erase_subject as erase_queued

        return await erase_queued(scenario.subject)

    assert run_async(queue_and_erase()) == {"messages": 0, "pending": 1}
    assert "messaging keeps 1 copy of a subject's data still to finish" in caplog.text


def test_an_export_lists_every_copy_until_erased(scenario: Scenario) -> None:
    """Each store's copies are in the export, under its label; none after erasure."""
    before = run_async(export_subject(scenario.subject))["copies"]
    run_async(erase_subject(scenario.subject))
    after = run_async(export_subject(scenario.subject))["copies"]

    assert sorted(before) == ["idempotency", "messaging", "webhooks"]
    assert [each["body"]["data"]["email"] for each in before["webhooks"]["deliveries"]]
    assert EMAIL in before["idempotency"]["responses"][0]["body"]
    assert [each["arguments"]["email"] for each in before["messaging"]["messages"]] == [
        EMAIL,
        EMAIL,
    ]
    assert after == {}


def test_erasures_of_copies_are_audited_without_the_data(scenario: Scenario) -> None:
    """One row per domain that erased something, with counts only."""
    run_async(erase_subject(scenario.subject, reason="ticket 812"))
    again = run_async(erase_subject(scenario.subject, reason="ticket 813"))
    audited = run_async(rows(audit_log, audit_log.c.action == "privacy.copies_erased"))

    assert again == 0
    assert sorted(
        (row.target, row.details["domain"], json.dumps(row.details["copies"]))
        for row in audited
    ) == [
        (scenario.subject, "idempotency", '{"responses": 1}'),
        (scenario.subject, "messaging", '{"messages": 2, "pending": 0}'),
        (
            scenario.subject,
            "webhooks",
            '{"deliveries": 1, "cancelled": 0, "subscriptions": 0}',
        ),
    ]
    assert all(row.details["reason"] == "ticket 812" for row in audited)
    assert not any(EMAIL in json.dumps(row.details) for row in audited)


class Talkative(DomainConfig):
    """A domain whose erasure hook answers with the data it erased."""

    async def erase_subject(self, subject: str, reason: str) -> dict[str, Any]:
        """Wrongly return the address."""
        return {"email": EMAIL}


@pytest.mark.parametrize(
    "answer",
    [{"email": EMAIL}, {"Deliveries": 1}, {"deliveries": -1}, {"deliveries": True}],
)
def test_hooks_answer_counts_by_kind_or_are_refused(answer: dict[str, Any]) -> None:
    """Anything but lower-case kinds and whole numbers could be personal data."""
    from domainkit.contrib.privacy.operations import _counts

    domain = Talkative("talkative", __import__("domainkit_tests"))

    with pytest.raises(ImproperlyConfigured, match="must return counts by kind"):
        _counts(domain, answer)
    assert _counts(domain, {"deliveries": 2, "pending": 0}) == {
        "deliveries": 2,
        "pending": 0,
    }


def test_domains_keep_no_copies_by_default() -> None:
    """The base hooks erase and export nothing."""
    domain = DomainConfig("plain", __import__("domainkit_tests"))

    assert run_async(domain.erase_subject("ann", "request")) == {}
    assert run_async(domain.export_subject("ann")) == {}


def test_subject_tags_name_each_subject_once() -> None:
    """Sorted, without repeats, between separators; none for no subjects."""
    from domainkit.ddd.personal import subject_tag, tagged_with

    assert subject_tag(["bob", "ann", "bob"]) == "\x1fann\x1fbob\x1f"
    assert subject_tag([]) is None
    assert subject_tag(["", "a\nb", "x" * 256]) is None
    assert tagged_with("ann") in "\x1fann\x1fbob\x1f"
    assert tagged_with("an") not in "\x1fann\x1fbob\x1f"


def test_messages_whose_markers_are_unknown_are_left(scenario: Scenario) -> None:
    """A message of an event or task this process does not know names nobody.

    It cannot be told whose it is, so it is left as it is, never guessed at.
    """
    from domainkit.contrib.messaging.erasure import erase_subject as erase_queued
    from domainkit.contrib.messaging.outbox import message_row
    from domainkit.contrib.messaging.tables import messages as outbox

    async def plant_and_erase() -> dict[str, int]:
        rows = [
            message_row(
                kind=kind,
                target=target,
                payload={"patient_id": scenario.subject, "email": EMAIL},
                headers={"event_type": "nowhere.Gone"} if kind == "event" else {},
                max_attempts=1,
            )
            for kind, target in (("event", "nowhere.handler"), ("task", "gone.task"))
        ]
        async with connections.engine().begin() as connection:
            await connection.execute(
                insert(outbox), [{**row, "status": DEAD} for row in rows]
            )
        return await erase_queued(scenario.subject)

    assert run_async(plant_and_erase()) == {"messages": 2, "pending": 0}
    unknown = run_async(
        rows(messages, messages.c.target.in_(["gone.task", "nowhere.handler"]))
    )
    assert [row.payload["email"] for row in unknown] == [EMAIL, EMAIL]
