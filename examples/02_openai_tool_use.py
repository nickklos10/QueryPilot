"""02 - OpenAI tool use: wire QueryPilot's tool schemas into a tool-use loop.

Run:
    pip install -e ".[openai]"
    export OPENAI_API_KEY=sk-...
    python examples/02_openai_tool_use.py

QueryPilot hands the model its safe tools (list_tables, describe_table,
search_schema, validate_sql, run_sql) via ``as_openai_tools()``. The model
decides which to call; ``qp.handle_tool_call`` routes every call back through
QueryPilot's validation + execution, so the model never touches the database
directly. Failures (bad SQL, blocked columns, unknown tables) come back as
``{"ok": false, "error": ..., "hints": [...]}`` payloads the model can act on,
so there is nothing to catch.

The QueryPilot connection and tool-schema build happen BEFORE the API-key guard,
so you can confirm the schema wiring even without a key configured.
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any

from _common import demo_database_url

from querypilot import QueryPilot

MODEL = "gpt-5.1"
QUESTION = "Which customer has the highest ARR? Use the database."


def main() -> None:
    # Build the QueryPilot instance and tool schemas first -- no key needed here.
    qp = QueryPilot.connect(database_url=demo_database_url(), dialect="sqlite")
    tools = qp.as_openai_tools()
    print(f"Built {len(tools)} OpenAI tool schemas: "
          + ", ".join(tool["function"]["name"] for tool in tools))

    # API-key guard: everything below needs a live OpenAI key.
    if not os.environ.get("OPENAI_API_KEY"):
        sys.exit("Set OPENAI_API_KEY to run the live tool-use loop.")

    from openai import OpenAI

    client = OpenAI()
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": qp.toolkit().instructions()},
        {"role": "user", "content": QUESTION},
    ]

    for _ in range(6):  # bounded tool-use loop
        response = client.chat.completions.create(
            model=MODEL,
            messages=messages,
            tools=tools,
        )
        message = response.choices[0].message
        messages.append(message.model_dump(exclude_none=True))

        if not message.tool_calls:
            print("\nAssistant:", message.content)
            return

        for tool_call in message.tool_calls:
            name = tool_call.function.name
            arguments = tool_call.function.arguments  # a JSON string; passed through as-is
            print(f"\n-> tool call: {name}({arguments})")
            output = qp.handle_tool_call(name, arguments)
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tool_call.id,
                    "content": json.dumps(output),
                }
            )

    print("\nStopped after the tool-use loop budget was exhausted.")


if __name__ == "__main__":
    main()
