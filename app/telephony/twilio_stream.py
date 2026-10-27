"""Transport A (default): Twilio Programmable Voice + Media Streams <-> our server <-> OpenAI Realtime.

    caller ──PSTN──> Twilio number ──POST /twilio/voice──> TwiML <Connect><Stream>
                         │
                         └──wss /twilio/media-stream (μ-law 8k, base64)──> this module
                                                              │
                                                              └──wss──> OpenAI Realtime

Why this as the default: you own the audio path, so it works the same with any LLM/voice stack,
you can record/inspect everything, and it's the most widely documented pattern.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging

import websockets
from fastapi import APIRouter, HTTPException, Request, Response, WebSocket, WebSocketDisconnect
from twilio.request_validator import RequestValidator
from twilio.twiml.voice_response import Connect, Dial, Hangup, Say, VoiceResponse

from app.call_lifecycle import finish_call, instructions_for
from app.config import get_clinic, get_settings
from app.prompts import greeting_instruction
from app.realtime_agent import RealtimeAgent, RealtimeHooks
from app.tools import CallContext

log = logging.getLogger(__name__)
router = APIRouter(prefix="/twilio")

FINAL_MARK = "final"
PENDING_ACTION_TIMEOUT_S = 15


def _stream_token(call_sid: str) -> str:
    return hmac.new(get_settings().app_secret.encode(), call_sid.encode(), hashlib.sha256).hexdigest()


async def _validate_twilio(request: Request, form: dict) -> None:
    s = get_settings()
    if not s.twilio_validate_signature:
        return
    # Behind ngrok/a proxy request.url is the internal URL; Twilio signed the public one.
    url = s.public_base_url + request.url.path
    if not RequestValidator(s.twilio_auth_token).validate(url, form, request.headers.get("X-Twilio-Signature", "")):
        raise HTTPException(status_code=403, detail="invalid Twilio signature")


@router.post("/voice")
async def incoming_call(request: Request) -> Response:
    form = dict(await request.form())
    await _validate_twilio(request, form)
    call_sid, caller = form.get("CallSid", ""), form.get("From", "")

    ws_url = get_settings().public_base_url.replace("https://", "wss://").replace("http://", "ws://")
    vr = VoiceResponse()
    connect = Connect()
    stream = connect.stream(url=f"{ws_url}/twilio/media-stream")
    stream.parameter(name="token", value=_stream_token(call_sid))
    stream.parameter(name="caller", value=caller)
    vr.append(connect)
    return Response(content=str(vr), media_type="application/xml")


def _twilio_redirect(call_sid: str, twiml: str) -> None:
    from twilio.rest import Client

    s = get_settings()
    Client(s.twilio_account_sid, s.twilio_auth_token).calls(call_sid).update(twiml=twiml)


class TwilioBridge:
    """State for one call. Tracks playback position so barge-in truncates the model's memory correctly."""

    def __init__(self, twilio_ws: WebSocket):
        self.twilio = twilio_ws
        self.stream_sid: str | None = None
        self.ctx: CallContext | None = None
        self.agent: RealtimeAgent | None = None
        self.latest_media_ts = 0  # ms of caller audio received from Twilio
        self.response_start_ts: int | None = None
        self.last_item_id: str | None = None
        self.marks: list[str] = []
        self.action_done = False

    # ---- hooks called by RealtimeAgent

    async def on_audio_delta(self, b64: str, item_id: str | None) -> None:
        await self.twilio.send_json({"event": "media", "streamSid": self.stream_sid, "media": {"payload": b64}})
        if item_id != self.last_item_id:
            self.last_item_id, self.response_start_ts = item_id, self.latest_media_ts
        await self._mark("chunk")

    async def on_speech_started(self) -> None:
        if self.marks and self.last_item_id and self.response_start_ts is not None:
            played_ms = max(0, self.latest_media_ts - self.response_start_ts)
            await self.agent.truncate(self.last_item_id, played_ms)
            await self.twilio.send_json({"event": "clear", "streamSid": self.stream_sid})
        self.marks.clear()
        self.last_item_id = self.response_start_ts = None

    async def on_turn_finished(self) -> None:
        if self.ctx and self.ctx.pending_action:
            # Twilio echoes the mark once all audio queued before it has played -> goodbye was heard.
            await self._mark(FINAL_MARK)
            asyncio.create_task(self._action_after_timeout())

    async def on_closed(self) -> None:
        try:
            await self.twilio.close()
        except Exception:
            pass

    # ---- internals

    async def _mark(self, name: str) -> None:
        await self.twilio.send_json({"event": "mark", "streamSid": self.stream_sid, "mark": {"name": name}})
        self.marks.append(name)

    async def _action_after_timeout(self) -> None:
        await asyncio.sleep(PENDING_ACTION_TIMEOUT_S)
        await self._do_pending_action()

    async def _do_pending_action(self) -> None:
        if self.action_done or not self.ctx or not self.ctx.pending_action:
            return
        self.action_done = True
        vr = VoiceResponse()
        if self.ctx.pending_action == "transfer":
            vr.append(Say("Please hold while I connect you."))
            vr.append(Dial(get_clinic().phone_transfer_number))
        else:
            vr.append(Hangup())
        try:
            await asyncio.to_thread(_twilio_redirect, self.ctx.call_sid, str(vr))
        except Exception:
            log.exception("failed to %s call %s", self.ctx.pending_action, self.ctx.call_sid)

    async def pump_twilio(self) -> None:
        async for raw in self.twilio.iter_text():
            msg = json.loads(raw)
            ev = msg.get("event")
            if ev == "media":
                self.latest_media_ts = int(msg["media"]["timestamp"])
                await self.agent.append_audio(msg["media"]["payload"])
            elif ev == "mark":
                name = msg.get("mark", {}).get("name")
                if self.marks:
                    self.marks.pop(0)
                if name == FINAL_MARK:
                    await self._do_pending_action()
            elif ev == "stop":
                return


async def _wait_for_start(ws: WebSocket) -> dict:
    async for raw in ws.iter_text():
        msg = json.loads(raw)
        if msg.get("event") == "start":
            return msg["start"]
    raise WebSocketDisconnect()


@router.websocket("/media-stream")
async def media_stream(ws: WebSocket) -> None:
    await ws.accept()
    bridge = TwilioBridge(ws)
    try:
        start = await _wait_for_start(ws)
    except WebSocketDisconnect:
        return
    call_sid = start["callSid"]
    params = start.get("customParameters", {})
    if not hmac.compare_digest(params.get("token", ""), _stream_token(call_sid)):
        log.warning("rejecting media stream with bad token for %s", call_sid)
        await ws.close(code=1008)
        return

    bridge.stream_sid = start["streamSid"]
    bridge.ctx = ctx = CallContext(call_sid=call_sid, transport="twilio_stream", caller_number=params.get("caller"))
    s = get_settings()
    url = f"{s.openai_realtime_url}?model={s.realtime_model}"

    try:
        async with websockets.connect(url, additional_headers={"Authorization": f"Bearer {s.openai_api_key}"}) as oai:
            hooks = RealtimeHooks(
                on_audio_delta=bridge.on_audio_delta,
                on_speech_started=bridge.on_speech_started,
                on_turn_finished=bridge.on_turn_finished,
                on_closed=bridge.on_closed,
            )
            bridge.agent = agent = RealtimeAgent(oai, ctx, hooks)
            await agent.configure(instructions_for(ctx))
            await agent.greet(greeting_instruction(get_clinic()))
            agent_task = asyncio.create_task(agent.run())
            try:
                await bridge.pump_twilio()
            except WebSocketDisconnect:
                pass
            finally:
                agent.closed = True
                agent_task.cancel()
    except Exception:
        log.exception("media stream failed for %s", call_sid)
    finally:
        await finish_call(ctx)
