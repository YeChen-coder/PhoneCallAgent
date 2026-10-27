"""Create tables and load providers from clinic.json plus a few demo patients/appointments.

    uv run python -m scripts.seed
"""

from __future__ import annotations

from datetime import datetime, time, timedelta

from sqlalchemy import select

from app.config import get_clinic
from app.db import Appointment, Patient, Provider, SessionLocal, init_db
from app.scheduling import now_local, to_utc_naive

DEMO_PATIENTS = [
    ("Alice", "Nguyen", "1985-04-12", "+15551230001"),
    ("Bob", "Smith", "1972-11-03", "+15551230002"),
    ("Carmen", "Lopez", "1990-07-21", "+15551230003"),
    ("Mei", "Lin", "1980-05-20", "+15551230004"),
]


def seed() -> None:
    init_db()
    clinic = get_clinic()
    with SessionLocal.begin() as db:
        for p in clinic.providers:
            existing = db.scalars(select(Provider).where(Provider.name == p.name)).first()
            if existing:
                existing.services = ",".join(p.services)
            else:
                db.add(Provider(name=p.name, services=",".join(p.services)))
        db.flush()

        if db.scalars(select(Patient)).first():
            print("patients already present; providers synced")
            return
        patients = [Patient(first_name=f, last_name=l, date_of_birth=d, phone=ph) for f, l, d, ph in DEMO_PATIENTS]
        db.add_all(patients)
        db.flush()

        # Give Alice an upcoming check-up so reschedule/cancel can be tried immediately.
        provider = db.scalars(select(Provider)).first()
        day = now_local(clinic).date() + timedelta(days=1)
        while not clinic.hours.get(["mon", "tue", "wed", "thu", "fri", "sat", "sun"][day.weekday()]):
            day += timedelta(days=1)
        start = datetime.combine(day, time(10, 0), tzinfo=clinic.tz)
        db.add(
            Appointment(
                patient_id=patients[0].id,
                provider_id=provider.id,
                service_key="checkup",
                start_utc=to_utc_naive(start),
                end_utc=to_utc_naive(start + timedelta(minutes=30)),
                source="seed",
            )
        )
        # Mei Lin (Mandarin speaker) has a follow-up with Dr. Shen the same day at 14:00.
        shen = db.scalars(select(Provider).where(Provider.name == "Dr. Wei Shen")).first()
        if shen:
            start2 = datetime.combine(day, time(14, 0), tzinfo=clinic.tz)
            db.add(
                Appointment(
                    patient_id=patients[3].id,
                    provider_id=shen.id,
                    service_key="followup",
                    start_utc=to_utc_naive(start2),
                    end_utc=to_utc_naive(start2 + timedelta(minutes=15)),
                    source="seed",
                )
            )
    print("seeded providers, demo patients and one appointment")


if __name__ == "__main__":
    seed()
