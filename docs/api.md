# Interfaces: REST API, CLI and web UI

All three interfaces share one runtime (`src/shopping_agent/runtime.py`): the same database, cache, providers, tools and model. Anything you can do in one, you can do in the others.

Start the server with `python -m uv run shop serve` (default `http://127.0.0.1:8000`). The interactive API reference (OpenAPI/Swagger) is at `/docs`.

## Authentication

Authentication is off when `API_KEY` is empty, which is the default for local use. When it is set:

- API clients send the `X-API-Key: <key>` header.
- Browsers are sent to `/ui/key`, which checks the key and stores it in an HTTP-only, `SameSite=Strict` cookie (`sa_key`).
- `GET /v1/health` always stays open.

The key is compared in constant time. `API_KEY` is this app's own access key and has nothing to do with LLM provider keys.

## REST API (`/v1`)

Errors use FastAPI's `{"detail": "..."}` format:

| Status | Meaning |
|---|---|
| 400 | Bad reference, such as an unknown `listing_id` |
| 401 | Missing or wrong API key |
| 404 | Not found |
| 422 | Invalid input, or the model declined the request |
| 502 | The model returned output that didn't validate |
| 503 | A component is not configured or the model is unavailable |

Tool-backed endpoints return a common envelope:

```json
{"data": {...}, "errors": [], "partial": false, "evidence": [{"id": "...", "claim": "...", "source_name": "...", "verification": "verified", ...}]}
```

### Agent

| Method and path | What it does |
|---|---|
| `POST /v1/chat` | Ask the agent. Body: `{"message": str, "previous_run_id": str or null, "stream": true, "platforms": ["amazon", "flipkart"]}`. `platforms` (optional) limits the listings used to those marketplaces; omitted means the server's `DEFAULT_PLATFORMS`. Unknown names return 422. |
| `GET /v1/runs?limit=20` | Recent runs (summary). |
| `GET /v1/runs/{run_id}` | A full run, as a `RunView`. |

**Streaming (`stream: true`, the default)** returns Server-Sent Events:

| Event | `data` |
|---|---|
| `stage` | `{"type": "stage", "message": "Researching products, prices and reviews"}` |
| `tool_call` | `{"message": "<tool name>", "data": {"args": {...}}}` |
| `tool_result` | `{"message": "<tool name>", "data": {"status": "ok", "latency_ms": 12}}` |
| `warning` | For example `"1 figure(s) could not be verified"`. |
| `done` | The run's summary (status, counts, tokens). |
| `final` | The complete `RunView` (same shape as `GET /v1/runs/{id}`). |

The run keeps going and is saved even if the client disconnects. With `stream: false`, the response is the `RunView` directly.

**Follow-ups:** pass `previous_run_id`. The earlier request and answer are given to the model as context. The earlier answer's products (up to 5) are carried into the new run, where they are shortlisted with fresh offers and price history. So "which of those has the better GPU?" and "a cheaper one?" both work.

**`RunView`** fields:

- `status`: `completed`, `needs_clarification` (see `clarification_question`) or `failed` (see `error`).
- `answer`: the model's `FinalAnswer`, after the grounding check. Figures that couldn't be traced are marked `[unverified]`.
- `items`: the shortlist, computed by code. Each item has the listing, `best_eligible` (best price without extra conditions), `best_conditional` (needs card/EMI/membership/exchange; see the scenario `conditions`), the requirement `checks`, `scenarios` A–D, `by_payment_method`, `history` and `timing`.
- `proposed_actions`: suggestions such as tracking a price or setting an alert. They are `pending`; nothing is saved without confirmation.
- `sources`: evidence cited by the answer and evidence about the shortlist.
- `uncertainties`, `conflicts`, `warnings`, `demo_data`.
- Counts: `unverified_figures`, `tool_calls`, `llm_calls`, and token usage.

### Products and listings

| Method and path | What it does |
|---|---|
| `GET /v1/search?q=&category=&max_price=&limit=` | Search products. Returns candidates grouped by product and variant, each with its listings. |
| `POST /v1/products/resolve-link` | `{"url": ...}`. Amazon, Flipkart, Croma, Reliance Digital, Vijay Sales, Tata CLiQ or a manufacturer link. The link is parsed for its product ID and looked up through providers; the page itself is never fetched. Returns `status`: `resolved`, `needs_confirmation`, `needs_full_link` or `not_found`. |
| `GET /v1/listings/{id}` | One listing (price, seller, condition, freshness). |
| `GET /v1/listings/{id}/details` | Specifications, with source and conflicts. |
| `GET /v1/listings/{id}/sellers` | The same variant at other sellers (variant gate applied). Different variants are listed separately with reasons. |
| `GET /v1/listings/{id}/offers` | Offers checked against your **saved profile**: scenarios A–D, stacking matrix, card-wise prices, and excluded offers with reasons. |
| `GET /v1/listings/{id}/price-history?days=90` | Daily prices, baseline stats, coverage tier and pre-sale price-rise check. |
| `GET /v1/listings/{id}/reviews?use_case=` | Review themes (complaints, positives, use-case issues) with sources. Uses the model. |

### Offers, calculators and sales

| Method and path | What it does |
|---|---|
| `POST /v1/offers/parse` | `{"text": ..., "listing_id": optional, "save": false}`. The model reads pasted offer terms into structured offers. Saved offers are `user_provided` and tied to that listing only. |
| `POST /v1/offers/emi-cost` | `{"price", "tenure_months", "annual_rate_pct", "processing_fee", "discount_forgone"}`. The true cost of a "No-Cost EMI". |
| `POST /v1/budget/plan` | `{"budget", "slots": [{"name", "quantity", "required", "options": [{"id", "name", "unit_price"}]}], "sort"}`. Multi-item combinations within a budget. |
| `GET /v1/sale-events?platform=&include_past=` | Known sale events with phases, partner banks, verification and the current sale state. |

### Profile

Your profile holds only high-level eligibility information. Card numbers, CVV, OTPs and passwords are never asked for, and there is nowhere to put them.

| Method and path | What it does |
|---|---|
| `GET /v1/profile` | Cards, memberships, exchange devices, preferences and the list of known preference keys. |
| `DELETE /v1/profile` | Forget everything. |
| `POST /v1/profile/payment-methods` | `{"issuer": "HDFC", "card_type": "credit" or "debit", "network": optional}` |
| `DELETE /v1/profile/payment-methods?issuer=&card_type=` | Remove a card. |
| `POST /v1/profile/memberships`, `DELETE /v1/profile/memberships/{name}` | For example `amazon prime`, `flipkart plus`. |
| `POST /v1/profile/exchange-devices`, `DELETE /v1/profile/exchange-devices` | `{"category", "model", "condition"}` |
| `PUT /v1/profile/preferences/{key}`, `DELETE /v1/profile/preferences/{key}` | `{"value": any JSON}`. Known keys: `prefers_emi`, `preferred_brands`, `avoided_brands`, `preferred_marketplaces`, `typical_budget`, `important_features`. |

### Tracking, alerts and sales

See [tracking.md](tracking.md) for how checks, alerts and sale features work.

| Method and path | What it does |
|---|---|
| `POST /v1/runs/{run_id}/actions/{action_id}/confirm` | Carry out an action the agent proposed. Optional body `{"target_price": 61000}` changes the target; for `save_preference`, `{"key": ..., "value": ...}`. Already confirmed → 400; unknown → 404. |
| `POST /v1/runs/{run_id}/actions/{action_id}/dismiss` | Dismiss a proposed action. |
| `GET /v1/watchlist` | Everything you track, with the last check. |
| `POST /v1/watchlist` | `{"listing_id" or "url", "target_effective", "target_price", "target_drop_pct", "priority": "high/normal/low"}`. Tracks the listing, or updates its targets if already tracked. |
| `PATCH /v1/watchlist/{id}` | Change targets, priority, notification switches or `active`. `clear_targets: true` removes all targets first. |
| `DELETE /v1/watchlist/{id}` | Stop tracking. |
| `POST /v1/watchlist/{id}/check` | Check now. Returns the prices and any new alerts. |
| `GET /v1/watchlist/{id}/effective-history?days=90` | Listed price and price for you at each check. |
| `GET /v1/alerts?limit=50` | Recent alerts. |
| `GET /v1/notifications?unread_only=false` | The in-app inbox, with an `unread` count. |
| `POST /v1/notifications/read` | `{"id": ...}` marks one as read; `{}` marks all. |
| `GET /v1/sale-prep?platform=` | The Sale Prep Plan for the next sale. |
| `GET /v1/sale-brief` | Tracked items against their pre-sale prices for the live (or next) sale. 404 if no sale is recorded. |
| `POST /v1/sale-events` | Record a sale: `{"name", "platform", "start", "end" (with timezone), "early_access_hours", "early_access_for", "partner_banks", "official_sources"}`. Stored as user-provided. |
| `POST /v1/worker/run-once` | Run one worker tick now: sale announcements, due checks and deliveries. |

### Health

`GET /v1/health` reports database, cache, demo mode and provider names. It returns 503 when the database or cache is down.

## Command line (`shop`)

Run commands with `python -m uv run shop <command>`, or just `shop` inside the virtual environment. `--help` works on every command.

| Command | What it does |
|---|---|
| `shop ask "laptop under 80k for coding, I have an HDFC credit card"` | One question. Shows progress, then the answer, computed prices, suggested actions and sources. Options: `--follow-up <run_id>`, `--json`, `--platform/-p amazon -p flipkart` (repeatable). |
| `shop chat` | A conversation: each message follows up on the previous answer. `new` starts a fresh topic; an empty line exits. |
| `shop runs`, `shop run <run_id>` | List and show saved runs. |
| `shop link <url>` | Resolve a product link, then show offers for your cards and the price history. |
| `shop history <listing_id or url> [--days 90]` | Price stats, a sparkline and the list of price changes. |
| `shop offers <listing_id or url>` | Scenarios A–D, card-wise prices, every offer and whether it applies to you. |
| `shop offer-parse "<terms>" [--listing <id or url>] [--save]` | Read pasted offer terms (uses the model). |
| `shop budget 1L -i "phone: Nova Pixelon 9 = 30,599" -i "phone: Acme Volt 5G = 18,999" -i "tablet: Zenith Tab S = 27,999" [--save-most]` | Every combination of items that fits one budget. Use `slot x2:` for two units and `slot?:` for an optional item. |
| `shop emi 65k 6 15 [--fee 199] [--forgone 2000]` | The true cost of a No-Cost EMI. Amounts accept `65k`, `1.2L` and `₹65,000`. |
| `shop parse-link <url>` | Show what a link identifies (offline). |
| `shop profile show` / `add-card HDFC credit` / `remove-card` / `add-membership "amazon prime"` / `remove-membership` / `add-device smartphone --model ...` / `set typical_budget 80000` / `unset` / `forget --yes` | Manage your profile. `add-card` refuses anything that looks like a card number. |
| `shop demo-seed [--days 60]` | Demo mode only: load the fictional catalog with a synthetic price history and a fictional "DEMO Festive Sale" starting in 3 days, so history charts and sale features have data. Refuses to run when real providers are configured unless you pass `--force`. Safe to run again. |
| `shop watch add <link or id> [--effective-target 65k] [--target 70k] [--drop-pct 10] [--priority high]` | Track a product and check it straight away. |
| `shop watch list` / `shop watch check [<id>]` / `shop watch remove <id>` | List, check now (all due items when no id is given), or stop tracking. |
| `shop inbox [--all] [--keep-unread]` | Your alerts. Shown alerts are marked as read. |
| `shop confirm <run_id> <action_id> [--target 61k]` | Carry out a suggested action from an answer. |
| `shop sale prep [--platform flipkart]` / `shop sale brief` | Sale Prep Plan and sale-start brief. |
| `shop sale add "Big Billion Days" --platform flipkart --start 2026-10-10 --end 2026-10-16 [--early-access-hours 24 --early-access-for "flipkart plus"] [--bank HDFC]` | Record a sale you know about (times are IST unless given with an offset). |
| `shop worker run [--once]` | The background worker: sale announcements, due price checks, notifications. |
| `shop eval run [--only id,id] [--tag offers] [--model provider:model] [--name baseline]` / `shop eval list` | The evaluation suite (see [evaluation.md](evaluation.md)). |
| `shop serve [--port 8000] [--reload]` | Run the API and web UI. |
| `shop version` | Print the version. |

## Web UI

The web UI is server-rendered (Jinja2) plus one small script, `web/static/app.js`, with no build step. It follows the system light/dark setting.

| Page | What it shows |
|---|---|
| `/` | Ask box with "Show prices from" checkboxes (Amazon, Flipkart, Croma, Reliance Digital, Vijay Sales, Tata CLiQ, other sellers; remembered in the browser), live progress (reads the SSE stream), a product-link box and recent questions. After an answer, "Follow-up to the last answer" continues the conversation. |
| `/runs/{id}` | The full answer: recommendations, deal insights, timing, the computed price table with requirement checks (✓ ◐ ✗ ?), scenarios A–D with conditions, card-wise prices, price history, suggested actions to confirm, uncertainties, conflicts and numbered sources. |
| `/link?url=` | Resolves a link and opens its product page. If the link is ambiguous, lists the possible matches. |
| `/listing/{id}?days=90` | Product page with the price-history chart, stats, offers and scenarios, the stacking matrix, other sellers of the same variant, specifications and a "paste an offer" form. |
| `/profile` | Add and remove cards, memberships, exchange devices and preferences, or delete everything. |
| `/budget` | Budget planner: a total budget plus `item: product = price` lines → every combination that fits, sorted by budget used or money saved. |
| `/watchlist` | The Sale Prep Plan with done and to-do steps, a form to add a sale, and the tracked products (listed price, price for you, targets, last and next check, errors) with Check and Remove buttons. |
| `/alerts` | The sale brief, when there is a sale, and the alert inbox. The menu shows the unread count. |

On a product page, "Track this" sets targets. Once a product is tracked, its chart gets a second line, "Best price for you", from the effective-price snapshots.

The **price-history chart** is a server-rendered SVG:

- Prices are drawn as steps, with the lowest and highest observed prices labelled.
- Hovering shows a crosshair and tooltip.
- A "Show as table" view is always available.
- Colours come from CSS variables, with separate light and dark values.
- It only ever says "lowest observed", never "lowest ever".
