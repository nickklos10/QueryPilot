from __future__ import annotations

import sys
import types

from querypilot import QueryPilot
from querypilot.core.types import DatabaseSchema, GeneratedSQL
from querypilot.generation.llm import (
    AnthropicSQLGenerator,
    OpenAICompatibleSQLGenerator,
    OpenAISQLGenerator,
)
from querypilot.generation.prompt_builder import build_sql_generation_prompt


VALID_SQL_JSON = (
    '{"sql": "SELECT customer_name, revenue FROM customers", '
    '"explanation": "Ranks customers by revenue."}'
)
INVALID_SQL_JSON = (
    '{"sql": "SELECT * FROM missing_table", "explanation": "First attempt."}'
)
REPAIRED_SQL_JSON = (
    '{"sql": "SELECT customer_name, revenue FROM customers ORDER BY revenue DESC", '
    '"explanation": "Repaired query."}'
)


class FakeOpenAIResponses:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return type(
            "Response",
            (),
            {
                "output_text": (
                    '{"sql": "SELECT customer_name, revenue FROM customers", '
                    '"explanation": "Ranks customers by revenue."}'
                )
            },
        )()


class FakeOpenAIClient:
    def __init__(self) -> None:
        self.responses = FakeOpenAIResponses()


class FakeAnthropicMessages:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        text_block = type(
            "TextBlock",
            (),
            {
                "type": "text",
                "text": (
                    '{"sql": "SELECT customer_name, revenue FROM customers", '
                    '"explanation": "Ranks customers by revenue."}'
                ),
            },
        )()
        return type("Message", (), {"content": [text_block]})()


class FakeAnthropicClient:
    def __init__(self) -> None:
        self.messages = FakeAnthropicMessages()


def _chat_completion(content: str):
    message = type("Message", (), {"content": content})()
    choice = type("Choice", (), {"message": message})()
    return type("ChatCompletion", (), {"choices": [choice]})()


class FakeChatCompletions:
    def __init__(self, contents: list[str]) -> None:
        self._contents = contents
        self.calls: list[dict] = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        index = min(len(self.calls) - 1, len(self._contents) - 1)
        return _chat_completion(self._contents[index])


class FakeChat:
    def __init__(self, contents: list[str]) -> None:
        self.completions = FakeChatCompletions(contents)


class FakeOpenAICompatibleClient:
    def __init__(self, contents: list[str]) -> None:
        self.chat = FakeChat(contents)


def _install_fake_openai_module(monkeypatch) -> None:
    """Stub the `openai` import so the default-client path is testable offline.

    CI installs only `.[dev,eval]` — the openai package is absent — and tests
    must never construct a real network client anyway.
    """

    class _FakeOpenAI:
        def __init__(self, base_url=None, api_key=None):
            self.base_url = base_url
            self.api_key = api_key

    module = types.ModuleType("openai")
    module.OpenAI = _FakeOpenAI
    monkeypatch.setitem(sys.modules, "openai", module)


class RepairingGenerator:
    def __init__(self) -> None:
        self.repair_calls = 0

    def generate(self, question: str, schema: DatabaseSchema, max_rows: int) -> GeneratedSQL:
        return GeneratedSQL(
            question=question,
            sql="SELECT * FROM missing_table",
            explanation="Initial candidate.",
        )

    def repair(
        self,
        question: str,
        schema: DatabaseSchema,
        max_rows: int,
        previous_sql: str,
        validation,
    ) -> GeneratedSQL:
        self.repair_calls += 1
        return GeneratedSQL(
            question=question,
            sql="SELECT customer_name, revenue FROM customers ORDER BY revenue DESC",
            explanation="Repaired query.",
        )


def test_prompt_builder_scopes_schema_and_safety_rules(demo_db_url: str) -> None:
    qp = QueryPilot.connect(demo_db_url, dialect="sqlite")

    prompt = build_sql_generation_prompt(
        "Which customers generated the most revenue?",
        qp.get_schema(),
        max_rows=25,
    )

    assert "customers(id INTEGER" in prompt.user_prompt
    assert "invoices(id INTEGER" in prompt.user_prompt
    assert "Return only JSON" in prompt.instructions
    assert "SELECT-only" in prompt.instructions
    assert "maximum row limit is 25" in prompt.instructions


def test_openai_generator_uses_responses_api_and_extracts_json(demo_db_url: str) -> None:
    client = FakeOpenAIClient()
    qp = QueryPilot.connect(demo_db_url, dialect="sqlite")
    generator = OpenAISQLGenerator(client=client, model="gpt-test")

    generated = generator.generate(
        "Which customers generated the most revenue?",
        qp.get_schema(),
        max_rows=100,
    )

    assert generated.sql == "SELECT customer_name, revenue FROM customers"
    assert generated.explanation == "Ranks customers by revenue."
    assert client.responses.calls[0]["model"] == "gpt-test"
    assert client.responses.calls[0]["text"]["format"]["type"] == "json_schema"


def test_anthropic_generator_uses_messages_api_and_extracts_json(demo_db_url: str) -> None:
    client = FakeAnthropicClient()
    qp = QueryPilot.connect(demo_db_url, dialect="sqlite")
    generator = AnthropicSQLGenerator(client=client, model="claude-test")

    generated = generator.generate(
        "Which customers generated the most revenue?",
        qp.get_schema(),
        max_rows=100,
    )

    assert generated.sql == "SELECT customer_name, revenue FROM customers"
    assert generated.explanation == "Ranks customers by revenue."
    assert client.messages.calls[0]["model"] == "claude-test"
    assert client.messages.calls[0]["messages"][0]["role"] == "user"


def test_ask_repairs_invalid_llm_sql_before_execution(demo_db_url: str) -> None:
    generator = RepairingGenerator()
    qp = QueryPilot.connect(demo_db_url, dialect="sqlite", max_rows=2, generator=generator)

    answer = qp.ask("Which customers generated the most revenue?")

    assert generator.repair_calls == 1
    assert answer.sql == "SELECT customer_name, revenue FROM customers ORDER BY revenue DESC LIMIT 2"
    assert answer.validation.valid is True


def test_openai_compatible_generator_uses_chat_completions_and_extracts_json(
    demo_db_url: str,
) -> None:
    client = FakeOpenAICompatibleClient([VALID_SQL_JSON])
    qp = QueryPilot.connect(demo_db_url, dialect="sqlite")
    generator = OpenAICompatibleSQLGenerator(client=client, model="llama3.1")

    generated = generator.generate(
        "Which customers generated the most revenue?",
        qp.get_schema(),
        max_rows=100,
    )

    assert generated.sql == "SELECT customer_name, revenue FROM customers"
    assert generated.explanation == "Ranks customers by revenue."
    call = client.chat.completions.calls[0]
    assert call["model"] == "llama3.1"
    assert call["response_format"] == {"type": "json_object"}
    assert call["messages"][0]["role"] == "system"
    assert call["messages"][1]["role"] == "user"


def test_openai_compatible_generator_defaults_base_url_and_optional_api_key(
    monkeypatch,
) -> None:
    # No injected client and no API key: local servers ignore the key, and the
    # base URL defaults to Ollama's endpoint. Construction must not hit the network.
    _install_fake_openai_module(monkeypatch)

    generator = OpenAICompatibleSQLGenerator(model="qwen2.5-coder")

    assert generator.model == "qwen2.5-coder"
    assert "localhost:11434" in str(generator.client.base_url)
    assert generator.client.api_key == "not-needed"


def test_openai_compatible_generator_honours_custom_base_url(monkeypatch) -> None:
    _install_fake_openai_module(monkeypatch)

    generator = OpenAICompatibleSQLGenerator(base_url="http://localhost:8000/v1")

    assert "localhost:8000" in str(generator.client.base_url)


def test_openai_compatible_generator_repairs_via_ask(demo_db_url: str) -> None:
    # The validator-driven repair loop must work unchanged: the first candidate
    # references a missing table, so QueryPilot asks the generator to repair.
    client = FakeOpenAICompatibleClient([INVALID_SQL_JSON, REPAIRED_SQL_JSON])
    generator = OpenAICompatibleSQLGenerator(client=client, model="llama3.1")
    qp = QueryPilot.connect(demo_db_url, dialect="sqlite", max_rows=2, generator=generator)

    answer = qp.ask("Which customers generated the most revenue?")

    assert len(client.chat.completions.calls) == 2
    repair_call = client.chat.completions.calls[1]
    assert "Repair" in repair_call["messages"][0]["content"]
    assert answer.sql == "SELECT customer_name, revenue FROM customers ORDER BY revenue DESC LIMIT 2"
    assert answer.validation.valid is True
