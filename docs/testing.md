# Testing

```powershell
python -m uv run pytest -q                 # everything
python -m uv run pytest tests/unit -q      # fast, no services needed
python -m uv run pytest -m "not db" -q     # skip database tests
```

A milestone is done only when `ruff check`, `ruff format --check`, `mypy` (strict, covering `src` and `tests`) and `pytest` all pass.

## Layout

| Folder | What it tests | Needs |
|---|---|---|
| `tests/unit/` | Deterministic services, parsers, security, provider parsing (HTTP mocked with `respx`), LLM adapters, structured output, prompts, agent logic | Nothing |
| `tests/integration/` | Repositories, migrations, the 11 tools on the demo catalog, the tool registry, the REST API (`test_api.py`), web pages (`test_web.py`), CLI commands (`test_cli.py`), demo seeding, Redis cache | Postgres test DB, Redis |
| `tests/agent/` | Whole agent runs driven by a scripted LLM | Postgres test DB |

Tracking is tested end to end in `tests/integration/test_tracking.py`. It uses a demo provider whose prices, offers and failures the test can change, a cache that never caches and a controllable clock. The tests cover:

- write tools refused without confirmation, and sensitive preferences rejected;
- a price crossing the target exactly once, with no repeat while it stays below;
- an ended offer being detected;
- a quota failure postponing the check to the next IST morning;
- sale announcements firing once per event and phase;
- Telegram delivery with retries and quiet hours, without storing the token;
- confirming a proposed alert;
- worker ticks checking only due items.

`test_tracking_interfaces.py` covers the same flows through the API, web pages and CLI. `test_tools.py` includes a stress test that runs six overlapping searches in parallel, three times, to guard against database deadlocks between concurrent tool calls. The evaluation runner is tested with the scripted model in `test_eval_runner.py`.

Markers: `db` (needs `TEST_DATABASE_URL`) and `redis` (needs `REDIS_URL`). Both skip automatically when the service is unreachable.

## Fixtures

- `db`: a session factory on the test database. Migrations run once per session and tables are truncated before each test.
- `demo_registry` / `tool_ctx`: providers backed by the fictional demo catalog with an in-memory cache, and a tool context with a fixed clock (`tests/factories.py: NOW`).
- `runtime`: a full `Runtime` on the test database, the demo catalog, a fixed clock and a scripted model playing the standard discovery scenario (`tests/scenarios.py`). API and web tests wrap it with `create_app(runtime=...)` and call the app in-process through `httpx.ASGITransport`. CLI tests call each command's async function with it and record the Rich output; commands that need no database run through Typer's `CliRunner`.
- `tests/factories.py`: builders for listings, offers (bank, coupon, exchange, cashback), profiles and daily price series.

## The scripted LLM (`tests/fakes.py`)

Agent tests don't call a real model. `ScriptedProvider` receives the messages, the tools offered and the requested response schema, and returns text, JSON or tool calls from a handler function. This makes agent behaviour testable deterministically:

- discovery end to end (tool use, invalid arguments, enrichment, ranking, grounding, review themes, persistence, events),
- a vague request answered with one clarifying question,
- stopping at the tool budget, and the fallback answer when synthesis output is invalid,
- refusing write and unknown tools,
- a clean failure when every model route is down,
- a product link with pasted offer terms applied to the right listing.

## Live checks

Real-model behaviour can't be unit-tested, so it is checked with live runs and the evaluation suite (M5) against the demo catalog. Run them sparingly: they use your free-tier quota (one request costs roughly 12–16 model calls). Things live runs have caught so far:

- a model skipping planned steps (fixed by deterministic enrichment),
- pasted offer text read as a budget,
- "good deal" claims without enough history,
- ranking by price alone,
- model numbers flagged as unverified prices.
