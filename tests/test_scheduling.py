from datetime import date, datetime

import pytest

from app import scheduling as sch
from app.config import get_clinic
from app.db import SessionLocal

clinic = get_clinic()
MONDAY_8AM = datetime(2026, 10, 19, 8, 0, tzinfo=clinic.tz)


def test_slots_respect_hours_lead_time_and_weekends():
    with SessionLocal() as db:
        slots = sch.find_slots(db, clinic, "checkup", date(2026, 10, 19), date(2026, 10, 25), limit=50, now=MONDAY_8AM)
    assert slots
    for s in slots:
        assert s.start.weekday() < 5  # clinic closed on weekends
        assert s.start >= MONDAY_8AM.replace(hour=9)  # lead time + opening hour
        assert not (12 <= s.start.hour < 13)  # lunch break
        assert sch._fits_hours(clinic, s.start, 30)


def test_part_of_day_and_provider_filter():
    with SessionLocal() as db:
        slots = sch.find_slots(
            db, clinic, "vaccine", date(2026, 10, 20), provider_name="Emily", part_of_day="afternoon", now=MONDAY_8AM
        )
    assert slots and all(s.provider_name == "Nurse Emily Wong" and s.start.hour >= 12 for s in slots)


def test_provider_must_offer_service():
    with SessionLocal() as db, pytest.raises(sch.SchedulingError):
        sch.find_slots(db, clinic, "vaccine", date(2026, 10, 20), provider_name="Shen", now=MONDAY_8AM)


def test_no_double_booking_and_reschedule():
    with SessionLocal.begin() as db:
        slot = sch.find_slots(db, clinic, "checkup", date(2026, 10, 20), now=MONDAY_8AM)[0]
        appt = sch.book(db, clinic, 1, "checkup", slot.id, now=MONDAY_8AM)
        with pytest.raises(sch.SchedulingError):
            sch.book(db, clinic, 2, "checkup", slot.id, now=MONDAY_8AM)
        # the booked slot is no longer offered for that provider
        again = sch.find_slots(db, clinic, "checkup", date(2026, 10, 20), date(2026, 10, 20), limit=50, now=MONDAY_8AM)
        assert all(s.id != slot.id for s in again)

        new_slot = again[-1]
        moved = sch.reschedule(db, clinic, 1, appt.id, new_slot.id, now=MONDAY_8AM)
        assert appt.status == "cancelled" and moved.status == "booked"


def test_cannot_cancel_someone_elses_appointment():
    with SessionLocal.begin() as db:
        slot = sch.find_slots(db, clinic, "checkup", date(2026, 10, 20), now=MONDAY_8AM)[0]
        appt = sch.book(db, clinic, 1, "checkup", slot.id, now=MONDAY_8AM)
        with pytest.raises(sch.SchedulingError):
            sch.cancel(db, 2, appt.id)


def test_rejects_closed_and_too_soon():
    with SessionLocal() as db:
        with pytest.raises(sch.SchedulingError):  # Saturday
            sch.book(db, clinic, 1, "checkup", f"1@{datetime(2026, 10, 24, 10, 0, tzinfo=clinic.tz).isoformat()}", now=MONDAY_8AM)
        with pytest.raises(sch.SchedulingError):  # inside the 60 min lead time
            sch.book(db, clinic, 1, "checkup", f"1@{datetime(2026, 10, 19, 9, 0, tzinfo=clinic.tz).isoformat()}", now=MONDAY_8AM.replace(minute=30))


def test_speakable():
    assert sch.speakable(datetime(2026, 10, 20, 9, 30)) == "Tuesday, October 20 at 9:30 AM"
    assert sch.speakable(datetime(2026, 10, 20, 13, 0)) == "Tuesday, October 20 at 1 PM"


def test_new_patients_not_offered_full_roster():
    with SessionLocal() as db:
        slots = sch.find_slots(db, clinic, "physical", date(2026, 10, 20), limit=20, now=MONDAY_8AM, new_patient=True)
        assert slots and all(s.provider_name != "Dr. Michael Patel" for s in slots)
        with pytest.raises(sch.SchedulingError, match="not accepting new patients"):
            sch.find_slots(db, clinic, "physical", date(2026, 10, 20), provider_name="Patel", now=MONDAY_8AM, new_patient=True)
        patel_slot = sch.find_slots(db, clinic, "physical", date(2026, 10, 20), provider_name="Patel", now=MONDAY_8AM)[0]
        with pytest.raises(sch.SchedulingError, match="not accepting new patients"):
            sch.book(db, clinic, 1, "physical", patel_slot.id, now=MONDAY_8AM, new_patient=True)


def test_is_open():
    assert sch.is_open(clinic, MONDAY_8AM.replace(hour=10))
    assert not sch.is_open(clinic, MONDAY_8AM.replace(hour=12, minute=30))  # lunch
    assert not sch.is_open(clinic, datetime(2026, 10, 24, 10, 0, tzinfo=clinic.tz))  # Saturday


def test_provider_aliases_in_both_languages():
    for said in ["沈医生", "沈大夫", "Dr. Shen", "doctor shen", "沈伟医生"]:
        assert clinic.provider_matches("Dr. Wei Shen", said), said
        assert not clinic.provider_matches("Dr. Michael Patel", said), said
    assert clinic.provider_matches("Nurse Emily Wong", "黄护士")
    with SessionLocal() as db:
        slots = sch.find_slots(db, clinic, "checkup", date(2026, 10, 20), provider_name="沈医生", now=MONDAY_8AM)
    assert slots and all(s.provider_name == "Dr. Wei Shen" for s in slots)
