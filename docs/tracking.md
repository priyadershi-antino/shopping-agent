# Tracking, alerts and sale intelligence

Track the products you want and get told when buying makes sense, especially around sales such as Big Billion Days and the Great Indian Festival. Everything here is computed by code; the language model is not involved.

## Watchlist

A watch item is one listing (one exact variant at one seller) with optional targets:

| Target | Fires when |
|---|---|
| `target_effective` | The **best price for you** reaches it: the lowest price with your saved cards and memberships, without extra conditions such as exchange. This is the target the agent proposes, and the most useful one. |
| `target_price` | The listed price reaches it. |
| `target_drop_pct` | The listed price is at least this many percent below the price when tracking began. |

You can also be told when an item comes back in stock, when its offers change and when a sale starts. Each of these can be switched off per item.

How to track:

- **Web:** "Track this" on a product page, or the form on `/watchlist`.
- **CLI:** `shop watch add <link or listing id> --effective-target 65k`.
- **API:** `POST /v1/watchlist`.
- **From an answer:** the agent proposes "price alert" or "track price". You confirm it, and only then is it saved (see [Confirmed actions](#confirmed-actions)).

## A price check (`tracking/monitor.py`)

1. **Refresh** the listing through the product providers: a lookup by ASIN/FSN when there is one, otherwise the product's listings. This goes through the cache, freshness TTLs and daily quotas like any other provider call. The new price is stored as a price observation, so tracking builds the price history.
2. **Offers seen in this refresh replace the old ones.** Offers from that provider which no longer appear for that listing are marked inactive, so an ended offer stops counting. Offers you pasted yourself are never touched.
3. **Price for you:** the `find_offers` tool runs with your saved profile. The best unconditional scenario becomes the effective price. It is stored as an **effective-price snapshot**, giving a second history line ("Best price for you") on the product page chart.
4. **Compare with the previous check** (`services/alerts.py`, pure functions):
   - Price targets fire only when the price **crosses** them, not on every check while it stays below.
   - A first check that is already below a target fires once.
   - Back in stock: out of stock → in stock or limited.
   - Offers: each usable offer (eligible or conditional for you) has a stable identity (type, bank or provider, card type, title without numbers) and a description of its terms. New, changed and ended offers are reported.
5. **Record** each alert with a `dedup_key`, so it can't fire twice:
   - price alerts: item + kind + target + IST date;
   - offer alerts: item + kind + terms.

   Price alerts also respect `ALERT_COOLDOWN_HOURS` per item and kind.
6. **Schedule the next check** (see below).

A failed check doesn't alert. It stores the error on the item and retries in an hour, or after midnight IST when a provider's daily quota is used up.

## Priority monitoring (`services/monitor_priority.py`)

Items are not all checked on one timer.

**Priority** is scored from three things:

- your priority for the item (high, normal or low);
- how close the price is to its target (within 3% or within 10%);
- whether it moved 5% or more since the last check.

The score maps to one of four levels: urgent, high, normal or low.

**Interval (minutes)** depends on the sale state:

| Sale state | Urgent | High | Normal | Low |
|---|---|---|---|---|
| Active or early access | 10 | 15 | 30 | 60 |
| Pre-sale or post-sale | 30 | 60 | 180 | 360 |
| Normal | 60 | 180 | 360 | 720 |

**Quota budget:** the total demand across all active items must fit within `MONITOR_CHECKS_PER_HOUR`. If it doesn't, low-priority items are slowed first, then normal ones, and so on, up to once a day. The worker checks due items **most overdue first**, so when quota is short it is the least urgent checks that wait.

## Sales

Sale events (`sale_events`) come from three places:

- the user: `shop sale add`, `POST /v1/sale-events`, or the form on `/watchlist`;
- the demo seeder;
- later, verified sources.

Each event carries a verification level. Unconfirmed dates are always labelled as such.

### Sale Mode states

| State | When |
|---|---|
| Normal | No sale nearby |
| Pre-sale | Within 14 days before the first phase |
| Early access | Inside an early-access phase |
| Active | The main sale is on |
| Post-sale | Within 3 days after the sale ends |

### Sale Prep Plan (`services/sale_prep.py`)

A checklist for the next sale on `/watchlist`, from `shop sale prep`, or at `GET /v1/sale-prep`:

1. **Confirm the dates:** done only if an official source verified them.
2. **Track the products:** one exact variant each.
3. **Build a price baseline:** at least 14 days of history per item. Otherwise a raised-then-discounted price can look like a deal.
4. **Set target prices.**
5. **Check bank offers:** the sale's partner banks compared with your saved cards, by bank name only.
6. **Early access:** whether you have the membership the early-access phase needs.
7. **Exchange:** whether a device is on file. Exchange values are always a range.
8. **On sale day:** compare with pre-sale history, not MRP. Bank offers need the full amount on one card and have caps. Nothing here ever needs card details.

### Announcements and the sale-start brief (`tracking/sales.py`)

Each worker tick checks the recorded sales:

- **3 days before** the first phase, a **sale upcoming** alert carries the prep plan's open steps. It is sent once per event.
- **When early access or the main sale starts**, a **sale started** alert carries the **brief**. It is sent once per event and phase. For every tracked item on that platform, the brief shows:
  - the listed price now against the **pre-sale median** listed price (the median of days 8–30 before the sale). Both are listed prices, so the comparison is like for like, and a price raised just before the sale doesn't hide the real discount;
  - whether the price was raised shortly before the sale;
  - the price for you with your cards, and whether the target is met.

The brief is also on `/alerts`, from `shop sale brief`, and at `GET /v1/sale-brief`.

## Notifications (`tracking/notify.py`)

Every alert is in the **in-app inbox**:

- web: `/alerts`, with an unread count in the menu;
- CLI: `shop inbox`;
- API: `GET /v1/notifications`.

Optional channels, set with `NOTIFY_CHANNELS`:

- `email`: SMTP with STARTTLS, using `SMTP_*` and `NOTIFY_EMAIL_TO`;
- `telegram`: Bot API `sendMessage`, using `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`.

Delivery behaviour:

- Delivery is retried up to 5 times.
- External delivery waits during `QUIET_HOURS` (IST, for example `23-7`). In-app alerts still appear immediately.
- Stored and logged errors are reduced to a short reason, such as "Telegram returned HTTP 401". The bot token is part of the Telegram URL and never reaches the database or logs.

## The worker (`tracking/worker.py`)

```powershell
python -m uv run shop worker run          # keeps running; one tick every WORKER_TICK_SECONDS
python -m uv run shop worker run --once   # one tick, e.g. from Task Scheduler
python -m uv run shop watch check         # same as one tick, with a summary
```

The worker is a plain asyncio loop; a scheduler library wasn't needed. Each tick does three things:

1. sale announcements;
2. up to 20 due price checks, one after another so quotas and rate limits stay predictable;
3. delivery of pending notifications.

A counter in Redis lets only one worker process each tick, so running two is harmless. `POST /v1/worker/run-once` and the "Check due items now" button run a tick on demand.

## Confirmed actions

The agent proposes actions (`ProposedAction`, status `pending`). The model can't save anything itself: the write tools (`track_product`, `create_price_alert`, `save_user_preference`) are never offered to it, and the registry refuses them without `allow_side_effects=True`.

Confirming runs the matching write tool:

- **Web:** Confirm on the answer.
- **CLI:** `shop confirm <run_id> <action_id> [--target 61k]`.
- **API:** `POST /v1/runs/{id}/actions/{aid}/confirm`. An optional body `{"target_price": ...}` changes the target.

The action is then marked `confirmed` in the stored run. Confirming twice is refused. Dismiss marks it `dismissed`.

Mapping from action to tool:

- `price_alert` → `create_price_alert` with `target_effective`.
- `track_price` → `track_product`.
- `save_preference` needs a key and value from the user. Keys or values that look like payment or login data, such as card numbers, CVV, OTP, PIN or passwords, are refused.

## Tables (migration `0005`)

| Table | Purpose |
|---|---|
| `watch_items` | One per user and listing: targets, notification switches, priority, and the last check (price, price for you, availability, usable offers), next check time and last error |
| `effective_price_snapshots` | Price for you at each check, with the winning scenario |
| `alert_events` | Each alert once (`dedup_key` is unique): kind, title, message, data |
| `notifications` | Delivery per alert and channel: status, attempts, error, read time |
