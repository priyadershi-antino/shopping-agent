# Configuration

Settings are read from environment variables and `.env` (see `src/shopping_agent/config.py`). Names are case-insensitive. `.env.example` lists them all.

## Core

| Setting | Default | Notes |
|---|---|---|
| `ENV` | `dev` | `dev`, `test` or `prod` |
| `LOG_LEVEL` | `INFO` | |
| `LOG_JSON` | `true` | JSON log lines. Secrets are redacted either way. |
| `DATABASE_URL` | local Postgres `shopping_agent` | Async driver: `postgresql+asyncpg://user:pass@127.0.0.1:5432/shopping_agent` |
| `TEST_DATABASE_URL` | local Postgres `shopping_agent_test` | Used by the test suite only |
| `REDIS_URL` | `redis://127.0.0.1:6379/0` | |
| `CACHE_KEY_PREFIX` | `sa:` | Prefix for all Redis keys |
| `API_HOST` / `API_PORT` | `127.0.0.1` / `8000` | |
| `API_KEY` | empty | Shared secret required on API calls when set. This is **not** an LLM key. |

## Data providers

| Setting | Default | Notes |
|---|---|---|
| `PRODUCT_PROVIDERS` | `auto` | Comma-separated, in priority order: `serpapi`, `fixture`. `auto` means SerpApi if its key is set, otherwise the demo catalog. |
| `DEFAULT_PLATFORMS` | `amazon,flipkart,croma,reliance_digital,vijay_sales,tata_cliq` | Platforms whose listings the agent uses unless a request picks others (web form checkboxes, `shop ask --platform`, `platforms` in `POST /v1/chat`). Names: `amazon`, `flipkart`, `croma`, `reliance_digital`, `vijay_sales`, `tata_cliq`, `other`. Empty means all, including small resellers whose prices are the least reliable. With SerpApi, choosing Amazon runs an Amazon.in search; choosing any other platform runs a Google Shopping search (which lists Flipkart, Croma and others). Each is one SerpApi search, so choosing fewer platforms saves quota. |
| `WEB_PROVIDERS` | `auto` | `tavily`, `fixture`. `auto` means Tavily if its key is set; with real product data (SerpApi) but no Tavily key it means **no web search** (the answer says so); with neither key, demo. |
| `SERPAPI_API_KEY` | empty | |
| `TAVILY_API_KEY` | empty | |
| `SERPAPI_DAILY_QUOTA` | `8` | Requests per Indian calendar day. Cached results don't count. |
| `TAVILY_DAILY_QUOTA` | `30` | |
| `PROVIDER_TIMEOUT_S` | `20` | Per-request timeout for data providers |

## Page fetching

| Setting | Default | Notes |
|---|---|---|
| `FETCH_EXTRA_DOMAINS` | empty | Comma-separated domains to allow in addition to the built-in allowlist (see [Security](security.md)) |
| `FETCH_MAX_BYTES` | `2000000` | Larger pages are rejected |
| `FETCH_MAX_CHARS` | `8000` | Text passed to the model is truncated to this length |

## Language model

A route is written `provider:model`. Providers: `gemini`, `groq`, `openrouter`, `openai`, `ollama`, `anthropic`.

| Setting | Default | Notes |
|---|---|---|
| `LLM_MODEL` | `gemini:gemini-flash-latest` | Default route for every role |
| `LLM_FALLBACKS` | `gemini:gemini-flash-lite-latest` | Tried in order when the primary fails (overload, timeout, auth) |
| `LLM_MODEL_EXTRACTION` | empty | Optional route for request analysis and offer parsing |
| `LLM_MODEL_RESEARCH` | empty | Optional route for planning and the tool-calling loop |
| `LLM_MODEL_SYNTHESIS` | empty | Optional route for the final answer, e.g. a premium model only here |
| `LLM_MODEL_ANALYSIS` | empty | Optional route for review theme analysis |
| `LLM_REQUESTS_PER_MINUTE` | `10` | Pacing per route, shared through Redis |
| `LLM_TIMEOUT_S` | `90` | |
| `LLM_MAX_TOKENS` | `8000` | Output cap per call |
| `LLM_REASONING_EFFORT` | `low` | Sent to Gemini, OpenAI and NVIDIA models that support it (`low`/`medium`/`high`). Empty disables it. |
| `GEMINI_API_KEY`, `GROQ_API_KEY`, `OPENROUTER_API_KEY`, `OPENAI_API_KEY`, `NVIDIA_API_KEY`, `ANTHROPIC_API_KEY` | empty | Only the keys for routes you use are required |
| `OLLAMA_BASE_URL` | `http://127.0.0.1:11434/v1` | Local models, no key |
| `ANTHROPIC_EFFORT` | `medium` | Claude effort level (`low` to `max`) |
| `ANTHROPIC_REFUSAL_FALLBACK` | `true` | Enables Anthropic's server-side fallback when a model declines a request |

### Examples

Free default with two Gemini models:

```env
LLM_MODEL=gemini:gemini-flash-latest
LLM_FALLBACKS=gemini:gemini-flash-lite-latest
```

Gemini first, then NVIDIA's hosted `gpt-oss-20b` (OpenAI-compatible, free tier, needs `NVIDIA_API_KEY`) when Gemini is rate-limited or its daily quota is used up:

```env
LLM_MODEL=gemini:gemini-flash-latest
LLM_FALLBACKS=gemini:gemini-flash-lite-latest,nvidia:openai/gpt-oss-20b
NVIDIA_API_KEY=...
```

NVIDIA retires models without much notice (a retired model returns HTTP 410), so check the current list at build.nvidia.com if the fallback stops working.

Premium model for the final answer only, free model for everything else:

```env
LLM_MODEL=gemini:gemini-flash-latest
LLM_MODEL_SYNTHESIS=anthropic:claude-opus-5-5
ANTHROPIC_API_KEY=...
```

Fully local:

```env
LLM_MODEL=ollama:llama3.1
LLM_FALLBACKS=
```

Pin a specific Gemini version for reproducible evaluations (aliases such as `gemini-flash-latest` move to newer models over time):

```env
LLM_MODEL=gemini:gemini-3.8-flash
```

## Evaluation

| Setting | Default | Notes |
|---|---|---|
| `EVAL_DATABASE_URL` | empty (uses `TEST_DATABASE_URL`) | **Wiped before every scenario.** Must not be the main database; the runner refuses if it is. |

## Tracking, alerts and notifications

| Setting | Default | Notes |
|---|---|---|
| `WORKER_TICK_SECONDS` | `60` | How often the worker runs a tick |
| `MONITOR_CHECKS_PER_HOUR` | `30` | Price checks per hour across all tracked items. Low-priority items slow down first to fit. Keep it within your provider quotas (SerpApi's free tier is small). |
| `ALERT_COOLDOWN_HOURS` | `6` | Minimum time between two price alerts of the same kind for the same item |
| `QUIET_HOURS` | empty | IST window with no email or Telegram, e.g. `23-7` or `22:30-07:00`. In-app alerts still appear. |
| `NOTIFY_CHANNELS` | empty | Extra channels besides in-app: `email`, `telegram` (comma-separated) |
| `SMTP_HOST`, `SMTP_PORT` (587), `SMTP_USERNAME`, `SMTP_PASSWORD`, `SMTP_FROM`, `NOTIFY_EMAIL_TO` | empty | Email via STARTTLS |
| `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` | empty | Create a bot with @BotFather; the chat id is your user or group id |

## Agent budgets (per request)

| Setting | Default | Notes |
|---|---|---|
| `AGENT_MAX_ITERATIONS` | `6` | LLM turns in the research loop |
| `AGENT_MAX_TOOL_CALLS` | `25` | Tool calls the model may make during research |
| `AGENT_MAX_SECONDS` | `150` | Wall-clock limit for research |

When a budget is reached the agent stops researching, notes the gap in the answer's uncertainties and still produces an answer from what it has.
