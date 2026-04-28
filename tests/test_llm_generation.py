from __future__ import annotations

from querypilot import QueryPilot
from querypilot.core.types import DatabaseSchema, GeneratedSQL
from querypilot.generation.llm import AnthropicSQLGenerator, OpenAISQLGenerator
from querypilot.generation.prompt_builder import build_sql_generation_prompt


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
