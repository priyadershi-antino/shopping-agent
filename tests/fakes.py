"""Scripted LLM provider for deterministic agent tests."""

import json
from collections.abc import Callable
from typing import Any

from shopping_agent.llm.client import LLMClient, LLMRole, ModelRoute
from shopping_agent.llm.types import (
    LLMResponse,
    Message,
    ResponseSchema,
    Role,
    ToolCall,
    ToolDef,
    Usage,
)

Reply = str | list[ToolCall] | Exception
Handler = Callable[[list[Message], list[ToolDef] | None, ResponseSchema | None], Reply]


class ScriptedProvider:
    name = "fake"

    def __init__(self, handler: Handler) -> None:
        self.handler = handler
        self.calls: list[tuple[list[Message], list[ToolDef] | None, ResponseSchema | None]] = []

    async def complete(
        self,
        messages: list[Message],
        *,
        model: str,
        tools: list[ToolDef] | None = None,
        response_schema: ResponseSchema | None = None,
        max_tokens: int = 8_000,
    ) -> LLMResponse:
        self.calls.append((list(messages), tools, response_schema))
        reply = self.handler(messages, tools, response_schema)
        if isinstance(reply, Exception):
            raise reply
        calls = reply if isinstance(reply, list) else []
        text = reply if isinstance(reply, str) else ""
        return LLMResponse(
            text=text,
            tool_calls=calls,
            stop_reason="tool_calls" if calls else "end",
            usage=Usage(input_tokens=100, output_tokens=20),
            provider=self.name,
            model=model,
            assistant_message=Message(role=Role.ASSISTANT, content=text, tool_calls=calls),
        )


def scripted_client(handler: Handler) -> tuple[LLMClient, ScriptedProvider]:
    provider = ScriptedProvider(handler)
    routes = {role: [ModelRoute(provider="fake", model="test-model")] for role in LLMRole}
    return LLMClient({"fake": provider}, routes), provider


def last_tool_payload(messages: list[Message]) -> dict[str, Any]:
    for m in reversed(messages):
        if m.role is Role.TOOL:
            data: dict[str, Any] = json.loads(m.content)
            return data
    return {}


def research_turns(messages: list[Message]) -> int:
    return sum(1 for m in messages if m.role is Role.ASSISTANT)


def facts_from(messages: list[Message]) -> dict[str, Any]:
    content = messages[-1].content
    data: dict[str, Any] = json.loads(content.split("FACTS:", 1)[1])
    return data


def analysis_json(**overrides: Any) -> str:
    base: dict[str, Any] = {
        "intent": "discover",
        "category": "laptop",
        "budget": {"minimum": None, "maximum": 80000, "target": None, "approximate": False},
        "use_cases": ["programming", "gaming"],
        "hard_constraints": [],
        "strong_preferences": [],
        "nice_to_have": [],
        "unknowns": [],
        "product_mentions": [],
        "target_price": None,
        "payment_methods": [{"issuer": "HDFC", "card_type": "credit"}],
        "memberships": [],
        "has_exchange_device": None,
        "prefers_emi": None,
        "pasted_offer_text": None,
        "needs_clarification": False,
        "clarification_question": None,
    }
    base.update(overrides)
    return json.dumps(base)
