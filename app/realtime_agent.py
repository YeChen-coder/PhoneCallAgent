"""Transport-agnostic OpenAI Realtime session handler.

Owns: session configuration, tool-call loop, transcript capture, deferred hangup/transfer.
Does NOT own audio transport. Transports plug in via the hooks on RealtimeHooks:

  twilio_stream  - our server relays G.711 μ-law audio between Twilio and OpenAI (this file
                   gets audio deltas and interruption events and forwards them to Twilio)
  openai_sip     - OpenAI terminates SIP itself; we only attach a "sideband" websocket for
                   tools/transcripts, so the audio hooks are never called
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from websockets.asyncio.client import ClientConnection

from app.config import get_settings
from app.tools import TOOL_SCHEMAS, CallContext, execute

log = logging.getLogger(__name__)

AsyncFn = Callable[..., Awaitable[None]]


async def _noop(*_a, **_k) -> None:
    return None


@dataclass
class RealtimeHooks:
    on_audio_delta: AsyncFn = _noop  # (b64_audio: str, item_id: str)
    on_speech_started: AsyncFn = _noop  # caller started talking -> barge-in
    on_turn_finished: AsyncFn = _noop  # a full assistant response (no more tool calls) is done
    on_closed: AsyncFn = _noop


def session_config(instructions: str, *, include_audio_format: bool = True) -> dict:
    s = get_settings()
    if s.vad_type == "server_vad":
        turn_detection = {"type": "server_vad", "silence_duration_ms": s.vad_silence_ms, "prefix_padding_ms": 300, "interrupt_response": True}
    else:
        turn_detection = {"type": "semantic_vad", "eagerness": s.vad_eagerness, "interrupt_response": True}
    audio_in: dict = {
        "turn_detection": turn_detection,
        "transcription": {"model": s.transcribe_model},
        "noise_reduction": {"type": "far_field"},
    }
    audio_out: dict = {"voice": s.realtime_voice}
    if include_audio_format:
        # Twilio Media Streams speak 8 kHz G.711 μ-law; Realtime accepts it natively -> no resampling.
        audio_in["format"] = {"type": "audio/pcmu"}
        audio_out["format"] = {"type": "audio/pcmu"}
    return {
        "type": "realtime",
        "model": s.realtime_model,
        "output_modalities": ["audio"],
        "instructions": instructions,
        "audio": {"input": audio_in, "output": audio_out},
        "tools": TOOL_SCHEMAS,
        "tool_choice": "auto",
    }


class RealtimeAgent:
    def __init__(self, ws: ClientConnection, ctx: CallContext, hooks: RealtimeHooks):
        self.ws = ws
        self.ctx = ctx
        self.hooks = hooks
        self.closed = False
        self._speech_stopped_at: float | None = None
        self._response_created_at: float | None = None

    async def send(self, event: dict) -> None:
        await self.ws.send(json.dumps(event))

    async def configure(self, instructions: str, *, include_audio_format: bool = True) -> None:
        await self.send({"type": "session.update", "session": session_config(instructions, include_audio_format=include_audio_format)})

    async def greet(self, instruction: str) -> None:
        await self.send({"type": "response.create", "response": {"instructions": instruction}})

    async def append_audio(self, b64_audio: str) -> None:
        await self.send({"type": "input_audio_buffer.append", "audio": b64_audio})

    async def truncate(self, item_id: str, audio_end_ms: int) -> None:
        await self.send({"type": "conversation.item.truncate", "item_id": item_id, "content_index": 0, "audio_end_ms": audio_end_ms})

    async def run(self) -> None:
        """Consume server events until the socket closes."""
        try:
            async for raw in self.ws:
                await self._handle(json.loads(raw))
        except Exception:
            if not self.closed:
                log.exception("realtime loop crashed (call %s)", self.ctx.call_sid)
        finally:
            self.closed = True
            await self.hooks.on_closed()

    async def _handle(self, ev: dict) -> None:
        t = ev.get("type", "")
        if t == "response.output_audio.delta":
            if self._speech_stopped_at is not None:
                log.info(
                    "latency call=%s speech_stopped->response.created=%.2fs ->first_audio=%.2fs",
                    self.ctx.call_sid,
                    (self._response_created_at or 0) - self._speech_stopped_at,
                    time.monotonic() - self._speech_stopped_at,
                )
                self._speech_stopped_at = None
            await self.hooks.on_audio_delta(ev["delta"], ev.get("item_id"))
        elif t == "input_audio_buffer.speech_started":
            await self.hooks.on_speech_started()
        elif t == "input_audio_buffer.speech_stopped":
            self._speech_stopped_at, self._response_created_at = time.monotonic(), None
        elif t == "response.created":
            self._response_created_at = time.monotonic()
        elif t == "conversation.item.input_audio_transcription.completed":
            self.ctx.transcript.append({"role": "caller", "text": ev.get("transcript", "").strip()})
        elif t == "response.output_audio_transcript.done":
            self.ctx.transcript.append({"role": "agent", "text": ev.get("transcript", "").strip()})
        elif t == "response.done":
            await self._on_response_done(ev["response"])
        elif t == "error":
            log.error("realtime error (call %s): %s", self.ctx.call_sid, ev.get("error"))

    async def _on_response_done(self, response: dict) -> None:
        calls = [o for o in response.get("output", []) if o.get("type") == "function_call"]
        if not calls:
            await self.hooks.on_turn_finished()
            return
        for call in calls:
            try:
                args = json.loads(call.get("arguments") or "{}")
            except json.JSONDecodeError:
                args = {}
            result = await asyncio.to_thread(execute, call["name"], args, self.ctx)
            await self.send(
                {
                    "type": "conversation.item.create",
                    "item": {"type": "function_call_output", "call_id": call["call_id"], "output": json.dumps(result)},
                }
            )
        # Let the model speak about the results (e.g. read out slots, or say goodbye before hangup).
        await self.send({"type": "response.create"})
