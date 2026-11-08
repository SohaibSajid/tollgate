# Client Onboarding Runbook (operator)

This is the repeatable process for giving a new client access to Tollgate. Every client gets the same three things: a **client pack** (base URL, key, models, limits, support), the **specification** `docs/api/openapi.json`, and the **integration guide** `docs/API_GUIDE.md`. The client's developer should not need anything else.

The client receives **only** their own customer API key (`tg_...`). The owner key, provider keys and the dashboard stay with you.

---

## 1. Before the first client (one time)

- [ ] Gateway deployed over HTTPS, with `/ready` returning 200 (see `docs/DEPLOYMENT.md`).
- [ ] `python manage.py doctor` shows `price review: current` for every model you will sell, and `Ledger integrity: PASS`.
- [ ] One live test request done yourself, with the provider's usage matching the ledger (see `docs/GEMINI.md`).
- [ ] A support contact (an email address is enough).
- [ ] You have decided your model list and pricing in `gateway/models.json`. Prices must be reviewed at least every 30 days (`TOLLGATE_PRICE_MAX_AGE_DAYS`), otherwise requests stop with `price_review_required`.

---

## 2. Onboard a client: one command

Run this on the control host, from the `gateway` folder:

```powershell
python manage.py onboard --name "Acme Ltd" --models gemini-3.5-flash-lite --credit 5.00 --expires-days 30 --spend-cap 20.00 --base-url https://api.example.com --support support@example.com
```

| option | required | meaning |
|---|---|---|
| `--name` | yes (new client) | Customer name shown in the dashboard. |
| `--customer-id` | instead of `--name` | Issue an extra or replacement key for an existing customer. |
| `--models` | yes | One or more model IDs. Only models enabled on this gateway are accepted. |
| `--credit` | no | Starting credit in USD (decimal, up to 6 places). |
| `--reference` | no | Unique reference for the credit (default `onboard-<customer id>`). Re-running with the same reference never adds the credit twice. |
| `--expires-days` | recommended | Key lifetime, 1–365 days. |
| `--spend-cap` | no | Lifetime spending limit in USD (settled + reserved). |
| `--label` | no | Key label, for example `Acme production`. |
| `--base-url` | recommended | Your public origin. Defaults to `TOLLGATE_PUBLIC_ORIGIN`. |
| `--support` | recommended | Printed on the client pack. |

The command prints the **client pack**:

```
================================================================
TOLLGATE CLIENT PACK
================================================================
Customer ID      : cus_eda9cf8ae84273f6d08b2dc6
Key ID           : key_f4cb6b714494f294c3f9e596
API key          : tg_uzoAoLx...
Base URL         : https://api.example.com/v1
Allowed models   : gemini-3.5-flash-lite
Key expires      : in 30 days
Starting balance : 5.000000 USD
Spend cap        : 20.000000
Specification    : docs/api/openapi.json (send this file)
Integration guide: docs/API_GUIDE.md (send this file)
Support          : support@example.com - quote X-Request-ID
...
The API key is shown ONCE and is not stored. Send it over a private channel.
```

**The key is printed once and never stored or logged.** Tollgate keeps only a SHA-256 hash. If the key is lost, issue a new one (§4).

### Same thing in the dashboard

Customers & keys → **Create a customer** → **Grant credits** (unique reference) → **Issue a virtual key** (pick models, expiry) → copy the key from the box that appears → optionally **Lifetime spending cap**.

---

## 3. Send the pack

1. Send the **API key** over a private channel, for example a password manager share or a one-time secret link. Do not paste it into a ticket or group chat.
2. Send the rest of the pack, `openapi.json` and `API_GUIDE.md`, by email or link. The template below is ready to paste.
3. Write down the customer ID and key ID in your client records.

### Client pack email template

> **Subject:** Your Tollgate API access
>
> Hello {name},
>
> Your access to the Tollgate AI API is ready.
>
> - **Base URL:** {base_url}/v1
> - **API key:** sent separately through {private channel}
> - **Models:** {models}
> - **Key expires:** {date}. We will send a replacement before then.
> - **Prepaid balance:** {credit} USD. Check it any time with `GET /v1/balance`.
> - **Limits:** 60 requests/minute, 5 concurrent chat requests, 256 KB per request, up to 4,096 output tokens.
>
> The integration guide (attached, `API_GUIDE.md`) gets you to a first request in five minutes. The full specification is attached as `openapi.json` (OpenAPI 3.1). It opens in Swagger Editor, Postman or Insomnia, and it can generate client code.
>
> The endpoint is OpenAI-compatible. If you already use the OpenAI SDK, change `base_url` and the key, and always send an `Idempotency-Key` header.
>
> For support, email {support} and include the `X-Request-ID` from the response. Never send your API key.

### Acceptance check (with the client)

Ask the client's developer to run the "check the key works" command in the guide (§2), then one chat request. In the dashboard → Requests you should see it `settled`, with a cost.

---

## 4. Key rotation

Do this before expiry, or at once if a key may have leaked.

1. Issue a new key for the same customer:
   ```powershell
   python manage.py onboard --customer-id cus_... --models gemini-3.5-flash-lite --expires-days 30 --label "Acme production 2"
   ```
2. Send it privately. The client deploys it.
3. Once the client confirms, revoke the old key: Dashboard → Customers & keys → Issued keys → **Revoke**, or `DELETE /admin/keys/{key_id}` with the owner key.

A customer can hold several active keys, so the switch has no downtime. Revoking blocks new requests immediately. Requests already in progress still settle normally.

**Suspected leak:** revoke first, then issue a replacement. Check Requests for unexpected use, by feature label and time.

---

## 5. Credits

- **Top up:** Dashboard → Grant credits, or `POST /admin/credits {customer_id, amount_usd, reference}`. Use a unique reference per payment, for example your invoice number. Repeating the same reference and amount is harmless. Reusing it with a different amount is refused (`reference_conflict`).
- A grant is an operator adjustment, not a payment. Collect money through your normal invoicing, or enable Stripe Checkout (`docs/OPERATIONS.md` → Payments).
- Tollgate never lets a balance go negative. Requests are refused with 402 once the available balance cannot cover the reservation.

## 6. Spend caps

`PUT /admin/spend-cap {customer_id, amount_usd}`, or Dashboard → Lifetime spending cap. This is a hard lifetime ceiling on settled + reserved spending, independent of the balance. Use `null` to remove it. A cap below what is already committed is refused (`cap_below_committed`).

## 7. Freezing (suspend a client)

Dashboard → Operations → Customer access → **Suspend new requests**, with a reason (8+ characters, audited). Or `POST /admin/customers/{id}/freeze {"frozen": true, "reason": "..."}`.

New requests get 403 `account_frozen`. Requests in progress still settle, and the balance is kept. Restore with **Restore access** (or `"frozen": false`).

## 8. Offboarding

1. Freeze the customer (§7) with reason "offboarding".
2. Revoke every key (Customers & keys → Issued keys).
3. Wait until Requests shows no `reserved`, `dispatched` or `usage_recorded` rows for them, then run **Check ledger integrity**.
4. Export the ledger (`GET /admin/ledger.csv`) for the final statement and any refund of unused credit, which you handle outside Tollgate.
5. Keep the customer record. Ledger rows are immutable by design, and accounting history is never deleted.

## 9. Record keeping per client

| field | where it comes from |
|---|---|
| Customer ID | client pack |
| Key IDs + expiry dates | client pack / Issued keys |
| Models allowed | client pack |
| Credit references | your invoices |
| Support contact on their side | client |

Set a reminder 7 days before each key expiry.
