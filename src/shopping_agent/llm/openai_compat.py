"""OpenAI-compatible chat completions (Gemini, Groq, OpenRouter, OpenAI, NVIDIA, Ollama).

Raw HTTP via httpx keeps one code path for all of them. The assistant message is kept as
returned (including Gemini's `extra_content` thought signatures) and replayed unchanged.
"""

import json
from typing import Any

from shopping_agent.errors import ProviderError
from shopping_agent.llm.types import (
    LLMResponse,
    Message,
    ResponseSchema,
    Role,
    StopReason,
    ToolCall,
    ToolDef,
    Usage,
)
from shopping_agent.providers.http import ProviderHttp

BASE_URLS: dict[str, str] = {
    "gemini": "https://generativelanguage.googleapis.com/v1beta/openai",
    "groq": "https://api.groq.com/openai/v1",
    "openrouter": "https://openrouter.ai/api/v1",
    "openai": "https://api.openai.com/v1",
    "nvidia": "https://integrate.api.nvidia.com/v1",
}

_FINISH: dict[str, StopReason] = {
    "stop": "end",
    "tool_calls": "tool_calls",
    "function_call": "tool_calls",
    "length": "max_tokens",
    "content_filter": "refusal",
}


def _strip_none(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _strip_none(v) for k, v in value.items() if v is not None}
    if isinstance(value, list):
        return [_strip_none(v) for v in value]
    return value


class OpenAICompatibleProvider:
    def __init__(
        self,
        name: str,
        http: ProviderHttp,
        *,
        base_url: str,
        api_key: str | None,
        reasoning_effort: str | None = None,
    ) -> None:
        self.name = name
        self._http = http
        self._url = base_url.rstrip("/") + "/chat/completions"
        self._api_key = api_key
        self._reasoning_effort = reasoning_effort

    def _encode(self, messages: list[Message]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for m in messages:
            if m.role is Role.ASSISTANT:
                state = m.provider_state or {}
                if state.get("provider") == self.name and isinstance(state.get("message"), dict):
                    out.append(state["message"])
                    continue
                msg: dict[str, Any] = {"role": "assistant", "content": m.content or None}
                if m.tool_calls:
                    msg["tool_calls"] = [
                        {
                            "id": c.id,
                            "type": "function",
                            "function": {"name": c.name, "arguments": json.dumps(c.arguments)},
                        }
                        for c in m.tool_calls
                    ]
                out.append(msg)
            elif m.role is Role.TOOL:
                out.append(
                    {
                        "role": "tool",
                        "tool_call_id": m.tool_call_id,
                        "content": ("ERROR: " if m.is_error else "") + m.content,
                    }
                )
            else:
                out.append({"role": m.role.value, "content": m.content})
        return out

    async def complete(
        self,
        messages: list[Message],
        *,
        model: str,
        tools: list[ToolDef] | None = None,
        response_schema: ResponseSchema | None = None,
        max_tokens: int = 8_000,
    ) -> LLMResponse:
        body: dict[str, Any] = {
            "model": model,
            "messages": self._encode(messages),
            "max_tokens": max_tokens,
        }
        if tools:
            body["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": t.name,
                        "description": t.description,
                        "parameters": t.parameters,
                    },
                }
                for t in tools
            ]
            body["tool_choice"] = "auto"
        if response_schema is not None:
            body["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": response_schema.name,
                    "schema": response_schema.json_schema,
                    "strict": True,
                },
            }
        if self._reasoning_effort:
            body["reasoning_effort"] = self._reasoning_effort
        headers = {"Authorization": f"Bearer {self._api_key}"} if self._api_key else {}
        data = await self._http.request_json("POST", self._url, json=body, headers=headers)
        return self._decode(data, model)

    def _decode(self, data: Any, model: str) -> LLMResponse:
        if not isinstance(data, dict) or not data.get("choices"):
            raise ProviderError(self.name, "response has no choices", transient=True)
        choice = data["choices"][0]
        message = choice.get("message") or {}
        text = message.get("content") or ""
        if isinstance(text, list):  # some servers return content parts
            text = "".join(p.get("text", "") for p in text if isinstance(p, dict))
        calls: list[ToolCall] = []
        for i, raw in enumerate(message.get("tool_calls") or []):
            fn = raw.get("function") or {}
            # gpt-oss on some hosts leaks its channel marker into the name ("name<|channel|>...").
            name = str(fn.get("name") or "").split("<|", 1)[0].strip()
            args_text = fn.get("arguments") or "{}"
            try:
                args = json.loads(args_text) if isinstance(args_text, str) else dict(args_text)
                error = None if isinstance(args, dict) else "arguments are not an object"
            except (json.JSONDecodeError, TypeError, ValueError):
                args, error = {}, "arguments were not valid JSON"
            calls.append(
                ToolCall(
                    id=raw.get("id") or f"call_{i}",
                    name=name,
                    arguments=args if isinstance(args, dict) else {},
                    argument_error=error,
                )
            )
        stop: StopReason = _FINISH.get(str(choice.get("finish_reason")), "other")
        if calls:
            stop = "tool_calls"
        usage = data.get("usage") or {}
        raw_message = _strip_none({**message, "role": "assistant"})
        for raw_call, call in zip(raw_message.get("tool_calls") or [], calls, strict=False):
            if isinstance(raw_call.get("function"), dict):
                raw_call["function"]["name"] = call.name  # replay the cleaned name
        return LLMResponse(
            text=text,
            tool_calls=calls,
            stop_reason=stop,
            usage=Usage(
                input_tokens=int(usage.get("prompt_tokens") or 0),
                output_tokens=int(usage.get("completion_tokens") or 0),
                cached_input_tokens=int(
                    (usage.get("prompt_tokens_details") or {}).get("cached_tokens") or 0
                ),
            ),
            provider=self.name,
            model=str(data.get("model") or model),
            assistant_message=Message(
                role=Role.ASSISTANT,
                content=text,
                tool_calls=calls,
                provider_state={"provider": self.name, "message": raw_message},
            ),
        )
