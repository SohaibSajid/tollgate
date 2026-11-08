# Tollgate 1.2 — prepaid AI gateway with wardens

**New in this update:** separate control and warden roles, shared-ledger API admission, process heartbeats, an independent Patrol monitor, draining, explicit control-outage policy, and a two-warden deployment configuration. Read [the API and warden guide](docs/WARDENS.md) first for this architecture. The old standalone command remains available. The database schema is version 5; back up the database before upgrading.

Tollgate sits between your SaaS backend and AI providers. It authenticates each customer's virtual key, reserves credits before generation, streams the answer, and settles the cost of provider-reported usage once. This release includes OpenAI Responses, Anthropic Messages, and a Gemini free-tier adapter behind a text-only OpenAI Chat Completions interface.

**Release status: deployable release candidate, not a claim of production certification.** Automated local verification is described in `docs/VALIDATION.md`. You must supply your own provider credentials and verify one small live request on each enabled provider account before accepting customer traffic. No live provider call or real payment was made while building this release.

## Start here

| you are | read |
|---|---|
| A **client developer** integrating with Tollgate | [Integration guide](docs/API_GUIDE.md) + [OpenAPI 3.1 specification](docs/api/openapi.json). The running server also publishes the spec at `/api-spec.json`. |
| The **operator** onboarding a client | [Onboarding runbook](docs/ONBOARDING.md): one command, `python manage.py onboard ...`, prints the client pack |
| The **operator** using the dashboard | [Admin panel guide](docs/ADMIN_PANEL.md) |
| Deploying for real users | [Deployment and go-live guide](docs/DEPLOYMENT.md), then [Operations](docs/OPERATIONS.md) |
| Reviewing the project | [Handover document](docs/HANDOVER.md) and the [readiness report](docs/READINESS.md) |

**Spec maintenance rule:** when any client-facing endpoint, field, header or error changes, update `gateway/app/api_contract.py`, run `python manage.py export-openapi` (it rewrites `docs/api/openapi.json` and the generated tables in `docs/API_GUIDE.md`), then run the tests. `tests/test_api_contract.py` fails if the spec, the guide and the server disagree.

**Want to test real AI without paying?** Follow [the Windows Gemini guide](docs/GEMINI.md). It enables only Gemini, uses a separate test database, and records $0 charges under Google's free-tier rates. You must confirm your project's Free tier in AI Studio; Tollgate cannot verify its billing tier. Do not enable billing for that test.

## What is included

| Area | Included behavior |
|---|---|
| Real AI | OpenAI, Anthropic, and free-tier Gemini text generation, JSON and incremental SSE, remote input token counting |
| Credit accounting | Integer micro-USD, atomic reservations, immutable ledger, cached-input pricing, saved price versions |
| Customer controls | Separate wallets, hashed virtual keys, expiry/revocation, model allowlists, lifetime caps, suspension |
| Reliability | Durable settlement queue, independent worker, bounded timeouts, idempotent settlement, conservative recovery |
| Dashboard | Expiring HttpOnly sessions, CSRF protection, customers, keys, playground, requests, ledger, operations, CSV export |
| Payments | Optional hosted Stripe Checkout, signed webhooks, amount/mode checks, duplicate-event protection |
| Deployment | PostgreSQL, separate migration/runtime database roles, Docker Compose, HTTPS through Caddy |
| Operations | Health/readiness endpoints, protected metrics, audit trail, reconciliation, backup and incident instructions |

One deployment belongs to **one SaaS business**. Customers are that business's end users. Deploy a separate instance/database for a different business. This is not a hosted multi-organization account service.

## Windows 11: first real request

Use Python 3.12 or 3.13 (both are tested). Open PowerShell in `C:\FYP\FYP\gateway`. Commands use the virtual environment directly; activation is unnecessary.

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe manage.py setup --mode live
.\.venv\Scripts\python.exe manage.py set-provider openai
.\.venv\Scripts\python.exe manage.py migrate
.\.venv\Scripts\python.exe manage.py doctor
.\.venv\Scripts\python.exe manage.py admin-key
.\.venv\Scripts\python.exe manage.py serve
```

`set-provider` asks for your provider key using hidden terminal input. Do not paste that key into the dashboard or commit it. To use Claude as well, run `manage.py set-provider anthropic` and restart. Only enabled providers with credentials appear in the model list. `setup --providers openai anthropic` selects those providers; `setup --providers gemini` selects Gemini alone. Environment variables override `.env` settings.

Open **http://127.0.0.1:8000/**, exactly matching `TOLLGATE_PUBLIC_ORIGIN`. Sign in using the **owner** key printed by `admin-key`.

1. Create a customer, then grant an explicit small development balance, such as `$1.00`.
2. Issue a customer key with access to `gpt-5.4-mini`. Copy it; the full key is shown once.
3. Use that customer key in Playground. Send a short prompt with an output limit of 256.
4. Inspect the request. After settlement, check the ledger charge and released reservation.
5. Compare the provider request ID and token usage with your provider account. This is your live acceptance test.

**Grant credits is an operator adjustment; it does not collect a payment.** The provider bills your provider account when generation runs. A customer's Tollgate balance is a separate prepaid entitlement.

The local server binds only to 127.0.0.1. For public deployment use `docs/OPERATIONS.md`, PostgreSQL and HTTPS, not an exposed development server.

## Upgrading the simulator version

Stop both old servers. Keep a backup of your entire existing project and database before replacing source files. Keep your `.env` private. The new `.env.example` is a template, not a replacement for your credentials.

After installing the new requirements, these commands preserve the old SQLite balances:

```powershell
.\.venv\Scripts\python.exe manage.py setup
.\.venv\Scripts\python.exe manage.py backup --output ..\backups\before-v1.db
.\.venv\Scripts\python.exe manage.py migrate
```

The additive migration upgrades earlier supported schemas to version 5 without replacing ledger entries. **Simulator credits were never paid money.** For live operation start with a fresh database (`TOLLGATE_DATABASE_PATH=data/live.db`) and explicitly grant development/promotional credits or receive verified payments. A fresh PostgreSQL deployment starts empty. This release does not silently copy SQLite data into PostgreSQL.

To keep practicing without paid AI, use `manage.py setup --mode simulator`, run the simulator in terminal A and gateway in terminal B:

```powershell
.\.venv\Scripts\python.exe -m uvicorn main:app --app-dir ..\simulator --host 127.0.0.1 --port 8001 --no-access-log
```

```powershell
.\.venv\Scripts\python.exe manage.py serve
```

## Developer integration

For the supported text subset, use your existing OpenAI client with a different `base_url` **and the customer's Tollgate virtual key**. Your provider key stays on the gateway. Do this in your SaaS backend; do not embed a shared customer key into public frontend code.

```python
from openai import OpenAI  # Install separately in your client application's environment.
import uuid

client = OpenAI(
    base_url="http://127.0.0.1:8000/v1",
    api_key="YOUR_CUSTOMER_VIRTUAL_KEY",
    max_retries=0,
)
operation_id = str(uuid.uuid4())
response = client.chat.completions.create(
    model="gpt-5.4-mini",
    messages=[{"role": "user", "content": "Explain atomic reservations in two sentences."}],
    max_completion_tokens=256,
    extra_headers={"Idempotency-Key": operation_id, "X-Tollgate-Feature": "support"},
)
print(response.choices[0].message.content)
```

Reuse the same idempotency key for the same logical operation. A duplicate returns HTTP 409 and the original request ID. **Tollgate does not replay stored answers**, and generation is never automatically retried. Inspect `GET /v1/requests/{id}` before deciding to create another operation. An omitted idempotency key makes each HTTP call a new operation.

A dependency-free-in-your-client example using the gateway's installed HTTPX is in `client/demo.py`. Use `--model gpt-5.4-mini` in live mode.

## Supported API boundary

Accepted chat fields: `model`, text `messages` with system/user/assistant roles, `max_completion_tokens` (or `max_tokens`), `stream`, and `stream_options.include_usage`. Output is one text choice. Output limits are 1–4096 locally, with an OpenAI minimum of 16. Claude and Gemini system messages must precede the user/assistant conversation. Gemini conversations must start and end with a user message. A final usage event is emitted for streaming, including when a client omits `include_usage`.

Unsupported parameters are rejected rather than silently forwarded. This release does **not** implement tool calling, file/image/audio/video processing, embeddings, native Responses endpoints, provider fallback, shared organization signup, email/password accounts, SSO/MFA, monthly subscriptions, tax automation, automated refunds, or arbitrary provider URLs. These are separate product extensions, not hidden switches.

## Important cost behavior

- Rates are in `gateway/models.json`, in USD per million tokens. Standard global provider pricing was reviewed on **22 October 2026**. Regional pricing, batch/flex/priority service, tools, and taxes are outside these rates.
- Live requests stop when a price review is older than the configured 30 days. Verify rates and limits with the provider before changing `reviewed_on` and `price_version`; a timestamp change alone is not a price review.
- Input token counting uses each provider's endpoint. OpenAI's exact input count is reserved at the uncached rate. Claude describes its count as an estimate, so Tollgate conservatively reserves a full context window at the highest configured input rate plus the output limit. Claude's hold can therefore be much higher than its final charge.
- Gemini uses native token counting and final usage, including thinking tokens once. Its reviewed free-tier model has $0 rates, so it needs no credit grant. The dashboard labels these rates as an assumption: a paid Google project may still incur provider charges. This adapter does not support paid Gemini rates. See `docs/GEMINI.md` for quotas and usage limits.
- Final costs use reported usage, with cached input billed separately. Reasoning is already included in OpenAI output usage and is not counted twice. Claude's input count is normalized to include its cache categories. Known five-minute cache writes are supported; unexpected cache-write durations require review. This text API does not let callers request prompt caching.
- USD calculations use integers and round up **once per request** to one micro-dollar. These are gateway charges using configured rates; the provider invoice may include other adjustments and must be reconciled separately.
- Unknown outcomes keep their reservation. OpenAI response retrieval can help only when storage is enabled and a provider response ID was captured. Claude, Gemini, and requests without an ID may need operator evidence. There is no automatic refund merely because a request is old.

Read `docs/ARCHITECTURE.md`, `docs/OPERATIONS.md`, `docs/SECURITY.md`, and `docs/VALIDATION.md` before public deployment.

## Tests

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

The PostgreSQL test requires a dedicated test database URL in `TOLLGATE_TEST_POSTGRES_URL`. CI provisions PostgreSQL automatically. Never point tests at a production database.

## Offline continuity (new in 1.2)

Customers' apps switch automatically from Tollgate to a standby relay and, as a guarded last resort, straight to the AI provider, using a signed spending allowance; an outside watchdog emails/texts them, and usage is settled automatically afterwards. Read `docs/OFFLINE.md` for the design, limits and setup. New commands: `offline-secret`, `relay`, `watchdog`. New files: `client/resilient_client.py`, `compose.relay.yaml`.

## Learning order

Start with `app/services/pricing.py`, then `app/database.py`, `app/services/ledger.py`, `app/services/live_provider.py`, `app/main.py`, and `app/services/worker.py`. Follow one request through reservation, dispatch, usage recording, and settlement before changing the financial rules.
