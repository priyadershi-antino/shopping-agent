# Extending the agent

## Add a tool

1. Create input and output Pydantic models and a `Tool` subclass (see `src/shopping_agent/tools/catalog_tools.py` for examples):

```python
class PriceAlertsInput(BaseModel):
    listing_id: str


class PriceAlertsOutput(BaseModel):
    alerts: list[str]


class ListAlertsTool(Tool[PriceAlertsInput, PriceAlertsOutput]):
    name = "list_alerts"
    description = "What the model reads to decide when to call this tool. Be specific."
    input_model = PriceAlertsInput
    output_model = PriceAlertsOutput
    timeout_s = 20.0
    side_effect = False  # True if it changes saved data

    async def run(self, ctx: ToolContext, args: PriceAlertsInput) -> ToolResult[PriceAlertsOutput]:
        async with ctx.sessions() as session:
            ...
        return ToolResult(data=PriceAlertsOutput(alerts=[...]), evidence=[...])
```

2. Register it in `default_tools()` (`tools/__init__.py`).
3. Rules:
   - Return provenance as `Evidence` for every external fact.
   - Report provider problems in `errors`, set `partial=True` and return what you have; don't raise.
   - Raise `ToolInputError` for references that don't exist, such as an unknown `listing_id`.
   - Wrap any external text with `wrap_untrusted()` and run `detect_injection()` on it.
   - Keep arithmetic in `services/`, not in the tool or the prompt.
   - Tools with `side_effect = True` are never offered to the model; they run only via `registry.execute(..., allow_side_effects=True)` after the user confirms.
4. If the agent should store the output in its state, add a branch to `ShoppingAgent._ingest`.
5. Add an integration test in `tests/integration/test_tools.py` using the demo catalog.

## Add a product data provider

Implement the `ProductProvider` protocol (`providers/product/base.py`):

```python
class MyProvider:
    name = "myprovider"

    async def search(self, query: ProductQuery) -> list[ProviderListing]: ...
    async def lookup(self, marketplace: Marketplace, external_id: str) -> list[ProviderListing]: ...
    async def get_details(self, ref: ProductRef) -> ProductDetails | None: ...
    async def get_listings(self, ref: ProductRef) -> list[ProviderListing]: ...
```

- Return raw-but-typed data; normalization, grouping and the variant gate are done centrally.
- Give each listing a stable `source_key`. Use `"{marketplace}:{external_id}"` when you know the marketplace ID, so different providers update the same listing.
- Set `source_type`, `source_name` and `verification` honestly. Aggregated data is `search_result` / `source_reported`.
- Use `ProviderHttp` for HTTP calls: it maps 429/5xx/timeouts to retryable errors and keeps URLs (and keys) out of error messages.
- Register it in `providers/factory.py` under a name, add a daily quota to `QuotaGuard`, and enable it with `PRODUCT_PROVIDERS=myprovider,serpapi`.
- Test parsing with `respx`-mocked responses (see `tests/unit/test_providers.py`).

Good candidates: Amazon Product Advertising API, Flipkart Affiliate API, a price-history service such as Keepa (as a `PriceHistoryProvider`).

## Add a web search provider

Implement `WebSearchProvider.search(query, max_results, include_domains, recent_days)` returning `WebResult`s (`providers/web/base.py`), then register it in `providers/factory.py` and enable it with `WEB_PROVIDERS`.

## Add or switch an LLM provider

- **Any OpenAI-compatible API** (most providers): add its base URL to `BASE_URLS` in `llm/openai_compat.py` and its key to `Settings` and `_key()` in `llm/factory.py`. It then works as `LLM_MODEL=newprovider:model-name`.
- **A provider with its own API:** implement `LLMProvider.complete(messages, model, tools, response_schema, max_tokens) -> LLMResponse` (`llm/types.py`). Keep the raw assistant message in `provider_state` and replay it unchanged on the next turn when the provider is the same.
- **Claude:** set `LLM_MODEL=anthropic:claude-opus-5-5` (or use it for one role, e.g. `LLM_MODEL_SYNTHESIS`) and `ANTHROPIC_API_KEY`. The adapter uses the official SDK. Current Claude models reject forced `tool_choice` and `temperature`, so the adapter uses `tool_choice: auto` and `output_config.format` for structured output. `ANTHROPIC_EFFORT` controls depth. `ANTHROPIC_REFUSAL_FALLBACK=true` enables Anthropic's server-side fallback (beta `server-side-fallback-2026-07-01`, `fallbacks: "default"`) so a declined request is retried on another model.

Structured outputs are generated from Pydantic models by `llm/schema.py`: refs are inlined, objects are closed and every field is required (optional fields become nullable). Avoid `Decimal` in LLM output models; use `float` and convert in code.

## Change a prompt

Prompts are in `src/shopping_agent/prompts/` and use `$placeholders`.

- Bump the `<!-- version: N -->` header whenever the behaviour changes. The version is recorded on every run.
- Keep rules about numbers, evidence and untrusted content consistent with `agent/system.md`.
- Run `tests/unit/test_llm.py` (prompt loading and placeholders) and the evaluation suite (M5) after changing a prompt.

## Add a database table or column

1. Change the models in `src/shopping_agent/db/models.py`.
2. Generate a migration: `python -m uv run alembic revision --autogenerate -m "describe change"`.
3. Rename the file and its revision id to the next number (`0004`, …) so the history stays readable, and set `down_revision` to the previous number.
4. Apply it with `python -m uv run alembic upgrade head`; `alembic check` must report no differences. `tests/integration/test_migrations.py` runs the round trip and the check.
5. Add the table to `APP_TABLES` in `tests/conftest.py` so tests start clean.

## Add a deterministic rule

Put it in `src/shopping_agent/services/` as a pure function over domain types, with unit tests in `tests/unit/`. Services must not do I/O.

## Adding a notification channel

1. Add a sender to `tracking/notify.py`: an async function taking the settings and the `AlertEventRow`. On failure, raise `DeliveryError` with a short message that contains no secrets.
2. Dispatch it in `deliver_pending` by channel name, and add its settings to `config.py`, `.env.example` and [configuration.md](configuration.md).
3. Users enable it with `NOTIFY_CHANNELS=...`. New alerts then get a `notifications` row for the channel, which the worker delivers with retries and quiet hours.

## Adding an evaluation scenario

Add one JSON line to `evals/dataset.jsonl` (see [evaluation.md](evaluation.md) for the fields). Golden prices must come from the deterministic engine, not from hand arithmetic. `tests/unit/test_eval_metrics.py` checks that the file parses and that every golden listing exists in the demo catalog.
