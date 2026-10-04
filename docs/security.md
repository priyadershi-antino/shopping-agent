# Security

## Trust boundaries

| Input | Trust | Handling |
|---|---|---|
| System and developer prompts (`prompts/`) | Trusted | Version-controlled |
| The user's request | Trusted for intent, never for executing writes without confirmation | Parsed into structured requirements |
| Tool results from our own computation | Trusted data | Passed to the model as JSON |
| Web pages, search snippets, reviews, offer text from pages, pasted offer text | **Untrusted** | Wrapped, flagged, never treated as instructions |

## Untrusted content and prompt injection (`security/untrusted.py`)

- External text is wrapped in `<untrusted_content source="…" url="…">…</untrusted_content>` before it reaches a model. Delimiter look-alikes inside the text are neutralised so content can't "close" the wrapper.
- The system prompt states that anything inside these tags is data: it cannot give instructions, change the task or ask for tool calls, and attempts are a warning sign about the source.
- Instruction-like patterns are detected and reported as `injection_flags`: attempts to override instructions, role claims ("SYSTEM:", "you are now"), requests to call tools or share secrets, recommendation manipulation ("always recommend"), and fake markup. The detector is tuned not to flag ordinary reviews, for example "can act as a hotspot" or "not receiving OTP SMS".
- Offer text pasted by the user is also wrapped before extraction.
- Even if a model is manipulated, the structural defences hold:
  - it sees only read-only tools;
  - it cannot fetch arbitrary URLs;
  - prices come from code;
  - the grounding check marks invented figures `[unverified]`.

## Outbound requests (`security/url_guard.py`, `providers/web/fetcher.py`)

`fetch_page` only reads a URL that passes all of these checks:

1. `https` only, no embedded credentials, default port only, not an IP-address URL;
2. the host is on the allowlist (or `FETCH_EXTRA_DOMAINS`);
3. every address the host resolves to is public: loopback, private, link-local (including cloud metadata `169.254.169.254`), carrier-grade NAT, multicast and IPv4-mapped private addresses are blocked;
4. redirects are followed manually (at most 3) and every hop is re-checked;
5. only text/HTML content, at most `FETCH_MAX_BYTES`, with a timeout.

The allowlist covers manufacturers, independent review sites, bank offer pages, official platform news (`stories.flipkart.com`, `aboutamazon.in`) and major news sites. **Marketplace product pages (Amazon, Flipkart, …) are deliberately not fetched.** Product data comes from providers, and pasted links are only parsed for their IDs.

## Secrets and personal data

- Secrets live in `.env` (git-ignored) and are `SecretStr` in settings, so they aren't printed.
- Logs redact secret-looking keys (`api_key`, `token`, `password`, `authorization`, `otp`, `cvv`, …), credentials in URLs, secret query parameters (`api_key=`, `key=`, `token=`) and card-number-like digit runs. httpx request logging is turned down because SerpApi puts its key in the query string.
- Provider error messages never include request URLs.
- The payment profile stores only the bank, credit/debit and optionally the network. The agent never asks for card numbers, CVV, OTP, PINs or passwords, and the system prompt forbids it.

## Actions

- The research loop exposes only read-only tools. Side-effect tools are refused by the registry unless the caller explicitly allows them after the user confirms.
- Suggested tracking, alerts and preference changes are stored as pending `ProposedAction`s. They are carried out only when the user confirms them (web button, `shop confirm`, or `POST /v1/runs/{id}/actions/{aid}/confirm`). Each one is confirmed at most once.
- The three write tools (`track_product`, `create_price_alert`, `save_user_preference`) are registered but never offered to the model. `save_user_preference` refuses keys or values that look like payment or login data, such as card number, CVV, OTP, PIN, UPI, passwords or long digit runs.
- Notification errors are reduced to a short reason before being stored or logged. The Telegram bot token is part of the API URL, so raw HTTP errors would otherwise expose it.
- The agent never purchases anything.

## API access

`API_KEY` (when set) is the shared secret for the REST API in remote deployments. Leave it empty for local use on `127.0.0.1`.
