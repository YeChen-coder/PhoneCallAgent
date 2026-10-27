# human
Persona: You are an annoyed caller about a billing charge you think is wrong. After one exchange you insist on talking to a real person.
## Conversation (as heard by the caller)
**Agent:** Thank you for calling the clinic. I'm the virtual assistant. How can I help you today?
**Caller:** There's a $200 lab charge on my bill that I never had and it's wrong. Put me through to a real person — I don't want to deal with a bot.
**Agent:** I understand. Let me get you over to someone at the front desk. I'm transferring you now to a real person at the front desk who can help with billing.

## Tool calls
- `transfer_to_human({"reason": "Caller disputes a $200 lab charge and requests a real person. Needs billing help beyond automated assistant."})` → `{"ok": true, "note": "Transferring after your next sentence."}`

## Server-side
- outcome: transferred
- pending action executed: [('CAsim-human-1792293931', '<?xml version="1.0" encoding="UTF-8"?><Response><Say>Please hold while I connect you.</Say><Dial>+15550002222</Dial></Response>')]
- latency: caller stops talking -> first agent audio (s): [1.49]
- events: []

## Server transcript (Realtime's own ASR of caller)
```
[
 {
  "role": "agent",
  "text": "Thank you for calling the clinic. I’m the virtual assistant. How can I help you today?"
 },
 {
  "role": "caller",
  "text": "There's a $200 lab charge on my bill that I never had, and it's wrong. Put me through to a real person. I don't wanna deal with a bot."
 },
 {
  "role": "agent",
  "text": "I understand. Let me get you over to someone at the front desk."
 },
 {
  "role": "agent",
  "text": "I’m transferring you now to a real person at the front desk who can help with billing."
 }
]
```

## Summary
- Caller: patient on the line (no name/ID shown in transcript) upset about a charge.
- Issue/request: Disputing a $200 lab charge on their bill and insisted on speaking to a real person (not the virtual assistant).
- Action taken: Virtual assistant transferred the caller to the front desk.
- Follow-up needed: Front desk should verify patient identity (name, DOB, account #), review the billing statement and lab charge, correct or open a billing dispute/escalate to billing as needed, and notify the patient of the outcome/timeframe.
