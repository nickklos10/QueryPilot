"""03 - Anthropic tool use: wire QueryPilot's tool schemas into a tool-use loop.

Run:
    pip install -e ".[anthropic]"
    export ANTHROPIC_API_KEY=sk-ant-...
    python examples/03_anthropic_tool_use.py

QueryPilot exposes the same four safe tools to Claude via ``as_anthropic_tools()``.
Tool calls are dispatched with the built-in ``qp.handle_anthropic_tool_call``
helper, which routes each call back through QueryPilot's validation + execution.

The connection and tool-schema build happen BEFORE the API-key guard, so the
schema wiring is verifiable even without a key configured.
"""

from __future__ import annotations

import os
import sys
from typing import Any

from _common import demo_database_url

from querypilot import QueryPilot

MODEL = "claude-sonnet-5"
QUESTION = "Which customer has the highest ARR? Use the database."


def main() -> None:
    # Build the QueryPilot instance and tool schemas first -- no key needed here.
    qp = QueryPilot.connect(database_url=demo_database_url(), dialect="sqlite")
    tools = qp.as_anthropic_tools()
    print(f"Built {len(tools)} Anthropic tool schemas: "
          + ", ".join(tool["name"] for tool in tools))

    # API-key guard: everything below needs a live Anthropic key.
    if not os.environ.get("ANTHROPIC_API_KEY"):
        sys.exit("Set ANTHROPIC_API_KEY to run the live tool-use loop.")

    import anthropic

    client = anthropic.Anthropic()
    messages: list[dict[str, Any]] = [{"role": "user", "content": QUESTION}]

    for _ in range(6):  # bounded tool-use loop
        message = client.messages.create(
            model=MODEL,
            max_tokens=1024,
            tools=tools,
            messages=messages,
        )
        messages.append({"role": "assistant", "content": message.content})

        if message.stop_reason != "tool_use":
            text = "".join(
                block.text for block in message.content if block.type == "text"
            )
            print("\nAssistant:", text)
            return

        tool_results = []
        for block in message.content:
            if block.type != "tool_use":
                continue
            print(f"\n-> tool call: {block.name}({block.input})")
            output = qp.handle_anthropic_tool_call(block.name, block.input)
            tool_results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": str(output),
                }
            )
        messages.append({"role": "user", "content": tool_results})

    print("\nStopped after the tool-use loop budget was exhausted.")


if __name__ == "__main__":
    main()
