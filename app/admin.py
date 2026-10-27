"""Minimal staff API: today's schedule, messages, call logs. Bearer ADMIN_TOKEN.

This is the seam where a real front-desk UI (or a sync job to the clinic's EHR/PMS) plugs in.
"""

from __future__ import annotations

import json
from datetime import date, datetime, time, timedelta

from fastapi import APIRouter, Depends, Header, HTTPException
from sqlalchemy import select

from app.config import get_clinic, get_settings
from app.db import Appointment, CallLog, Message, SessionLocal
from app.scheduling import from_utc_naive, to_utc_naive


def _auth(authorization: str = Header(default="")) -> None:
    if authorization != f"Bearer {get_settings().admin_token}":
        raise HTTPException(status_code=401)


router = APIRouter(prefix="/admin", dependencies=[Depends(_auth)])


@router.get("/appointments")
def appointments(day: date | None = None) -> list[dict]:
    clinic = get_clinic()
    day = day or datetime.now(clinic.tz).date()
    start = to_utc_naive(datetime.combine(day, time.min, tzinfo=clinic.tz))
    with SessionLocal() as db:
        rows = db.scalars(
            select(Appointment)
            .where(Appointment.start_utc >= start, Appointment.start_utc < start + timedelta(days=1))
            .order_by(Appointment.start_utc)
        ).all()
        return [
            {
                "id": a.id,
                "time": from_utc_naive(a.start_utc, clinic).strftime("%H:%M"),
                "patient": f"{a.patient.first_name} {a.patient.last_name}",
                "provider": a.provider.name,
                "service": a.service_key,
                "status": a.status,
                "source": a.source,
            }
            for a in rows
        ]


@router.get("/messages")
def messages(include_handled: bool = False) -> list[dict]:
    with SessionLocal() as db:
        q = select(Message).order_by(Message.created_at.desc())
        if not include_handled:
            q = q.where(Message.handled.is_(False))
        return [
            {"id": m.id, "from": m.caller_name, "callback": m.callback_number, "urgency": m.urgency, "category": m.category, "body": m.body, "at": m.created_at.isoformat()}
            for m in db.scalars(q).all()
        ]


@router.get("/calls")
def calls(limit: int = 50) -> list[dict]:
    with SessionLocal() as db:
        rows = db.scalars(select(CallLog).order_by(CallLog.started_at.desc()).limit(limit)).all()
        return [
            {
                "call_sid": c.call_sid,
                "from": c.from_number,
                "outcome": c.outcome,
                "summary": c.summary,
                "transcript": json.loads(c.transcript or "[]"),
                "at": c.started_at.isoformat(),
            }
            for c in rows
        ]
