"""Claude via the official Anthropic SDK (premium option).

- Thinking stays at the model default (adaptive); depth is set with `output_config.effort`.
- Structured output uses `output_config.format` (forced `tool_choice` is rejected by current
  models); tools run with `tool_choice` auto.
- No `temperature` (rejected by current models).
- Assistant content blocks (including thinking) are replayed unchanged on the next turn.
- Optional server-side refusal fallback (`fallbacks: "default"`).
"""

import json
from typing import Any

import anthropic

from shopping_agent.errors import LLMRefusalError, ProviderError, RateLimitedError
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

FALLBACK_BETA = "server-side-fallback-2026-07-01"
_REPLAYABLE = {"text", "thinking", "redacted_thinking", "tool_use"}
_STOP: dict[str, StopReason] = {
    "end_turn": "end",
    "stop_sequence": "end",
    "tool_use": "tool_calls",
    "max_tokens": "max_tokens",
    "refusal": "refusal",
}


def _block_dict(block: Any) -> dict[str, Any]:
    data = (
        block.model_dump(mode="json", exclude_none=True)
        if hasattr(block, "model_dump")
        else dict(block)
    )
    return dict(data)


class AnthropicProvider:
    name = "anthropic"

    def __init__(
        self,
        client: anthropic.AsyncAnthropic,
        *,
        effort: str | None = "medium",
        refusal_fallback: bool = True,
    ) -> None:
        self._client = client
        self._effort = effort
        self._refusal_fallback = refusal_fallback

    def _encode(self, messages: list[Message]) -> tuple[str, list[dict[str, Any]]]:
        system_parts: list[str] = []
        out: list[dict[str, Any]] = []
        pending_results: list[dict[str, Any]] = []

        def flush() -> None:
            if pending_results:
                out.append({"role": "user", "content": list(pending_results)})
                pending_results.clear()

        for m in messages:
            if m.role is Role.SYSTEM:
                system_parts.append(m.content)
                continue
            if m.role is Role.TOOL:
                pending_results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": m.tool_call_id,
                        "content": m.content,
                        "is_error": m.is_error,
                    }
                )
                continue
            flush()
            if m.role is Role.ASSISTANT:
                state = m.provider_state or {}
                if state.get("provider") == self.name and isinstance(state.get("message"), list):
                    out.append({"role": "assistant", "content": state["message"]})
                    continue
                blocks: list[dict[str, Any]] = []
                if m.content:
                    blocks.append({"type": "text", "text": m.content})
                blocks.extend(
                    {"type": "tool_use", "id": c.id, "name": c.name, "input": c.arguments}
                    for c in m.tool_calls
                )
                out.append(
                    {"role": "assistant", "content": blocks or [{"type": "text", "text": ""}]}
                )
            else:
                out.append({"role": "user", "content": m.content})
        flush()
        return "\n\n".join(system_parts), out

    async def complete(
        self,
        messages: list[Message],
        *,
        model: str,
        tools: list[ToolDef] | None = None,
        response_schema: ResponseSchema | None = None,
        max_tokens: int = 8_000,
    ) -> LLMResponse:
        system, encoded = self._encode(messages)
        params: dict[str, Any] = {"model": model, "max_tokens": max_tokens, "messages": encoded}
        if system:
            params["system"] = system
        output_config: dict[str, Any] = {}
        if self._effort:
            output_config["effort"] = self._effort
        if response_schema is not None:
            output_config["format"] = {"type": "json_schema", "schema": response_schema.json_schema}
        if output_config:
            params["output_config"] = output_config
        if tools:
            params["tools"] = [
                {"name": t.name, "description": t.description, "input_schema": t.parameters}
                for t in tools
            ]
            params["tool_choice"] = {"type": "auto"}
        if self._refusal_fallback:
            params["betas"] = [FALLBACK_BETA]
            params["fallbacks"] = "default"
        try:
            response = await self._client.beta.messages.create(**params)
        except anthropic.RateLimitError as exc:
            raise RateLimitedError(self.name) from exc
        except anthropic.AuthenticationError as exc:
            raise ProviderError(self.name, "authentication failed (check the API key)") from exc
        except anthropic.APITimeoutError as exc:
            raise ProviderError(self.name, "timed out", transient=True) from exc
        except anthropic.APIConnectionError as exc:
            raise ProviderError(self.name, "connection error", transient=True) from exc
        except anthropic.APIStatusError as exc:
            raise ProviderError(
                self.name, f"request failed ({exc.status_code})", transient=exc.status_code >= 500
            ) from exc
        return self._decode(response, model)

    def _decode(self, response: Any, model: str) -> LLMResponse:
        stop: StopReason = _STOP.get(str(getattr(response, "stop_reason", "")), "other")
        if stop == "refusal":
            raise LLMRefusalError(f"{self.name}: the model declined this request")
        texts: list[str] = []
        calls: list[ToolCall] = []
        replay: list[dict[str, Any]] = []
        for block in response.content:
            kind = getattr(block, "type", None)
            if kind in _REPLAYABLE:
                replay.append(_block_dict(block))
            if kind == "text":
                texts.append(block.text)
            elif kind == "tool_use":
                args = block.input if isinstance(block.input, dict) else {}
                if isinstance(block.input, str):
                    try:
                        args = json.loads(block.input)
                    except json.JSONDecodeError:
                        args = {}
                calls.append(ToolCall(id=block.id, name=block.name, arguments=args))
        usage = getattr(response, "usage", None)
        text = "".join(texts)
        return LLMResponse(
            text=text,
            tool_calls=calls,
            stop_reason="tool_calls" if calls else stop,
            usage=Usage(
                input_tokens=int(getattr(usage, "input_tokens", 0) or 0),
                output_tokens=int(getattr(usage, "output_tokens", 0) or 0),
                cached_input_tokens=int(getattr(usage, "cache_read_input_tokens", 0) or 0),
            ),
            provider=self.name,
            model=str(getattr(response, "model", model)),
            assistant_message=Message(
                role=Role.ASSISTANT,
                content=text,
                tool_calls=calls,
                provider_state={"provider": self.name, "message": replay},
            ),
        )
