# Validation record — 23 October 2026

## Warden update checks

- Python 3.12 suite: 64 tests discovered, 63 passed, 1 PostgreSQL integration test skipped because no dedicated PostgreSQL test service was available.
- Eighteen new cluster tests cover separate service routes; control fail-open and fail-closed; invalid-key and empty-wallet rejection during control downtime; shared-credit contention between wardens; cross-warden idempotency and settlement; draining and its recheck inside the reservation transaction; expired/duplicate process leases; storage/worker outage; customer isolation; Patrol reports; shared key revocation; and additive schema 2 to 3 migration with preserved balances.
- A separate local smoke check ran six OS processes: simulator, control, two wardens, worker, and Patrol. Real HTTP/SSE calls succeeded. A repeat through a different warden returned 409. After stopping control and then warden 1, warden 2 stayed ready, served degraded-mode requests, and the independent worker settled usage. Patrol remained alive. No external AI request was made.
- Python syntax, dashboard JavaScript syntax, and deployment YAML structure checked. The HAProxy/Caddy deployment configuration is supplied but has not been executed here. Docker, PostgreSQL, and HTTPS acceptance remain staging tasks.
- The rebuilt installer reproduced all 54 release files byte-for-byte, omitted private configuration/data, and refused to overwrite an existing destination.
- Earlier browser checks below describe the previous interface. Chromium was unavailable for a fresh browser run in this environment, so the new attendance panel has not had a browser render test here.

The cluster uses one shared accounting database, not an offline replica per warden. No database-failure availability, transparent stream resumption, throughput target, or uptime percentage is claimed.

This record separates checks actually performed from deployment checks still requiring infrastructure or credentials.

## Previous release checks

- Python 3.12 on Linux, with the supplied direct dependency versions installed in a clean virtual environment.
- `python -m unittest discover -s tests -v`: **46 test methods discovered; 45 passed; 1 PostgreSQL integration test skipped because no PostgreSQL test URL/server was available.**
- Original multi-process reservation, repeated settlement, ledger immutability, transaction rollback, ownership, malformed response, disconnect recovery, and four child-process crash-boundary scenarios retained and passing.
- New OpenAI Responses and Anthropic Messages protocol tests with mocked HTTP transports: streaming/JSON normalization, cached token pricing, conservative reservations, duplicate suppression, known rejection versus ambiguous failure, and recovery using a saved OpenAI response ID.
- Twelve Gemini tests use mocked HTTP transports: fragmented UTF-8 SSE, cumulative final usage, thinking tokens counted once, system-message mapping, zero-balance settlement, idempotency, provider selection, quota errors, safe diagnostics, and incomplete/invalid streams retained for review. No Google request is made by these tests.
- OpenAI/Claude provider-error regression tests verify actionable error categories while excluding raw provider messages and secrets.
- Session creation, CSRF rejection, logout, key expiry, account suspension, shared rate limits, stale price rejection, and worker readiness checks.
- Signed webhook tests: timestamp/signature rejection, wrong amount rejection, repeated delivery and distinct success notification deduplication, and refund-event suspension.
- Operator release is audited and cannot release the same reservation twice. Wallet/ledger/hold reconciliation passes.
- Browser test in headless Chromium against actual local Uvicorn gateway + simulator processes: sign in, create customer, grant credits, create key, stream a response, inspect settled status, reload with session retained, check reconciliation, mobile layout, sign out. No JavaScript page errors. Desktop and mobile screenshots were inspected internally.
- Gemini browser check against a local Uvicorn gateway with mocked Google HTTP: sign in, see the Free-tier assumption, create an unfunded customer and key, receive streamed text and final usage, inspect $0 settlement, check reconciliation and mobile width. No JavaScript page errors. Desktop and mobile screenshots were inspected internally.
- JavaScript syntax, Python syntax, Compose YAML structure, and separation of migration/runtime database credentials checked.
- The updated single-file installer recreated all 46 project files byte-for-byte and refused to overwrite an existing directory. Gemini-only setup, preservation of the owner key, separate test database, migration, and local diagnostics were verified with a dummy provider credential. Earlier backup and reconciliation CLI checks remain applicable.

Browser flows used temporary databases with the simulator or mocked Google HTTP. Protocol tests used test credentials and mocked provider responses. No live Google, OpenAI, or Anthropic call was performed, and no provider billing tier was verified.

## Required before public traffic

1. Run CI or the documented PostgreSQL integration test against a dedicated database. This exercises simultaneous reservations, repeated settlements, and immutable ledger constraints using the PostgreSQL driver.
2. Build and run the Compose stack in staging. Check migration, runtime-role permissions, HTTPS, worker activity, restart recovery, and backup/restore.
3. Run one bounded live request for each enabled provider on your account. For this Gemini adapter, use a project confirmed to be on Free tier. Check exact model availability, API permissions, counted input, final reported usage, rate tier, and the gateway charge. Local tests cannot establish your Google billing tier.
4. Exercise Stripe test-mode checkout and a signed webhook through your actual endpoint. Configure live merchant credentials only after validating your own account and business requirements.
5. Verify your public origin, secret management, backups, incident ownership, monitoring, and ingress limits. Perform workload-specific load tests and an independent security review before claiming an SLA or broad production readiness.

## Known limits

The PostgreSQL global write lock is correct by design but has not been benchmarked for your workload. No throughput, uptime, penetration-test, or compliance certification is claimed. Only the documented text API is supported. Provider outcomes that cannot be recovered automatically require an operator decision. The owner console has one trusted administrator identity, and one deployment serves one SaaS business.
