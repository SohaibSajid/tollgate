# Design: bring-your-own-key and self-service accounts (for approval)

Phase 1 (done): per-end-user tracking via `X-Tollgate-End-User`, schema 6.
Phase 2 (done, 2026-11-17): BYOK as designed below. Decisions taken: policy setting with `customer_only` available (default `customer_first` for compatibility); wallet stays a spending budget. Code: `app/services/provider_keys.py`, adapters take an optional `key`, `requests.key_source`, admin `PUT/DELETE /admin/customers/{id}/provider-keys/{provider}`, `manage.py credentials-key` and `customer-key`, dashboard BYOK panel and "Paid by" column. Relay and direct tier now also use the customer's key: the stored Fernet ciphertext is sealed into the signed allowance as claim `pk`; the relay unseals it with the shared `TOLLGATE_CREDENTIALS_KEY`; the direct tier returns `own_keys` and the resilient client takes `own_keys=`. `customer_only` refuses allowances without a customer key.
Phase 3 (done, 2026-11-17): self-service accounts as designed below, sign-up mode `approval` by default. Code: `app/services/accounts.py` (scrypt passwords, `accounts` and `portal_sessions` tables), `app/portal.py` (`/portal` page and `/portal/*` JSON routes), `ui/portal.html`, `ui/portal.js`, owner `GET /admin/accounts`, `POST /admin/accounts/{id}/state`, dashboard "Client accounts" panel. Not included: email verification, password reset, 2FA.

Principle: no rebuild. A **customer** becomes the client business (tenant). Reserve → call once → settle once, the ledger and the owner dashboard stay as they are.

## Phase 2: bring your own provider key (BYOK)

| part | design |
|---|---|
| storage | New table `provider_keys(customer_id, provider, ciphertext, hint, created_at)`, one key per business per provider. Encrypted with Fernet (`cryptography` package, new dependency) using a new secret `TOLLGATE_CREDENTIALS_KEY`. Only a 4-character hint is ever shown again. |
| admin API | `PUT` / `DELETE /admin/customers/{id}/provider-keys/{provider}`. Overview lists provider and hint only. Dashboard form under Customers & keys. |
| request path | After authentication, load the business's key for the model's provider and pass it to token counting, generation and worker recovery. Adapters read the key from the call instead of global settings (they currently read it in one header function each: `gemini_provider.py`, `live_provider.py`). |
| policy | New setting `TOLLGATE_PROVIDER_KEY_POLICY`: `customer_only` refuses a business with no key (new error `provider_key_missing`); `customer_first` falls back to the deployment key. Each request records `key_source` (`customer` or `deployment`). |
| money | Provider bills the business directly. The wallet stays and acts as a **spending budget**: reservations, caps and freeze still stop runaway spend. No change to settlement. |
| fail-open | The business's wrapper (INTEGRATION_MODES.md mode 3) uses the same key directly, so outage usage lands on the same account. |
| contract | Additive: new error code, `export-openapi`, guide update. |
| tests | Encryption round trip; key never in responses or logs; request uses the business's key (simulator checks the header); missing key behaviour per policy; isolation between businesses. |

## Phase 3: self-service accounts (client portal)

| part | design |
|---|---|
| accounts | Table `accounts(id, customer_id, email UNIQUE, password_hash, approved, created_at)`. Password hashing with stdlib `hashlib.scrypt` and a per-account salt. |
| sign-up | `POST /portal/signup` creates customer + account. `TOLLGATE_SIGNUP_MODE`: `approval` (operator approves in the dashboard), `open`, or `invite` (code). |
| login | `POST /portal/login`, session cookie scoped to that customer (sessions table gains `customer_id`; owner sessions unchanged). Same CSRF and origin rules, rate limited. |
| portal page | New `portal.html`: budget and balance, provider keys (BYOK), issue/revoke own `tg_` keys, requests, usage by end user. Every query filtered by the session's customer. |
| owner | Owner dashboard unchanged, plus an "Accounts" list with approve/suspend. |
| limits | No email verification or password reset in the first version (no SMTP in dev). |
| tests | Tenant isolation (A cannot read B), approval flow, CSRF, rate limits, wrong password. |

## Decisions needed (recommended first)

1. **Missing business key:** `customer_only` (Tollgate never pays for a business) / `customer_first`.
2. **Wallet under BYOK:** keep as spending budget (no settlement change) / tracking only, never block (touches reservation logic).
3. **Sign-up:** operator approval / open / invite code.

## Effort

Phase 2: medium, about one working session. Phase 3: large, about two. Each phase ends with tests, `export-openapi`, docs and a live check.
