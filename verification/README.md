# Verification harnesses

These scripts produced the results in `docs/READINESS.md`. They start real processes (simulator, gateway, worker, control, wardens, Patrol) and talk to them over HTTP. They are **not** part of the unit test suite.

| script | what it checks | needs |
|---|---|---|
| `e2e.py <gateway dir>` | Simulator flow end to end with the official OpenAI Python SDK: onboarding, JSON, streaming, idempotent retry, model restriction, settlement, admin separation | a configured simulator-mode `gateway/.env`, `pip install openai` |
| `prod_check.py <gateway dir>` | Production mode on PostgreSQL: migration roles, docs hidden, host allow-list, HSTS, session cookie, onboarding as runtime role, ledger immutability, no secrets in logs | PostgreSQL on 127.0.0.1:5433 with database `tollgate`, role `tollgate_app` (password `apppass`) created by `deploy/init-db.sh` |
| `cluster_check.py <gateway dir>` | Control + 2 wardens + worker + Patrol: failover, restart, drain, control outage, worker crash, database restart, exactly-once billing | as above, plus Linux `pg_ctl` at `/usr/lib/postgresql/16/bin` to stop and start the database |

They were written for Linux. On Windows, prefer `client/live_smoke.py` and the Docker Compose stacks.
