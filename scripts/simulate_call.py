"""Simulated phone calls against the REAL OpenAI Realtime API, without Twilio.

A synthetic caller (LLM persona + TTS) plays Twilio's role on /twilio/media-stream: it streams
8 kHz μ-law audio in real time, echoes playback marks like Twilio does, transcribes the agent's
audio, and decides what to say next. Everything server-side is the production code path.

    uv run python -m scripts.simulate_call                 # all scenarios
    uv run python -m scripts.simulate_call reschedule new  # selected scenarios

Outputs per call in data/sim_calls/<scenario>/: transcript.md, call.wav (caller left, agent right).
"""

from __future__ import annotations

import asyncio
import base64
import io
import json
import os
import socket
import sys
import threading
import time
import wave
from dataclasses import dataclass, field
from pathlib import Path

import uvicorn
import websockets
from openai import OpenAI

from app.config import get_settings
from app.db import CallLog, SessionLocal
from app.telephony import twilio_stream
from scripts.seed import seed

OUT_DIR = Path(os.environ.get("SIM_OUT", "data/sim_calls"))
FRAME = 160  # 20 ms @ 8 kHz μ-law
AGENT_TURN_SILENCE_S = 2.0
AGENT_FIRST_AUDIO_TIMEOUT_S = 25
MAX_CALLER_TURNS = 12
CALLER_TTS_MODEL = "gpt-4o-mini-tts"


@dataclass
class Scenario:
    key: str
    persona: str
    caller_number: str = "+15559990000"
    voice: str = "alloy"
    tts_instructions: str = "Speak like a regular person on a phone call."


SCENARIOS = [
    Scenario(
        "reschedule",
        "You are Alice Nguyen, born April 12 1985. You have a check-up booked soon and want to move it to "
        "next Wednesday afternoon, any doctor is fine. Give your info only when asked.",
        caller_number="+15551230001",
        voice="shimmer",
    ),
    Scenario(
        "new_patient",
        "You are Kevin Brown, born March 3 1979, never been to this clinic. You want an annual physical, "
        "preferably a weekday morning in the next two weeks. Your phone is the number you're calling from.",
        voice="echo",
    ),
    Scenario(
        "new_patient_patel",
        "You are Priya Shah, born November 22 1988, never been to this clinic. A friend recommended "
        "Dr. Michael Patel and you want a general check-up with him specifically next week. If he can't take "
        "you, reluctantly accept another doctor.",
        voice="sage",
    ),
    Scenario(
        "faq_refill",
        "You are Dan Miller, an existing patient. First ask where to park. Then ask for a refill of your "
        "metformin to be sent to Shoppers Drug Mart on King Street. Accept leaving a message.",
        voice="onyx",
    ),
    Scenario(
        "emergency",
        "You are a caller who has had crushing chest pain and shortness of breath for 20 minutes and "
        "wants to book the earliest appointment.",
        voice="ash",
    ),
    Scenario(
        "human",
        "You are an annoyed caller about a billing charge you think is wrong. After one exchange you "
        "insist on talking to a real person.",
        voice="coral",
    ),
    Scenario(
        "chinese",
        "你是王丽，1992年8月8日出生，第一次来这个诊所。你只说中文。你想预约打疫苗，下周任何时间都行。",
        voice="nova",
    ),
    Scenario(
        "zh_shen",
        "你是陈明，1975年3月15日出生，健康卡上的英文名是 Ming Chen。你第一次来这个诊所，只说普通话。"
        "朋友推荐了沈医生，你想约沈医生做个体检（general check-up），下周上午最好。你称呼医生为“沈医生”。",
        voice="onyx",
        tts_instructions="用自然的普通话说，像在打电话。",
    ),
    Scenario(
        "mixed_reschedule",
        "你是 Mei Lin（林美），1980年5月20日出生，是老病人，跟 Dr. Shen 约了一个 follow-up。你说话中英夹杂，"
        "像很多多伦多的华人一样，比如：“Hi，我想 reschedule 一下我跟 Dr. Shen 的 appointment，改到下个礼拜四"
        "afternoon 可以吗？” 被问到信息时也中英混着说。",
        caller_number="+15551230004",
        voice="shimmer",
        tts_instructions="Speak naturally, switching between Mandarin and English mid-sentence like a bilingual Torontonian.",
    ),
    Scenario(
        "cantonese",
        "你係黃志強，1968年11月2號出世，健康卡上面嘅英文名係 Chi Keung Wong。你第一次嚟呢間診所，只講廣東話，"
        "唔識講普通話。你想打流感針（vaccination），下個禮拜邊日都得。用廣東話口語回答，例如「我想打流感針呀」。",
        voice="ash",
        tts_instructions="用香港口音嘅廣東話講，好似打電話咁。Speak Cantonese, not Mandarin.",
    ),
]

# ---------------------------------------------------------------- G.711 μ-law (audioop is gone in 3.13)

_BIAS, _CLIP = 0x84, 32635


def _lin2ulaw(s: int) -> int:
    sign = 0x80 if s < 0 else 0
    s = min(abs(s), _CLIP) + _BIAS
    exp, mask = 7, 0x4000
    while exp > 0 and not s & mask:
        exp, mask = exp - 1, mask >> 1
    return ~(sign | (exp << 4) | ((s >> (exp + 3)) & 0x0F)) & 0xFF


def _ulaw2lin(u: int) -> int:
    u = ~u & 0xFF
    s = ((((u & 0x0F) << 3) + _BIAS) << ((u >> 4) & 7)) - _BIAS
    return -s if u & 0x80 else s


ULAW2LIN = [_ulaw2lin(i) for i in range(256)]


def pcm24k_to_ulaw8k(pcm: bytes) -> bytes:
    samples = memoryview(pcm).cast("h")
    out = bytearray()
    for i in range(0, len(samples) - 2, 3):
        out.append(_lin2ulaw((samples[i] + samples[i + 1] + samples[i + 2]) // 3))
    return bytes(out)


def trim_silence(ulaw: bytes, threshold: int = 300) -> bytes:
    """Drop TTS trailing silence so latency is measured from the real end of speech."""
    end = len(ulaw)
    while end > 0 and abs(ULAW2LIN[ulaw[end - 1]]) < threshold:
        end -= 1
    return ulaw[: end + 800]  # keep 100 ms tail


def ulaw_to_wav(ulaw: bytes) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(8000)
        w.writeframes(b"".join(ULAW2LIN[b].to_bytes(2, "little", signed=True) for b in ulaw))
    return buf.getvalue()


# ---------------------------------------------------------------- fake Twilio caller


@dataclass
class FakeTwilioCaller:
    ws: websockets.ClientConnection
    stream_sid: str = "MZsim"
    outgoing: bytearray = field(default_factory=bytearray)
    agent_turn_audio: bytearray = field(default_factory=bytearray)
    caller_track: bytearray = field(default_factory=bytearray)
    agent_track: bytearray = field(default_factory=bytearray)
    last_agent_audio_at: float = 0.0
    turn_first_audio_at: float = 0.0
    play_until: float = 0.0
    media_ts: int = 0
    saw_final_mark: bool = False
    closed: bool = False
    events: list[str] = field(default_factory=list)

    async def sender(self) -> None:
        """Stream caller audio (or silence) every 20 ms, like a real phone line."""
        start = time.monotonic()
        n = 0
        while not self.closed:
            chunk = bytes(self.outgoing[:FRAME])
            del self.outgoing[:FRAME]
            chunk = chunk.ljust(FRAME, b"\xff")
            self.caller_track += chunk
            if len(self.agent_track) < len(self.caller_track):
                self.agent_track += b"\xff" * (len(self.caller_track) - len(self.agent_track))
            try:
                await self.ws.send(
                    json.dumps({"event": "media", "streamSid": self.stream_sid, "media": {"timestamp": str(self.media_ts), "payload": base64.b64encode(chunk).decode()}})
                )
            except websockets.ConnectionClosed:
                self.closed = True
                return
            n += 1
            self.media_ts += 20
            await asyncio.sleep(max(0, start + n * 0.02 - time.monotonic()))

    async def receiver(self) -> None:
        try:
            async for raw in self.ws:
                msg = json.loads(raw)
                ev = msg.get("event")
                if ev == "media":
                    audio = base64.b64decode(msg["media"]["payload"])
                    if not self.agent_turn_audio:
                        self.turn_first_audio_at = time.monotonic()
                    self.agent_turn_audio += audio
                    # place agent audio on the timeline where it would actually be heard
                    now_pos = len(self.caller_track)
                    if len(self.agent_track) < now_pos:
                        self.agent_track += b"\xff" * (now_pos - len(self.agent_track))
                    self.agent_track += audio
                    now = time.monotonic()
                    self.play_until = max(self.play_until, now) + len(audio) / 8000
                    self.last_agent_audio_at = now
                elif ev == "mark":
                    name = msg["mark"]["name"]
                    asyncio.create_task(self._echo_mark_after_playback(name))
                elif ev == "clear":
                    self.events.append("agent audio cleared (barge-in)")
                    self.play_until = time.monotonic()
        except websockets.ConnectionClosed:
            pass
        self.closed = True

    async def _echo_mark_after_playback(self, name: str) -> None:
        await asyncio.sleep(max(0, self.play_until - time.monotonic()))
        if name == "final":
            self.saw_final_mark = True
        try:
            await self.ws.send(json.dumps({"event": "mark", "streamSid": self.stream_sid, "mark": {"name": name}}))
        except websockets.ConnectionClosed:
            pass

    async def wait_agent_turn(self) -> bytes:
        """Return the agent's audio for one turn (ends after AGENT_TURN_SILENCE_S of no new audio)."""
        t0 = time.monotonic()
        while not self.agent_turn_audio and not self.closed:
            if time.monotonic() - t0 > AGENT_FIRST_AUDIO_TIMEOUT_S:
                return b""
            await asyncio.sleep(0.05)
        while not self.closed and not self.saw_final_mark:
            idle = time.monotonic() - max(self.last_agent_audio_at, self.play_until)
            if idle > AGENT_TURN_SILENCE_S:
                break
            await asyncio.sleep(0.05)
        audio = bytes(self.agent_turn_audio)
        self.agent_turn_audio.clear()
        return audio

    async def say(self, ulaw: bytes) -> None:
        self.outgoing += ulaw
        while self.outgoing and not self.closed:
            await asyncio.sleep(0.05)


# ---------------------------------------------------------------- caller brain


class CallerBrain:
    def __init__(self, client: OpenAI, scenario: Scenario):
        self.client, self.scenario = client, scenario
        self.history: list[dict] = [
            {
                "role": "system",
                "content": "You are role-playing a person phoning a medical clinic. The other side is the clinic's "
                f"phone assistant.\n{scenario.persona}\nSpeak like a real caller: short, natural, one or two "
                "sentences. If the assistant asks you to spell or repeat something, do it. When your goal is handled "
                "(or you are told to hang up / are being transferred) and the assistant says goodbye, reply with "
                "exactly <HANGUP>.",
            }
        ]

    def transcribe(self, ulaw: bytes) -> str:
        if len(ulaw) < 1600:  # < 0.2 s
            return ""
        f = ("agent.wav", ulaw_to_wav(ulaw), "audio/wav")
        return self.client.audio.transcriptions.create(model=get_settings().transcribe_model, file=f).text.strip()

    def reply(self, agent_text: str) -> str:
        self.history.append({"role": "user", "content": agent_text or "(silence)"})
        r = self.client.chat.completions.create(model=get_settings().text_model, messages=self.history)
        text = (r.choices[0].message.content or "").strip()
        self.history.append({"role": "assistant", "content": text})
        return text

    def tts(self, text: str) -> bytes:
        r = self.client.audio.speech.create(
            model=CALLER_TTS_MODEL, voice=self.scenario.voice, input=text, response_format="pcm",
            instructions=self.scenario.tts_instructions,
        )
        return trim_silence(pcm24k_to_ulaw8k(r.read()))


# ---------------------------------------------------------------- orchestration


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def start_server() -> int:
    port = _free_port()
    from app.main import app

    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    while not server.started:
        time.sleep(0.05)
    return port


async def run_scenario(port: int, client: OpenAI, sc: Scenario, actions: list) -> dict:
    call_sid = f"CAsim-{sc.key}-{int(time.time())}"
    brain = CallerBrain(client, sc)
    lines: list[str] = []
    timings: list[float] = []
    async with websockets.connect(f"ws://127.0.0.1:{port}/twilio/media-stream", max_size=None) as ws:
        caller = FakeTwilioCaller(ws)
        await ws.send(json.dumps({"event": "connected"}))
        await ws.send(
            json.dumps(
                {
                    "event": "start",
                    "start": {
                        "streamSid": caller.stream_sid,
                        "callSid": call_sid,
                        "customParameters": {"token": twilio_stream._stream_token(call_sid), "caller": sc.caller_number},
                    },
                }
            )
        )
        tasks = [asyncio.create_task(caller.sender()), asyncio.create_task(caller.receiver())]
        t_said = None
        for _turn in range(MAX_CALLER_TURNS + 1):
            audio = await caller.wait_agent_turn()
            if t_said and audio:
                # caller stopped talking -> first agent audio byte (includes VAD end-of-turn wait)
                timings.append(round(caller.turn_first_audio_at - t_said, 2))
            agent_text = await asyncio.to_thread(brain.transcribe, audio)
            lines.append(f"**Agent:** {agent_text or '(no audio)'}")
            print(f"  agent : {agent_text}")
            if caller.saw_final_mark or caller.closed:
                break
            reply = await asyncio.to_thread(brain.reply, agent_text)
            if "<HANGUP>" in reply:
                lines.append("*(caller hangs up)*")
                break
            print(f"  caller: {reply}")
            lines.append(f"**Caller:** {reply}")
            await caller.say(await asyncio.to_thread(brain.tts, reply))
            t_said = time.monotonic()
        else:
            lines.append("*(turn limit reached)*")
        await asyncio.sleep(1)
        try:
            await ws.send(json.dumps({"event": "stop"}))
        except websockets.ConnectionClosed:
            pass
        caller.closed = True
        for t in tasks:
            t.cancel()

    log = None
    for _ in range(900):  # server persists log after the LLM summary, which is slow on long calls
        with SessionLocal() as db:
            log = db.query(CallLog).filter_by(call_sid=call_sid).first()
        if log:
            break
        await asyncio.sleep(0.1)

    out = OUT_DIR / sc.key
    out.mkdir(parents=True, exist_ok=True)
    with wave.open(str(out / "call.wav"), "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(8000)
        n = max(len(caller.caller_track), len(caller.agent_track))
        c, a = caller.caller_track.ljust(n, b"\xff"), caller.agent_track.ljust(n, b"\xff")
        w.writeframes(
            b"".join(
                ULAW2LIN[c[i]].to_bytes(2, "little", signed=True) + ULAW2LIN[a[i]].to_bytes(2, "little", signed=True)
                for i in range(n)
            )
        )
    tool_calls = json.loads(log.tool_calls) if log and log.tool_calls else []
    report = [
        f"# {sc.key}\n",
        f"Persona: {sc.persona}\n",
        "## Conversation (as heard by the caller)\n",
        *[f"{ln}\n" for ln in lines],
        "\n## Tool calls\n",
        *[f"- `{t['name']}({json.dumps(t['args'], ensure_ascii=False)})` → `{json.dumps(t['result'], ensure_ascii=False)[:400]}`\n" for t in tool_calls],
        f"\n## Server-side\n- outcome: {log.outcome if log else 'NO CALL LOG'}\n",
        f"- pending action executed: {[a for a in actions if a[0] == call_sid]}\n",
        f"- latency: caller stops talking -> first agent audio (s): {timings}\n",
        f"- events: {caller.events}\n",
        f"\n## Server transcript (Realtime's own ASR of caller)\n```\n{json.dumps(json.loads(log.transcript), ensure_ascii=False, indent=1) if log and log.transcript else ''}\n```\n",
        f"\n## Summary\n{log.summary if log else ''}\n",
    ]
    (out / "transcript.md").write_text("".join(report), encoding="utf-8")
    return {"scenario": sc.key, "outcome": log.outcome if log else None, "tools": [t["name"] for t in tool_calls], "gaps": timings}


async def main(selected: list[str]) -> None:
    s = get_settings()
    if not s.openai_api_key:
        sys.exit("Put OPENAI_API_KEY in .env first.")
    seed()
    actions: list = []
    # No real Twilio call to redirect: record hangup/transfer instead.
    twilio_stream._twilio_redirect = lambda sid, twiml: actions.append((sid, twiml))
    port = start_server()
    client = OpenAI(api_key=s.openai_api_key)
    results = []
    for sc in SCENARIOS:
        if selected and sc.key not in selected:
            continue
        print(f"\n=== {sc.key} ===")
        try:
            results.append(await run_scenario(port, client, sc, actions))
        except Exception as e:  # keep going with other scenarios
            print(f"  FAILED: {e!r}")
            results.append({"scenario": sc.key, "error": repr(e)})
    print("\n" + json.dumps(results, indent=1, ensure_ascii=False))
    (OUT_DIR / "results.json").write_text(json.dumps(results, indent=1, ensure_ascii=False), encoding="utf-8")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # Windows consoles default to GBK/cp1252
    asyncio.run(main(sys.argv[1:]))
