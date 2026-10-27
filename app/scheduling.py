"""Scheduling domain logic. Deterministic, no LLM in here.

The LLM only *proposes* actions through tools; every rule (business hours, lead time,
double-booking, provider/service compatibility) is enforced here. This is the single most
important design principle for booking agents: never trust the model with the calendar.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import ClinicConfig
from app.db import Appointment, Patient, Provider

WEEKDAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
MAX_SEARCH_DAYS = 14
MAX_SLOTS_PER_DAY = 3


class SchedulingError(Exception):
    """A rule violation that should be explained to the caller (message is caller-safe)."""


@dataclass
class Slot:
    provider_id: int
    provider_name: str
    start: datetime  # tz-aware, clinic local time
    end: datetime

    @property
    def id(self) -> str:
        return f"{self.provider_id}@{self.start.isoformat()}"


def parse_slot_id(slot_id: str) -> tuple[int, datetime]:
    try:
        provider_part, start_part = slot_id.split("@", 1)
        start = datetime.fromisoformat(start_part)
        if start.tzinfo is None:
            raise ValueError
        return int(provider_part), start
    except ValueError as exc:
        raise SchedulingError("That time slot reference is invalid; please search for slots again.") from exc


def to_utc_naive(dt: datetime) -> datetime:
    return dt.astimezone(timezone.utc).replace(tzinfo=None)


def from_utc_naive(dt: datetime, clinic: ClinicConfig) -> datetime:
    return dt.replace(tzinfo=timezone.utc).astimezone(clinic.tz)


def speakable(dt: datetime) -> str:
    """'Tuesday, October 20 at 9:30 AM' (portable; avoids platform-specific strftime flags)."""
    hour = dt.hour % 12 or 12
    minute = f":{dt.minute:02d}" if dt.minute else ""
    ampm = "AM" if dt.hour < 12 else "PM"
    return f"{dt.strftime('%A, %B')} {dt.day} at {hour}{minute} {ampm}"


def now_local(clinic: ClinicConfig) -> datetime:
    return datetime.now(clinic.tz)


# ---------------------------------------------------------------- availability


def _windows(clinic: ClinicConfig, day: date) -> list[tuple[datetime, datetime]]:
    if day in clinic.closed_dates:
        return []
    out = []
    for start, end in clinic.hours.get(WEEKDAYS[day.weekday()], []):
        out.append(
            (datetime.combine(day, start, tzinfo=clinic.tz), datetime.combine(day, end, tzinfo=clinic.tz))
        )
    return out


def _fits_hours(clinic: ClinicConfig, start: datetime, minutes: int) -> bool:
    end = start + timedelta(minutes=minutes)
    return any(ws <= start and end <= we for ws, we in _windows(clinic, start.date()))


def _part_of_day_ok(t: time, part: str | None) -> bool:
    if part == "morning":
        return t < time(12)
    if part == "afternoon":
        return time(12) <= t < time(17)
    if part == "evening":
        return t >= time(17)
    return True


def _busy(db: Session, provider_id: int, start_utc: datetime, end_utc: datetime) -> list[tuple[datetime, datetime]]:
    rows = db.execute(
        select(Appointment.start_utc, Appointment.end_utc).where(
            Appointment.provider_id == provider_id,
            Appointment.status == "booked",
            Appointment.start_utc < end_utc,
            Appointment.end_utc > start_utc,
        )
    ).all()
    return [(r[0], r[1]) for r in rows]


def _check_bookable(clinic: ClinicConfig, start: datetime, minutes: int, now: datetime) -> None:
    if start < now + timedelta(minutes=clinic.min_booking_lead_minutes):
        raise SchedulingError("That time is too soon to book. Please choose a later time.")
    if start.date() > (now + timedelta(days=clinic.max_booking_days_ahead)).date():
        raise SchedulingError(f"We only book up to {clinic.max_booking_days_ahead} days ahead.")
    if not _fits_hours(clinic, start, minutes):
        raise SchedulingError("The clinic is not open for that whole appointment time.")


def is_open(clinic: ClinicConfig, now: datetime | None = None) -> bool:
    now = now or now_local(clinic)
    return any(ws <= now < we for ws, we in _windows(clinic, now.date()))


def eligible_providers(
    db: Session, clinic: ClinicConfig, service_key: str, provider_name: str | None = None, new_patient: bool = False
) -> list[Provider]:
    providers = [p for p in db.scalars(select(Provider).where(Provider.active)) if p.offers(service_key)]
    if provider_name:
        providers = [p for p in providers if clinic.provider_matches(p.name, provider_name)]
    if new_patient:
        if providers and not any(clinic.accepts_new(p.name) for p in providers):
            raise SchedulingError(
                "That provider is not accepting new patients. Offer another provider who offers this service."
            )
        providers = [p for p in providers if clinic.accepts_new(p.name)]
    return providers


def find_slots(
    db: Session,
    clinic: ClinicConfig,
    service_key: str,
    date_from: date,
    date_to: date | None = None,
    provider_name: str | None = None,
    part_of_day: str | None = None,
    limit: int = 6,
    now: datetime | None = None,
    new_patient: bool = False,
) -> list[Slot]:
    service = clinic.service(service_key)
    if not service:
        raise SchedulingError(f"Unknown service '{service_key}'.")
    providers = eligible_providers(db, clinic, service_key, provider_name, new_patient)
    if not providers:
        raise SchedulingError("No provider matching that request offers this service.")

    now = now or now_local(clinic)
    date_from = max(date_from, now.date())
    date_to = min(date_to or date_from + timedelta(days=MAX_SEARCH_DAYS - 1), date_from + timedelta(days=MAX_SEARCH_DAYS - 1))
    duration = timedelta(minutes=service.minutes)
    step = timedelta(minutes=clinic.slot_minutes)

    range_start = to_utc_naive(datetime.combine(date_from, time.min, tzinfo=clinic.tz))
    range_end = to_utc_naive(datetime.combine(date_to + timedelta(days=1), time.min, tzinfo=clinic.tz))
    busy = {p.id: _busy(db, p.id, range_start, range_end) for p in providers}

    results: list[Slot] = []
    day = date_from
    while day <= date_to and len(results) < limit:
        day_slots: list[Slot] = []
        for ws, we in _windows(clinic, day):
            t = ws
            while t + duration <= we:
                if _part_of_day_ok(t.time(), part_of_day):
                    try:
                        _check_bookable(clinic, t, service.minutes, now)
                    except SchedulingError:
                        t += step
                        continue
                    s_utc, e_utc = to_utc_naive(t), to_utc_naive(t + duration)
                    for p in providers:
                        if not any(bs < e_utc and be > s_utc for bs, be in busy[p.id]):
                            day_slots.append(Slot(p.id, p.name, t, t + duration))
                            break  # one provider per start time keeps the spoken list short
                t += step
        # Spread suggestions across the day instead of reading out 9:00, 9:15, 9:30...
        if day_slots:
            stride = max(1, len(day_slots) // MAX_SLOTS_PER_DAY)
            results.extend(day_slots[::stride][:MAX_SLOTS_PER_DAY])
        day += timedelta(days=1)
    return results[:limit]


# ---------------------------------------------------------------- appointments


def book(
    db: Session,
    clinic: ClinicConfig,
    patient_id: int,
    service_key: str,
    slot_id: str,
    reason: str | None = None,
    now: datetime | None = None,
    new_patient: bool = False,
) -> Appointment:
    service = clinic.service(service_key)
    if not service:
        raise SchedulingError(f"Unknown service '{service_key}'.")
    provider_id, start = parse_slot_id(slot_id)
    start = start.astimezone(clinic.tz)
    provider = db.get(Provider, provider_id)
    if not provider or not provider.active or not provider.offers(service_key):
        raise SchedulingError("That provider does not offer this service.")
    if new_patient and not clinic.accepts_new(provider.name):
        raise SchedulingError("That provider is not accepting new patients. Offer another provider.")
    _check_bookable(clinic, start, service.minutes, now or now_local(clinic))

    s_utc = to_utc_naive(start)
    e_utc = s_utc + timedelta(minutes=service.minutes)
    # Re-check conflicts at write time (the slot may have been taken since it was offered).
    # Production on Postgres: also add an exclusion constraint on (provider_id, tsrange) so the DB
    # itself guarantees no double booking under concurrent calls.
    if _busy(db, provider_id, s_utc, e_utc):
        raise SchedulingError("Sorry, that time was just taken. Let me find other options.")
    appt = Appointment(
        patient_id=patient_id,
        provider_id=provider_id,
        service_key=service_key,
        start_utc=s_utc,
        end_utc=e_utc,
        reason=reason,
    )
    db.add(appt)
    db.flush()
    return appt


def _own_appointment(db: Session, patient_id: int, appointment_id: int) -> Appointment:
    appt = db.get(Appointment, appointment_id)
    if not appt or appt.patient_id != patient_id or appt.status != "booked":
        raise SchedulingError("I couldn't find that appointment on your file.")
    return appt


def cancel(db: Session, patient_id: int, appointment_id: int) -> Appointment:
    appt = _own_appointment(db, patient_id, appointment_id)
    appt.status = "cancelled"
    db.flush()
    return appt


def reschedule(
    db: Session,
    clinic: ClinicConfig,
    patient_id: int,
    appointment_id: int,
    new_slot_id: str,
    now: datetime | None = None,
) -> Appointment:
    old = _own_appointment(db, patient_id, appointment_id)
    old.status = "cancelled"  # free the old slot first so the patient can move within overlapping times
    db.flush()
    return book(db, clinic, patient_id, old.service_key, new_slot_id, old.reason, now)


def upcoming(db: Session, patient_id: int, now_utc: datetime | None = None) -> list[Appointment]:
    now_utc = now_utc or datetime.now(timezone.utc).replace(tzinfo=None)
    return list(
        db.scalars(
            select(Appointment)
            .where(Appointment.patient_id == patient_id, Appointment.status == "booked", Appointment.start_utc >= now_utc)
            .order_by(Appointment.start_utc)
        )
    )


# ---------------------------------------------------------------- patients


def find_patient(db: Session, first_name: str, last_name: str, date_of_birth: str) -> Patient | None:
    return db.scalars(
        select(Patient).where(
            func.lower(Patient.first_name) == first_name.strip().lower(),
            func.lower(Patient.last_name) == last_name.strip().lower(),
            Patient.date_of_birth == date_of_birth.strip(),
        )
    ).first()


def _digits(phone: str | None) -> str:
    d = "".join(ch for ch in phone or "" if ch.isdigit())
    return d[-10:]  # compare national numbers: +1 416... == 416...


def match_patient(
    db: Session, first_name: str, last_name: str, date_of_birth: str, caller_number: str | None
) -> tuple[Patient | None, str]:
    """Identity check that tolerates phone-ASR name errors without getting weaker.

    1. exact first + last + DOB                         -> "exact"
    2. first/last swapped (common with Chinese names)   -> "swapped"
    3. last name + DOB, and caller ID matches the file   -> "caller_id"  (first name may be misheard: Mei/May)
    Anything else -> no match. DOB is always required; a fuzzy first name alone never passes.
    """
    p = find_patient(db, first_name, last_name, date_of_birth)
    if p:
        return p, "exact"
    p = find_patient(db, last_name, first_name, date_of_birth)
    if p:
        return p, "swapped"
    caller = _digits(caller_number)
    if caller:
        for last in {last_name, first_name}:
            candidates = db.scalars(
                select(Patient).where(
                    func.lower(Patient.last_name) == last.strip().lower(),
                    Patient.date_of_birth == date_of_birth.strip(),
                )
            ).all()
            hits = [c for c in candidates if _digits(c.phone) == caller]
            if len(hits) == 1:
                return hits[0], "caller_id"
    return None, "none"


def register_patient(db: Session, first_name: str, last_name: str, date_of_birth: str, phone: str) -> Patient:
    try:
        date.fromisoformat(date_of_birth)
    except ValueError as exc:
        raise SchedulingError("Date of birth must be a valid date.") from exc
    if find_patient(db, first_name, last_name, date_of_birth):
        raise SchedulingError("A patient with that name and birth date already exists.")
    p = Patient(first_name=first_name.strip(), last_name=last_name.strip(), date_of_birth=date_of_birth, phone=phone)
    db.add(p)
    db.flush()
    return p
