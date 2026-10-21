from __future__ import annotations

import json
import re
from typing import Any

from querypilot.core.types import DatabaseSchema, GeneratedSQL, ValidationResult
from querypilot.generation.prompt_builder import build_sql_generation_prompt


SQL_JSON_SCHEMA: dict[str, Any] = {
    "name": "querypilot_sql",
    "schema": {
        "type": "object",
        "properties": {
            "sql": {"type": "string"},
            "explanation": {"type": "string"},
        },
        "required": ["sql", "explanation"],
        "additionalProperties": False,
    },
    "strict": True,
}

# Default base URL for OpenAI-compatible local endpoints (Ollama's `/v1`).
DEFAULT_LOCAL_BASE_URL = "http://localhost:11434/v1"


class OpenAISQLGenerator:
    def __init__(
        self,
        client: Any | None = None,
        model: str = "gpt-5.1",
        max_output_tokens: int = 800,
    ) -> None:
        self.client = client or _default_openai_client()
        self.model = model
        self.max_output_tokens = max_output_tokens

    def generate(self, question: str, schema: DatabaseSchema, max_rows: int) -> GeneratedSQL:
        prompt = build_sql_generation_prompt(question, schema, max_rows)
        return self._create(question, prompt.instructions, prompt.user_prompt)

    def repair(
        self,
        question: str,
        schema: DatabaseSchema,
        max_rows: int,
        previous_sql: str,
        validation: ValidationResult,
    ) -> GeneratedSQL:
        prompt = build_sql_generation_prompt(
            question,
            schema,
            max_rows,
            validation_errors=validation.errors,
            previous_sql=previous_sql,
        )
        return self._create(question, prompt.instructions, prompt.user_prompt)

    def _create(self, question: str, instructions: str, user_prompt: str) -> GeneratedSQL:
        response = self.client.responses.create(
            model=self.model,
            instructions=instructions,
            input=user_prompt,
            max_output_tokens=self.max_output_tokens,
            text={"format": {"type": "json_schema", **SQL_JSON_SCHEMA}},
        )
        return _generated_from_text(question, _extract_openai_text(response))


class OpenAICompatibleSQLGenerator(OpenAISQLGenerator):
    """SQL generator for OpenAI-compatible local endpoints.

    Targets servers that expose the OpenAI Chat Completions API over a local
    ``base_url`` — Ollama (``http://localhost:11434/v1``), vLLM, LM Studio, and
    llama.cpp's server. It reuses the OpenAI extra (no new dependency) and the
    same schema-scoped prompt + JSON parsing as :class:`OpenAISQLGenerator`,
    but talks Chat Completions (which local servers implement) instead of the
    Responses API, and makes the API key optional — local servers ignore it,
    yet the ``openai`` client rejects an empty string, so a harmless placeholder
    is used when none is supplied.
    """

    def __init__(
        self,
        client: Any | None = None,
        model: str = "llama3.1",
        max_output_tokens: int = 800,
        *,
        base_url: str | None = None,
        api_key: str | None = None,
    ) -> None:
        self.client = client or _default_openai_compatible_client(base_url, api_key)
        self.model = model
        self.max_output_tokens = max_output_tokens

    def _create(self, question: str, instructions: str, user_prompt: str) -> GeneratedSQL:
        response = self.client.chat.completions.create(
            model=self.model,
            max_tokens=self.max_output_tokens,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": instructions},
                {"role": "user", "content": user_prompt},
            ],
        )
        return _generated_from_text(question, _extract_chat_completion_text(response))


class AnthropicSQLGenerator:
    def __init__(
        self,
        client: Any | None = None,
        model: str = "claude-sonnet-4-20250514",
        max_tokens: int = 800,
    ) -> None:
        self.client = client or _default_anthropic_client()
        self.model = model
        self.max_tokens = max_tokens

    def generate(self, question: str, schema: DatabaseSchema, max_rows: int) -> GeneratedSQL:
        prompt = build_sql_generation_prompt(question, schema, max_rows)
        return self._create(question, prompt.instructions, prompt.user_prompt)

    def repair(
        self,
        question: str,
        schema: DatabaseSchema,
        max_rows: int,
        previous_sql: str,
        validation: ValidationResult,
    ) -> GeneratedSQL:
        prompt = build_sql_generation_prompt(
            question,
            schema,
            max_rows,
            validation_errors=validation.errors,
            previous_sql=previous_sql,
        )
        return self._create(question, prompt.instructions, prompt.user_prompt)

    def _create(self, question: str, system_prompt: str, user_prompt: str) -> GeneratedSQL:
        message = self.client.messages.create(
            model=self.model,
            max_tokens=self.max_tokens,
            system=system_prompt,
            messages=[{"role": "user", "content": user_prompt}],
        )
        return _generated_from_text(question, _extract_anthropic_text(message))


def _generated_from_text(question: str, text: str) -> GeneratedSQL:
    try:
        payload = json.loads(_strip_code_fence(text))
    except json.JSONDecodeError as exc:
        return GeneratedSQL(
            question=question,
            sql=None,
            errors=[f"LLM response was not valid JSON: {exc}"],
        )

    sql = payload.get("sql")
    if not isinstance(sql, str) or not sql.strip():
        return GeneratedSQL(question=question, sql=None, errors=["LLM response did not include SQL."])
    explanation = payload.get("explanation")
    return GeneratedSQL(
        question=question,
        sql=sql.strip(),
        explanation=explanation if isinstance(explanation, str) else None,
    )


def _strip_code_fence(text: str) -> str:
    stripped = text.strip()
    match = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", stripped, flags=re.DOTALL)
    return match.group(1).strip() if match else stripped


def _extract_openai_text(response: Any) -> str:
    output_text = getattr(response, "output_text", None)
    if isinstance(output_text, str):
        return output_text
    output = getattr(response, "output", None)
    if isinstance(output, list):
        for item in output:
            for content in getattr(item, "content", []) or []:
                text = getattr(content, "text", None)
                if isinstance(text, str):
                    return text
    return str(response)


def _extract_chat_completion_text(response: Any) -> str:
    choices = getattr(response, "choices", None)
    if isinstance(choices, list) and choices:
        message = getattr(choices[0], "message", None)
        content = getattr(message, "content", None)
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            chunks: list[str] = []
            for part in content:
                text = getattr(part, "text", None)
                if isinstance(text, str):
                    chunks.append(text)
                elif isinstance(part, dict) and isinstance(part.get("text"), str):
                    chunks.append(part["text"])
            if chunks:
                return "".join(chunks)
    return str(response)


def _extract_anthropic_text(message: Any) -> str:
    content = getattr(message, "content", None)
    if isinstance(content, list):
        chunks: list[str] = []
        for block in content:
            text = getattr(block, "text", None)
            if isinstance(text, str):
                chunks.append(text)
            elif isinstance(block, dict) and isinstance(block.get("text"), str):
                chunks.append(block["text"])
        if chunks:
            return "".join(chunks)
    return str(message)


def _default_openai_client() -> Any:
    try:
        from openai import OpenAI
    except ImportError as exc:
        raise ImportError("Install querypilot[openai] to use OpenAISQLGenerator.") from exc
    return OpenAI()


def _default_openai_compatible_client(base_url: str | None, api_key: str | None) -> Any:
    try:
        from openai import OpenAI
    except ImportError as exc:
        raise ImportError(
            "Install querypilot[openai] to use OpenAICompatibleSQLGenerator."
        ) from exc
    return OpenAI(
        base_url=base_url or DEFAULT_LOCAL_BASE_URL,
        # Local servers ignore the key, but the openai client rejects an empty
        # string, so fall back to a harmless placeholder when none is given.
        api_key=api_key or "not-needed",
    )


def _default_anthropic_client() -> Any:
    try:
        import anthropic
    except ImportError as exc:
        raise ImportError("Install querypilot[anthropic] to use AnthropicSQLGenerator.") from exc
    return anthropic.Anthropic()
