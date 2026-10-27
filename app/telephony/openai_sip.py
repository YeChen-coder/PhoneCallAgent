"""Transport B (optional): Twilio Elastic SIP Trunk -> OpenAI SIP endpoint, we attach a sideband socket.

    caller ──PSTN──> Twilio number ──SIP trunk──> sip:<project_id>@sip.api.openai.com;transport=tls
                                                         │ webhook: realtime.call.incoming
                                                         v
                                   POST /openai/sip/webhook (this module) -> accept call
                                   wss api.openai.com/v1/realtime?call_id=... (tools, transcripts)

Pros vs. Media Streams: lower latency (one fewer audio hop), OpenAI handles jitter/barge-in, our server
never touches audio. Cons: tied to OpenAI's telephony, less control/visibility over audio.
Setup: OpenAI dashboard -> project -> Webhooks (event realtime.call.incoming) -> this URL.
"""

from __future__ import annotations

import asyncio
import logging
import re

import httpx
import websockets
from fastapi import APIRouter, HTTPException, Request, Response

from app.call_lifecycle import finish_call, instructions_for
from app.config import get_clinic, get_settings
from app.prompts import greeting_instruction
from app.realtime_agent import RealtimeAgent, RealtimeHooks, session_config
from app.tools import CallContext

log = logging.getLogger(__name__)
router = APIRouter(prefix="/openai/sip")
API = "https://api.openai.com/v1/realtime/calls"
GOODBYE_GRACE_S = 4  # no playback marks over SIP; give the goodbye time to play
_tasks: set[asyncio.Task] = set()


def _caller_from_headers(headers: list[dict]) -> str | None:
    for h in headers:
        if h.get("name", "").lower() == "from":
            m = re.search(r"(\+?\d{7,15})", h.get("value", ""))
            return m.group(1) if m else None
    return None


async def _post(path: str, json_body: dict | None = None) -> None:
    s = get_settings()
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.post(f"{API}/{path}", headers={"Authorization": f"Bearer {s.openai_api_key}"}, json=json_body or {})
        r.raise_for_status()


async def _run_sideband(call_id: str, ctx: CallContext) -> None:
    s = get_settings()

    async def on_turn_finished() -> None:
        if not ctx.pending_action:
            return
        await asyncio.sleep(GOODBYE_GRACE_S)
        if ctx.pending_action == "transfer":
            await _post(f"{call_id}/refer", {"target_uri": f"tel:{get_clinic().phone_transfer_number}"})
        else:
            await _post(f"{call_id}/hangup")

    try:
        async with websockets.connect(
            f"{s.openai_realtime_url}?call_id={call_id}",
            additional_headers={"Authorization": f"Bearer {s.openai_api_key}"},
        ) as ws:
            agent = RealtimeAgent(ws, ctx, RealtimeHooks(on_turn_finished=on_turn_finished))
            await agent.greet(greeting_instruction(get_clinic()))
            await agent.run()
    finally:
        await finish_call(ctx)


@router.post("/webhook")
async def sip_webhook(request: Request) -> Response:
    s = get_settings()
    body = await request.body()
    from openai import OpenAI

    try:
        event = OpenAI(api_key=s.openai_api_key, webhook_secret=s.openai_webhook_secret).webhooks.unwrap(body, request.headers)
    except Exception as exc:
        raise HTTPException(status_code=400, detail="invalid webhook signature") from exc

    if event.type != "realtime.call.incoming":
        return Response(status_code=200)

    call_id = event.data.call_id
    sip_headers = [h.model_dump() if hasattr(h, "model_dump") else dict(h) for h in (event.data.sip_headers or [])]
    ctx = CallContext(call_sid=call_id, transport="openai_sip", caller_number=_caller_from_headers(sip_headers))

    # Accept with the full session config (audio format is negotiated by SIP, so omit it).
    await _post(f"{call_id}/accept", session_config(instructions_for(ctx), include_audio_format=False))
    task = asyncio.create_task(_run_sideband(call_id, ctx))
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
    return Response(status_code=200)
