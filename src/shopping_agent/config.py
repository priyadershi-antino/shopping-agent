"""Application settings loaded from environment variables and `.env`."""

from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    env: Literal["dev", "test", "prod"] = "dev"
    log_level: str = "INFO"
    log_json: bool = True

    # Postgres (async driver). Example: postgresql+asyncpg://user:pass@127.0.0.1:5432/shopping_agent
    database_url: SecretStr = SecretStr(
        "postgresql+asyncpg://postgres:postgres@127.0.0.1:5432/shopping_agent"
    )
    test_database_url: SecretStr = SecretStr(
        "postgresql+asyncpg://postgres:postgres@127.0.0.1:5432/shopping_agent_test"
    )
    # Wiped before every evaluation scenario. Empty = use TEST_DATABASE_URL.
    eval_database_url: SecretStr = SecretStr("")

    redis_url: str = "redis://127.0.0.1:6379/0"
    cache_key_prefix: str = "sa:"

    api_host: str = "127.0.0.1"
    api_port: int = Field(default=8000, ge=1, le=65535)
    # Optional shared secret for remote deployments; empty means no API-key check.
    api_key: SecretStr = SecretStr("")

    # --- Data providers ---
    # Comma-separated, in priority order. "auto" = real providers whose keys are set,
    # otherwise the clearly-labelled demo fixtures.
    product_providers: str = "auto"
    web_providers: str = "auto"
    serpapi_api_key: SecretStr = SecretStr("")
    tavily_api_key: SecretStr = SecretStr("")
    # Daily request budgets (free tiers are small). Enforced with Redis counters.
    serpapi_daily_quota: int = Field(default=8, ge=0)
    tavily_daily_quota: int = Field(default=30, ge=0)
    provider_timeout_s: float = Field(default=20.0, gt=0)

    # --- Page fetching (fetch_page tool) ---
    # Extra domains to allow in addition to the built-in allowlist (comma-separated).
    fetch_extra_domains: str = ""
    fetch_max_bytes: int = Field(default=2_000_000, gt=0)
    fetch_max_chars: int = Field(default=8_000, gt=0)

    # --- LLM ---
    # Default route as "provider:model". Providers: gemini, groq, openrouter, openai, nvidia,
    # ollama, anthropic. Per-role overrides (optional) use the same format.
    llm_model: str = "gemini:gemini-flash-latest"
    llm_model_extraction: str = ""
    llm_model_research: str = ""
    llm_model_synthesis: str = ""
    llm_model_analysis: str = ""
    # Comma-separated fallback routes tried in order when the primary fails.
    llm_fallbacks: str = "gemini:gemini-flash-lite-latest"
    llm_requests_per_minute: int = Field(default=10, ge=1)
    llm_timeout_s: float = Field(default=90.0, gt=0)
    llm_max_tokens: int = Field(default=8_000, ge=256)
    gemini_api_key: SecretStr = SecretStr("")
    groq_api_key: SecretStr = SecretStr("")
    openrouter_api_key: SecretStr = SecretStr("")
    openai_api_key: SecretStr = SecretStr("")
    nvidia_api_key: SecretStr = SecretStr("")
    anthropic_api_key: SecretStr = SecretStr("")
    ollama_base_url: str = "http://127.0.0.1:11434/v1"
    # Reasoning depth for OpenAI-compatible models that support it (e.g. Gemini): low/medium/high.
    llm_reasoning_effort: str = "low"
    # Claude: effort level and server-side refusal fallback (see docs/extending.md).
    anthropic_effort: str = "medium"
    anthropic_refusal_fallback: bool = True

    # --- Agent budgets ---
    agent_max_iterations: int = Field(default=6, ge=1, le=20)
    agent_max_tool_calls: int = Field(default=25, ge=1, le=100)
    agent_max_seconds: float = Field(default=150.0, gt=0)

    # --- Tracking and alerts (worker) ---
    worker_tick_seconds: int = Field(default=60, ge=5)
    # Price checks per hour across all watched items; low-priority items slow down first.
    monitor_checks_per_hour: int = Field(default=30, ge=1)
    # Minimum hours between two alerts of the same kind for the same item.
    alert_cooldown_hours: float = Field(default=6.0, ge=0)
    # No email/Telegram during these IST hours, e.g. "23-7" (in-app alerts still appear).
    quiet_hours: str = ""
    # Delivery channels besides in-app: email, telegram (comma-separated).
    notify_channels: str = ""
    smtp_host: str = ""
    smtp_port: int = Field(default=587, ge=1, le=65535)
    smtp_username: str = ""
    smtp_password: SecretStr = SecretStr("")
    smtp_from: str = ""
    notify_email_to: str = ""
    telegram_bot_token: SecretStr = SecretStr("")
    telegram_chat_id: str = ""

    def notify_channel_list(self) -> list[str]:
        return self._csv(self.notify_channels)

    @staticmethod
    def _csv(value: str) -> list[str]:
        return [v.strip().lower() for v in value.split(",") if v.strip()]

    def product_provider_names(self) -> list[str]:
        if self.product_providers.strip().lower() != "auto":
            return self._csv(self.product_providers)
        return ["serpapi"] if self.serpapi_api_key.get_secret_value() else ["fixture"]

    def web_provider_names(self) -> list[str]:
        if self.web_providers.strip().lower() != "auto":
            return self._csv(self.web_providers)
        if self.tavily_api_key.get_secret_value():
            return ["tavily"]
        # Real product data without web search: demo web pages must not mix into real answers.
        return [] if self.product_provider_names() != ["fixture"] else ["fixture"]

    def fetch_extra_domain_list(self) -> list[str]:
        return self._csv(self.fetch_extra_domains)


@lru_cache
def get_settings() -> Settings:
    return Settings()
