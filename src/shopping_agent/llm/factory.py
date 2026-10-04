"""Build the LLM client from settings. Switching models is a configuration change only."""

import anthropic
import httpx

from shopping_agent.config import Settings
from shopping_agent.errors import ConfigurationError
from shopping_agent.llm.anthropic_provider import AnthropicProvider
from shopping_agent.llm.client import LLMClient, LLMRole, ModelRoute, RateLimiter
from shopping_agent.llm.openai_compat import BASE_URLS, OpenAICompatibleProvider
from shopping_agent.llm.types import LLMProvider
from shopping_agent.providers.cache import Cache
from shopping_agent.providers.http import ProviderHttp


def routes_from_settings(settings: Settings) -> dict[LLMRole, list[ModelRoute]]:
    default = ModelRoute.parse(settings.llm_model)
    fallbacks = [ModelRoute.parse(v) for v in settings.llm_fallbacks.split(",") if v.strip()]
    overrides = {
        LLMRole.EXTRACTION: settings.llm_model_extraction,
        LLMRole.RESEARCH: settings.llm_model_research,
        LLMRole.SYNTHESIS: settings.llm_model_synthesis,
        LLMRole.ANALYSIS: settings.llm_model_analysis,
    }
    routes: dict[LLMRole, list[ModelRoute]] = {}
    for role, override in overrides.items():
        primary = ModelRoute.parse(override) if override.strip() else default
        chain = [primary, *[f for f in fallbacks if f != primary]]
        if primary != default and default not in chain:
            chain.append(default)
        routes[role] = chain
    return routes


def _key(settings: Settings, provider: str) -> str:
    secret = {
        "gemini": settings.gemini_api_key,
        "groq": settings.groq_api_key,
        "openrouter": settings.openrouter_api_key,
        "openai": settings.openai_api_key,
        "nvidia": settings.nvidia_api_key,
        "anthropic": settings.anthropic_api_key,
    }[provider].get_secret_value()
    if not secret:
        raise ConfigurationError(f"{provider.upper()}_API_KEY is not set")
    return secret


def build_llm_client(
    settings: Settings, cache: Cache | None, http_client: httpx.AsyncClient
) -> LLMClient:
    routes = routes_from_settings(settings)
    needed = {r.provider for chain in routes.values() for r in chain}
    providers: dict[str, LLMProvider] = {}
    # The shared client's timeout suits data lookups; model calls (long answers) need more.
    timeout = httpx.Timeout(settings.llm_timeout_s, connect=10.0)
    for name in needed:
        if name in BASE_URLS:
            providers[name] = OpenAICompatibleProvider(
                name,
                ProviderHttp(
                    name,
                    http_client,
                    attempts=3,
                    max_wait_s=8,
                    timeout=timeout,
                    retry_timeouts=False,
                ),
                base_url=BASE_URLS[name],
                api_key=_key(settings, name),
                reasoning_effort=(
                    (settings.llm_reasoning_effort or None)
                    if name in {"gemini", "openai", "nvidia"}
                    else None
                ),
            )
        elif name == "ollama":
            providers[name] = OpenAICompatibleProvider(
                name,
                ProviderHttp(
                    name,
                    http_client,
                    attempts=2,
                    max_wait_s=4,
                    timeout=timeout,
                    retry_timeouts=False,
                ),
                base_url=settings.ollama_base_url,
                api_key=None,
            )
        elif name == "anthropic":
            providers[name] = AnthropicProvider(
                anthropic.AsyncAnthropic(
                    api_key=_key(settings, name), timeout=settings.llm_timeout_s, max_retries=2
                ),
                effort=settings.anthropic_effort or None,
                refusal_fallback=settings.anthropic_refusal_fallback,
            )
        else:
            raise ConfigurationError(f"unknown LLM provider '{name}'")
    limiter = RateLimiter(cache, settings.llm_requests_per_minute) if cache is not None else None
    return LLMClient(providers, routes, limiter=limiter, max_tokens=settings.llm_max_tokens)
