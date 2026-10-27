"""End-to-end test of the Twilio <-> Realtime bridge against a fake OpenAI Realtime server.

Simulates: greeting audio -> model calls end_call -> bridge returns tool output -> model says goodbye
-> bridge sends a 'final' mark -> Twilio echoes it -> bridge hangs up via Twilio REST -> call log saved.
"""

import asyncio
import json
import threading
import time

import websockets
from fastapi.testclient import TestClient

from app.config import get_settings
from app.db import CallLog, SessionLocal
from app.telephony import twilio_stream


def _start_fake_openai() -> tuple[int, list[dict]]:
    received: list[dict] = []
    ready = threading.Event()
    port_box: list[int] = []

    async def handler(ws):
        async def recv_until(event_type):
            while True:
                ev = json.loads(await ws.recv())
                received.append(ev)
                if ev["type"] == event_type:
                    return ev

        await recv_until("session.update")
        await recv_until("response.create")  # greeting
        await ws.send(json.dumps({"type": "response.output_audio.delta", "item_id": "i1", "delta": "AAAA"}))
        await ws.send(json.dumps({"type": "response.done", "response": {"output": [{"type": "message"}]}}))
        await ws.send(
            json.dumps(
                {
                    "type": "response.done",
                    "response": {
                        "output": [{"type": "function_call", "name": "end_call", "call_id": "c1", "arguments": '{"reason":"done"}'}]
                    },
                }
            )
        )
        await recv_until("response.create")  # after tool output
        await ws.send(json.dumps({"type": "response.output_audio_transcript.done", "transcript": "Goodbye!"}))
        await ws.send(json.dumps({"type": "response.output_audio.delta", "item_id": "i2", "delta": "BBBB"}))
        await ws.send(json.dumps({"type": "response.done", "response": {"output": [{"type": "message"}]}}))
        try:
            await ws.wait_closed()
        except Exception:
            pass

    def run():
        async def main():
            async with websockets.serve(handler, "127.0.0.1", 0) as server:
                port_box.append(server.sockets[0].getsockname()[1])
                ready.set()
                await asyncio.Future()

        asyncio.run(main())

    threading.Thread(target=run, daemon=True).start()
    ready.wait(5)
    return port_box[0], received


def test_bridge_tool_call_and_hangup(monkeypatch):
    port, received = _start_fake_openai()
    monkeypatch.setattr(get_settings(), "openai_realtime_url", f"ws://127.0.0.1:{port}")
    redirects: list[tuple[str, str]] = []
    monkeypatch.setattr(twilio_stream, "_twilio_redirect", lambda sid, twiml: redirects.append((sid, twiml)))

    from app.main import app

    with TestClient(app) as client, client.websocket_connect("/twilio/media-stream") as ws:
        ws.send_json({"event": "connected"})
        ws.send_json(
            {
                "event": "start",
                "start": {
                    "streamSid": "MZ1",
                    "callSid": "CA42",
                    "customParameters": {"token": twilio_stream._stream_token("CA42"), "caller": "+15551230001"},
                },
            }
        )
        ws.send_json({"event": "media", "media": {"timestamp": "20", "payload": "//8="}})
        payloads = []
        while True:
            msg = ws.receive_json()
            if msg["event"] == "media":
                payloads.append(msg["media"]["payload"])
            if msg["event"] == "mark" and msg["mark"]["name"] == "final":
                break
        ws.send_json({"event": "mark", "mark": {"name": "final"}})
        for _ in range(50):
            if redirects:
                break
            time.sleep(0.05)
        ws.send_json({"event": "stop"})
        # Wait for the call log inside the context; leaving it would cancel the endpoint task.
        for _ in range(100):
            with SessionLocal() as db:
                log = db.query(CallLog).filter_by(call_sid="CA42").first()
            if log:
                break
            time.sleep(0.05)

    assert payloads == ["AAAA", "BBBB"]
    assert redirects and "<Hangup" in redirects[0][1]
    session = next(e for e in received if e["type"] == "session.update")["session"]
    assert session["audio"]["input"]["format"]["type"] == "audio/pcmu"
    assert any(e["type"] == "input_audio_buffer.append" for e in received)
    assert any(e["type"] == "conversation.item.create" and e["item"]["call_id"] == "c1" for e in received)

    assert log and log.outcome == "completed" and "Goodbye" in log.transcript
