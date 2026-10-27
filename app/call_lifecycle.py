"""Per-call bookkeeping shared by all transports: build the prompt, persist the call log, summarize."""

from __future__ import annotations

import asyncio
import json
import logging

from app.config import get_clinic, get_settings
from app.db import CallLog, SessionLocal, utcnow
from app.knowledge import get_kb
from app.prompts import build_instructions
from app.scheduling import now_local
from app.tools import CallContext

log = logging.getLogger(__name__)


def instructions_for(ctx: CallContext) -> str:
    clinic = get_clinic()
    return build_instructions(clinic, get_kb(), ctx.caller_number, now_local(clinic))


def _summarize(ctx: CallContext) -> str | None:
    s = get_settings()
    if not (s.summarize_calls and s.openai_api_key and ctx.transcript):
        return None
    from openai import OpenAI

    convo = "\n".join(f"{t['role']}: {t['text']}" for t in ctx.transcript)
    resp = OpenAI(api_key=s.openai_api_key).chat.completions.create(
        model=s.text_model,
        messages=[
            {
                "role": "system",
                "content": "Summarize this clinic phone call for front-desk staff in 2-4 bullet points, in English "
                "even if the call was in another language: who called, what they wanted, what was done, any "
                "follow-up needed. Note the language the caller used.",
            },
            {"role": "user", "content": convo},
        ],
    )
    return resp.choices[0].message.content


def _persist(ctx: CallContext) -> None:
    try:
        summary = _summarize(ctx)
    except Exception:
        log.exception("summary failed for %s", ctx.call_sid)
        summary = None
    with SessionLocal.begin() as db:
        db.add(
            CallLog(
                call_sid=ctx.call_sid,
                transport=ctx.transport,
                from_number=ctx.caller_number,
                patient_id=ctx.patient_id,
                ended_at=utcnow(),
                transcript=json.dumps(ctx.transcript, ensure_ascii=False),
                tool_calls=json.dumps(ctx.tool_log, ensure_ascii=False, default=str),
                summary=summary,
                outcome=ctx.outcome or "no_action",
            )
        )


async def finish_call(ctx: CallContext) -> None:
    await asyncio.to_thread(_persist, ctx)
    log.info("call %s finished: outcome=%s turns=%d", ctx.call_sid, ctx.outcome, len(ctx.transcript))
