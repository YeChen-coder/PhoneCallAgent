from datetime import timedelta

from fastapi.testclient import TestClient

from app.config import get_clinic
from app.knowledge import BM25Index, get_kb
from app.scheduling import now_local
from app.tools import CallContext, execute


def _future_day() -> str:
    return (now_local(get_clinic()) + timedelta(days=7)).date().isoformat()


def test_booking_requires_identity_then_succeeds():
    ctx = CallContext(call_sid="t1", transport="text", caller_number="+15551230001")
    slots = execute("find_available_slots", {"service": "checkup", "date_from": _future_day()}, ctx)
    assert slots["ok"] and slots["slots"]
    slot_id = slots["slots"][0]["slot_id"]

    denied = execute("book_appointment", {"slot_id": slot_id, "service": "checkup"}, ctx)
    assert denied["ok"] is False

    assert execute("verify_patient", {"first_name": "alice", "last_name": "NGUYEN", "date_of_birth": "1985-04-12"}, ctx)["ok"]
    booked = execute("book_appointment", {"slot_id": slot_id, "service": "checkup"}, ctx)
    assert booked["ok"] and ctx.outcome == "booked"

    mine = execute("list_my_appointments", {}, ctx)
    assert len(mine["appointments"]) == 1


def test_wrong_dob_does_not_verify():
    ctx = CallContext(call_sid="t2", transport="text")
    r = execute("verify_patient", {"first_name": "Alice", "last_name": "Nguyen", "date_of_birth": "1985-04-13"}, ctx)
    assert r["ok"] is False and ctx.patient_id is None


def test_end_call_and_message():
    ctx = CallContext(call_sid="t3", transport="text", caller_number="+15550009999")
    refill = {"caller_name": "Dan", "callback_number": "+15550009999", "category": "refill", "message": "Refill metformin at Shoppers"}
    assert execute("take_message", refill, ctx)["ok"] is False  # no DOB -> staff can't find the file
    assert execute("take_message", {**refill, "date_of_birth": "1970-01-02"}, ctx)["ok"]
    execute("end_call", {"reason": "done"}, ctx)
    assert ctx.pending_action == "hangup" and ctx.outcome == "message_taken"


def test_knowledge_search():
    hits = BM25Index(get_kb().chunks).search("where can I park my car", k=1)
    assert hits and "parking" in hits[0].title.lower()


def test_twilio_voice_webhook_returns_stream_twiml():
    from app.main import app

    with TestClient(app) as client:
        r = client.post("/twilio/voice", data={"CallSid": "CA123", "From": "+15551230001"})
    assert r.status_code == 200
    assert "<Connect><Stream" in r.text and "/twilio/media-stream" in r.text and 'name="token"' in r.text


def test_transfer_refused_when_closed(monkeypatch):
    from app import scheduling

    ctx = CallContext(call_sid="t4", transport="text")
    monkeypatch.setattr(scheduling, "is_open", lambda clinic, now=None: False)
    r = execute("transfer_to_human", {"reason": "wants a person"}, ctx)
    assert r["ok"] is False and ctx.pending_action is None
    monkeypatch.setattr(scheduling, "is_open", lambda clinic, now=None: True)
    assert execute("transfer_to_human", {"reason": "wants a person"}, ctx)["ok"] and ctx.pending_action == "transfer"


def test_verify_tolerates_misheard_first_name_only_with_caller_id():
    # Alice is on file with +15551230001; ASR heard "Alicia"
    misheard = {"first_name": "Alicia", "last_name": "Nguyen", "date_of_birth": "1985-04-12"}
    stranger = CallContext(call_sid="t5", transport="text", caller_number="+15550000000")
    assert execute("verify_patient", misheard, stranger)["ok"] is False
    own_phone = CallContext(call_sid="t6", transport="text", caller_number="+1 (555) 123-0001")
    r = execute("verify_patient", misheard, own_phone)
    assert r["ok"] and r["patient"] == "Alice Nguyen" and own_phone.patient_id
    # wrong DOB never passes, even from the patient's own phone
    assert execute("verify_patient", {**misheard, "date_of_birth": "1985-04-13"}, CallContext(call_sid="t7", transport="text", caller_number="+15551230001"))["ok"] is False


def test_verify_accepts_swapped_first_and_last_name():
    ctx = CallContext(call_sid="t8", transport="text")
    assert execute("verify_patient", {"first_name": "Nguyen", "last_name": "Alice", "date_of_birth": "1985-04-12"}, ctx)["ok"]
