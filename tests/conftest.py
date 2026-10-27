import os
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_tmp = tempfile.mkdtemp()
os.environ["DATABASE_URL"] = f"sqlite:///{Path(_tmp, 'test.db').as_posix()}"
os.environ["CLINIC_CONFIG_PATH"] = str(ROOT / "clinic.json")
os.environ["KNOWLEDGE_DIR"] = str(ROOT / "knowledge")
os.environ["TWILIO_VALIDATE_SIGNATURE"] = "false"
os.environ["SEND_SMS_CONFIRMATIONS"] = "false"
os.environ["SUMMARIZE_CALLS"] = "false"
os.environ["OPENAI_API_KEY"] = ""

import pytest  # noqa: E402

from app.config import get_clinic  # noqa: E402
from app.db import Base, Patient, Provider, SessionLocal, engine  # noqa: E402


@pytest.fixture(autouse=True)
def fresh_db():
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    with SessionLocal.begin() as db:
        for p in get_clinic().providers:
            db.add(Provider(name=p.name, services=",".join(p.services)))
        db.add(Patient(first_name="Alice", last_name="Nguyen", date_of_birth="1985-04-12", phone="+15551230001"))
        db.add(Patient(first_name="Bob", last_name="Smith", date_of_birth="1972-11-03", phone="+15551230002"))
    yield
