# Deployment and Go-Live Guide

Three ways to run Tollgate, from simplest to production:

| | for | database | HTTPS | status |
|---|---|---|---|---|
| **A. Local demo** | development, classroom demo | SQLite | no | manually verified |
| **B. Temporary public demo** | showing a teacher or tester over the internet | SQLite | via tunnel | **not yet tested** |
| **C. Production** (`compose.yaml` or `compose.wardens.yaml`) | real clients | PostgreSQL | Caddy (automatic) | app verified on PostgreSQL; Docker images not yet built (see `READINESS.md`) |

---

## A. Local demo (Windows, PowerShell)

From `C:\FYP\FYP\gateway`:

```powershell
python -m pip install -r requirements.txt
python manage.py setup --mode simulator
python manage.py migrate
```

Open three terminals in `C:\FYP\FYP\gateway`:

```powershell
# Terminal 1: fake AI provider
python -m uvicorn main:app --app-dir ..\simulator --host 127.0.0.1 --port 8001 --no-access-log
# Terminal 2: gateway + dashboard
python manage.py serve
# Terminal 3: settlement worker
python manage.py worker
```

Then onboard a test client and call it:

```powershell
python manage.py onboard --name "Demo client" --models sim-basic --credit 1.00 --expires-days 7
curl.exe http://127.0.0.1:8000/v1/models -H "Authorization: Bearer <key from the pack>"
```

Dashboard: <http://127.0.0.1:8000/>. Sign in with `python manage.py admin-key`.

For real AI answers, follow `docs/GEMINI.md` (free tier, records $0).

---

## B. Temporary public demo (tunnel) — not yet tested

Use this to let a teacher call your laptop's gateway from their own machine for a day. **Give them only a restricted customer key** (one model, small balance, short expiry). Never give them the owner key.

1. Install Cloudflare's `cloudflared` and run `cloudflared tunnel --url http://127.0.0.1:8000`. It prints a random `https://<name>.trycloudflare.com` address.
2. Stop the gateway. In `gateway\.env` set `TOLLGATE_PUBLIC_ORIGIN=https://<name>.trycloudflare.com`, then start it again. The host allow-list and dashboard sign-in both check this value.
3. Onboard the teacher's key: `python manage.py onboard --name "Teacher" --models sim-basic --credit 0.50 --expires-days 2 --base-url https://<name>.trycloudflare.com`.
4. When the demo ends, revoke the key and stop the tunnel.

Limits: the address changes on every run, it depends on your laptop being on, and it is not suitable for real clients.

---

## C. Production

### 1. What you need

- A Linux server with Docker Engine and the Compose plugin: 2 vCPU, 2–4 GB RAM and 40 GB disk are enough to start. Any VPS provider works.
- A domain or subdomain, for example `api.yourbrand.com`, with an **A record** pointing to the server's IP.
- Inbound ports **80 and 443** open. Nothing else needs to be public.
- A provider account with billing (OpenAI and/or Anthropic) for paying clients. See §7 on Gemini.

### 2. Get the code onto the server

```bash
git clone <your private repo> tollgate && cd tollgate
```

Never copy your local `gateway/.env` or `gateway/data/` to the server. They hold your development secrets and databases.

### 3. Root `.env` (Compose secrets)

```bash
cp deploy/root.env.example .env
python3 -c "import secrets;print(secrets.token_urlsafe(32))"   # run twice, once per password
nano .env
```

```
TOLLGATE_DOMAIN=api.yourbrand.com
POSTGRES_PASSWORD=<random value 1>
TOLLGATE_DB_PASSWORD=<random value 2>
```

### 4. Gateway `.env` (application secrets)

```bash
cp gateway/.env.example gateway/.env
cd gateway
python3 manage.py setup --mode live --providers openai     # generates the owner key
python3 manage.py set-provider openai                       # hidden prompt; paste the provider key
cd ..
```

These two commands need Python 3.11+ with `pip install -r requirements.txt` on the host. Without them, edit `gateway/.env` by hand: set `TOLLGATE_DEVELOPMENT_API_KEY` (32+ random URL-safe characters), the provider key, and `TOLLGATE_ENABLED_PROVIDERS=["openai"]`. Compose itself supplies `TOLLGATE_ENVIRONMENT=production`, `TOLLGATE_MODE=live`, the PostgreSQL URL and the HTTPS public origin.

Then lock the files down: `chmod 600 .env gateway/.env`.

### 5. Prices and margin

`gateway/models.json` sets **what your clients pay**, in USD per million tokens. If you copy the provider's prices exactly, you make no margin. Add your markup, for example provider price × 1.3. Each change needs:

- a new `price_version` (for example `openai-2026-11-08-m30`), and
- `reviewed_on` set to today.

Prices older than `TOLLGATE_PRICE_MAX_AGE_DAYS` (30 by default) make `/ready` return 503 and chat return `price_review_required`. **Put a monthly reminder in your calendar** to check provider prices and update `reviewed_on`. The shipped catalog was reviewed on 2026-10-22, so it expires on **2026-11-21** unless re-reviewed.

### 6. Start

```bash
docker compose config --quiet          # validates without printing secrets
docker compose up -d --build
docker compose ps
curl https://api.yourbrand.com/health
curl https://api.yourbrand.com/ready
```

Migration runs once, then the gateway and worker start. Caddy gets the HTTPS certificate automatically. The database is never exposed.

Restrict the dashboard to your own IP address (optional, recommended). Add this inside the site block of `deploy/Caddyfile`, above `reverse_proxy` (*untested snippet: run `docker compose exec edge caddy validate --config /etc/caddy/Caddyfile` after editing*):

```
@operator_only {
    path / /admin/* /auth/* /assets/*
    not remote_ip 203.0.113.7
}
respond @operator_only 404
```

### 7. Acceptance test before the first client

```bash
docker compose exec gateway python manage.py doctor
docker compose exec gateway python manage.py onboard --name "Self test" --models gpt-5.4-mini --credit 0.20 --expires-days 1
```

Make one chat request with that key. Then compare the dashboard's request (tokens, cost) with the provider's usage page. **This is your go-live check.** Revoke the test key afterwards.

### 8. Backups

```bash
docker compose exec -T database pg_dump -U tollgate -Fc tollgate > backup-$(date +%F).dump
```

Run it daily (cron) and copy the file off the server. Test a restore into a scratch database at least once. See `docs/OPERATIONS.md` → Backups and restore.

### 9. Scaling up

When one gateway is not enough, or you want zero-downtime restarts, switch to `compose.wardens.yaml`: one control service (dashboard and admin), two **warden** API nodes behind HAProxy, a worker and Patrol. Add more wardens by copying a `warden-N` service with a unique `TOLLGATE_NODE_ID`, adding its ID to `TOLLGATE_EXPECTED_WARDENS`, and adding a `server` line in `deploy/wardens.cfg`. See `docs/WARDENS.md`.

---

## Business checklist (outside the code)

These are not software tasks, but you need them before charging real clients.

- **Provider terms.** Read your AI provider's terms on reselling or wrapping their API, and their usage policies, which your clients must also follow. Free tiers (including Gemini's) are meant for testing, so check their terms before offering free-tier access to others. Use a paid tier for paying clients.
- **Taking payments.** Tollgate's optional Stripe Checkout needs a Stripe merchant account, and **Stripe does not accept businesses registered in Pakistan**. Until you have a supported setup, use **manual top-ups**: invoice the client, receive payment (bank transfer, Payoneer, Wise or a local gateway), then run **Grant credits** with the invoice number as the reference. This needs no code change.
- **Your own terms of service and privacy notice**: what you log (metadata only, no prompts), refunds of unused credit, uptime expectations, and acceptable use.
- **Support channel**: one email address, and a response-time promise.
- **Monitoring**: an external uptime check on `https://<domain>/ready`, and alerts as in `docs/OPERATIONS.md` → Monitoring.

I'm not a lawyer or accountant. For contracts, tax and payment compliance, check with a local professional.
