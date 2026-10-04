"""Provider-neutral LLM types.

Assistant turns keep the provider's raw message in `provider_state` so it can be replayed
unchanged to the same provider (Claude thinking blocks, Gemini thought signatures). A
different provider ignores it and rebuilds the turn from the neutral fields.
"""

from enum import StrEnum
from typing import Any, Literal, Protocol

from pydantic import BaseModel, Field


class Role(StrEnum):
    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"


class ToolCall(BaseModel):
    id: str
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    argument_error: str | None = None  # arguments were not valid JSON


class Message(BaseModel):
    role: Role
    content: str = ""
    tool_calls: list[ToolCall] = Field(default_factory=list)
    tool_call_id: str | None = None  # role=tool
    tool_name: str | None = None  # role=tool
    is_error: bool = False  # role=tool
    provider_state: dict[str, Any] | None = None  # {"provider": name, "message": raw}

    @classmethod
    def system(cls, text: str) -> "Message":
        return cls(role=Role.SYSTEM, content=text)

    @classmethod
    def user(cls, text: str) -> "Message":
        return cls(role=Role.USER, content=text)

    @classmethod
    def tool_result(cls, call: ToolCall, content: str, *, is_error: bool = False) -> "Message":
        return cls(
            role=Role.TOOL,
            content=content,
            tool_call_id=call.id,
            tool_name=call.name,
            is_error=is_error,
        )


class ToolDef(BaseModel):
    name: str
    description: str
    parameters: dict[str, Any]


class ResponseSchema(BaseModel):
    name: str
    json_schema: dict[str, Any]


class Usage(BaseModel):
    input_tokens: int = 0
    output_tokens: int = 0
    cached_input_tokens: int = 0

    def __add__(self, other: "Usage") -> "Usage":
        return Usage(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            cached_input_tokens=self.cached_input_tokens + other.cached_input_tokens,
        )


StopReason = Literal["end", "tool_calls", "max_tokens", "refusal", "other"]


class LLMResponse(BaseModel):
    text: str
    tool_calls: list[ToolCall] = Field(default_factory=list)
    stop_reason: StopReason
    usage: Usage = Field(default_factory=Usage)
    provider: str
    model: str
    assistant_message: Message


class LLMProvider(Protocol):
    name: str

    async def complete(
        self,
        messages: list[Message],
        *,
        model: str,
        tools: list[ToolDef] | None = None,
        response_schema: ResponseSchema | None = None,
        max_tokens: int = 8_000,
    ) -> LLMResponse: ...
