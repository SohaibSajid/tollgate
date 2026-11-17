# Project map (for fast onboarding)

Version 1.2.0, DB schema 6 (6 adds `requests.end_user`). FastAPI app in `gateway/app`, same code runs as roles `standalone`, `control` (dashboard + `/admin`), `warden` (client `/v1` API). One deployment = one SaaS business; its customers are that business's end users or apps.

## Layout

| path | what it is |
|---|---|
| `gateway/manage.py` | CLI: setup, set-provider, migrate, doctor, admin-key, rotate-admin-key, onboard, serve, worker, patrol, reconcile, backup, relay, watchdog, offline-secret, export-openapi |
| `gateway/app/main.py` | App factory, middleware stack, client and admin routes, dashboard auth (`/auth/login`, `/auth/session`, `/auth/logout`), `/health`, `/ready` |
| `gateway/app/config.py` | All settings (env prefix `TOLLGATE_`, `.env` overridden by real env vars) |
| `gateway/app/database.py` | SQLite/PostgreSQL schema and migrations |
| `gateway/app/services/ledger.py` | Money core: customers, keys, grant, reserve, dispatch, record_usage, settle, release, reconciliation |
| `gateway/app/services/worker.py` | Settlement worker (charges once, releases hold) |
| `gateway/app/services/live_provider.py`, `gemini_provider.py`, `provider_client.py` | OpenAI/Anthropic and Gemini adapters, token counting |
| `gateway/app/services/pricing.py`, `routing/providers.py` | Price math, model catalog, 30-day price freshness check (`price_is_current`) |
| `gateway/app/services/cluster.py`, `patrol.py` | Node heartbeats/leases, independent monitor |
| `gateway/app/services/continuity.py`, `relay.py`, `watchdog.py`, `notifier.py` | Offline tiers: standby relay, signed allowances, outside watchdog, alerts |
| `gateway/app/services/accounts.py`, `gateway/app/portal.py`, `ui/portal.html`, `ui/portal.js` | Self-service client accounts: sign-up (`TOLLGATE_SIGNUP_MODE`), scrypt passwords, portal sessions (cookie `tollgate_portal`, CSRF + Origin), `/portal/*` routes scoped to the account's customer |
| `gateway/app/services/provider_keys.py` | BYOK: each customer's own provider key, Fernet-encrypted, `get` used before token counting and dispatch |
| `gateway/app/security/authentication.py`, `sessions.py` | Customer key and owner session auth, rate limits, CSRF/origin checks |
| `gateway/app/middleware/*` | request id and security headers, body size limit, service role gate |
| `gateway/app/api_contract.py` | Source of the OpenAPI spec and generated tables in `docs/API_GUIDE.md` |
| `gateway/app/ui/dashboard.{html,js,css}` | Operator dashboard (plain JS, no build step) |
| `gateway/models.json` | Model catalog and prices (USD per million tokens) |
| `client/` | `demo.py`, `resilient_client.py` (3-tier failover), `direct_providers.py`, `live_smoke.py` |
| `simulator/main.py` | Fake provider for cost-free testing |
| `verification/` | e2e, prod and cluster check scripts |
| `deploy/`, `compose*.yaml`, `Dockerfile` | Docker stack (PostgreSQL, Caddy, HAProxy, relay) — not used in the current dev setup |
| `gateway/tests/` | 151 tests; `test_api_contract.py` fails if spec, guide and server disagree |

## One request

Auth customer key → validate (fields, model access, limits, price freshness) → count input tokens → one write transaction: reserve worst case (checks balance, cap, frozen, concurrency, idempotency) → dispatch → call provider once → persist usage + settlement job → worker writes one ledger charge and releases the hold. States: reserved, dispatched, usage_recorded, settled, released, unresolved.

## Invariants (do not break)

- Money is integer micro-USD; round up once per request.
- Available = balance − held; ledger entries immutable; one charge per request.
- Provider timeout is not evidence of a free request; no automatic regeneration or refund.
- Prompts and answers are never stored.
- Unknown request fields are rejected (422), never ignored.

## Client API in one glance

`GET /v1/models`, `POST /v1/chat/completions` (body: model, messages, max_completion_tokens, stream, stream_options), `GET /v1/balance`, `GET /v1/requests/{id}`, plus `/health`, `/ready`. Headers: `Authorization: Bearer tg_…`, `Idempotency-Key`, `X-Tollgate-Feature` (label, 1–64 chars), `X-Tollgate-End-User` (client's user ID, 1–128 chars, stored as `requests.end_user`, part of the fingerprint when sent, totals in `/admin/overview` → `end_users`). Offline: `POST /v1/allowance`, `POST /v1/offline/sync`. Details: openapi.json.

## Key settings (names only)

`control_failure_policy` (fail_open default), `price_max_age_days` (30), `session_hours` (8, max 24), `customer_requests_per_minute` (60), `customer_concurrency` (5), `enabled_providers`, `openai_api_key` / `anthropic_api_key` / `gemini_api_key` (one set per deployment, held on the gateway), `allowance_*`, `emergency_direct_enabled`, `direct_customers`, `relay_url`, `watch_*`, `smtp_*`, `sms_webhook_url`.

## Auth model

- Owner key (`TOLLGATE_DEVELOPMENT_API_KEY`) → dashboard session cookie `tollgate_session` (HttpOnly, SameSite strict, DB-backed, tied to the key hash) and `/admin`.
- Customer `tg_` key → `/v1` only; hashed at rest; model allow-list, expiry, revocation, spend cap, freeze.
- Provider keys → gateway config (deployment keys) and/or per-customer BYOK keys in `provider_keys` (encrypted with `TOLLGATE_CREDENTIALS_KEY`). `TOLLGATE_PROVIDER_KEY_POLICY`: `customer_first` or `customer_only`. `requests.key_source` records which was used.

## Known limits

Text chat only (no embeddings, tools, images, audio). Only Gemini free tier verified live. Docker stack documented but not the current dev path. Stripe unusable for a Pakistan-registered business; credits are granted manually.
