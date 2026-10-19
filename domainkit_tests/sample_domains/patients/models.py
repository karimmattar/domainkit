"""Patients, their visits and appointments, and the cards and badges they hold."""

from datetime import date, datetime
from typing import Annotated
from uuid import UUID

from domainkit.ddd import AggregateRoot, Personal


class Patient(AggregateRoot[UUID]):
    """A person the clinic treats: the subject of their own data."""

    name: Annotated[str, Personal(of="id")]
    email: Annotated[str | None, Personal(of="id")] = None
    registered_at: datetime


class Visit(AggregateRoot[UUID]):
    """A visit, kept for years; its notes are erased after one."""

    patient_id: UUID
    visited_at: datetime
    notes: Annotated[str | None, Personal(of="patient_id")] = None
    symptoms: Annotated[str, Personal(of="patient_id")] | None = None


class Appointment(AggregateRoot[UUID]):
    """An appointment: personal data about the patient and the doctor."""

    patient_id: UUID
    doctor_id: str
    booked_at: datetime
    patient_name: Annotated[str, Personal(of="patient_id")]
    doctor_phone: Annotated[str | None, Personal(of="doctor_id")] = None


class Card(AggregateRoot[int]):
    """A loyalty card whose number is unique."""

    holder_id: str
    number: Annotated[str, Personal(of="holder_id")]


class Badge(AggregateRoot[int]):
    """A badge that erases itself: a birth date cannot be None."""

    holder_id: str
    born_on: Annotated[date, Personal(of="holder_id")]
    erased_count: int = 0

    def erase_personal_data(self, fields: frozenset[str]) -> None:
        """Keep the year of birth only."""
        if "born_on" in fields:
            self.born_on = date(self.born_on.year, 1, 1)
            self.erased_count += 1


class Room(AggregateRoot[int]):
    """Holds nothing personal."""

    name: str
    opened_on: date
