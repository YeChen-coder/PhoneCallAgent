"""Database models. SQLite for local dev; point DATABASE_URL at Postgres for production.

Times are stored as naive UTC datetimes. Convert to the clinic's timezone only at the edges
(tool inputs/outputs, admin views).
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import ForeignKey, Index, String, Text, create_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship, sessionmaker

from app.config import get_settings


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Base(DeclarativeBase):
    pass


class Provider(Base):
    __tablename__ = "providers"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120), unique=True)
    services: Mapped[str] = mapped_column(String(500))  # comma-separated service keys
    active: Mapped[bool] = mapped_column(default=True)

    def offers(self, service_key: str) -> bool:
        return service_key in self.services.split(",")


class Patient(Base):
    __tablename__ = "patients"
    id: Mapped[int] = mapped_column(primary_key=True)
    first_name: Mapped[str] = mapped_column(String(80))
    last_name: Mapped[str] = mapped_column(String(80))
    date_of_birth: Mapped[str] = mapped_column(String(10))  # YYYY-MM-DD
    phone: Mapped[str] = mapped_column(String(20), index=True)  # E.164
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    appointments: Mapped[list[Appointment]] = relationship(back_populates="patient")


class Appointment(Base):
    __tablename__ = "appointments"
    id: Mapped[int] = mapped_column(primary_key=True)
    patient_id: Mapped[int] = mapped_column(ForeignKey("patients.id"))
    provider_id: Mapped[int] = mapped_column(ForeignKey("providers.id"))
    service_key: Mapped[str] = mapped_column(String(40))
    start_utc: Mapped[datetime]
    end_utc: Mapped[datetime]
    status: Mapped[str] = mapped_column(String(20), default="booked")  # booked | cancelled
    reason: Mapped[str | None] = mapped_column(Text)
    source: Mapped[str] = mapped_column(String(20), default="phone_agent")
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    patient: Mapped[Patient] = relationship(back_populates="appointments")
    provider: Mapped[Provider] = relationship()

    __table_args__ = (Index("ix_appt_provider_time", "provider_id", "start_utc"),)


class Message(Base):
    """Callback requests / messages for staff (after-hours, prescription refills, anything the agent can't do)."""

    __tablename__ = "messages"
    id: Mapped[int] = mapped_column(primary_key=True)
    call_sid: Mapped[str | None] = mapped_column(String(64))
    caller_name: Mapped[str | None] = mapped_column(String(160))
    callback_number: Mapped[str] = mapped_column(String(20))
    body: Mapped[str] = mapped_column(Text)
    urgency: Mapped[str] = mapped_column(String(20), default="normal")
    category: Mapped[str] = mapped_column(String(20), default="other")
    handled: Mapped[bool] = mapped_column(default=False)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class CallLog(Base):
    __tablename__ = "call_logs"
    id: Mapped[int] = mapped_column(primary_key=True)
    call_sid: Mapped[str] = mapped_column(String(64), index=True)
    transport: Mapped[str] = mapped_column(String(20))
    from_number: Mapped[str | None] = mapped_column(String(20))
    patient_id: Mapped[int | None] = mapped_column(ForeignKey("patients.id"))
    started_at: Mapped[datetime] = mapped_column(default=utcnow)
    ended_at: Mapped[datetime | None]
    transcript: Mapped[str | None] = mapped_column(Text)  # JSON list of {role, text}
    tool_calls: Mapped[str | None] = mapped_column(Text)  # JSON list of {name, args, result}
    summary: Mapped[str | None] = mapped_column(Text)
    outcome: Mapped[str | None] = mapped_column(String(40))


_settings = get_settings()
engine = create_engine(
    _settings.database_url,
    connect_args={"check_same_thread": False} if _settings.database_url.startswith("sqlite") else {},
)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


def init_db() -> None:
    Base.metadata.create_all(engine)
