# Status, decisions and roadmap

Last updated 2026-11-17. Branch `main`, pushed through commit `d0b553e`.

## Done this stretch

- Verified Tollgate dev setup (health, ready, doctor, ledger PASS).
- Dashboard reachable at `http://host.docker.internal:8100` (hosts-file line + matching public origin).
- Fixed dashboard sign-in not surviving refresh: `crypto.randomUUID` is missing on plain-http hosts and crashed `dashboard.js` before session restore. Added `uuid()` fallback (`gateway/app/ui/dashboard.js`).
- Added `docs/integrations/SAATHI.md` (backend → Tollgate guide) and a ready prompt for the Saathi project.
- Added `docs/integrations/INTEGRATION_MODES.md`: env-var zero-code mode, base URL mode, fail-open wrapper (outage vs decision rules), resilient client, key ownership, per-user tracking today. Covers supervisor points 1, 2 (positioning), 3 (client side) and 5.
- Dev DB has two customers named "Saathi app" plus "Smoke test"; the Saathi backend uses one tg_ key (check Customers & keys). `.env.example` now holds the host.docker.internal origin (separate commit, revert with `git revert d0b553e` if unwanted).

- Phase 1 of the supervisor rework done: optional `X-Tollgate-End-User` header, stored as `requests.end_user` (schema 6, migration adds the column if missing), returned by `GET /v1/requests/{id}`, part of the idempotency fingerprint when sent, dashboard Requests column + "Usage by end user" panel + Playground field. Contract exported, 125 tests pass. Dev DB migrated (backup in `backups/`).
- Phase 2 BYOK built: each customer's own provider key stored encrypted (Fernet, `TOLLGATE_CREDENTIALS_KEY`, new dependency `cryptography==50.0.2`), used for token counting, generation and OpenAI recovery; `TOLLGATE_PROVIDER_KEY_POLICY` = `customer_first` (default) or `customer_only` (refuse with 403 `provider_key_missing`); `key_source` on every request. 134 tests pass.
- Relay and direct tier use the customer's own key too (sealed in the allowance). Liability gap closed.
- Phase 3 self-service accounts built: `/portal` sign-up (operator approval by default), login, client workspace (budget, own provider key, API keys, requests, end users, integration example); owner approves/suspends in the dashboard. Docs: `docs/CLIENT_PORTAL.md`. 151 tests pass.
- Avela test state: customer "Avela" `cus_539c2e9c05740b961f54e095`, sends `X-Tollgate-End-User` (user `15`) and feature `saathi-chat`; its own Gemini key still to be saved (Paid by showed "Your key"). Avela backend has its own direct-Gemini fallback.
- Note: root `Tollgate_openapi.json` and `Tollgate_REST_API_Specification.pdf` are older copies; `docs/api/openapi.json` is current.

## Open items

- Re-review `gateway/models.json` prices before 2026-11-22 (update `reviewed_on` and `price_version` together).
- Confirm end-to-end: phone message → Saathi backend → Tollgate Requests row `settled` with tokens.
- Supervisor feedback below.
- Check later: `client/resilient_client.py` fails over on any 503, including `provider_unavailable`, which the gateway raises for any provider HTTP error (possibly after sending). The API guide says to read `GET /v1/requests/{id}` first for that code. The fail-open wrapper in INTEGRATION_MODES.md treats it as an unknown outcome.

## Supervisor feedback (meeting notes) and fit with the project

| point | already in project | gap | effort |
|---|---|---|---|
| Integration: code-level wrapper vs traffic interception | Tollgate is an OpenAI-compatible proxy: client changes only `base_url` and key. SDKs that read `OPENAI_BASE_URL` / `OPENAI_API_KEY` need zero code change. `client/resilient_client.py` is a wrapper with failover. | Document the "zero-code" env-var route. True network-level interception (no config change at all) is not built and would be a different product. | Docs only |
| Client uses own master provider key; Tollgate not liable | Provider keys live only in the gateway config; one deployment = one business, so a business running its own instance uses its own keys. | If one hosted Tollgate serves many businesses with their own keys (BYOK), per-customer encrypted provider keys are needed: currently one global key set per deployment. | Docs/positioning now; BYOK is a moderate feature (schema, encryption, adapter lookup) |
| Fail-open: Tollgate down must not stop the client app | Control outage is `fail_open`; relay tier and guarded direct-to-provider tier exist (`docs/OFFLINE.md`); `resilient_client.py` fails over on connect error or 503. | Plain SDK clients have no fallback. Direct tier is off by default and not run against live providers. Ledger outage still fails closed by design (prepaid enforcement). A "tracking-only, never block" mode does not exist. | Doc a simple client fallback pattern; tracking-only mode touches billing paths and needs approval |
| User management and per-user request tracking | Customers, wallets, keys, caps, freeze, per-request records, `X-Tollgate-Feature` label, dashboard Requests/Ledger/Spend by feature. | No end-user field on a request: per-user tracking means one Tollgate customer per user, or abusing the feature label (1–64 chars, `A-Za-z0-9_.:-`) as a user id. Proper fix: optional `X-Tollgate-End-User` header stored on the request, shown in the dashboard. | Small-to-moderate: schema migration, API contract change, openapi export, tests (needs approval) |
| Clear integration instructions for client developers | `docs/API_GUIDE.md`, `docs/ONBOARDING.md`, `docs/integrations/SAATHI.md` | One short "integration modes" page for a generic client (tic-tac-toe style example) | Docs only |

## Suggested order

1. Done: integration modes page (`docs/integrations/INTEGRATION_MODES.md`).
2. Done: per-end-user tracking via `X-Tollgate-End-User`.
3. Done: BYOK.
4. Done: self-service accounts and client portal.
5. Remaining for public launch: public HTTPS deployment (`docs/DEPLOYMENT.md`), price re-review before 2026-11-22, email verification and password reset (needs SMTP), optional client library with built-in fail-open.
