# Tollgate 1.2 — Phase A: Implementation Audit and Prioritized Plan

Date: 3 November 2026. Scope: the repository as supplied plus the offline-continuity work added earlier in this engagement. **No code was changed for this audit.**

## 0. How this audit was done, and its limits

I read the source (not only the docs), listed every route, compiled every Python file, syntax-checked the dashboard script, and ran every test that can run in the audit sandbox.

| Check | Result |
|---|---|
| All Python files compile (`py_compile`) | Pass |
| `dashboard.js` syntax (`node --check`) | Pass |
| `test_continuity_core.py` (19 tests, money rules for allowances) | **Pass (executed)** |
| `test_direct_providers.py` (5 tests, provider request/answer mapping) | **Pass (executed)** |
| `test_alerts_and_client.py` (12 tests, 3-tier client, notifier, watchdog) | **Pass (executed)** |
| The original suite: `test_gateway`, `test_cluster`, `test_live`, `test_gemini`, `test_recovery` (64 tests) | **NOT run.** The sandbox has no network, so FastAPI, Pydantic and Uvicorn cannot be installed. The project's own record (`docs/VALIDATION.md`, 23 Oct 2026) says 63 passed and 1 PostgreSQL test was skipped. I have not reproduced that. |
| `test_continuity_system.py` (relay ↔ primary over real ASGI apps) | **NOT run** (same reason). Written, unexecuted. |
| Docker/Compose stack, HAProxy, Caddy, PostgreSQL, TLS, DNS | **NOT run** (no Docker, no network, no domain) |
| Any real provider call, real email/SMS, real Stripe test-mode | **NOT run** (no credentials, no network) |

Consequence: every "implemented" below means *the code exists and I read it*. Only the rows marked **executed** are verified by me. Please run `python -m unittest discover -s tests -v` inside `gateway/` on your machine (with `pip install -r requirements.txt`) and send me the output before Phase B.

**Risk I introduced and have not been able to test against the full suite:** schema version 5 (new tables, one added column), the reconciliation query now including allowance holds, new settings fields, and four new routes. By reading, the existing schema-migration test (`PRAGMA user_version=2` then `initialize()`) remains compatible, and no existing test asserts a schema or app version number. Treat that as an expectation, not a result.

## 1. Architecture map (as implemented)

```
Customer app ──HTTPS──► Caddy ──/v1/*──► HAProxy ──► warden-1 ─┐
 (virtual tg_ key)        │  (256 KB cap)  (leastconn,           ├─► PostgreSQL (wallets, requests, ledger,
                          │                 /ready every 2 s)  warden-2 ─┘     settlement jobs, leases, audit)
                          └─ everything else ─► control (dashboard, /admin/*, /auth/*, /webhooks/stripe)
                                      worker (settles jobs, recovers stuck requests)   patrol (reads leases, writes report)
Offline (new, outside the main host):  relay (+SQLite)  ·  watchdog  ·  resilient client (journal.db)  ·  optional direct-to-provider
```

Code: `gateway/app/main.py` (routes and request flow), `services/ledger.py` (money), `services/cluster.py` (leases/drain), `services/worker.py`, `services/patrol.py`, `services/live_provider.py` + `gemini_provider.py` (adapters), `services/continuity.py`, `relay.py`, `services/watchdog.py`, `services/notifier.py` (offline), `client/` (resilient client).

## 2. Feature-status matrix

Legend: **V** verified by me (executed) · **T** implemented and has tests I could not run · **I** implemented, little or no automated test · **P** partial · **M** missing.

### Requirements §3–§4: request flow and billing

| Requirement | Status | Evidence |
|---|---|---|
| Virtual key auth, revocation, expiry, model allow-list, frozen customer | T | `security/authentication.py`; tests `test_revocation_model_rules_and_caps`, `test_expired_key_and_frozen_wallet_cannot_generate` |
| Rate limit (shared across wardens), concurrency limit, spend cap | T | `ledger.py` ~245–270; `test_rate_limit_is_shared_and_readiness_checks_worker` |
| Atomic reservation before provider call | T | `Ledger.reserve`; `test_concurrent_processes_admit_only_one_affordable_request`, `test_concurrent_http_requests_do_not_overspend`, `test_two_wardens_cannot_reserve_the_same_money` |
| Integer micro-USD, round-up once | T | `pricing.py`; `test_integer_pricing_and_version_snapshot` |
| Pricing versioned per request (price_version and rate snapshot stored on each request, incl. cached and cache-write rates) | T | `database.py` requests columns |
| Immutable ledger | T | DB triggers; `test_reopened_database_preserves_queue_and_immutable_ledger` |
| Settlement exactly once; durable jobs; rollback as one operation | T | `test_repeated_settlement_charges_once`, `test_settlement_rolls_back_as_one_operation`, `test_crash_boundaries_preserve_money_and_jobs` |
| Unknown outcomes keep the hold; no silent retry; idempotency | T | `test_unknown_dispatch_keeps_credits_reserved`, `test_live_stream_and_duplicate_do_not_generate_twice` |
| Streaming (SSE) with final usage | T | `test_sse_final_usage_is_settled` |
| Reject when authoritative data cannot be verified | T | `test_storage_failure_stops_admission_and_readiness` |
| Real PostgreSQL concurrency | T, **skipped without a PG service** | `test_real_postgres_concurrency_settlement_and_constraints` |

### Requirements §5–§6: high availability and Patrol

| Scenario | Status | Evidence / gap |
|---|---|---|
| 1 Both wardens healthy | T | cluster tests |
| 2, 3 One warden stops | T (app level) / **I (HAProxy)** | `test_other_warden_can_inspect_and_settle_a_request`; the HAProxy health-check removal has **no test** |
| 4 Warden crashes mid-request | T | `test_crash_boundaries_preserve_money_and_jobs` |
| 5 Control stops, dependencies healthy | T | `test_control_outage_fail_open_preserves_api_and_accounting`, `test_fail_closed_blocks_when_control_is_unavailable` |
| 6 Database unavailable | T | `test_storage_failure_stops_admission_and_readiness` |
| 7 Worker stops and recovers | T | `test_missing_settlement_worker_pauses_new_generation` |
| 8 Restart with pending jobs | T | `test_reopened_database_preserves_queue_and_immutable_ledger` |
| 9 HAProxy stops routing to an unhealthy warden | **I** | `deploy/wardens.cfg` is correct by reading (`/ready`, `inter 2s fall 2 rise 2`); never executed |
| 10 Whole stack restart | **P** | database-level only; no full-stack test |
| Patrol: missing nodes, stale worker, unresolved usage; does not release holds or restart services | T | `test_patrol_flags_missing_nodes_without_releasing_holds`; dashboard panel has no browser test |

### Requirements §7: offline continuity

| Item | Status | Evidence / gap |
|---|---|---|
| Allowance issue holds money; included in reconciliation; sweeper releases after grace | **V** | `test_continuity_core` |
| Signed relay records; tamper rejected; duplicates skipped; cap enforced; closed/expired rejected | **V** (settlement side) | `test_continuity_core` |
| Direct records charged as *unverified*, only on direct-enabled allowances | **V** | `test_continuity_core` |
| Schema 4 → 5 upgrade keeps balances | **V** (SQLite only) | `test_continuity_core`; PostgreSQL `ALTER` path **untested** |
| Direct-provider request/answer mapping (OpenAI, Anthropic, Gemini) | **V** with sample payloads only | `test_direct_providers`; **never run against a live provider** |
| Client: primary → relay → direct order; journal; sync once; local allowance guard; no retry after read timeout (direct) | **V** (with mock transport) | `test_alerts_and_client` |
| Notifier (SMTP + SMS webhook), watchdog alert-once / recover-once | **V** (mocks) | `test_alerts_and_client`; no real SMTP/SMS |
| Relay: signature/expiry/model/cap enforcement, local SQLite journal | **T** | `relay.py` read; `test_continuity_system` **not run** |
| Relay ↔ primary end-to-end cycle | **T, unexecuted** | `test_continuity_system` |
| Failover only when the primary provably did not run the request | **P** | Client treats connect errors and **503** as safe. See bug B3 (502). |
| Relay storage loss, prolonged outage, sync interrupted midway, network flaps | **M** | no tests; storage loss is documented as business-absorbed |
| Relay rate/concurrency limits | **M** | relay is bounded only by the allowance cap |
| Streaming through the resilient client | **M** | non-streaming only |

### Requirements §8: providers

| Item | Status |
|---|---|
| OpenAI, Anthropic, Gemini adapters, error mapping, interrupted streams, usage validation, unsupported fields rejected (`extra="forbid"`) | T (mock transports) |
| Any **live** provider test | **M** (needs your credentials) |
| Provider keys never returned to customers (normal path) | T; **deliberately relaxed in the optional direct tier** |
| No silent provider fallback | T |

### Requirements §9: dashboard, security, payments

| Item | Status |
|---|---|
| Customers, keys (create/revoke/expiry/models), credits, spend cap, freeze, requests, ledger, CSV export, cluster/Patrol panel, drain, resolve-unknown | T (API) / dashboard UI has no browser test |
| Sessions, CSRF, same-origin, owner-key separation | T (`test_sessions_csrf_and_logout`) |
| Stripe Checkout, signed webhook, amount/mode/dup checks | T (mocked); no real test-mode run |
| UI for **contacts and allowances** (new) | **M** (API only) |
| `GET /admin/customers` list, `GET /admin/keys` list | **M** (data appears in `/admin/overview`) |

### Requirements §14: public REST API

| Required | Today | Status |
|---|---|---|
| `POST /v1/chat/completions` | exists | T |
| `GET /v1/models` | exists; lists only the key's allowed models with `input_usd_per_million`, `price_version`, `reviewed_on` and `pricing_source` | T |
| `GET /v1/account` | **absent** | **M** |
| `GET /v1/credits` | absent; `GET /v1/balance` is the equivalent | **M** (alias/rename) |
| `GET /v1/usage` with pagination and filters | **absent** (`GET /v1/requests/{id}` only) | **M** |
| Key management endpoints | admin create and revoke only; no list | **P** |
| Admin: customers, credits, cap, freeze, usage | exist; **pricing is a file (`models.json`), no endpoint** | **P** |
| Consistent errors | `{"error":{code,message}}`; 401/402/403/404/409/422/429/500/503/504 used | T |
| `400` for invalid request | **code uses 422** | **P** (decision needed) |
| Interactive OpenAPI at a public URL | **disabled in production** (`show_docs = docs_enabled and environment != "production"`, `main.py` ~140); HAProxy also 404s everything except `/v1/` | **M** |
| Developer/teacher onboarding guide, JS example, test-credential process | Python `client/demo.py` only | **M** |
| Public HTTPS deployment, DNS, TLS, external-network test | **not performed; cannot be done from this sandbox** | **M** |
| Public health endpoint | `/health` and `/ready` exist (minimal) | T |
| CORS | none configured | **M** (decision needed) |

## 3. Known bugs, security risks and limitations

| ID | Severity | Finding | Proposed action |
|---|---|---|---|
| B1 | **High (process)** | The originally uploaded archive contained a real-looking `gateway/.env` (OpenAI, Gemini, owner key) plus `.venv` and a test database. `.gitignore` is correct, so this is about sharing the folder, not git. | Rotate all three keys now. Share only the git-tracked tree. |
| B2 | High | Production docs are unreachable (code disables them; HAProxy blocks the path), so the "teacher can open `/docs`" requirement cannot be met as built. | Serve a read-only OpenAPI limited to `/v1` routes from a public path; exclude admin routes from the schema. |
| B3 | High (my offline work) | The resilient client fails over on connect errors and **503**. If HAProxy is down, Caddy typically answers **502** (or 503 when it has no upstream), and 502 is not recognised, so the client raises instead of failing over. A 502 can also mean the connection broke after the request was forwarded, so it is not automatically safe. | Define one unambiguous "never reached a warden" signal (for example, Caddy `handle_errors` mapping upstream-connect failures to 503 with a marker header), then add client tests. |
| B4 | Medium | Requirement says invalid requests return `400`; code returns `422 invalid_request`. | Decide: keep 422 and document, or switch to 400 for validation. Update tests and docs. |
| B5 | Medium | Watchdog fetches contacts using the **owner key** from the standby host. | Add a read-only scoped credential for `/admin/contacts`. |
| B6 | Medium | Relay has no rate or concurrency limit; abuse is bounded only by the allowance amount. | Add per-allowance concurrency and request-rate limits to the relay. |
| B7 | Medium | Direct tier: the restricted provider key leaves the gateway; usage is self-reported. Documented trade-off, off by default. | Keep optional; require provider-side budget caps; reconcile against provider consoles. |
| B8 | Medium | Allowance tokens use a shared HMAC secret; any host holding it can mint allowances. | Consider asymmetric signatures later. |
| B9 | Low | `docs/VALIDATION.md` and the README still describe the 1.1 test counts. | Update after the suite is re-run. |
| B10 | Low | Browser use: no CORS, and a virtual key in a browser is exposed to the page. | Decide the supported integration model (server-to-server recommended) and document it. |
| L1 | Info | No streaming in the resilient client; relay streaming records need an extra fetch. | Document, or implement later. |
| L2 | Info | Single-host Compose is process redundancy only (database, ingress and host remain single points of failure). | State it in the final report. |

## 4. Prioritized plan

Each increment ends with the tests run, results, files changed and what remains. Nothing is called done until it passes on a machine with the full dependencies.

| Priority | Increment | Outcome | Needs from you |
|---|---|---|---|
| P0 | **Run the full suite** locally and CI; send results | A real baseline for the 64 original tests and my changes | A machine with `pip install -r requirements.txt` (and ideally the PostgreSQL service) |
| P1 | **Public API completion (Phase B/H)**: `/v1/account`, `/v1/credits` (keep `/v1/balance` as an alias), `/v1/usage` with pagination and filters, admin list endpoints, read-only pricing view; decide 400 vs 422; public OpenAPI limited to `/v1` | Endpoints required by section 14 with API tests | Your decisions on B4 and B10 |
| P2 | **Offline hardening**: B3 (safe unreachable signal), B5, B6; tests for relay storage loss, interrupted sync, expiry, prolonged outage; dashboard UI for contacts and allowances | Section 7 acceptance tests | None for code; a staging host for end-to-end runs |
| P3 | **Core deployment (Phase C)**: execute `compose.wardens.yaml` and `compose.relay.yaml`; failure drills 2, 3, 9, 10; backup and restore rehearsal | Deployment test report | A Linux host with Docker, a domain, DNS access |
| P4 | **Provider verification (Phase D)**: one tiny real call per provider; compare provider usage with Tollgate's ledger; verify direct mappings | Live-provider report | Restricted test keys for OpenAI, Anthropic, Gemini |
| P5 | **Launch (sections 14.A, 14.G)**: HTTPS on the real domain, external-network test, onboarding guide for your teacher with a scoped test key and `curl` + Python + JavaScript examples | Public API usable by your teacher | Domain, TLS, a test customer and key issued by you |
| P6 | **Documentation (Phase G)**: final diagrams, setup guide, environment guide, test report, limitations | Final package | – |

## 5. What cannot be completed without you

- Running the original suite and any PostgreSQL, Docker, HAProxy or Caddy test: needs a machine with network and Docker.
- Public HTTPS deployment, DNS and an external-network test: needs your domain and a server.
- Live provider and Stripe test-mode verification: needs your credentials.
- Real email/SMS verification: needs SMTP and an SMS gateway.

Until those are done, the honest status is: **billing core and HA logic are implemented and previously reported as tested; the public API surface, public documentation, offline end-to-end path and the full deployment are incomplete or unverified.**
