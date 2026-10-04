# Shopping Agent: notes for Claude Code

An AI shopping research and deal-intelligence agent for India (Python 3.13, FastAPI, Postgres, Redis). Documentation is in `docs/` (start at `docs/README.md`). Keep the docs in step with the code: when you change behaviour, settings, tools, tables or prompts, update the matching doc in the same change.

## Commands (Windows; `uv` is not on PATH, so use `python -m uv`)

```powershell
python -m uv run ruff format .; python -m uv run ruff check .   # lint/format
python -m uv run mypy                                           # strict, covers src and tests
python -m uv run pytest -q                                      # needs Postgres + Redis for db/redis tests
python -m uv run alembic upgrade head                           # dev DB; tests migrate their own DB
python -m uv run alembic revision --autogenerate -m "..."       # then rename to the next 000N id
```

A milestone is done only when ruff, format check, mypy and pytest are all green.

## Environment

- Postgres 18 is a Windows service. Redis runs in WSL. Use `127.0.0.1`, not `localhost`: WSL forwards IPv4 only.
- `.env` holds secrets. Never print or commit it. Inspect key presence without showing values.
- With no data-provider keys, the app runs on the FICTIONAL demo catalog (`providers/fixtures/demo_catalog.json`: brands Acme, Zenith, Nova, Orbit). Keep demo data clearly labelled.
- PowerShell 5.1 mangles inline `python -c` quoting and writes UTF-8 with a BOM. Put scripts in the scratchpad instead.

## Architecture rules (do not break)

- **Code computes, the LLM reasons.** Prices, offer stacking, effective-price scenarios, price statistics and ranking live in `services/` (deterministic, unit-tested). The LLM never does arithmetic.
- **Offers:** unknown stackability means not combined. Conditional prices (card, EMI, membership, exchange) are always shown with their conditions. Upfront cost is separate from deferred cashback.
- **Price history:** never "lowest ever"; only "lowest in our N-day observed history", and only with minimum coverage.
- **Variant gate:** never compare prices across variants (RAM, storage, connectivity, condition, region).
- **Provenance:** external facts carry `Evidence`. The grounding guard (`agent/facts.py`) marks untraceable figures `[unverified]`.
- **Security:** external text is wrapped in `<untrusted_content>`. `fetch_page` uses an allowlist plus a public-IP check. Amazon and Flipkart pages are never scraped: links are parsed and resolved via providers. Never ask for or store card numbers, CVV, OTP or passwords.
- **Tools:** the LLM sees only the 11 read-only tools. The 3 write tools (`track_product`, `create_price_alert`, `save_user_preference`) need `allow_side_effects=True`, which is set only by a user's explicit action or confirmation (`tracking/actions.py`).
- **Providers and LLMs** sit behind protocols plus registries. Switching models is config only (`LLM_MODEL=provider:model`). Claude goes through the official `anthropic` SDK (no forced `tool_choice`, no `temperature`, structured output via `output_config.format`). Other providers go through the OpenAI-compatible adapter (httpx).
- **Prompts** are versioned markdown in `src/shopping_agent/prompts/`. Bump `<!-- version: N -->` when the behaviour changes.

## Layout

`domain/` types · `services/` deterministic logic · `providers/` data sources, cache, quota · `security/` · `db/` models, migrations, repositories · `tools/` 11 read-only + 3 write tools · `llm/` adapters, routing, structured output · `agent/` orchestrator, analyzers, facts and grounding, run store, views · `prompts/` · `runtime.py` shared runtime · `api/` REST + SSE · `web/` templates, static, SVG chart · `cli/` Typer + Rich · `demo.py` demo seeding · `tracking/` watch checks, alerts, sales, notifications, worker · `evals/` scenarios, metrics, runner, reports

Tests: the `runtime` fixture (tests/conftest.py) gives a full Runtime on the test DB with the demo catalog, a fixed clock and the scripted discovery model (`tests/scenarios.py`). CLI tests call the async command functions; `asyncio.run` inside tests is avoided.

## Status

All planned milestones are done: M0 scaffold, M1 deterministic core, M2 providers/tools/persistence, M3 LLM layer and agent, M4 interfaces (REST + SSE, `shop` CLI, web UI), M5 evaluation (`evals/`, `docs/evaluation.md`), M6 tracking, alerts and sale intelligence (`tracking/`, `docs/tracking.md`).

Evaluation: `shop eval run` (real model, demo data) wipes `EVAL_DATABASE_URL` (default `TEST_DATABASE_URL`) per scenario. Reports are in `evals/reports/` (`baseline-initial` is before the fixes it found; `baseline` is the current one). A full run is about 33 × 40 s and roughly 450 model calls, so mind free-tier quotas.

Pending (needs Gemini quota): rerun `shop eval run --only follow-up-gpu,axis-cashback,hinglish-lakh,budget-range`. Two of these check fixes made after the last run (follow-up carry-over, card-offer facts / synthesis prompt v5). The other two hit the exhausted daily quota.
