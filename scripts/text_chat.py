"""Talk to the receptionist by typing - same prompt, same tools, same DB, no phone needed.

    uv run python -m scripts.text_chat [--caller +15551230001]

Use this to iterate on the prompt and tool logic quickly; voice adds latency/ASR issues on top,
but the decision-making is the same.
"""

from __future__ import annotations

import argparse
import json
import uuid

from openai import OpenAI

from app.call_lifecycle import instructions_for
from app.config import get_settings
from app.db import init_db
from app.tools import CallContext, chat_completions_tools, execute


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--caller", default="+15551230001")
    args = ap.parse_args()

    init_db()
    s = get_settings()
    client = OpenAI(api_key=s.openai_api_key)
    ctx = CallContext(call_sid=f"text-{uuid.uuid4().hex[:8]}", transport="text", caller_number=args.caller)
    messages: list[dict] = [{"role": "system", "content": instructions_for(ctx)}]
    tools = chat_completions_tools()

    print("Type your side of the call. Ctrl+C to quit.\n")
    messages.append({"role": "user", "content": "(phone rings, caller connected)"})
    while True:
        resp = client.chat.completions.create(model=s.text_model, messages=messages, tools=tools)
        msg = resp.choices[0].message
        messages.append(msg.model_dump(exclude_none=True))
        if msg.tool_calls:
            for tc in msg.tool_calls:
                tool_args = json.loads(tc.function.arguments or "{}")
                result = execute(tc.function.name, tool_args, ctx)
                print(f"  [tool] {tc.function.name}({tool_args}) -> {result}")
                messages.append({"role": "tool", "tool_call_id": tc.id, "content": json.dumps(result)})
            continue
        print(f"Agent: {msg.content}\n")
        if ctx.pending_action:
            print(f"[call would now {ctx.pending_action}]")
            return
        try:
            messages.append({"role": "user", "content": input("You: ")})
        except (KeyboardInterrupt, EOFError):
            return


if __name__ == "__main__":
    main()
