"""LLM layer: schemas, adapters, routing/fallback, rate limiting, structured output, prompts."""

import json
from collections.abc import AsyncIterator
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
import respx
from pydantic import BaseModel

from shopping_agent.config import Settings
from shopping_agent.errors import (
    LLMRefusalError,
    LLMUnavailableError,
    ProviderError,
    RateLimitedError,
    ValidationFailedError,
)
from shopping_agent.llm.anthropic_provider import FALLBACK_BETA, AnthropicProvider
from shopping_agent.llm.client import LLMClient, LLMRole, ModelRoute, RateLimiter, RunUsage
from shopping_agent.llm.factory import routes_from_settings
from shopping_agent.llm.openai_compat import BASE_URLS, OpenAICompatibleProvider
from shopping_agent.llm.schema import strict_schema, tool_parameters
from shopping_agent.llm.structured import generate_structured
from shopping_agent.llm.types import Message, ResponseSchema, Role, ToolCall, ToolDef
from shopping_agent.prompts import ALL_PROMPTS, load_prompt
from shopping_agent.providers.cache import InMemoryCache
from shopping_agent.providers.http import ProviderHttp
from tests.fakes import scripted_client

# --- schemas ---


class Inner(BaseModel):
    amount: Decimal | None = None
    label: str = "x"


class Outer(BaseModel):
    items: list[Inner]
    note: str | None = None


def _objects(node: Any) -> list[dict[str, Any]]:
    found = []
    if isinstance(node, dict):
        if node.get("type") == "object":
            found.append(node)
        for v in node.values():
            found.extend(_objects(v))
    elif isinstance(node, list):
        for v in node:
            found.extend(_objects(v))
    return found


def test_strict_schema_shape() -> None:
    schema = strict_schema(Outer)
    text = json.dumps(schema)
    assert "$ref" not in text
    assert "$defs" not in text
    assert '"default"' not in text
    for obj in _objects(schema):
        assert obj["additionalProperties"] is False
        assert set(obj["required"]) == set(obj["properties"])
    amount = schema["properties"]["items"]["items"]["properties"]["amount"]
    assert amount == {"anyOf": [{"type": "number"}, {"type": "null"}]}


def test_tool_parameters_keep_optional_arguments_optional() -> None:
    params = tool_parameters(Outer)
    assert params["required"] == ["items"]
    assert "$ref" not in json.dumps(params)


# --- OpenAI-compatible adapter ---


@pytest.fixture
async def http_client() -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient() as client:
        yield client


def gemini(client: httpx.AsyncClient) -> OpenAICompatibleProvider:
    return OpenAICompatibleProvider(
        "gemini",
        ProviderHttp("gemini", client, attempts=2, max_wait_s=0),
        base_url=BASE_URLS["gemini"],
        api_key="k",
        reasoning_effort="low",
    )


URL = BASE_URLS["gemini"] + "/chat/completions"
TOOL = ToolDef(
    name="search_products", description="d", parameters={"type": "object", "properties": {}}
)


@respx.mock
async def test_openai_compat_request_and_tool_call_decoding(http_client: httpx.AsyncClient) -> None:
    raw_call = {
        "id": "c1",
        "type": "function",
        "function": {"name": "search_products", "arguments": '{"query": "laptop"}'},
        "extra_content": {"google": {"thought_signature": "sig"}},
    }
    route = respx.post(URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "model": "gemini-x",
                "choices": [
                    {
                        "finish_reason": "tool_calls",
                        "message": {"role": "assistant", "content": None, "tool_calls": [raw_call]},
                    }
                ],
                "usage": {"prompt_tokens": 50, "completion_tokens": 7},
            },
        )
    )
    response = await gemini(http_client).complete(
        [Message.system("s"), Message.user("u")],
        model="gemini-x",
        tools=[TOOL],
        response_schema=ResponseSchema(name="X", json_schema={"type": "object"}),
    )
    body = json.loads(route.calls.last.request.content)
    assert route.calls.last.request.headers["Authorization"] == "Bearer k"
    assert body["tool_choice"] == "auto"
    assert body["tools"][0]["function"]["name"] == "search_products"
    assert body["response_format"]["json_schema"]["strict"] is True
    assert body["reasoning_effort"] == "low"
    assert response.stop_reason == "tool_calls"
    assert response.tool_calls[0].arguments == {"query": "laptop"}
    assert response.usage.input_tokens == 50

    # The raw assistant message (with the thought signature) is replayed unchanged.
    follow = respx.post(URL).mock(
        return_value=httpx.Response(
            200, json={"choices": [{"finish_reason": "stop", "message": {"content": "done"}}]}
        )
    )
    await gemini(http_client).complete(
        [
            Message.user("u"),
            response.assistant_message,
            Message.tool_result(response.tool_calls[0], "oops", is_error=True),
        ],
        model="gemini-x",
    )
    sent = json.loads(follow.calls.last.request.content)["messages"]
    assert sent[1]["tool_calls"][0]["extra_content"]["google"]["thought_signature"] == "sig"
    assert sent[2] == {"role": "tool", "tool_call_id": "c1", "content": "ERROR: oops"}


@respx.mock
async def test_openai_compat_bad_arguments_and_foreign_state(
    http_client: httpx.AsyncClient,
) -> None:
    respx.post(URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "finish_reason": "tool_calls",
                        "message": {
                            "tool_calls": [
                                {"id": "c", "function": {"name": "t", "arguments": "{bad"}}
                            ]
                        },
                    }
                ]
            },
        )
    )
    foreign = Message(
        role=Role.ASSISTANT,
        content="hi",
        tool_calls=[ToolCall(id="x", name="t", arguments={"a": 1})],
        provider_state={"provider": "anthropic", "message": [{"type": "thinking"}]},
    )
    response = await gemini(http_client).complete([Message.user("u"), foreign], model="m")
    assert response.tool_calls[0].argument_error
    sent = json.loads(respx.calls.last.request.content)["messages"][1]
    assert sent["tool_calls"][0]["function"]["arguments"] == '{"a": 1}'


@respx.mock
async def test_openai_compat_cleans_leaked_channel_marker_in_tool_name(
    http_client: httpx.AsyncClient,
) -> None:
    leaked = {
        "id": "c1",
        "function": {"name": "search_products<|channel|>commentary", "arguments": "{}"},
    }
    respx.post(URL).mock(
        return_value=httpx.Response(
            200, json={"choices": [{"message": {"tool_calls": [leaked]}, "finish_reason": "stop"}]}
        )
    )
    response = await gemini(http_client).complete([Message.user("u")], model="m")
    assert response.tool_calls[0].name == "search_products"
    replayed = response.assistant_message.provider_state or {}
    assert replayed["message"]["tool_calls"][0]["function"]["name"] == "search_products"


@respx.mock
async def test_llm_http_uses_its_own_timeout_and_does_not_retry_timeouts(
    http_client: httpx.AsyncClient,
) -> None:
    route = respx.post(URL).mock(side_effect=httpx.ReadTimeout("slow"))
    provider = OpenAICompatibleProvider(
        "gemini",
        ProviderHttp(
            "gemini",
            http_client,
            attempts=3,
            max_wait_s=0,
            timeout=httpx.Timeout(77.0),
            retry_timeouts=False,
        ),
        base_url=BASE_URLS["gemini"],
        api_key="k",
    )
    with pytest.raises(ProviderError, match="timed out"):
        await provider.complete([Message.user("u")], model="m")
    assert route.call_count == 1
    assert route.calls.last.request.extensions["timeout"]["read"] == 77.0


def test_nvidia_is_a_known_provider() -> None:
    assert BASE_URLS["nvidia"] == "https://integrate.api.nvidia.com/v1"
    settings = Settings(
        _env_file=None,
        llm_model="gemini:g",
        llm_fallbacks="nvidia:openai/gpt-oss-20b",
    )
    chain = routes_from_settings(settings)[LLMRole.RESEARCH]
    assert chain[-1].provider == "nvidia"
    assert chain[-1].model == "openai/gpt-oss-20b"


@respx.mock
async def test_openai_compat_server_errors(http_client: httpx.AsyncClient) -> None:
    route = respx.post(URL).mock(return_value=httpx.Response(503))
    with pytest.raises(ProviderError):
        await gemini(http_client).complete([Message.user("u")], model="m")
    assert route.call_count == 2


# --- Anthropic adapter (SDK stubbed) ---


class _StubMessages:
    def __init__(self, response: Any) -> None:
        self.response = response
        self.params: dict[str, Any] = {}

    async def create(self, **params: Any) -> Any:
        self.params = params
        return self.response


def _anthropic(response: Any) -> tuple[AnthropicProvider, _StubMessages]:
    stub = _StubMessages(response)
    client = SimpleNamespace(beta=SimpleNamespace(messages=stub))
    return AnthropicProvider(client, effort="medium"), stub  # type: ignore[arg-type]


def _block(**data: Any) -> SimpleNamespace:
    return SimpleNamespace(model_dump=lambda **_: dict(data), **data)


async def test_anthropic_request_shape_and_replay() -> None:
    thinking = _block(type="thinking", thinking="", signature="sig")
    tool_use = _block(type="tool_use", id="t1", name="search_products", input={"query": "x"})
    response = SimpleNamespace(
        stop_reason="tool_use",
        content=[thinking, tool_use],
        usage=SimpleNamespace(input_tokens=10, output_tokens=5, cache_read_input_tokens=3),
        model="claude-opus-5-5",
    )
    provider, stub = _anthropic(response)
    result = await provider.complete(
        [Message.system("sys"), Message.user("hi")],
        model="claude-opus-5-5",
        tools=[TOOL],
        response_schema=ResponseSchema(name="X", json_schema={"type": "object"}),
    )
    p = stub.params
    assert p["system"] == "sys"
    assert "temperature" not in p
    assert p["tool_choice"] == {"type": "auto"}
    assert p["output_config"] == {
        "effort": "medium",
        "format": {"type": "json_schema", "schema": {"type": "object"}},
    }
    assert p["betas"] == [FALLBACK_BETA]
    assert p["fallbacks"] == "default"
    assert result.tool_calls[0].arguments == {"query": "x"}
    assert result.usage.cached_input_tokens == 3

    final = SimpleNamespace(
        stop_reason="end_turn", content=[_block(type="text", text="ok")], usage=None, model="m"
    )
    provider2, stub2 = _anthropic(final)
    call = result.tool_calls[0]
    await provider2.complete(
        [
            Message.user("hi"),
            result.assistant_message,
            Message.tool_result(call, "r1"),
            Message.tool_result(ToolCall(id="t2", name="x"), "r2", is_error=True),
        ],
        model="m",
    )
    msgs = stub2.params["messages"]
    assert msgs[1]["content"][0]["type"] == "thinking"  # replayed unchanged
    assert [b["tool_use_id"] for b in msgs[2]["content"]] == ["t1", "t2"]  # one user message
    assert msgs[2]["content"][1]["is_error"] is True


async def test_anthropic_refusal() -> None:
    provider, _ = _anthropic(
        SimpleNamespace(stop_reason="refusal", content=[], usage=None, model="m")
    )
    with pytest.raises(LLMRefusalError):
        await provider.complete([Message.user("x")], model="m")


# --- client: routing, fallback, usage ---


async def test_fallback_to_next_route_and_usage() -> None:
    failing, _ = scripted_client(
        lambda *_: ProviderError("primary", "server error 503", transient=True)
    )
    working, _ = scripted_client(lambda *_: "fine")
    client = LLMClient(
        {"a": failing._providers["fake"], "b": working._providers["fake"]},
        {
            role: [ModelRoute(provider="a", model="m1"), ModelRoute(provider="b", model="m2")]
            for role in LLMRole
        },
    )
    usage = RunUsage()
    response = await client.complete(LLMRole.RESEARCH, [Message.user("x")], usage=usage)
    assert response.text == "fine"
    assert [(c.route, c.ok) for c in usage.calls] == [("a:m1", False), ("b:m2", True)]
    assert usage.total.input_tokens == 100
    assert usage.cost_usd is None


async def test_all_routes_failing_and_refusals() -> None:
    client, _ = scripted_client(lambda *_: ProviderError("fake", "down"))
    with pytest.raises(LLMUnavailableError, match="down"):
        await client.complete(LLMRole.SYNTHESIS, [Message.user("x")])
    refusing, provider = scripted_client(lambda *_: LLMRefusalError("no"))
    with pytest.raises(LLMRefusalError):
        await refusing.complete(LLMRole.SYNTHESIS, [Message.user("x")])
    assert len(provider.calls) == 1


def test_route_configuration() -> None:
    settings = Settings(
        llm_model="gemini:gemini-flash-latest",
        llm_model_synthesis="anthropic:claude-opus-5-5",
        llm_fallbacks="groq:llama-x, gemini:gemini-flash-latest",
    )
    routes = routes_from_settings(settings)
    assert [str(r) for r in routes[LLMRole.RESEARCH]] == [
        "gemini:gemini-flash-latest",
        "groq:llama-x",
    ]
    assert [str(r) for r in routes[LLMRole.SYNTHESIS]] == [
        "anthropic:claude-opus-5-5",
        "groq:llama-x",
        "gemini:gemini-flash-latest",
    ]
    with pytest.raises(ValueError, match="provider:model"):
        ModelRoute.parse("just-a-model")


def test_cost_tracking_for_priced_models() -> None:
    from shopping_agent.llm.client import CallRecord
    from shopping_agent.llm.types import Usage

    usage = RunUsage(
        calls=[
            CallRecord(
                role=LLMRole.SYNTHESIS,
                route="anthropic:claude-opus-5-5",
                latency_ms=1,
                usage=Usage(input_tokens=1_000_000, output_tokens=100_000),
                ok=True,
            )
        ]
    )
    assert usage.cost_usd == Decimal(6)


async def test_rate_limiter_waits_for_next_window() -> None:
    now = [120.0]
    slept: list[float] = []

    async def sleep(seconds: float) -> None:
        slept.append(seconds)
        now[0] += seconds

    limiter = RateLimiter(InMemoryCache(), rpm=2, clock=lambda: now[0], sleep=sleep)
    await limiter.acquire("r")
    await limiter.acquire("r")
    await limiter.acquire("r")  # third request in the same minute waits
    assert len(slept) == 1
    assert slept[0] == pytest.approx(60.05)

    strict = RateLimiter(InMemoryCache(), rpm=1, max_wait_s=5, clock=lambda: 130.0, sleep=sleep)
    await strict.acquire("r")
    with pytest.raises(RateLimitedError):
        await strict.acquire("r")


# --- structured output ---


class Answer(BaseModel):
    value: int


async def test_structured_output_repairs_once() -> None:
    replies = iter(['{"value": "nope"}', '```json\n{"value": 3}\n```'])
    client, provider = scripted_client(lambda *_: next(replies))
    result = await generate_structured(client, LLMRole.EXTRACTION, [Message.user("x")], Answer)
    assert result.value == 3
    repair = provider.calls[1][0][-1]
    assert "did not match" in repair.content
    assert provider.calls[0][2] is not None
    assert provider.calls[0][2].name == "Answer"


async def test_structured_output_gives_up_after_repair() -> None:
    client, _ = scripted_client(lambda *_: "not json at all")
    with pytest.raises(ValidationFailedError):
        await generate_structured(client, LLMRole.EXTRACTION, [Message.user("x")], Answer)


# --- prompts ---


def test_all_prompts_load_with_versions() -> None:
    for name in ALL_PROMPTS:
        prompt = load_prompt(name)
        assert prompt.version >= 1
        assert prompt.template


def test_prompt_rendering_requires_every_placeholder() -> None:
    prompt = load_prompt("agent/research")
    assert prompt.placeholders == {"max_tool_calls", "analysis", "plan", "profile"}
    with pytest.raises(KeyError):
        prompt.render(analysis="{}")
    text = prompt.render(max_tool_calls="5", analysis="{}", plan="-", profile="{}")
    assert "at most 5 tool calls" in text


def test_system_prompt_states_core_rules() -> None:
    text = load_prompt("agent/system").template
    for phrase in ("untrusted_content", "lowest ever", "card numbers", "Never claim a price will"):
        assert phrase in text
