# Deal engine

All price and offer logic is deterministic code in `src/shopping_agent/services/`, covered by unit tests. The LLM quotes these results; it never computes them.

## Offers

An `Offer` (`domain/offer.py`) records its type (`seller`, `coupon`, `membership`, `bank`, `cashback`, `exchange`, `other`), its benefit (a flat amount or a percentage with an optional cap), minimum order value, payment requirement (issuer, credit/debit, network), EMI rule, membership requirement, exchange terms, category scope, stacking terms, whether the benefit is instant or deferred, validity dates, and its **verification level**:

| Level | Meaning |
|---|---|
| `verified` | From an official or primary source |
| `source_reported` | From a secondary source (aggregator, news, model extraction with complete terms) |
| `user_provided` | Pasted or typed by the user |
| `unverified` | Incomplete or unconfirmed, e.g. a percentage offer with no stated cap |

A scenario's verification is the weakest of the offers it uses.

### Eligibility (`offer_engine.check_eligibility`)

Each offer is classified for the user as **eligible**, **conditional** (achievable, but depends on something unconfirmed or an action) or **ineligible**, with reasons:

| Check | Result |
|---|---|
| Offer is for another platform | ineligible |
| Expired | ineligible |
| Starts in the future | conditional ("offer starts …") |
| Category not covered / excluded | ineligible (conditional if the category is unknown) |
| Listing price below the minimum order value | ineligible |
| Needs a card and the user has saved no cards | conditional ("requires HDFC credit card") |
| Needs a card and none of the user's cards match (bank, credit/debit) | ineligible |
| Needs a specific network the user's card doesn't confirm | conditional |
| EMI-only offer | conditional unless the user prefers EMI |
| Full-payment-only offer and the user prefers EMI | conditional |
| Needs a membership the user doesn't have | conditional |
| Exchange offer | always conditional (value depends on the device) |
| `unverified` offer | conditional |
| Deferred cashback, stale information, unknown EMI rule | note only |

Bank names are normalized ("HDFC Bank Ltd" matches "hdfc", "State Bank of India" matches "SBI"). A co-branded card counts for its bank: a "Flipkart Axis Bank" card satisfies an offer for Axis cards. It doesn't work the other way: a plain Axis card does not get an offer that is only for the Flipkart Axis card.

### Stacking (`offer_engine.pair_status`)

For every pair of offers, the stacking matrix says **combinable**, **incompatible** or **unknown**:

1. Either offer is exclusive ("cannot be clubbed") → incompatible
2. Two offers of the same one-per-order type (bank, coupon, exchange) → incompatible
3. Offers on different platforms → incompatible
4. Offers that need cards from different banks → incompatible
5. The terms of either offer say it combines with the other's type → combinable
6. Otherwise → **unknown, and unknown is never combined**

### Applying offers

Offers in a combination are applied in a fixed order: seller → coupon → membership → other → bank → cashback → exchange. Each percentage is computed on the order value **after** the earlier discounts. That is the conservative assumption, since a smaller base gives a smaller discount. The minimum order value is checked at the point of application, so a coupon can make a bank offer's minimum fail. Caps are applied, and no benefit can exceed the order value.

- **Instant** benefits reduce the price paid at checkout.
- **Deferred** cashback is calculated at its position but not deducted; it is reported separately as "cashback later".
- **Exchange** values are a range: the low end is the stated minimum (or zero) plus any bonus, and the high end is the "up to" value plus the bonus. The checkout price is shown as best case and conservative.

### Scenarios (`offer_engine.build_scenarios`)

Every valid combination of usable offers is enumerated (only pairs that are explicitly combinable). The engine then reports:

| Label | Scenario | Shown when |
|---|---|---|
| A | Standard price | Always |
| B | Best single offer the user is eligible for | It beats A |
| C | Best combination of eligible offers | It beats B |
| D | Best price with conditions (exchange, EMI, membership, unconfirmed eligibility) | Its best case beats the best unconditional price; always listed with its conditions |

Scenarios are ranked by net cost after cashback, then by checkout price. Ineligible offers are listed separately with their reasons.

**Worked example (demo data, HDFC credit card holder).** A Flipkart listing at ₹73,490 has a 10% HDFC credit-card offer (cap ₹4,000, minimum ₹30,000, combinable with coupons), a ₹2,000 coupon, an exchange offer (₹2,000–₹15,000 plus a ₹3,000 bonus) and a 5% Axis cashback offer:

- A: ₹73,490
- B: ₹69,490 (HDFC alone: 10% = ₹7,349, capped at ₹4,000)
- C: ₹67,490 (coupon first, then 10% of ₹71,490 capped at ₹4,000)
- D: from ₹55,490 best case (exchange; conditional on the device)
- The Axis offer is excluded: none of the user's cards match.

### Card-wise matrix (`offer_engine.card_matrix`)

For each saved payment method, the best price with no conditions, using only offers that card qualifies for, plus a "without card offers" row. This answers "which card should I use?".

### True cost of No-Cost EMI (`offer_engine.no_cost_emi_true_cost`)

With No-Cost EMI the merchant discounts the interest upfront, so the instalments add up to the price. The bank still charges GST on the interest portion and a processing fee plus GST, and any discount that only applies to full payment is lost:

```
EMI factor   = r(1+r)^n / ((1+r)^n − 1)      r = annual rate / 12, n = months
principal    = price / (n × factor)
interest     = price − principal
true cost    = price + GST on interest + processing fee × (1 + GST) + forgone discount
```

## Price history (`price_baseline.py`)

History is built only from our own observations. Each listing records a timestamped price whenever it is seen; identical repeats within 30 minutes are skipped.

- **Daily closes:** the last price seen on each Indian calendar day.
- **Minimum and maximum** use every observation, so intraday lows count. **Average and median** use the daily closes.
- **Coverage tier** from the observed span: under 14 days *insufficient*, 14–29 *minimum*, 30–59 *good*, 60+ *strong*.
- **Trend:** mean of the last 7 days against the 7 days before, with ±2% as the threshold. It needs at least 2 days in each window, otherwise it is unknown.
- **At or below observed low:** the current price is at or below every earlier observation.
- **Wording:** never "lowest ever". At most "Lowest price in our N-day observed history (M observations)", and only with at least *minimum* coverage.
- **Discount vs MRP** is reported, but savings are judged against the observed history, not the list price.

### Pre-sale price hikes (`detect_pre_sale_hike`)

The median price in the 7 days before a sale is compared with the median of days 8–30 before it. An increase of 5% or more is flagged, and sale prices should then be compared with the earlier median. At least 3 days of data are needed in each window.

## Timing (`timing.py`)

Buy-now-or-wait advice comes from evidence, with no predictions. The first rule that applies wins:

1. The effective price is at or below the user's target → **target met**
2. A sale is starting soon (pre-sale) → **consider waiting for the sale**, with the wording "prices may or may not drop"
3. The price is at the observed low, with usable history → **consider buying**
4. The user has a target → **set an alert** at the target
5. Usable history and the observed low is below the current price → **set an alert** at the observed low
6. Otherwise → **need more data** (tracking builds history)

Signals such as recent trend, a sale being active, an offer expiring within 48 hours, or low stock are attached as evidence. When a pre-sale price rise is detected, it is added as a signal and stated in the summary ("judge sale discounts against ₹X, not the raised price"), and the synthesis prompt (v3) requires the answer's timing section to mention it. The observed range in the summary is of the listed price, so it is worded that way to avoid comparing it with an after-offer price.

## Variant gate (`variant_gate.py`)

A listing enters a price comparison only if it matches the target variant:

- **Rejected:** a different model number, RAM, storage, connectivity (Wi-Fi vs cellular), model year or generation; a renewed, refurbished, open-box or used unit (unless the user accepts that condition); an international or imported unit.
- **Passed but unverified:** the listing doesn't state an attribute the target specifies, or its condition is unknown.

Rejected listings are shown separately as "different variant", never mixed into the comparison.

**Risk flags:** not new, international unit (India warranty may not apply), seller not stated, seller not on a known authorised list, and a price below 60% of the median of at least two comparable listings ("verify seller, condition and variant").

## Which seller represents a product

A product (one variant) is often sold by several sellers. The shortlist shows the seller where **this user pays least**: the lowest best price without extra conditions, using their cards and memberships. That is not always the lowest listed price. In the demo, Flipkart lists the Nova Swift 16 at ₹68,990 and Amazon at ₹69,990, but with an SBI card Amazon is ₹66,490. Offers are therefore computed for up to 3 sellers of each shortlisted variant (`SELLERS_PER_PRODUCT`). This tool reads stored data only, so it costs no provider quota.

## Requirement matching (`requirement_match.py`)

Each requirement is checked as **meets**, **partial**, **fails** or **unknown**:

- Numeric (`≥`, `≤`), equality, one-of, not-one-of and contains checks against the product's attributes and specifications. A qualitative wish ("good battery") is reported as unknown; it is assessed from specs and reviews.
- Numbers with units are compared as numbers ("16GB", "6000 mAh", "1 TB" = 1024 GB).
- Text matches when every word of the requirement appears in the fact, after normalising synonyms. So "RTX 4060" matches "NVIDIA GeForce RTX 4060 8GB", "brand new" matches "new", and "Wi-Fi" matches "wifi". "RTX 4060" does not match "RTX 4050", and "new" does not match "renewed".
- Requirements that name the product ("model = Nimbus 15") are checked against its title and model number.
- Budget: meets if the listed price fits; partial if it fits only with eligible offers, or only if offer conditions are met; for an approximate budget ("around 80k"), partial up to 10% over with a note; otherwise fails.

### Suspiciously cheap items (`variant_gate.suspiciously_cheap`)

Real search results can contain a price far below everything else (a mislisted unit, an accessory, a doubtful reseller). When the shortlist has at least four items, any item priced below 40% of the median is flagged `price_warning`. It is ranked after the other items that pass the requirements (never hidden), and the answer must tell the user to verify the seller, condition and variant, and must not call it a top pick or a deal.

## Sale state (`sale_mode.py`)

From recorded sale events: **pre-sale** (within 14 days before the first phase), **early access** (inside an early-access phase), **active**, **post-sale** (within 3 days after) and **normal**. Dates are never hard-coded; events carry their verification level, and unverified events are presented as unconfirmed.

## Parsing Indian prices (`india_price_parser.py`)

The parser understands "₹1,00,000", "Rs. 75,999/-", "80k", "1.2L", "1.5 lakh", "40 hazaar", ranges ("40-50k", "between 40k and 50k", "40k se 50k") and qualifiers in English and Hinglish ("under", "upto", "tak", "ke andar", "se kam", "above", "around", "lagbhag"). Bare numbers without a currency sign, magnitude suffix or budget keyword are ignored, so "RTX 4060" and "16GB" are never read as money.

## Budget planner (`budget_planner.py`)

Given slots (for example a phone and a tablet, each with candidate options at their effective prices, quantities, and optional slots) and a total budget, the planner lists every feasible combination, either using the most of the budget or saving the most. If nothing fits, it reports the shortfall.

The web page `/budget` and `shop budget` take one option per line: `item: product = price` (prices such as `30,599`, `34k`, `1.2L`). `item x2:` asks for two units and `item?:` marks the item as optional (`parse_items`).
