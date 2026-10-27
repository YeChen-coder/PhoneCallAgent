"""FastAPI entrypoint.  Run:  uv run uvicorn app.main:app --port 8000"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app import admin
from app.db import init_db
from app.telephony import openai_sip, twilio_stream

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    yield


app = FastAPI(title="Clinic Phone Agent", lifespan=lifespan)
app.include_router(twilio_stream.router)
app.include_router(openai_sip.router)
app.include_router(admin.router)


@app.get("/health")
def health() -> dict:
    return {"ok": True}
