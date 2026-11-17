# Integrating an app with Tollgate: modes, fail-open and key ownership

For a developer who already calls an AI provider from their backend, such as a tic-tac-toe game whose server asks an LLM for the next move. This page covers how much code changes, what happens when Tollgate is down, and whose provider key pays. The API itself is described in [`API_GUIDE.md`](../API_GUIDE.md) and [`openapi.json`](../api/openapi.json).

## 1. Pick a mode

| mode | code change | Tollgate down | best for |
|---|---|---|---|
| **1. Environment variables** | none, if the app already uses the OpenAI SDK with supported fields | app fails | demos, quick trials |
| **2. Base URL + headers** | change base URL and key, add `Idempotency-Key` | app fails | normal integration |
| **3. Fail-open wrapper** | one small function | app keeps working with the business's own provider key; usage not tracked during the outage | apps that must never stop |
| **4. Resilient client** | use `client/resilient_client.py` | relay or direct tier keeps working; usage is synced back afterwards | outages with accounting kept |

In every mode the `tg_` key stays on the server. Never put it in a browser, mobile app or APK.

## 2. Mode 1: environment variables only

The official OpenAI SDKs read `OPENAI_BASE_URL` and `OPENAI_API_KEY` from the environment. Pointing them at Tollgate needs no code change:

```powershell
$env:OPENAI_BASE_URL = "http://host.docker.internal:8100/v1"
$env:OPENAI_API_KEY  = "<tg_ key from the client pack>"
```

This only works if the app:

- calls **Chat Completions** (not the Responses API, embeddings, images or audio),
- uses a model ID from `GET /v1/models` (for example `gemini-3.5-flash-lite` or `gpt-5.4-mini`),
- sends only `model`, `messages`, `max_completion_tokens` (or `max_tokens`), `stream` and `stream_options`. Anything else, such as `temperature`, `top_p`, `n` or `tools`, returns **422 `invalid_request`**,
- uses only `system`, `user` and `assistant` messages with text content.

Without code you cannot send an `Idempotency-Key`, so an SDK retry after a timeout counts as a new request. Move to mode 2 before real traffic.

Apps that use a provider's own SDK (Gemini, Anthropic) cannot use this mode. Tollgate exposes only the OpenAI Chat Completions format, so they use mode 2 with the OpenAI SDK or plain HTTP.

## 3. Mode 2: base URL and headers (recommended)

```python
import uuid
from openai import OpenAI

client = OpenAI(base_url="http://host.docker.internal:8100/v1", api_key=TG_KEY, max_retries=0)

reply = client.chat.completions.create(
    model="gemini-3.5-flash-lite",
    messages=[{"role": "user", "content": "Board: X O _ / _ X _ / _ _ O. Reply with the best free cell number."}],
    max_completion_tokens=16,
    extra_headers={"Idempotency-Key": str(uuid.uuid4()), "X-Tollgate-Feature": "tictactoe-move"},
)
move = reply.choices[0].message.content
```

- `Idempotency-Key`: one new value per logical request, reused only to retry that same request.
- `X-Tollgate-Feature`: optional cost label (1–64 characters, `A-Za-z0-9_.:-`). It appears under Spend by feature in the dashboard.
- Error handling, streaming and the retry policy: [`API_GUIDE.md`](../API_GUIDE.md) §8, §9 and §11. A Docker example: [`SAATHI.md`](SAATHI.md).

## 4. Mode 3: fail-open wrapper

Goal: if Tollgate is down, the app still answers by calling the provider with **the business's own key**.

**Fail-open covers Tollgate outages, not Tollgate decisions.**

| Tollgate result | meaning | wrapper does |
|---|---|---|
| connection refused, DNS failure, connect timeout | Tollgate never received the request | call the provider directly |
| 503 `storage_unavailable`, `authentication_unavailable`, `control_unavailable`, `warden_draining`, `warden_unavailable`, `worker_unavailable`, `price_review_required` | Tollgate did not call the provider | call the provider directly |
| 503 with no Tollgate error body (load balancer has no healthy node) | request did not reach Tollgate | call the provider directly |
| 401, 402, 403, 422, 429 | a deliberate refusal: bad key, no credits, frozen, invalid request, rate limit | **do not** bypass it; return the error |
| read timeout, dropped connection, 504, 502 or 503 `provider_*` | the provider **may** have run and charged the request | do not fall back blindly. Retry once with the **same** `Idempotency-Key`: 409 `duplicate_request` means it ran, so read `GET /v1/requests/{context.original_request_id}` |

```python
import os
import uuid

import httpx

BASE = os.environ["TOLLGATE_BASE_URL"]
KEY = os.environ["TOLLGATE_API_KEY"]
NOT_RUN = {
    None, "storage_unavailable", "authentication_unavailable", "control_unavailable",
    "warden_draining", "warden_unavailable", "worker_unavailable", "price_review_required",
}
http = httpx.Client(timeout=httpx.Timeout(60, connect=3))


class UnknownOutcome(Exception):
    pass


class Refused(Exception):
    pass


def error_code(response):
    try:
        return response.json()["error"]["code"]
    except (ValueError, KeyError, TypeError):
        return None


def ask(messages, model, max_tokens=256, key=None):
    key = key or str(uuid.uuid4())
    try:
        response = http.post(
            f"{BASE}/v1/chat/completions",
            headers={"Authorization": f"Bearer {KEY}", "Idempotency-Key": key},
            json={"model": model, "messages": messages, "max_completion_tokens": max_tokens},
        )
    except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout):
        return ask_provider_directly(messages, model, max_tokens)
    except httpx.HTTPError as exc:
        raise UnknownOutcome(key) from exc
    if response.status_code == 200:
        return response.json()["choices"][0]["message"]["content"]
    code = error_code(response)
    if response.status_code == 503 and code in NOT_RUN:
        return ask_provider_directly(messages, model, max_tokens)
    if response.status_code >= 500:
        raise UnknownOutcome(key)
    raise Refused(response.status_code, code)
```

`ask_provider_directly` is the call the app made before Tollgate, using the business's own provider key and native model name.

Rules for the direct path:

- **No budget enforcement and no Tollgate record.** Log each direct call yourself (time, model, token counts, which user) so it can be reconciled later.
- Use a **restricted provider key with a spending limit set at the provider**. That limit is the only cap during an outage.
- To avoid a 3-second connect wait on every call during a long outage, skip Tollgate for a short period (for example 30 seconds) after a fallback, then try it again.

## 5. Mode 4: resilient client (tracked continuity)

`client/resilient_client.py` (`ResilientTollgate`) does the same failover, but keeps the money accounted for. While Tollgate is healthy it holds a signed, pre-paid **allowance**. During an outage it uses the standby relay, then (only if the operator approved it) a restricted emergency key directly. Afterwards `sync()` uploads token counts so the wallet is charged once.

```python
from resilient_client import ResilientTollgate

tg = ResilientTollgate(api_key=TG_KEY, primary_url="https://api.example.com", relay_url="https://relay.example.com")
tg.refresh_allowance()
reply = tg.chat([{"role": "user", "content": "Next move?"}], model="gemini-3.5-flash-lite")
tg.sync()
```

- Requires operator setup: `manage.py offline-secret`, a standby relay, and the allowance endpoints described in [`OFFLINE.md`](../OFFLINE.md). These offline endpoints are not part of the public `/v1` specification.
- No streaming. The direct tier is off by default and has not been run against live providers.
- It saves prompts and answers in the app's own `journal.db`. Tollgate still stores neither.

## 6. Whose provider key, who pays

- **Bring your own key (BYOK):** each client business gives its **own** provider key (for example its Gemini key) once. The operator saves it in the dashboard (Customers & keys → Customer's own provider key) or with `manage.py customer-key gemini --customer-id cus_...`. It is stored encrypted; only the last four characters are ever shown.
- Requests from that business then call the provider with **its** key, so the provider bills the business directly, not the Tollgate operator. `key_source` on each request (and "Paid by" in the dashboard) shows whose key was used.
- `TOLLGATE_PROVIDER_KEY_POLICY=customer_only` makes this mandatory: a business without its own key gets 403 `provider_key_missing`, so the operator never pays for it. `customer_first` (default) falls back to the operator's keys.
- The client app does not change: it still sends only its `tg_` key. Tollgate still enforces the balance (now a spending budget), spend cap, model allow-list, freeze and revocation.
- In fail-open mode 3 the app calls the provider with the same business's own key, so outage usage also lands on its own provider account.
- The standby relay and direct tier (mode 4) also use the business's own key: it travels encrypted inside the signed allowance (the relay needs the same `TOLLGATE_CREDENTIALS_KEY`), and `ResilientTollgate(..., own_keys={"gemini": "<key>"})` uses it for the direct tier. With `customer_only`, a business without its own key gets no allowance.

## 7. Tracking usage per end user

Send the optional header `X-Tollgate-End-User` with **your** user's ID on each chat request:

```python
extra_headers={"Idempotency-Key": str(uuid.uuid4()), "X-Tollgate-End-User": "player_8f2a41"}
```

- 1–128 characters from `A-Za-z0-9_.:-`. Use an opaque ID from your database, never an email or name.
- `GET /v1/requests/{id}` returns it as `end_user` (`null` when not sent).
- The dashboard shows it in **Requests** and totals it in **Usage by end user** (settled requests, tokens and cost per customer and end user).
- When sent, it is part of the idempotency fingerprint: the same `Idempotency-Key` with a different end user returns 409 `idempotency_conflict`.

For separate budgets per user, create one Tollgate customer per user or team instead; each gets its own wallet, cap and `tg_` key.

## 8. Checklist for a new client app

Developers who use an AI coding assistant can paste the ready-made prompt in [`CLIENT_PROMPT.md`](CLIENT_PROMPT.md).


1. The business signs up at `/portal` and the operator approves it ([CLIENT_PORTAL.md](../CLIENT_PORTAL.md)), or the operator runs `manage.py onboard` and sends the client pack. The business adds its own provider key and creates its `tg_` key in the portal.
2. The backend stores the base URL and key as server-side secrets.
3. Choose mode 2 or 3. Use mode 3 when the app must keep working during a Tollgate outage.
4. Send only the supported fields; send a new `Idempotency-Key` per request and `X-Tollgate-End-User` with your user's ID.
5. Verify with one request: in the dashboard, **Requests** shows it as `settled` with token counts.
