"""02 - OpenAI tool use: wire QueryPilot's tool schemas into a tool-use loop.

Run:
    pip install -e ".[openai]"
    export OPENAI_API_KEY=sk-...
    python examples/02_openai_tool_use.py

QueryPilot hands the model four safe tools (ask_database, search_schema,
validate_sql, execute_sql) via ``as_openai_tools()``. The model decides which
to call; every call is routed back through QueryPilot's validation + execution,
so the model never touches the database directly.

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


def dispatch_tool(qp: QueryPilot, name: str, arguments: dict[str, Any]) -> Any:
    """Route an OpenAI tool call back through QueryPilot's safe surface."""
    if name == "ask_database":
        return qp.ask(str(arguments["question"])).model_dump()
    if name == "search_schema":
        return [match.model_dump() for match in qp.search_schema(str(arguments["query"]))]
    if name == "validate_sql":
        return qp.validate_sql(str(arguments["sql"])).model_dump()
    if name == "execute_sql":
        return qp.execute_sql(str(arguments["sql"])).model_dump()
    raise ValueError(f"Unknown tool: {name}")


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
    messages: list[dict[str, Any]] = [{"role": "user", "content": QUESTION}]

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
            arguments = json.loads(tool_call.function.arguments)
            print(f"\n-> tool call: {name}({arguments})")
            output = dispatch_tool(qp, name, arguments)
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tool_call.id,
                    "content": json.dumps(output, default=str),
                }
            )

    print("\nStopped after the tool-use loop budget was exhausted.")


if __name__ == "__main__":
    main()
