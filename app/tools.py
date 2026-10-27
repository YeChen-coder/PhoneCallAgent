"""Tools the voice model can call, and the dispatcher that executes them.

Design notes
- Tool schemas are written once in the Realtime API shape and converted for Chat Completions
  (scripts/text_chat.py) so voice and text testing exercise the exact same tools.
- Identity lives in CallContext, not in tool arguments: after verify_patient succeeds, the
  patient id is pinned to the call. The model never passes a patient_id, so it cannot act on
  someone else's record even if a caller tries to talk it into it.
- Tool results are small JSON dicts with caller-safe text; errors come back as
  {"ok": false, "error": ...} so the model can recover conversationally.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from app import scheduling as sch
from app.config import get_clinic
from app.db import Message, SessionLocal
from app.knowledge import get_kb
from app.notifications import send_sms

log = logging.getLogger(__name__)


@dataclass
class CallContext:
    call_sid: str
    transport: str  # "twilio_stream" | "openai_sip" | "text"
    caller_number: str | None = None
    patient_id: int | None = None
    patient_name: str | None = None
    patient_is_new: bool = False  # registered during this call -> new-patient roster rules apply
    # Set by end_call / transfer_to_human; the transport executes it after the goodbye is spoken.
    pending_action: str | None = None
    transfer_reason: str | None = None
    outcome: str | None = None
    transcript: list[dict] = field(default_factory=list)
    tool_log: list[dict] = field(default_factory=list)


def _fn(name: str, description: str, properties: dict, required: list[str] | None = None) -> dict:
    return {
        "type": "function",
        "name": name,
        "description": description,
        "parameters": {"type": "object", "properties": properties, "required": required or [], "additionalProperties": False},
    }


_STR = {"type": "string"}

TOOL_SCHEMAS: list[dict] = [
    _fn(
        "verify_patient",
        "Verify an existing patient's identity. Required before viewing, changing or cancelling appointments.",
        {"first_name": _STR, "last_name": _STR, "date_of_birth": {"type": "string", "description": "YYYY-MM-DD"}},
        ["first_name", "last_name", "date_of_birth"],
    ),
    _fn(
        "register_patient",
        "Create a record for a new patient. Use only after confirming they have never been to the clinic.",
        {
            "first_name": _STR,
            "last_name": _STR,
            "date_of_birth": {"type": "string", "description": "YYYY-MM-DD"},
            "phone": {"type": "string", "description": "Callback number in E.164; omit to use caller ID"},
        },
        ["first_name", "last_name", "date_of_birth"],
    ),
    _fn(
        "find_available_slots",
        "Search open appointment times. Returns a few options spread across days.",
        {
            "service": {"type": "string", "description": "Service key, e.g. checkup"},
            "date_from": {"type": "string", "description": "YYYY-MM-DD, earliest acceptable day"},
            "date_to": {"type": "string", "description": "YYYY-MM-DD, latest acceptable day (optional)"},
            "provider_name": {"type": "string", "description": "Optional preferred provider"},
            "part_of_day": {"type": "string", "enum": ["morning", "afternoon", "evening", "any"]},
        },
        ["service", "date_from"],
    ),
    _fn(
        "book_appointment",
        "Book a slot returned by find_available_slots for the verified/registered patient.",
        {
            "slot_id": {"type": "string", "description": "slot_id exactly as returned by find_available_slots"},
            "service": {"type": "string"},
            "reason": {"type": "string", "description": "Short non-diagnostic reason for visit, optional"},
        },
        ["slot_id", "service"],
    ),
    _fn("list_my_appointments", "List the verified patient's upcoming appointments.", {}),
    _fn(
        "cancel_appointment",
        "Cancel one of the verified patient's appointments. Confirm with the caller first.",
        {"appointment_id": {"type": "integer"}},
        ["appointment_id"],
    ),
    _fn(
        "reschedule_appointment",
        "Move an existing appointment to a new slot from find_available_slots.",
        {"appointment_id": {"type": "integer"}, "new_slot_id": _STR},
        ["appointment_id", "new_slot_id"],
    ),
    _fn(
        "take_message",
        "Leave a message for clinic staff (refills, forms, billing, results questions, anything else).",
        {
            "caller_name": _STR,
            "callback_number": {"type": "string", "description": "E.164, confirmed with the caller"},
            "date_of_birth": {"type": "string", "description": "YYYY-MM-DD; required for refill, billing and results"},
            "category": {"type": "string", "enum": ["refill", "billing", "results", "forms", "other"]},
            "message": _STR,
            "urgency": {"type": "string", "enum": ["normal", "urgent"]},
        },
        ["caller_name", "callback_number", "category", "message"],
    ),
    _fn(
        "transfer_to_human",
        "Transfer the call to front-desk staff. Tell the caller you're transferring before calling this.",
        {"reason": _STR},
        ["reason"],
    ),
    _fn("end_call", "Hang up after you have said goodbye.", {"reason": _STR}, ["reason"]),
]

if not get_kb().inline:
    TOOL_SCHEMAS.append(
        _fn(
            "search_clinic_info",
            "Search the clinic's policies and FAQ (location, parking, insurance, refills, etc).",
            {"query": _STR},
            ["query"],
        )
    )


def chat_completions_tools() -> list[dict]:
    """Same tools in the Chat Completions shape."""
    return [
        {"type": "function", "function": {k: t[k] for k in ("name", "description", "parameters")}} for t in TOOL_SCHEMAS
    ]


# ---------------------------------------------------------------- dispatcher


def _need_patient(ctx: CallContext) -> None:
    if ctx.patient_id is None:
        raise sch.SchedulingError("The caller must be verified (verify_patient) or registered first.")


def _appt_view(a, clinic) -> dict:
    start = sch.from_utc_naive(a.start_utc, clinic)
    return {
        "appointment_id": a.id,
        "when": sch.speakable(start),
        "date": start.strftime("%Y-%m-%d %H:%M"),  # numeric anchor: models mistranslate month names
        "provider": a.provider.name,
        "service": clinic.service(a.service_key).name if clinic.service(a.service_key) else a.service_key,
    }


def execute(name: str, args: dict[str, Any], ctx: CallContext) -> dict:
    """Run one tool call synchronously (call via asyncio.to_thread from async code)."""
    try:
        result = _execute(name, args, ctx)
    except sch.SchedulingError as e:
        result = {"ok": False, "error": str(e)}
    except Exception:
        log.exception("tool %s failed", name)
        result = {"ok": False, "error": "Internal error. Apologize and offer to take a message or transfer."}
    ctx.tool_log.append({"name": name, "args": args, "result": result})
    log.info("tool %s(%s) -> %s", name, json.dumps(args), json.dumps(result)[:300])
    return result


def _execute(name: str, args: dict[str, Any], ctx: CallContext) -> dict:
    clinic = get_clinic()

    if name == "end_call":
        ctx.pending_action, ctx.outcome = "hangup", ctx.outcome or "completed"
        return {"ok": True, "note": "Call will end after your goodbye."}
    if name == "transfer_to_human":
        if not sch.is_open(clinic):
            return {
                "ok": False,
                "error": "The clinic is closed, so no staff can take the call now. Offer to take a message; "
                "staff call back the next business day.",
            }
        ctx.pending_action, ctx.transfer_reason, ctx.outcome = "transfer", args.get("reason"), "transferred"
        return {"ok": True, "note": "Transferring after your next sentence."}
    if name == "search_clinic_info":
        return {"ok": True, "results": get_kb().search(args["query"])}

    with SessionLocal.begin() as db:
        if name == "verify_patient":
            p, how = sch.match_patient(db, args["first_name"], args["last_name"], args["date_of_birth"], ctx.caller_number)
            if not p:
                return {"ok": False, "error": "No match. Ask them to double-check spelling and birth date, or register if new."}
            ctx.patient_id, ctx.patient_name = p.id, f"{p.first_name} {p.last_name}"
            result = {"ok": True, "patient": ctx.patient_name}
            if how != "exact":
                result["note"] = f"Matched the file as {ctx.patient_name}; use this spelling from now on."
            return result

        if name == "register_patient":
            phone = args.get("phone") or ctx.caller_number or ""
            p = sch.register_patient(db, args["first_name"], args["last_name"], args["date_of_birth"], phone)
            ctx.patient_id, ctx.patient_name = p.id, f"{p.first_name} {p.last_name}"
            ctx.patient_is_new = True
            return {"ok": True, "patient": ctx.patient_name}

        if name == "find_available_slots":
            part = args.get("part_of_day")
            slots = sch.find_slots(
                db,
                clinic,
                args["service"],
                date.fromisoformat(args["date_from"]),
                date.fromisoformat(args["date_to"]) if args.get("date_to") else None,
                provider_name=args.get("provider_name"),
                part_of_day=None if part in (None, "any") else part,
                new_patient=ctx.patient_is_new,
            )
            if not slots:
                return {"ok": True, "slots": [], "note": "Nothing open in that range. Suggest a wider range."}
            return {
                "ok": True,
                "slots": [
                    {"slot_id": s.id, "when": sch.speakable(s.start), "date": s.start.strftime("%Y-%m-%d %H:%M"), "provider": s.provider_name}
                    for s in slots
                ],
            }

        if name == "book_appointment":
            _need_patient(ctx)
            appt = sch.book(
                db, clinic, ctx.patient_id, args["service"], args["slot_id"], args.get("reason"), new_patient=ctx.patient_is_new
            )
            db.refresh(appt)
            view = _appt_view(appt, clinic)
            ctx.outcome = "booked"
            send_sms(appt.patient.phone, f"{clinic.name}: your {view['service']} with {view['provider']} is booked for {view['when']}.")
            return {"ok": True, "booked": view}

        if name == "list_my_appointments":
            _need_patient(ctx)
            return {"ok": True, "appointments": [_appt_view(a, clinic) for a in sch.upcoming(db, ctx.patient_id)]}

        if name == "cancel_appointment":
            _need_patient(ctx)
            appt = sch.cancel(db, ctx.patient_id, int(args["appointment_id"]))
            ctx.outcome = "cancelled"
            return {"ok": True, "cancelled": _appt_view(appt, clinic)}

        if name == "reschedule_appointment":
            _need_patient(ctx)
            appt = sch.reschedule(db, clinic, ctx.patient_id, int(args["appointment_id"]), args["new_slot_id"])
            db.refresh(appt)
            ctx.outcome = "rescheduled"
            return {"ok": True, "rescheduled_to": _appt_view(appt, clinic)}

        if name == "take_message":
            if args.get("category") in ("refill", "billing", "results") and not args.get("date_of_birth"):
                raise sch.SchedulingError(
                    "Staff need the caller's date of birth to find their file. Ask for it, read it back, then retry."
                )
            db.add(
                Message(
                    call_sid=ctx.call_sid,
                    caller_name=args.get("caller_name"),
                    callback_number=args.get("callback_number") or ctx.caller_number or "unknown",
                    body=args["message"] + (f"\n(DOB given: {args['date_of_birth']})" if args.get("date_of_birth") else ""),
                    urgency=args.get("urgency") or "normal",
                    category=args.get("category") or "other",
                )
            )
            ctx.outcome = ctx.outcome or "message_taken"
            return {"ok": True, "note": "Message saved. Staff usually call back within one business day."}

    return {"ok": False, "error": f"Unknown tool {name}"}
