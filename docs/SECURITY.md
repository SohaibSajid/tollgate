# Security boundary

This is a self-hosted, single-business gateway. The operator and database administrators are trusted. It does not implement multiple staff roles, signup, SSO/MFA, PCI certification, compliance attestations, or a penetration-test guarantee.

## Implemented controls

- Customer and owner credentials are distinct. Customer keys are high-entropy secrets stored as SHA-256 hashes, with model allowlists, optional expiry, and revocation. Owner comparison uses constant-time digest comparison.
- Dashboard sessions are hashed in the database, expire, support logout, and become invalid after owner-key rotation plus process restart. Cookies are HttpOnly and SameSite Strict, with Secure set on HTTPS origins. Cookie-authenticated admin mutations require a same-origin request and a CSRF token.
- Rate windows are persisted and atomically shared across processes. Customer rates apply per customer across keys. Concurrent reservations are bounded per customer. Configure ingress limits for distributed unauthenticated floods; application limits do not replace network protection.
- Request bodies, message lengths, provider response bytes, SSE event sizes, and operation deadlines are bounded. Error responses avoid returning raw upstream errors and credentials.
- Real provider destinations are fixed HTTPS hosts. Callers cannot supply URLs. Redirects and environment proxies are disabled for provider/payment clients.
- The CSP allows same-origin scripts/styles only. No third-party JavaScript/CDN is loaded. Dynamic data is rendered with textContent. CSV values are escaped against spreadsheet formulas.
- JSON money is formatted as decimal strings. The ledger uses integers; no floating-point financial arithmetic occurs.
- Database migrations are explicit in production. Runtime PostgreSQL credentials cannot modify immutable ledger/audit records or perform schema administration. Container runs as a non-root user with a read-only filesystem and dropped capabilities.
- Generation is not automatically retried. Database failures stop new reservations. Uncertain dispatches keep holds.

## Operator responsibilities

Keep provider/owner credentials and backups restricted. Use HTTPS, restrict host ingress, review dependency/image updates, and apply a data retention policy. Do not serve development-mode docs or SQLite as an internet-facing production service. Production mode enforces PostgreSQL, HTTPS origin, and live mode.

Provider retention still applies to prompts sent upstream. OpenAI `store=false` disables this integration's stored response retrieval; it is not a blanket claim of zero provider retention. Enabling stored responses trades additional recovery ability for provider-side persistence.

The financial ledger records customer credit movements. It does not replace merchant accounting, tax calculations, provider invoice verification, or an independent security review. Refund/dispute handling requires operator follow-up and must not be ignored.

## Reporting an issue

Include the Tollgate request ID, timestamp, release version, and a redacted description. Do not include keys or prompt/response content. Revoke exposed keys immediately and restart all processes after owner/provider secret rotation.

## Security review — 8 November 2026

Scope: authentication and tenant isolation, secret handling, rate limiting and abuse protection, input validation, log contents, error messages, admin exposure, dependencies. Method: code reading plus checks against the running server in production mode on PostgreSQL 16 (`docs/READINESS.md`).

### Verified (checked against a running server)

| area | result |
|---|---|
| Tenant isolation | A customer reads only its own balance and requests. Another customer's request ID returns 404 `request_not_found`. Customer keys get 401 on every `/admin/*` path. |
| Key storage | API keys are stored as SHA-256 hashes plus a 10-character prefix. The plaintext is shown once and is not in the database or the logs. |
| Revocation and expiry | Take effect on the next request, and are rechecked inside the reservation transaction. |
| Ledger immutability | The PostgreSQL runtime role is refused `UPDATE` on `ledger` (InsufficientPrivilege). |
| Production hardening | `/docs` and `/openapi.json` are 404. Unknown `Host` headers are 400. HSTS is sent. Dashboard login rejects a foreign `Origin`, and the cookie is `Secure; HttpOnly; SameSite=strict`. |
| Logs | Request log lines carry request ID, method, status and duration only. No key, owner key or provider key appeared in gateway or worker output during the production run. |
| Input validation | Strict schemas: unknown fields, wrong types, oversize bodies and over-limit messages are refused before any reservation. |
| Error messages | Database, provider and unexpected errors return generic messages with a request ID. Raw upstream messages and stack traces are not returned. |
| Rate limits | 429 with an accurate `Retry-After` (fixed in this review; it was always 60). |
| Browser access | No CORS headers, so a browser cannot call `/v1` with a key from another origin. |
| Dependencies | All direct dependencies pinned to exact versions. `pip-audit` reports **no known vulnerabilities** (8 Nov 2026). `python-dotenv` is now pinned explicitly because `manage.py` imports it. |

### Findings and status

| # | severity | finding | status |
|---|---|---|---|
| 1 | **High** (process) | The project archive shared for handover contained `gateway/.env`, with a real Gemini API key and the owner key, plus `gateway/data/*.db`. `.gitignore` and `.dockerignore` are correct; the leak was in manual zipping. | **Owner action:** rotate the Gemini key in Google AI Studio and run `manage.py rotate-admin-key`. Package releases with `git archive` (see `docs/HANDOVER.md`), never by zipping the working folder. |
| 2 | Medium | No per-IP limit on failed `/v1` authentication. A flood of random keys costs one indexed database lookup each. | Documented. Add edge rate limiting (Cloudflare, or a Caddy rate-limit module) before heavy public exposure. Keys are 256-bit random, so guessing is infeasible. |
| 3 | Low | Behind Caddy, all dashboard logins share one client IP, so the 20/min login limit is global rather than per user. | Documented. Brute force is still infeasible (owner key is 32+ random characters). Optional fix: run uvicorn with `--proxy-headers --forwarded-allow-ips` set to the proxy's address. |
| 4 | Low | The dashboard and `/admin` are reachable on the public domain in the single-gateway deployment. | Mitigated by owner key, sessions and CSRF. Optional IP allow-list snippet in `docs/DEPLOYMENT.md` §6. |
| 5 | Low | The dashboard link pointed to `/docs`, which is disabled in production. | **Fixed:** it now links to `/api-spec.json`, the curated client specification. FastAPI's internal schema, which includes admin routes, stays hidden. |
| 6 | Info | psycopg_pool deprecation warning (implicit pool open). | **Fixed:** the pool is opened explicitly. |
| 7 | Info | `doctor` on an unmigrated PostgreSQL database printed a raw `UndefinedTable` traceback. | **Fixed:** it prints "Run python manage.py migrate before starting this release." and exits 1. |

Not covered: penetration testing, load testing, Docker image scanning (images could not be built in the review environment), and TLS configuration of the live domain.
