# Operations runbook

## Public deployment

Use a host with Docker Engine and Compose, a domain pointing to it, outbound access to the configured providers, and inbound ports 80/443. No cloud account is provisioned by these files.

1. Keep secrets outside Git. Generate different strong URL-safe values for the database administrator password, runtime database password, and owner key.
2. Copy `deploy/root.env.example` to `.env` at the project root. Set your domain and the two database passwords. Avoid URL punctuation in these passwords because Compose builds connection URLs.
3. Configure `gateway/.env` from its example. Set provider credentials, `TOLLGATE_MODE=live`, and the owner key. Review `gateway/models.json` against your provider account. Set `TOLLGATE_PUBLIC_ORIGIN=https://your-domain` for consistency; Compose supplies this to containers.
4. Run `docker compose config --quiet` to validate configuration without printing secrets.
5. Run `docker compose up -d --build`. Migration runs before API/worker startup. Caddy obtains HTTPS certificates. The database/API ports are not published directly.
6. Check `docker compose ps`, `https://your-domain/health`, and `https://your-domain/ready`. Readiness checks database access, recent worker activity, and price freshness; it does not spend money probing providers.
7. Sign in, create one test customer and key, grant a small development balance, and run a short live request. Compare provider usage with the final ledger charge before accepting users.

The database initialization script creates `tollgate_app` for normal runtime access. Migrations use the separate database owner. The runtime role cannot alter schema or update/delete ledger/audit entries. Fresh PostgreSQL volumes are supported. If using an existing database, create the runtime role and apply equivalent grants before rollout; initialization scripts run only on an empty PostgreSQL data directory.

Docker image tags are maintained major-version tags, not immutable digests. Review and pin image digests in your deployment/release process. The supplied Compose stack has not been executed in this build environment; CI's PostgreSQL gate and your staging acceptance test remain required.

## Secrets and identity

Never paste provider keys into the customer key field. Never put owner/provider keys in a public frontend, screenshots, support logs, or source control. Use `manage.py set-provider openai` or `anthropic` locally. In production use your host's secret provisioning rather than copying `.env` into an image.

`manage.py rotate-admin-key` updates the local owner key. Restart every gateway process, then show the replacement locally with `admin-key`. In a managed deployment rotate the configured environment secret instead. Revoke customer keys from the dashboard and issue replacements. Existing dispatched work still settles.

## Payments

Stripe Checkout is optional; the core gateway works with explicit administrative grants. Configure `TOLLGATE_STRIPE_SECRET_KEY`, `TOLLGATE_STRIPE_WEBHOOK_SECRET`, and the matching `TOLLGATE_STRIPE_LIVE_MODE`. Use test credentials and test mode first. You need your own eligible Stripe merchant account; Tollgate does not create one or establish eligibility.

Configure the endpoint `https://your-domain/webhooks/stripe` for:

- `checkout.session.completed`
- `checkout.session.async_payment_succeeded`
- `charge.refunded`
- `charge.dispute.created`

Create checkout using the customer key at `POST /v1/billing/checkout` with an allowed amount (`5.00`, `10.00`, `25.00`, or `50.00`) and a stable unique reference. A paid signed event must match the saved order's currency, amount, session, and mode before credits are granted. Browser redirects never grant money. Repeating webhook deliveries or distinct success notifications for the same order cannot grant twice.

The integration sells prepaid credits only: no subscription management, automatic tax, coupon, invoice, or refund automation. Refund/dispute events freeze a matched customer's new work and create an audit entry. Unmatched events create a review entry. **The operator must reconcile the refund/dispute and any consumed credit before restoring access.** Automatic negative balances or refund debits are intentionally absent.

## Monitoring

- `/health`: process liveness/version/mode.
- `/ready`: HTTP 200 when database, worker heartbeat, and price review are ready; 503 otherwise. No paid provider probe.
- `/admin/metrics`: owner-protected Prometheus-style gauges for customers, unresolved requests, settlement states, and last worker heartbeat.
- `/admin/reconciliation`: check wallet/ledger and hold/request invariants.
- Dashboard Operations: worker activity, unresolved counts, audited corrections, and account suspension.

Alert on worker heartbeat older than 60 seconds, a growing pending/blocked queue, unresolved requests beyond your support window, failed reconciliation, and price-review expiry. Monitor logs for provider/worker failures. Never log authorization headers, prompts, answers, or `.env` content. Add your host's ingress traffic limits, availability monitoring, log retention, and alert delivery; these external services are not provisioned here.

## Unknown request procedure

1. Find its Tollgate request ID, provider ID, price version, reservation, and state.
2. Do not resend generation automatically. Check the provider account/support records.
3. If complete usage is known and within the saved reservation, use Operations → Record verified usage with the evidence reference. Evidence and usage are persisted atomically, then the normal worker settles.
4. If no billable work is confirmed, release the hold with that evidence. If the business elects to absorb an uncertain cost, state that explicitly in the evidence. A released hold is not a provider refund.
5. Usage above the bound stays blocked. Investigate the model/rate configuration, suspend affected customers if needed, and handle any business loss explicitly. The UI cannot silently debit above the reservation.

Stored OpenAI responses can be retrieved when `TOLLGATE_OPENAI_STORE_RESPONSES=true` and the response ID was captured. Review retention/privacy obligations first. Missing IDs and Claude failures cannot be reconstructed from a timer.

## Backups and restore

Local SQLite: `python manage.py backup --output ../backups/tollgate-before-upgrade.db` uses SQLite's online backup API. Keep the new file somewhere protected and separate from the application host.

For PostgreSQL, take encrypted off-host backups with your managed database service or `pg_dump`. For the supplied Compose stack, a binary-safe host command is:

```sh
docker compose exec -T database sh -c 'pg_dump -U "$POSTGRES_USER" -Fc "$POSTGRES_DB"' > tollgate-backup.dump
```

Use a shell that preserves binary stdout; Windows PowerShell 5 redirection does not. Use a Linux host/WSL or your database provider's backup facility. Restore into a separate empty database, start matching application code, run `manage.py reconcile`, and inspect representative customers before switching traffic. Keep the old deployment/database until verification finishes. Never run destructive restore against the live database casually.

## Updates and shutdown

Back up first, install the reviewed source/requirements, run migrations using the owner role, then restart API and worker. Schema 2 is additive over the original simulator schema. Rollback requires the matching application/database backup; do not run old schema-1 code against schema 2.

Stop accepting new traffic before shutdown. Let in-flight requests finish, then stop API and worker. If the process is killed, durable jobs remain. Dispatched requests with unknown usage retain holds until recovery or operator resolution.

Price reviews require checking provider documentation and your specific account's rates. Update numeric rates and a new version/date, restart the API, and rerun a small acceptance test. Existing requests keep their original rate snapshot.

## Sources reviewed for this release

- OpenAI token counting: https://developers.openai.com/api/docs/guides/token-counting
- OpenAI streaming: https://developers.openai.com/api/docs/guides/streaming-responses
- OpenAI model/rates: https://developers.openai.com/api/docs/models/gpt-5.4-mini
- Claude token counting: https://platform.claude.com/docs/en/build-with-claude/token-counting
- Claude streaming: https://platform.claude.com/docs/en/build-with-claude/streaming
- Claude pricing: https://platform.claude.com/docs/en/about-claude/pricing
- Stripe webhook operations: https://docs.stripe.com/events/manage-webhook-endpoints
