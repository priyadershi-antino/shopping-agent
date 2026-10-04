# Shopping Agent

A personal AI shopping researcher and deal analyst for India. It researches products across sources, checks variants, works out the **actual effective price** you can get after verified offers, tracks price history, and helps you get the most from sales such as Flipkart Big Billion Days and Amazon Great Indian Festival.

The agent never buys anything. You always make the final purchase decision.

> Status: all planned milestones are complete: deterministic deal engine, providers and tools, the agent, API, CLI and web UI, the evaluation suite, and tracking, alerts and sale intelligence. See [docs/](docs/README.md).

## How it works

One agent with fixed stages and a bounded research loop:

```
understand request → resolve links / pasted offers → plan → research (LLM + read-only tools,
capped by iterations, tool calls and time) → deterministic analysis (offers, effective prices,
price history, requirement matching) → final answer (LLM) → grounding check → proposed actions
```

The LLM decides what to research and how to explain it. Prices, offer combinations and statistics are always computed by code. Every figure in the final answer is checked against the collected data, and anything untraceable is marked `[unverified]`. Saving data (tracking, alerts, preferences) is only proposed and needs your confirmation.

## Language models

The default is free: Gemini via Google AI Studio (`GEMINI_API_KEY`), with automatic fallback to a second model when the first is overloaded. To use a premium model, change config only, e.g. `LLM_MODEL=anthropic:claude-opus-5-5`, or use one only for the final answer with `LLM_MODEL_SYNTHESIS`. See `.env.example`.

## Data sources

| Purpose | Provider | Key |
|---|---|---|
| Product search, per-store prices, Amazon ASIN lookup | [SerpApi](https://serpapi.com) (Google Shopping India) | `SERPAPI_API_KEY` |
| Reviews, offer terms, sale announcements | [Tavily](https://tavily.com) web search | `TAVILY_API_KEY` |
| Specs, bank terms, reviews (page reading) | Allowlisted sites only | — |

Without keys the app runs on a **fictional demo catalog** (brands such as Acme, Zenith, Nova and Orbit), clearly labelled "DEMO". Marketplace product pages are never scraped: links are parsed for IDs and resolved through providers. Each provider has a daily request budget (`*_DAILY_QUOTA`), so free tiers last through a sale.

## Documentation

Full documentation is in [docs/](docs/README.md): [setup](docs/setup.md), [configuration](docs/configuration.md), [architecture](docs/architecture.md), [deal engine](docs/deal-engine.md), [security](docs/security.md), [interfaces: API, CLI, web UI](docs/api.md), [tracking and sales](docs/tracking.md), [evaluation](docs/evaluation.md), [extending](docs/extending.md) and [testing](docs/testing.md).

## Requirements

- Python 3.13+
- [uv](https://docs.astral.sh/uv/) (`pip install uv`)
- PostgreSQL (tested on 18)
- Redis (tested on 8)

## Setup

```powershell
python -m uv sync
copy .env.example .env      # then fill in DB credentials and API keys
python -m uv run alembic upgrade head
```

## Development

```powershell
python -m uv run ruff check .
python -m uv run ruff format --check .
python -m uv run mypy
python -m uv run pytest
```

Tests marked `db` need the `shopping_agent_test` database. Tests marked `redis` need Redis. Both are skipped automatically when the service isn't reachable.

## Running

```powershell
python -m uv run shop serve     # API + web UI on http://127.0.0.1:8000 (API reference at /docs)
python -m uv run shop ask "laptop under 80k for coding, I have an HDFC credit card"
python -m uv run shop chat      # conversation with follow-ups
python -m uv run shop link "https://www.amazon.in/dp/..."   # offers for your cards + price history
python -m uv run shop profile add-card HDFC credit          # bank + card type only, never numbers
python -m uv run shop demo-seed # demo mode: synthetic price history and a fictional sale
python -m uv run shop watch add "https://www.flipkart.com/..." --effective-target 65k
python -m uv run shop worker run   # background price checks, sale announcements, alerts
python -m uv run shop sale prep    # checklist for the next sale
python -m uv run shop eval run     # evaluation suite (real model, demo data)
```

See [docs/api.md](docs/api.md) for every endpoint and command.
