# Shopping Agent documentation

A personal AI shopping researcher and deal analyst for India. It works out the **actual effective price you can get** (eligibility checked, offers never assumed to stack, every number traced to evidence) and helps you buy at the right time and place, including during sales such as Flipkart Big Billion Days and Amazon Great Indian Festival. You always make the final purchase decision; the agent never buys anything.

| Document | What it covers |
|---|---|
| [Setup](setup.md) | Install, databases, API keys, running the tests, demo mode |
| [Configuration](configuration.md) | Every setting, its default and when to change it |
| [Architecture](architecture.md) | Components, the agent pipeline, data flow, data model, observability |
| [Deal engine](deal-engine.md) | How effective prices, offer stacking, EMI cost, price history and timing are computed |
| [Security](security.md) | Untrusted content, prompt injection, URL fetching, secrets, write actions |
| [Extending](extending.md) | Adding a tool, a data provider, an LLM provider, a prompt or a migration |
| [Interfaces](api.md) | REST API (with streaming), the `shop` CLI and the web UI |
| [Tracking and sales](tracking.md) | Watchlist, price checks, alerts, offer changes, priority monitoring, Sale Mode, prep plan, sale brief, notifications, worker |
| [Evaluation](evaluation.md) | Scenario dataset, metrics, running the suite, the baseline and what it found |
| [Testing](testing.md) | Test layout, markers, the scripted LLM, live checks |

## Status

| Milestone | Scope | State |
|---|---|---|
| M0 | Scaffold: config, logging, Postgres, Redis, health endpoint | Done |
| M1 | Deterministic core: offer engine, price baseline, variant gate, parsers | Done |
| M2 | Providers (SerpApi, Tavily, demo), 11 tools, persistence, security | Done |
| M3 | LLM layer (Gemini default, swappable) and the agent | Done |
| M4 | Interfaces: REST API with streaming, CLI, minimal web UI | Done |
| M5 | Evaluation dataset, runner and baseline report | Done |
| M6 | Watchlist, price alerts, background worker, sale intelligence, notifications | Done |

Possible next steps: official affiliate APIs (Amazon PA-API, Flipkart Affiliate) as providers, third-party price history, automated verification of sale dates from official pages, and a post-purchase price-drop watch.
