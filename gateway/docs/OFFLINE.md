# Offline continuity (Tollgate 1.2)

> Three automatic tiers: **1 Tollgate wardens → 2 standby relay → 3 direct to the AI provider.** The customer's app switches by itself; users do nothing.

Goal: customers keep getting AI answers, and every cent is still accounted for, even when **both wardens and the main database are down**, and customers are told about it.

## The hard limit (read this first)

Tollgate is the only place that holds your provider keys and your customers' balances. When all of it is down, nothing inside it can answer. Continuity therefore needs three things that live **outside** the failed system:

1. A **standby relay** on separate infrastructure (another host, region or cloud) with its own provider keys and a tiny local SQLite file. It never connects to the main database.
2. A **signed allowance** the customer's app fetched earlier, while the system was healthy. It is a pre-approved spending limit (for example $1.00 for 4 hours). The money is held in the wallet immediately, so offline spending can never overdraw it.
3. An **outside watchdog** that watches the primary `/ready` address from another network and sends the alerts, using a local copy of customer contacts.

This is *bounded* continuity, not unlimited: offline spending stops when the allowance is used up or expires. Customers who never fetched an allowance cannot be served until the system returns.

## Tier 3: direct to the AI provider (optional last resort)

If Tollgate and the relay are both unreachable, the resilient client can call the provider's own API itself. This is the only way to serve a request when *nothing* of yours is reachable, but **the provider key has to leave your gateway** to make it possible. That is a real security trade-off, so it is off by default and guarded:

- `TOLLGATE_EMERGENCY_DIRECT_ENABLED=true` turns it on, and each customer must be listed in `TOLLGATE_DIRECT_CUSTOMERS`.
- Use **separate restricted emergency keys** (own project/workspace, with a spending limit set **at the provider**). Never hand out your main keys. The provider-side limit is what really bounds your loss if a key leaks; Tollgate cannot enforce it.
- The key is delivered only inside an allowance response over TLS, only for the providers of the models requested, and expires with the allowance (rotate the emergency key after any suspected leak).
- The client checks the allowance locally before every call and refuses when the worst-case cost would exceed it.
- Direct records cannot be signed by anyone you control, so they are **self-reported by the customer's app**. They are charged as `settled_unverified` / `capped_unverified`, capped at the allowance, and only on allowances issued with direct access. An app can under-report; compare each allowance's reported spend (`GET /admin/allowances`) with the usage shown in your provider console for the emergency key, and review any gap.
- The client never retries a direct call after a read timeout: the provider may already have charged it (`UnknownOutcome`).
- Direct mode talks to the providers' public APIs (OpenAI Chat Completions, Anthropic Messages, Gemini `generateContent`) without streaming. These request/response mappings are unit-tested with sample payloads but have **not** been run against the live providers.

Order of tiers is configurable: `ResilientTollgate(..., failover_order=("direct",))` skips the relay.

## What happens, step by step

| Phase | What happens |
|---|---|
| Healthy | The customer app calls `POST /v1/allowance` (with `include_direct` if approved). Tollgate holds the amount (`held_micro`) and returns a signed token, price list, relay address and, if approved, emergency keys. The app renews it before it expires. |
| Outage | The primary is unreachable (connection refused, DNS failure, connect timeout, or HTTP 503). The client tries the relay with the token: it verifies the signature offline, reserves cost inside the allowance, calls the provider once, and signs a usage record. If the relay is unreachable too, the client calls the provider directly (tier 3, if enabled). |
| Alert | The watchdog sees 3 failed `/ready` checks (default) and sends one email/SMS to each customer contact. It sends one more when service returns. |
| Journal | The client app saves every prompt and answer in **its own** `journal.db`. Tollgate still never stores prompts or answers. |
| Recovery | The client calls `sync()`. It uploads the relay's **signed** records and any direct records (token counts only). Tollgate verifies each signature, re-prices from its own price list, charges once, and releases the unused hold when the allowance is closed. The wallet continues from the result. |

## Money rules (all enforced in code and tests)

- An allowance is created inside the same transaction that checks `available >= amount`, the spend cap and frozen status.
- Held money for active allowances is included in `reconciliation` (`held = request holds + allowance remainders`).
- Records are charged **once** (`allowance_id + record_id` is unique) and are signed by the relay. Edited token counts fail verification (`bad_signature`).
- Cost is re-calculated by the main system. A relay or client cannot set a price.
- Usage beyond the allowance is capped at the allowance (the business absorbs the excess); the wallet never goes negative.
- Records after expiry (plus 5 minutes of clock skew) or after the allowance is closed are rejected.
- Allowances not closed by the customer are closed by the worker after `ALLOWANCE_SYNC_GRACE_SECONDS` (default 24 h), releasing the remaining hold.

## What the client does and does not do

`client/resilient_client.py` fails over only when the primary provably did **not** run the request (connect error, connect timeout, 503). After a read timeout or a 504 the primary may already have charged the request, so the client raises `UnknownOutcome` instead of risking a second charge. Streaming is not supported by the resilient client in this version; the relay itself does support streaming.

## Setup

1. On the main system: `python manage.py offline-secret`, set `TOLLGATE_RELAY_URL`, run `python manage.py migrate` (schema 4), restart wardens, control and worker.
2. On the standby host: copy `gateway/.env` (provider keys, the same `ALLOWANCE_SECRET`, the same `models.json`), set `TOLLGATE_RELAY_DOMAIN`, `TOLLGATE_WATCH_READY_URL` (your public `/ready`), `TOLLGATE_WATCH_CONTROL_URL`, SMTP and/or SMS settings, then `docker compose -f compose.relay.yaml up -d --build`. Without Docker: `python manage.py relay --port 8100` and `python manage.py watchdog`.
3. Set customer contacts: `PUT /admin/customers/{id}/contact` with `email` and/or `phone` (E.164, e.g. `+923001234567`).
4. In the customer app use `ResilientTollgate(...)`; call `refresh_allowance()` on a timer and `sync()` after recovery.

## Operating notes and honest limits

- Keep **model prices identical** on both sides (`models.json`), and keep each `reviewed_on` current.
- The standby host holds real provider keys. Secure it as strictly as the main system.
- If the relay's disk is lost before records are uploaded, those uses cannot be charged (the business absorbs them); clients also keep the signed record from each non-streaming response.
- Watchdog contacts are refreshed every 5 minutes while healthy. A customer added during an outage gets no notice.
- The watchdog alerts on `/ready` failing. If only the dashboard (control) is down and wardens are ready, no customer alert is sent.
- SMS uses a generic JSON webhook `{"to","message"}`; adapt a small receiver for Twilio, Vonage, or a local gateway.
- Shared-secret signing means any host holding `ALLOWANCE_SECRET` can mint allowances. Treat it like the owner key. Asymmetric signatures are a sensible later step.
- Executed here: 36 tests (money rules, provider mapping, three-tier client, notifier, watchdog). Not executed: the relay/primary integration tests, Docker deployment, a real SMTP/SMS send, and any real provider call.

## API additions

| Endpoint | Service | Purpose |
|---|---|---|
| `POST /v1/allowance` | warden | Hold money; receive a signed token, prices and (if approved) emergency keys |
| `POST /v1/offline/sync` | warden | Upload signed relay records and direct records; optionally close the allowance |
| `PUT /admin/customers/{id}/contact` | control | Store email/phone for alerts |
| `GET /admin/contacts` | control | Contacts for the watchdog |
| `GET /admin/allowances` | control | Recent allowances |
| `POST /v1/chat/completions` | relay | Same body as the primary; bearer token is the allowance |
| `GET /v1/offline/records` | relay | Signed usage records for one allowance |
