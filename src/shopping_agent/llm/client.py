"""LLM client: role-based routing, ordered fallback, rate limiting and usage accounting."""

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, Field
from redis.exceptions import RedisError

from shopping_agent.errors import (
    LLMRefusalError,
    LLMUnavailableError,
    ProviderError,
    RateLimitedError,
)
from shopping_agent.llm.types import (
    LLMProvider,
    LLMResponse,
    Message,
    ResponseSchema,
    ToolDef,
    Usage,
)
from shopping_agent.providers.cache import Cache

log = logging.getLogger(__name__)


class LLMRole(StrEnum):
    EXTRACTION = "extraction"  # requirements, offers from text
    RESEARCH = "research"  # planning + tool-calling loop
    SYNTHESIS = "synthesis"  # final answer
    ANALYSIS = "analysis"  # review themes


class ModelRoute(BaseModel):
    provider: str
    model: str

    @classmethod
    def parse(cls, value: str) -> "ModelRoute":
        provider, sep, model = value.strip().partition(":")
        if not sep or not provider or not model:
            raise ValueError(f"model route must look like 'provider:model', got {value!r}")
        return cls(provider=provider.lower(), model=model)

    def __str__(self) -> str:
        return f"{self.provider}:{self.model}"


# USD per million tokens (input, output). Free tiers cost nothing; unknown models are not priced.
PRICES_PER_MTOK: dict[str, tuple[Decimal, Decimal]] = {
    "anthropic:claude-opus-5-5": (Decimal(4), Decimal(20)),
    "anthropic:claude-sonnet-5-5": (Decimal(2), Decimal(10)),
    "anthropic:claude-haiku-4-5": (Decimal(1), Decimal(5)),
}


class CallRecord(BaseModel):
    role: LLMRole
    route: str
    latency_ms: int
    usage: Usage
    ok: bool
    error: str | None = None


class RunUsage(BaseModel):
    calls: list[CallRecord] = Field(default_factory=list)

    @property
    def total(self) -> Usage:
        total = Usage()
        for c in self.calls:
            total = total + c.usage
        return total

    @property
    def cost_usd(self) -> Decimal | None:
        cost = Decimal(0)
        priced = False
        for c in self.calls:
            price = PRICES_PER_MTOK.get(c.route)
            if price is None:
                continue
            priced = True
            cost += (
                Decimal(c.usage.input_tokens) * price[0] + Decimal(c.usage.output_tokens) * price[1]
            ) / 1_000_000
        return cost if priced else None


class RateLimiter:
    """Requests-per-minute pacing per route, shared across processes via the cache."""

    def __init__(
        self,
        cache: Cache,
        rpm: int,
        *,
        max_wait_s: float = 65.0,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._cache = cache
        self._rpm = rpm
        self._max_wait_s = max_wait_s
        self._clock = clock
        self._sleep = sleep

    async def acquire(self, route: str) -> None:
        waited = 0.0
        while True:
            now = self._clock()
            window = int(now // 60)
            try:
                count = await self._cache.incr(f"rl:{route}:{window}", 120)
            except (RedisError, OSError):
                return  # pacing is best-effort; providers still enforce their own limits
            if count <= self._rpm:
                return
            delay = 60 - (now % 60) + 0.05
            if waited + delay > self._max_wait_s:
                raise RateLimitedError(route, "local requests-per-minute budget exhausted")
            log.info("rate limit reached for %s; waiting %.1fs", route, delay)
            await self._sleep(delay)
            waited += delay


class LLMClient:
    def __init__(
        self,
        providers: dict[str, LLMProvider],
        routes: dict[LLMRole, list[ModelRoute]],
        *,
        limiter: RateLimiter | None = None,
        max_tokens: int = 8_000,
    ) -> None:
        missing = {str(r) for rs in routes.values() for r in rs if r.provider not in providers}
        if missing:
            raise ValueError(f"no provider configured for routes: {sorted(missing)}")
        self._providers = providers
        self._routes = routes
        self._limiter = limiter
        self._max_tokens = max_tokens

    def routes_for(self, role: LLMRole) -> list[ModelRoute]:
        return self._routes[role]

    async def complete(
        self,
        role: LLMRole,
        messages: list[Message],
        *,
        tools: list[ToolDef] | None = None,
        response_schema: ResponseSchema | None = None,
        max_tokens: int | None = None,
        usage: RunUsage | None = None,
    ) -> LLMResponse:
        errors: list[str] = []
        for route in self._routes[role]:
            provider = self._providers[route.provider]
            started = time.perf_counter()
            try:
                if self._limiter is not None:
                    await self._limiter.acquire(str(route))
                response = await provider.complete(
                    messages,
                    model=route.model,
                    tools=tools,
                    response_schema=response_schema,
                    max_tokens=max_tokens or self._max_tokens,
                )
            except LLMRefusalError:
                raise
            except (ProviderError, TimeoutError) as exc:
                message = str(exc) if isinstance(exc, ProviderError) else f"{route}: timed out"
                errors.append(message)
                if usage is not None:
                    usage.calls.append(
                        CallRecord(
                            role=role,
                            route=str(route),
                            latency_ms=int((time.perf_counter() - started) * 1000),
                            usage=Usage(),
                            ok=False,
                            error=message,
                        )
                    )
                log.warning("llm route %s failed: %s", route, message)
                continue
            if usage is not None:
                usage.calls.append(
                    CallRecord(
                        role=role,
                        route=str(route),
                        latency_ms=int((time.perf_counter() - started) * 1000),
                        usage=response.usage,
                        ok=True,
                    )
                )
            return response
        raise LLMUnavailableError("all language-model routes failed: " + "; ".join(errors))
