# Clinic Phone Agent — an AI phone receptionist for small clinics (skeleton)

**English** | [中文](README_zh.md)

Answer the call → answer common clinic questions → book / reschedule / cancel appointments → take a message or transfer to a human when it can't handle something → write a post-call summary for the front desk.

> This is a **working skeleton with tests**, not optimized yet. Read sections 1–3 first to see how the industry approaches this, then section 4 for how to run it.

---

## 1. How the industry does it (three routes)

The problem is well defined, and the industry basically has three ways to solve it:

| Route | Examples | What you build | Best for |
|---|---|---|---|
| **A. Buy an off-the-shelf product** | Vertical SaaS products, "AI receptionist for dental/medical" | Configuration + integration | A clinic using it directly, with no tech team |
| **B. Hosted voice-agent platform** | Vapi, Retell AI, Bland, Synthflow | Write the prompt; expose "check availability / book" as webhooks the platform calls | Most common for agencies / contractors; fastest to launch |
| **C. Build it yourself** | Twilio (telephony) + OpenAI Realtime (voice model), or the Pipecat / LiveKit Agents frameworks | You own audio bridging, tool calling and logging | When you want control over data, cost and compliance, or are building a product |

Whichever route you pick, **the architecture is the same**; the only difference is which layers you build and which you buy:

```
Phone network (PSTN)
   │
Carrier / phone number (Twilio)    ← handled by the platform in route B
   │  audio stream
Voice model (speech-to-speech: OpenAI Realtime;
         or a three-stage pipeline: STT → LLM → TTS)   ← handled by the platform in route B
   │  tool calls (function calling)
Your business backend: scheduling rules, patients,
appointments, messages             ← you build this layer on every route
   │
The clinic's existing system (EHR / booking software) or our own DB
```

**This repo takes route C**, for these reasons:

- You've already used OpenAI Realtime, so it's what you know best.
- The interview is about "can you build it", and building it yourself proves you understand every layer. If you later need to ship fast, switching to B (Vapi/Retell) only replaces the voice layer — **`app/scheduling.py` + `app/tools.py` stay exactly as they are**, because they're already plain functions a webhook can call.
- The scale is small: one or two hundred patients, roughly a dozen to a few dozen calls a day. **One small server + SQLite/Postgres is enough** — no queues, no K8s, no microservices.

### About "one or two hundred clients" — be sure to clarify this in the interview

- If it means **one clinic with one or two hundred patients** → that's the single-tenant setup this repo has now.
- If it means **the company serves one or two hundred clinics** → it needs to be multi-tenant: turn `clinic.json` into a `clinics` table in the database, and look up each clinic's config / prompt / data by the number that was called (Twilio's `To` parameter). The code change is small, but the architecture discussion shifts focus.

---

## 2. About RAG: **not needed** at this scale

A small clinic's "knowledge" is its address, parking, opening hours, cancellation policy, insurance… a few pages, a few thousand words.
**Putting all of it directly in the system prompt** beats RAG: no retrieval latency (on a phone call every 300 ms is audible), nothing gets missed in retrieval, and it's prompt-cache friendly.

`app/knowledge.py` already switches automatically: when the knowledge base is under 12k characters it's inlined into the prompt; above that, the model automatically gets a `search_clinic_info` tool (a tiny BM25, zero dependencies). Only at hundreds of documents would we move to embeddings + pgvector.

**What really needs "retrieval" is structured data** — open slots, appointments, patients — and that's handled by **tool calls that query the database**, not RAG.

---

## 3. Architecture and key design decisions (talk about these in the interview)

```
Incoming call ──> Twilio number ──POST /twilio/voice──> returns TwiML <Connect><Stream>
                │
                └── wss /twilio/media-stream  (G.711 μ-law 8kHz)
                          │   app/telephony/twilio_stream.py
                          │   (audio bridge + barge-in handling)
                          │
                          └── wss OpenAI Realtime  (app/realtime_agent.py:
                                     │   session config, tool loop, transcription)
                                     │ function call
                                     v
                            app/tools.py  (tool definitions + dispatch; identity bound to the call context)
                                     │
                            app/scheduling.py  (pure business rules, no LLM)
                                     │
                            app/db.py  (SQLite → Postgres)
Call ends ──> app/call_lifecycle.py: saves transcript + tool-call log + LLM summary → /admin/calls for the front desk
```

### Design principles

1. **The model only "proposes"; the code "executes".** Opening hours, minimum notice, double-booking prevention, and whether the provider offers that service are all hard-checked in `scheduling.py`. Even if the model gets the time wrong, the booking won't go through. Conflicts are checked again at write time (the slot may have just been taken).
2. **Identity is bound to the call, not passed as a parameter.** After `verify_patient` (name + date of birth) succeeds, the patient_id is stored in `CallContext`; the tools **have no patient_id parameter at all**, so no matter how a caller "persuades" the model, they can't touch someone else's appointments.
3. **There's always a fallback.** Can't handle it → `take_message`; caller asks for a person / is upset / gets stuck twice → `transfer_to_human` to the front desk; medical emergency → tell them to hang up and call 911 (this is the first safety rule in the prompt).
4. **No medical advice, no test results.**
5. **Finish speaking before hanging up.** `end_call` / transfers don't execute immediately: we wait until the model has finished saying goodbye and Twilio sends back a `mark` event confirming the audio has played, and only then hang up or transfer.
6. **Barge-in (interruptions).** As soon as the caller starts talking: clear the audio Twilio hasn't played yet + send `conversation.item.truncate` to tell the model "you only got this far", so the model doesn't assume the caller heard the part that was never played.
7. **Text and voice share the same tools.** `scripts/text_chat.py` uses the same prompt, tools and database, so you can test the whole business flow by typing instead of placing a call every time.

### Two ways to connect the phone line (both implemented)

- **Default: Twilio Media Streams** (`twilio_stream.py`) — audio flows through our server. Controllable, debuggable, best documented.
- **Optional: OpenAI SIP** (`openai_sip.py`) — a Twilio Elastic SIP Trunk connects directly to `sip:<project_id>@sip.api.openai.com`; OpenAI sends a `realtime.call.incoming` webhook, we accept the call and attach a sideband websocket that only handles tool calls. One fewer hop and lower latency, but tighter lock-in to OpenAI.

---

## 4. How to run it

```bash
uv sync
```

```bash
cp .env.example .env
```

Fill in `OPENAI_API_KEY` (the Twilio entries can stay empty for now).

```bash
uv run python -m scripts.seed
```

**Test by typing first, no phone needed:**

```bash
uv run python -m scripts.text_chat
```

Try: "I'm Alice Nguyen, born 1985-04-12, and I'd like to move tomorrow's appointment to next Wednesday afternoon."

**Run the tests:**

```bash
uv run pytest -q
```

**Connect a real phone line:**

1. Start the server:
   ```bash
   uv run uvicorn app.main:app --port 8000
   ```
2. Expose it over public HTTPS, then put the address in `PUBLIC_BASE_URL` in `.env`:
   ```bash
   ngrok http 8000
   ```
3. Twilio Console → buy a number → Voice "A call comes in" → Webhook `POST https://<your-domain>/twilio/voice`.
4. Call the number.

**Front-desk endpoints** (`Authorization: Bearer <ADMIN_TOKEN>`): `GET /admin/appointments?day=YYYY-MM-DD`, `/admin/messages`, `/admin/calls`.

---

## 5. Code map

| File | Purpose |
|---|---|
| `clinic.json` | Clinic config: time zone, opening hours, holidays, services and durations, providers, transfer number |
| `knowledge/*.md` | FAQ / policies (the clinic just edits the markdown) |
| `app/scheduling.py` | Scheduling rules: find open slots, book, reschedule, cancel, look up / register patients |
| `app/tools.py` | The model's 10 tools + dispatcher + `CallContext` |
| `app/prompts.py` | System prompt (role, tone, safety rules, flow; current time and caller number injected per call) |
| `app/realtime_agent.py` | Transport-agnostic Realtime session handling: config, tool loop, transcription |
| `app/telephony/twilio_stream.py` | Twilio ⇄ OpenAI audio bridge, barge-in, finish-before-hangup, signature validation |
| `app/telephony/openai_sip.py` | Direct SIP connection |
| `app/call_lifecycle.py` | Persist call records + post-call LLM summary |
| `app/admin.py` | Front-desk viewing endpoints |
| `scripts/text_chat.py` | Text-based testing |
| `tests/` | Scheduling rules, tool permissions, TwiML, plus an end-to-end bridge test against a fake OpenAI server |

---

## 6. Simulated call testing (2026-10-17, real OpenAI Realtime)

`scripts/simulate_call.py`: a "fake caller" (an LLM playing a role + TTS-synthesized voice) stands in for Twilio and calls `/twilio/media-stream` in real phone format (8kHz μ-law, real-time streaming, playback marks sent back). The server side runs the production code. Each call writes `transcript.md` and a two-channel recording `call.wav` under `data/sim_calls/<scenario>/`.

```bash
uv run python -m scripts.simulate_call
```

Four rounds in total; the problems found and fixed along the way:

| Problem | Cause | Fix |
|---|---|---|
| After the caller finished, the agent took 4–8 seconds to respond | `semantic_vad` is too slow to detect end-of-speech on phone-quality audio | Switched to `server_vad` at 600 ms; latency is now a steady 1.1–2 s; configurable via `VAD_TYPE` |
| Still "transferring to a human" after hours (nobody would answer) | Opening hours weren't checked before transferring | `transfer_to_human` checks opening hours in code; when closed, the agent takes a message instead |
| A new patient was booked with Dr. Chen, who isn't accepting new patients | The rule only existed in the FAQ | Added `accepting_new_patients` to `clinic.json`, enforced in code for availability lookups and bookings |
| Refill messages had no date of birth, so the front desk couldn't find the chart | Relied on the prompt alone; the model followed it inconsistently | Added `category` to `take_message`; refill, billing and test-result messages are rejected without a date of birth |
| Chinese names: 丽 / 李 / 力 sound alike and got mixed up, registered as "李王" | Homophones can't be told apart over the phone | Ask for the English name on the health card instead and read it back letter by letter |
| In Chinese calls, October was said as November | The model slipped when translating English month names | Tools also return a numeric `date` |
| Greeting left out the clinic name; said "one moment" three times in one turn; read prompt rules aloud | Prompt wording | Revised the prompt |
| Hung up right after booking without asking "anything else?" | Prompt wording | Revised the prompt: ask if there's anything else first, then call `end_call` right after saying goodbye |
| Said "transferring you now", then backtracked with "the clinic is closed" | Prompt wording | Only announce the transfer after the tool succeeds |

### Multilingual (2026-10-18)

The sample clinic's providers are made up: Dr. Wei Shen (English, Mandarin, Cantonese), Dr. Patel (English only, not accepting new patients), Nurse Wong (English, Cantonese); the front desk speaks English and Mandarin. To use a real roster, only `clinic.json` needs to change: `name_zh`, `aliases`, `languages`, `front_desk_languages`.

| Scenario | Result | Problem found → fix |
|---|---|---|
| New Mandarin-speaking patient asks for "沈医生" (Dr. Shen) | ✅ Booked; front desk got an English summary | Matching was by English name only, so "沈医生" wasn't found → added `aliases` to `clinic.json`; code now matches both Chinese and English names |
| Reschedule in mixed Chinese/English (existing patient Mei Lin) | ✅ Passed on the second run | First run heard "Mei" as "May" and verification failed → allow first-name spelling differences when last name + DOB + caller number all match; reversed name order is recognized too. A wrong DOB is never accepted |
| New Cantonese-speaking patient wants a flu shot | ✅ All Cantonese on the second run | First run mixed in Mandarin → prompt now states explicitly that "Cantonese is not Mandarin" |
| English caller asks for Dr. Patel, who isn't taking new patients | ✅ Blocked in code; offered Dr. Shen instead | — |
| Chinese-speaking patients heard English first when the call connected | ✅ | Switched to a bilingual Chinese/English greeting |

Also: summaries and messages for the front desk are always in English, noting which language the caller spoke.
Still worth watching: Realtime's Cantonese is clearly weaker than its Mandarin, so with many Cantonese-speaking patients it needs focused testing with real people; it occasionally switches back to English near the end of a call.

**Not yet solved / things to watch:**

- Synthetic voices are too clean. Real calls have noise, accents and mid-sentence pauses, so ASR accuracy and VAD false triggers will definitely be worse than here. Must test with a real number and real people before launch.
- Chaining several tools in one turn (verify identity → register → check availability) adds about 1 second per round, up to 3–4 seconds. Could merge tools or have the model call them in parallel.
- Chinese speech occasionally uses the wrong word, e.g. saying something else instead of "疫苗" (vaccine) — though it may also be the test script's own speech recognition mishearing.
- Rules enforced only by the prompt are unreliable; anything that must always hold has to be enforced in code.

## 7. The genuinely hard parts (not in the skeleton yet — bring them up proactively in the interview)

1. **Integrating with the clinic's existing booking system** — the biggest unknown and the most likely cause of schedule overruns. The clinic is probably already using some EHR / booking software (common in Canada: Jane App, OSCAR, Accuro; in the US: Athena, eClinicalWorks, Dentrix…). Either it has an API (swap the DB reads/writes in `scheduling.py` for calls to that API), or it doesn't (then our DB is the source of truth and the front desk views it in `/admin`, or we build a sync). **This is the first question to ask.**
2. **Compliance.** US: HIPAA — you need a BAA with both OpenAI and Twilio (both offer one, but through an enterprise process); Ontario, Canada: PHIPA. Call recordings and transcripts are PHI: they need encrypted storage, a retention period and access controls, and callers must be told before recording.
3. **Recognition accuracy for names / dates of birth.** Phone audio is 8kHz, so names are easily misheard. Common approaches: ask the caller to spell it and read it back, or first match candidates by caller number and then confirm with date of birth.
4. **Latency.** The goal is to start responding < 1 second after the caller finishes. Speech-to-speech (Realtime) has an edge here over the three-stage STT→LLM→TTS pipeline; deploy the server in a region close to OpenAI / Twilio (US East).
5. **Evaluation.** Prepare a set of scenario scripts (booking, rescheduling, misheard names, medical questions, emergencies, asking for a person, speaking Chinese…) and use the `text_chat` logic for automated regression tests; after launch, spot-check call summaries weekly.
6. **Production hardening.** SQLite → Postgres, with an exclusion constraint to guarantee no double-booking; replace `/admin` with real login; deploy (one small machine on Fly.io / Render / AWS is enough); monitor Twilio and OpenAI error rates.
7. **Outbound calls** (appointment reminders, confirmations) — a natural next step: place outbound calls with Twilio and connect them to the same agent.

## 8. Questions to ask the client in the interview

- What booking / medical records system do you use now? Does it have an API?
- Roughly how many calls a day? When are the peaks? How are after-hours calls handled today?
- Roughly what share of calls are bookings, reschedules, questions, and prescription refills?
- Which languages do you need?
- Are you in the US or Canada? What are your compliance requirements (BAA / recording disclosure)?
- Does "one or two hundred clients" mean patients, or the number of clinics you serve?
- Which number should calls be transferred to? What happens when nobody picks up?

## References

- OpenAI Realtime conversations: https://developers.openai.com/api/docs/guides/realtime-conversations
- OpenAI Realtime SIP: https://developers.openai.com/api/docs/guides/realtime-sip
- Twilio Elastic SIP Trunking × OpenAI: https://www.twilio.com/en-us/blog/developers/tutorials/product/openai-realtime-api-elastic-sip-trunking
- Overview of AI receptionist architecture (freeCodeCamp): https://www.freecodecamp.org/news/how-ai-receptionists-work-the-architecture-behind-ai-phone-agents/
