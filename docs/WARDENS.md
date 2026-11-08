# API integration and wardens

Tollgate 1.1 has three service roles. `standalone` runs the existing combined application. `control` runs customer/key management and the monitoring dashboard. `warden` runs the customer API and calls the selected provider directly. Applications use a customer virtual key; they do not need to open a playground.

This is the first implemented stage of the warden architecture. It tolerates a control-process failure while the ledger and settlement worker are available. It does not promise uninterrupted operation through every dependency failure.

## Request flow

1. The customer's application sends `POST /v1/chat/completions` with its virtual key, model, messages, output limit, and a stable `Idempotency-Key` for that logical operation.
2. The HTTPS edge forwards `/v1/*` to HAProxy. HAProxy selects a ready warden. Other paths go to control.
3. The warden authenticates the key and applies request limits. It checks its own lease, drain status, settlement worker attendance, model rules, and price freshness.
4. It obtains a provider-specific input bound and reserves money in a short database transaction. Lease and access checks run again under that lock. Wardens share the same database; they never keep independently spendable copies of a wallet balance.
5. The warden marks dispatch, calls the provider once, and returns JSON or streams SSE text directly to the application.
6. Final provider usage and a settlement job are saved together. A separate worker charges actual cost once and releases the unused hold.
7. Control reads the same durable metadata for the dashboard. This is the monitoring mirror: request IDs, model, feature, usage, cost, state, and originating warden. Request bodies and answers are not mirrored or stored by the gateway.

## Attendance and Patrol

Each control/warden process has a unique configured node ID and a random process instance ID. It claims a database lease at startup, sends a heartbeat every five seconds, and becomes stale after twenty seconds by default. The database supplies the lease clock. Duplicate live IDs are rejected. Expired/replaced processes cannot resume admitting requests; restart them to obtain a fresh lease. Graceful shutdown marks the lease offline. After a crash, restarting the same ID may need to wait for expiry.

Patrol is a separate process. Every heartbeat interval it checks attendance, expected wardens, settlement-worker activity, and unresolved usage; it persists a report and logs changed findings. The dashboard shows that report in Operations. Patrol does not call AI, change balances, refund unknown requests, restart services, or directly configure the load balancer.

HAProxy independently probes each warden's `/ready` endpoint every two seconds, removes it after two failed checks, and restores it after two successes. Detection time includes probe timeouts and is not a zero-downtime guarantee. `/health` only says the process can respond. `/ready` additionally checks usable storage, the warden's lease, drain policy, recent settlement worker, control policy, and price dates. It does not make a paid AI health-check request.

Drain a warden through the owner-authenticated endpoint `POST /admin/wardens/{node_id}/drain` with `{"draining":true,"reason":"Planned maintenance"}`. New generations are rejected and readiness becomes 503, while already reserved work may finish. Set `draining:false` to restore it. The drain flag survives a process restart and changes are audited.

## Exact failure policies

`TOLLGATE_CONTROL_FAILURE_POLICY=fail_open` is the default for wardens. It means generation may continue when there is no recent control-service heartbeat, **provided the shared ledger, customer checks, warden lease, prices, and settlement worker are available**. Such requests record `control_degraded=1` and return `X-Tollgate-Control-Status: degraded`.

`fail_closed` returns 503 for new generation when control is unavailable. It may take up to the heartbeat expiry window to recognize an abrupt control failure. This setting never converts an invalid key, insufficient credits, or a database outage into permission to generate.

| Failure | Current behavior |
|---|---|
| Control process stops | `fail_open` permits funded requests; `fail_closed` blocks after attendance expires. |
| One warden stops | Healthy wardens can accept new work. The load balancer needs time to detect the failed node. |
| Warden dies mid-stream | The client sees interruption. No automatic replay or stream migration; inspect the original request. |
| Ledger database fails | New requests fail closed with 503. A live provider request may still have incurred cost; do not release its hold without evidence. |
| Settlement worker stops | Pending jobs survive. After its heartbeat is sixty seconds old, wardens pause new generation. |
| Patrol stops | Generation continues. The control dashboard shows that Patrol has no recent report. |
| Provider quota or service fails | Return the provider adapter's safe error. No silent provider fallback. |
| Credits cannot cover a hold | Return 402 before generation; other wardens see the same reserved balance. |

## Use it from an application

The supported contract is a **text subset** of OpenAI-style Chat Completions. It supports model/messages, `max_completion_tokens` or `max_tokens`, JSON or SSE, and final usage. Images, tools, arbitrary OpenAI fields, the Responses public API, audio, and embeddings are outside this version. SDKs that add other fields may receive 422. Disable SDK automatic generation retries and preserve one idempotency key per logical request.

Customer endpoints are `GET /v1/models`, `GET /v1/balance`, `POST /v1/chat/completions`, and `GET /v1/requests/{request_id}`. Administration stays on control. An owner key cannot generate, and a customer key cannot administer.

Python example, using the already installed `httpx` dependency:

```python
import getpass
import uuid
import httpx

key = getpass.getpass("Customer virtual key: ")
operation = str(uuid.uuid4())  # Keep this ID if the network result is uncertain.
with httpx.Client(timeout=240, trust_env=False) as client:
    response = client.post(
        "http://127.0.0.1:8002/v1/chat/completions",
        headers={"Authorization": "Bearer " + key,
                 "Idempotency-Key": operation,
                 "X-Tollgate-Feature": "support-reply"},
        json={"model": "gemini-3.5-flash-lite",
              "messages": [{"role": "user", "content": "Explain an API."}],
              "max_completion_tokens": 128},
    )
    response.raise_for_status()
    print(response.json()["choices"][0]["message"]["content"])
```

Use `sim-basic` with the local simulator, or a model returned by `/v1/models` for your virtual key. Keep keys on the SaaS backend, outside browser JavaScript. For hosted use, change the origin to your HTTPS API address. The provider credential remains in server configuration. Changing the base URL **and supplying Tollgate's virtual key** is the integration path for supported clients.

The included `client/demo.py` accepts `--base-url`, `--model`, `--stream`, `--parallel`, and `--operation-key`. It asks for the virtual key using hidden input, prints the operation/request IDs, warden, control status, usage, cost, and balance. It makes no automatic generation retry. HTTP errors at the load balancer may have a non-JSON body; integrations must handle that as a failed transport/HTTP response.

An identical retry with the same customer/idempotency key returns 409 and the original request ID; it does not regenerate or replay a cached answer. A retry with changed input and the same key returns an idempotency conflict. If no response headers arrived, repeating the identical request with the same key lets the server reject an already recorded operation. Do not invent a new key merely to hide a network failure.

## Upgrade and run on Windows 11

Stop running Tollgate processes. Keep your existing folder and make a database backup using the old code first. Replace the individual source files and add the new files from the file index. Keep your `.env` and data directory; `.env.example` is not a replacement for your secrets. The installer can instead write a separate new folder and refuses to overwrite an existing folder.

From `C:\FYP\FYP\gateway`:

```powershell
.\.venv\Scripts\python.exe manage.py backup --output ..\backups\before-wardens.db
.\.venv\Scripts\python.exe manage.py setup
.\.venv\Scripts\python.exe manage.py migrate
.\.venv\Scripts\python.exe manage.py doctor
```

`backup` above is for SQLite. Use `pg_dump` for PostgreSQL. Schema 3 adds cluster tables and request metadata; it preserves existing wallets and ledger entries. Keep backups for rollback; older code cannot open schema 3. Do not run migration and public traffic together.

For the simplest local demonstration, keep the currently working Gemini settings and database path. Use the same `gateway` folder in every terminal. Keep `TOLLGATE_PUBLIC_ORIGIN=http://127.0.0.1:8000`. Set `TOLLGATE_WORKER_ENABLED=false` in `gateway/.env` so the worker is independent. Environment variables in a terminal override `.env`; remove old overrides if they point at a different database or mode.

Run one command in each separate terminal:

```powershell
# Terminal 1
.\.venv\Scripts\python.exe manage.py serve --role control --node-id control-1 --port 8000
# Terminal 2
.\.venv\Scripts\python.exe manage.py serve --role warden --node-id warden-1 --port 8002
# Terminal 3
.\.venv\Scripts\python.exe manage.py serve --role warden --node-id warden-2 --port 8003
# Terminal 4
.\.venv\Scripts\python.exe manage.py worker
# Terminal 5
.\.venv\Scripts\python.exe manage.py patrol --node-id patrol-1
```

If you chose simulator mode, also run the simulator in its own terminal on port 8001 using the command in README.md. Every API process on this one PC shares the same SQLite path. Never share SQLite across hosts or network filesystems; use one PostgreSQL service for that deployment.

Open the control dashboard at port 8000 and create a customer virtual key as before. In control mode the playground is hidden; use the application client from another terminal:

```powershell
.\.venv\Scripts\python.exe ..\client\demo.py --base-url http://127.0.0.1:8002 --model gemini-3.5-flash-lite --stream
```

Stop only the control process with Ctrl+C and run that client again. The warden still answers and reports degraded control; the independent worker settles usage. Repeat against port 8003 to demonstrate the second warden. This native Windows setup tests the components directly; automatic routing between ports is provided by the load-balancer deployment below, not by the demo client.

## Load-balanced deployment

Use Linux or Docker Desktop with its WSL 2 backend. `compose.wardens.yaml` is a separate deployment file, not an override of `compose.yaml`. Follow `docs/OPERATIONS.md` for secrets, root `.env`, DNS, TLS, backups, and separate migration/runtime PostgreSQL users. Review prices and provider account tiers first.

```powershell
docker compose -f compose.wardens.yaml config --quiet
docker compose -f compose.wardens.yaml up --build -d
docker compose -f compose.wardens.yaml ps
```

Caddy terminates HTTPS and routes customer API traffic to HAProxy. HAProxy routes directly to `warden-1` or `warden-2`. Control, wardens, worker, and Patrol share PostgreSQL. Only Caddy publishes external ports. Adding a third warden requires a unique service/node ID, an HAProxy backend entry, and an update to `TOLLGATE_EXPECTED_WARDENS` in the Compose configuration. Keep model catalogs and policy settings identical across wardens; hot configuration distribution is not implemented.

The HAProxy/Caddy configuration disables generation retries. A request selected just before a warden fails can still fail; new requests use remaining ready nodes after health detection. Shared idempotency prevents duplicate recorded operations if a caller resubmits the same logical request through another warden.

This Compose stack uses a separate `tollgate-wardens` project and an empty PostgreSQL volume on first run. It does not migrate your local SQLite customers automatically. Do not run two public Compose stacks on ports 80/443 simultaneously. A single-host stack is a demonstration of process redundancy, not host, database, or ingress high availability.

## What a later offline mode would require

Fully disconnected wardens need preallocated, non-overlapping spending allowances. For example, a $1 wallet could lock $0.40 for warden A and $0.40 for B, leaving $0.20 unallocated. Both wardens must never cache permission to spend the whole $1. Each allowance needs an authenticated assignment, a durable local spending log, an expiry rule, and idempotent reconciliation. Expiry alone is not proof that allowance funds were unused; a disconnected in-flight provider call may still cost money.

Local identity/model policy snapshots also expire, and revocations cannot arrive immediately during a partition. Storage loss, late usage, token-bound breaches, and queued-event duplication need fault tests before offering this mode. This bounded offline feature and a separate telemetry outbox are **not implemented in 1.1**. Unrestricted paid fail-open is not a safe substitute.

## Verification and references

See `docs/VALIDATION.md` for performed tests and remaining staging checks. The intended deployment uses active health checks and separate control/data responsibilities described by these primary references, accessed 23 October 2026:

- AWS, Static stability: https://docs.aws.amazon.com/whitepapers/latest/aws-fault-isolation-boundaries/static-stability.html
- HAProxy, Health checks: https://www.haproxy.com/documentation/haproxy-configuration-tutorials/reliability/health-checks/
- HAProxy, Retries and redispatches: https://www.haproxy.com/documentation/haproxy-configuration-tutorials/reliability/retries/
