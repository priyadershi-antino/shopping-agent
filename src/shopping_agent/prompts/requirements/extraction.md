<!-- version: 2 -->
Convert the shopping request into a structured analysis. Requests may be in English or Hinglish.

Rules:
- Extract only what the user said or clearly implied. Do not invent requirements. Put anything important but unstated in `unknowns`.
- `hard_constraints` are must-haves ("at least 16GB RAM", "must be Wi-Fi + cellular", "not Samsung"). `strong_preferences` are stated priorities ("good battery", "light"). `nice_to_have` are mild wishes.
- Use these attribute names where they fit: ram_gb, storage_gb, brand, connectivity (wifi/cellular), display_size_inch, refresh_rate_hz, weight_kg, battery_wh, battery_mah, gpu, cpu, os, condition. Numbers are plain numbers (16, not "16GB").
- Use op `qualitative` for subjective wishes ("good camera"); give the wish as text in `value`.
- Budget amounts are in rupees as plain numbers (80k = 80000, 1.2L = 120000). Use the deterministic budget hint below when it is present.
- `target_price` is a price the user wants to be alerted at or wait for ("tell me when it's below 65k"). It is not a budget: leave `budget` empty unless the user also states what they can spend.
- Payment details: record only the bank and card type the user mentions (e.g. HDFC credit card). Never ask for or record card numbers.
- If the user pasted offer or coupon terms, copy that text verbatim into `pasted_offer_text`.
- `product_mentions` lists specific products named by the user.
- Intents: discover (find products), compare (named products), alternatives (cheaper/similar to something), link_lookup (a product link was given), deal_check (is this a good price/deal), price_history, track (watch a price), budget_plan (several items within one budget), sale_prep (prepare for an upcoming sale), offer_check (check an offer/coupon), general.
- Set `needs_clarification` only when the missing information would change the research completely (for example no product category, no product and no link). Otherwise proceed and list the gaps in `unknowns`.

Deterministic hints (computed by code, trust these):
$hints
