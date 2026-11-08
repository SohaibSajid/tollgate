# Readiness Report — Tollgate 1.2

Date: 8 November 2026. Every claim below gives the level it was verified at. Nothing is listed as working unless it was actually run.

**Levels:** implemented → unit-tested → integration-tested → manually verified → deployed → externally verified.

**Environment for these results:** Linux, Python 3.13, PostgreSQL 16, on a clean copy of the original project with this release's changes applied. Windows results are the student's to confirm (see the end).

## Summary

| area | level | evidence |
|---|---|---|
| Automated test suite | **integration-tested** | 121 tests, OK, **0 skipped** (the PostgreSQL test included) |
| Client API specification (OpenAPI 3.1) | **integration-tested** | `docs/api/openapi.json` passes `openapi-spec-validator`. 17 contract tests run the real server against it. Mutation checks confirm the tests fail when the spec, schemas or headers drift. |
| Integration guide matches spec | **unit-tested** | Error and state tables in `API_GUIDE.md` are generated from the contract code, and a test fails if they are stale. |
| Guide examples | **manually verified** | curl, the official OpenAI Python SDK 3.46 (JSON, streaming, 409 retry, 403), and the Node.js 22 `fetch` example all ran against the server. |
| One-command onboarding (`manage.py onboard`) | **manually verified** | On SQLite and on PostgreSQL as the restricted runtime role. The key is printed once and not stored in plaintext. |
| Authentication, tenant isolation, admin/client separation | **integration-tested** | Contract tests, plus production-mode run (`verification/prod_check.py`, 23/23) |
| Exactly-once billing | **integration-tested** | Contract test, plus 21 settled requests = 21 charges after failure injection |
| Production mode (PostgreSQL, HTTPS origin, live mode) | **manually verified** | Gateway and worker processes in production mode against PostgreSQL 16, with the runtime role unable to alter the ledger |
| Multi-node topology (control + 2 wardens + worker + Patrol) | **manually verified** | `verification/cluster_check.py`, 21/21 (details below) |
| Warden failover | **manually verified** | Warden stopped → its `/ready` fails → all traffic served by the other warden, 0 failed requests |
| Restart recovery | **manually verified** | Warden restart and rejoin; worker `SIGKILL` → answers still delivered, backlog settled after restart; PostgreSQL stop/start → requests refused safely, then normal service |
| Draining and control outage | **manually verified** | Drain moves traffic. Control down + `fail_open` keeps serving, marked `degraded`. |
| Clean shutdown on SIGTERM | **manually verified** | serve, worker, Patrol, wardens (restart immediately with the same node ID) |
| Security review | **done** | `docs/SECURITY.md`. 7 findings: 3 fixed, 4 documented with mitigations. `pip-audit`: no known vulnerabilities. |
| Real AI provider answer with real token accounting | **not done here** | This environment cannot reach provider APIs. `client/live_smoke.py` is ready for the student to run against Gemini. |
| Docker images and Compose stacks | **not done here** | Docker Hub is blocked from this environment, so images could not be built. The same processes and PostgreSQL roles were verified without containers. `compose.yaml` image tag fixed (1.0.0 → 1.2.0). |
| HAProxy and Caddy themselves | **not run** | Their health-check behaviour was reproduced by a small balancer that routes only to wardens whose `/ready` is 200, with no retries (same rules as `deploy/wardens.cfg`). |
| Public HTTPS deployment | **not deployed** | `docs/DEPLOYMENT.md` gives the steps. |
| Offline continuity / watchdog | unit-tested (existing tests only) | Not re-verified end to end in this round. |

## Multi-node run in detail (`verification/cluster_check.py`)

```
PASS control, warden-1 and warden-2 all ready
PASS admin API is not served by a warden
PASS generation is not served by control
PASS traffic spread across both wardens
PASS warden-1 stopped -> its /ready fails
PASS all requests still succeed via warden-2 — [200, 200, 200, 200, 200, 200]
PASS warden-1 restarted immediately after SIGTERM and rejoined
PASS operator drains warden-2 from control
PASS drained warden-2 reports not ready
PASS traffic moves to warden-1 during drain
PASS warden-2 back in service after undrain
PASS control down + fail_open: generation continues, marked degraded
PASS worker killed: answers still delivered, charges wait in queue — {'usage_recorded'}
PASS restarted worker settles the backlog
PASS database down: requests refused safely
PASS database back: wardens become ready again
PASS generation works after database restart
PASS ledger reconciliation after all failures
PASS exactly one charge per settled request — (21, 21)
PASS Patrol is reporting
```

## Changes in this release

| file | change |
|---|---|
| `gateway/app/api_contract.py` | **New.** Strict response models, error catalog, OpenAPI 3.1 builder, generated guide tables |
| `docs/api/openapi.json` | **New.** Published client specification (6 operations, 31 documented error codes) |
| `gateway/tests/test_api_contract.py` | **New.** 17 contract tests |
| `gateway/manage.py` | New `export-openapi` and `onboard` commands. Clean "run migrate" error. Database always closed. |
| `gateway/app/main.py` | New public `GET /api-spec.json` (the curated client spec, not the internal schema) |
| `gateway/app/security/authentication.py` | `Retry-After` now gives the real seconds left in the window, not always 60 |
| `gateway/app/database.py` | PostgreSQL pool opened explicitly (deprecation warning). Missing schema reported as "run migrate". |
| `gateway/app/ui/dashboard.html` | Version label 1.2. Spec link works in production. |
| `gateway/requirements.txt` | `python-dotenv` pinned explicitly |
| `compose.yaml` | Image tag 1.0.0 → 1.2.0 |
| `client/live_smoke.py` | **New.** Manual live check (not in CI) |
| `docs/` | **New:** API_GUIDE, ONBOARDING, ADMIN_PANEL (with screenshots), DEPLOYMENT, READINESS, HANDOVER. **Updated:** SECURITY review, README "Start here", stale paths and schema version. |
| `verification/` | **New.** The three Linux harness scripts that produced this report |

No billing, settlement, streaming, failover or continuity logic was changed. No test was weakened or removed.

## Still to do (student, on Windows)

1. Run the suite: `python -m unittest discover -s tests` → expect **121 tests OK (skipped=1)**, or 0 skipped with `TOLLGATE_TEST_POSTGRES_URL` set to a throwaway Docker PostgreSQL.
2. Real provider test with Gemini (`docs/GEMINI.md`), then `python client\live_smoke.py --model gemini-3.5-flash-lite`. Record the output here.
3. `docker compose up` on Windows Docker Desktop (local images), first `compose.yaml`, then `compose.wardens.yaml`.
4. **Re-review `models.json` prices before 21 November 2026.** After that date, live models stop with `price_review_required`.
5. Rotate the Gemini key and the owner key that were in the shared archive (SECURITY finding 1).
