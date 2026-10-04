# Setup

## Prerequisites

| Requirement | Notes |
|---|---|
| Python 3.13+ | |
| [uv](https://docs.astral.sh/uv/) | `pip install uv`. If `uv` is not on your PATH, run it as `python -m uv …` (used throughout these docs). |
| PostgreSQL (tested on 18) | Stores the catalog, price history, offers, evidence and agent runs. |
| Redis (tested on 8) | Caching, provider quotas and LLM rate limiting. Redis running in WSL works. |

> **Windows + WSL:** use `127.0.0.1`, not `localhost`, in connection URLs. `localhost` resolves to IPv6 first and WSL forwards only IPv4, so connections to `localhost` time out.

## 1. Install dependencies

```powershell
python -m uv sync
```

## 2. Configure

```powershell
copy .env.example .env
```

Edit `.env` (never commit it):

- `DATABASE_URL` and `TEST_DATABASE_URL`: your Postgres password.
- `GEMINI_API_KEY`: a free key from [Google AI Studio](https://aistudio.google.com). This powers the agent.
- Optional: `SERPAPI_API_KEY` and `TAVILY_API_KEY` for live product and web data. Without them the app uses the demo catalog (see below).

See [Configuration](configuration.md) for every setting.

## 3. Create the databases and run migrations

Create two empty databases, `shopping_agent` and `shopping_agent_test`, for example with pgAdmin or:

```sql
CREATE DATABASE shopping_agent;
CREATE DATABASE shopping_agent_test;
```

Then apply the migrations to the development database (tests migrate their own database automatically):

```powershell
python -m uv run alembic upgrade head
```

## 4. Check everything works

```powershell
python -m uv run ruff check .
python -m uv run ruff format --check .
python -m uv run mypy
python -m uv run pytest -q
```

Tests marked `db` need the test database and tests marked `redis` need Redis. They are skipped automatically when the service isn't reachable. See [Testing](testing.md).

## Demo mode

With no `SERPAPI_API_KEY`/`TAVILY_API_KEY`, the app runs on a **fictional demo catalog** (`src/shopping_agent/providers/fixtures/demo_catalog.json`). Its brands (Acme, Zenith, Nova, Orbit), prices and offers are invented and every source is labelled "DEMO", so demo results can't be mistaken for real market data. The catalog deliberately includes realistic traps:

- an 8 GB listing that contradicts the manufacturer's 16 GB specification,
- a renewed unit priced far below new ones,
- bank offers that combine with a coupon, an exchange offer and a deferred cashback offer,
- a web page containing a prompt-injection attempt.

The agent still needs an LLM key in demo mode.

## Running

```powershell
python -m uv run shop demo-seed                  # optional: load the fictional catalog, price history and sale
python -m uv run shop serve                      # web UI at http://127.0.0.1:8000, API docs at /docs
python -m uv run shop ask "laptop under 80k for programming, I have an HDFC credit card"
python -m uv run shop --help                     # all commands
```

Add `python -m uv run shop worker run` in a second terminal for price tracking and alerts (see below).

## Moving from demo to real data

1. Add `SERPAPI_API_KEY` to `.env` (Tavily is optional). The DEMO banner disappears once real product data is in use.
2. Remove the fictional history and sale that `shop demo-seed` wrote, so they can't appear next to real products: `python -m uv run shop demo-clear`.
3. Without a Tavily key there is **no web search**: no independent reviews, offer pages or sale announcements. Prices, listings, effective prices and history still work, and the answer says that web research was unavailable. Paste offer terms yourself when you need them.

**Choosing platforms:** the ask form has "Show prices from" checkboxes, and the CLI has `--platform`. With SerpApi, Amazon is searched directly on Amazon.in and the others come from Google Shopping's per-store prices (Flipkart appears there). Only listings from the chosen platforms are used. Each search spends SerpApi searches (see `SERPAPI_DAILY_QUOTA`), and fewer platforms means fewer.

## Data-source accounts

| Service | Used for | Free tier |
|---|---|---|
| Google AI Studio | Gemini (default LLM) | Yes, with rate limits; overloaded models return 503, so a fallback model is configured |
| SerpApi | Google Shopping India search, per-store prices, Amazon ASIN lookup | Yes, small monthly quota; the app spends at most `SERPAPI_DAILY_QUOTA` requests per day |
| Tavily | Web search for reviews, bank-offer terms, sale announcements | Yes; capped by `TAVILY_DAILY_QUOTA` |
| Groq / OpenRouter / Ollama | Optional alternative or fallback LLMs | Free options available |
| Anthropic | Optional premium LLM (Claude) | Paid |

## Tracking and alerts

Run the worker next to the server so tracked prices are checked and alerts are sent:

```powershell
python -m uv run shop worker run
```

Alternatively, schedule `shop worker run --once` with Windows Task Scheduler every few minutes. For email or Telegram alerts, set `NOTIFY_CHANNELS` and the matching settings (see [configuration.md](configuration.md#tracking-alerts-and-notifications)).

## Evaluation

`shop eval run` uses `EVAL_DATABASE_URL`, falling back to `TEST_DATABASE_URL`. It wipes that database before every scenario, so a separate database such as `shopping_agent_eval` is best. See [evaluation.md](evaluation.md).
