# Tollgate API — Integration Guide

**API version 1.2.0** · Machine-readable specification: [`docs/api/openapi.json`](api/openapi.json) (OpenAPI 3.1)

This guide is everything a developer needs to connect an application to Tollgate. If this guide and `openapi.json` ever disagree, the specification wins. Both are checked against the running server by automated tests.

---

## 1. What you receive from the operator

Your **client pack** contains:

| item | example | notes |
|---|---|---|
| Base URL | `https://api.example.com/v1` | All client endpoints live under `/v1`. |
| API key | `tg_Xk3...` (46 characters) | Shown once. Store it as a server-side secret. |
| Allowed models | `gemini-3.5-flash-lite` | Your key can call only these. |
| Key expiry | 30 days | Ask for a new key before it expires. |
| Starting balance | `5.000000` USD | Prepaid credits. |
| Support contact | `support@example.com` | Always quote the `X-Request-ID`. |

**Keep the key on your server.** Never put it in a browser, mobile app, or public repository. Anyone with the key can spend your credits. Tollgate does not send CORS headers, so browser calls are refused by design.

---

## 2. Connect in five minutes

Replace `$BASE` with your base URL **without** `/v1` (for example `https://api.example.com`) and `$KEY` with your key.

### Check the key works

```bash
curl $BASE/v1/models -H "Authorization: Bearer $KEY"
```

### curl — one answer

```bash
curl $BASE/v1/chat/completions \
  -H "Authorization: Bearer $KEY" \
  -H "Content-Type: application/json" \
  -H "Idempotency-Key: 6f1c2e0a-order-1234" \
  -d '{"model":"gemini-3.5-flash-lite","messages":[{"role":"user","content":"Explain an API gateway in one sentence."}],"max_completion_tokens":256}'
```

### Python — official OpenAI SDK

Tollgate's chat endpoint is OpenAI-compatible, so the official SDK works by changing `base_url`. This is tested with `openai` 3.x, both JSON and streaming.

```python
import uuid
from openai import OpenAI

client = OpenAI(api_key="tg_...", base_url="https://api.example.com/v1")

answer = client.chat.completions.create(
    model="gemini-3.5-flash-lite",
    messages=[{"role": "user", "content": "Explain an API gateway in one sentence."}],
    max_completion_tokens=256,
    extra_headers={"Idempotency-Key": str(uuid.uuid4())},
)
print(answer.choices[0].message.content)
print(answer.usage)
```

> **Important with SDKs.** The OpenAI SDK retries automatically after timeouts and 5xx errors. Always pass an `Idempotency-Key` as shown. The SDK repeats the same headers on its retries, so Tollgate recognises a retry and will not generate (or charge) twice. Alternatively create the client with `max_retries=0` and handle retries yourself.

To read Tollgate's headers (cost, request ID) with the SDK, use `with_raw_response`:

```python
raw = client.chat.completions.with_raw_response.create(model=..., messages=..., extra_headers={"Idempotency-Key": key})
answer = raw.parse()
print(raw.headers["X-Tollgate-Request-ID"], raw.headers["X-Tollgate-Cost-USD"])
```

### Python — streaming

```python
stream = client.chat.completions.create(
    model="gemini-3.5-flash-lite",
    messages=[{"role": "user", "content": "Write a haiku about gateways."}],
    stream=True,
    extra_headers={"Idempotency-Key": str(uuid.uuid4())},
)
for chunk in stream:
    if chunk.choices and chunk.choices[0].delta.content:
        print(chunk.choices[0].delta.content, end="", flush=True)
    if chunk.usage:
        print("\nTokens used:", chunk.usage.total_tokens)
```

### JavaScript / TypeScript (Node.js 18+, no SDK)

```javascript
const response = await fetch(`${process.env.TOLLGATE_BASE}/v1/chat/completions`, {
  method: "POST",
  headers: {
    "Authorization": `Bearer ${process.env.TOLLGATE_KEY}`,
    "Content-Type": "application/json",
    "Idempotency-Key": crypto.randomUUID(),
  },
  body: JSON.stringify({
    model: "gemini-3.5-flash-lite",
    messages: [{ role: "user", content: "Explain an API gateway in one sentence." }],
    max_completion_tokens: 256,
  }),
});
const data = await response.json();
if (!response.ok) {
  throw new Error(`${data.error.code}: ${data.error.message} (request ${data.error.request_id})`);
}
console.log(data.choices[0].message.content);
```

The official `openai` npm package also works with `new OpenAI({ apiKey, baseURL: "https://api.example.com/v1" })`. The same `Idempotency-Key` advice applies.

---

## 3. Authentication

Every `/v1/` request needs:

```
Authorization: Bearer tg_<your key>
```

`/health` and `/ready` need no key. A missing, wrong, revoked or expired key returns **401 `invalid_api_key`**. Keys are stored only as a hash, so the operator cannot recover a lost key. They revoke it and issue a new one.

---

## 4. Endpoints

| method | path | purpose |
|---|---|---|
| GET | `/health` | Is the process alive? (no key) |
| GET | `/ready` | Can this node take traffic now? 200 or 503 (no key) |
| GET | `/v1/models` | Models your key may use, with prices |
| POST | `/v1/chat/completions` | Generate text (JSON or streaming) |
| GET | `/v1/balance` | Your credit balance |
| GET | `/v1/requests/{request_id}` | State and final cost of one of your requests |

Full schemas, examples and every error response for each endpoint are in `openapi.json`. Open it in [Swagger Editor](https://editor.swagger.io) or any OpenAPI viewer.

---

## 5. Models and prices

`GET /v1/models` returns only the models your key may use:

```json
{"object": "list", "data": [{
  "id": "gemini-3.5-flash-lite", "object": "model", "owned_by": "gemini",
  "pricing_source": "free_tier_assumed", "price_version": "gemini-free-tier-2026-10-22",
  "reviewed_on": "2026-10-22",
  "input_usd_per_million": "0.000000", "output_usd_per_million": "0.000000",
  "cached_usd_per_million": "0.000000",
  "max_output_tokens": 4096, "max_input_tokens": 40000
}]}
```

Prices are **USD per million tokens**. Cost of one request = `input_tokens × input_price / 1,000,000 + output_tokens × output_price / 1,000,000`, rounded **up** to the next micro-dollar. Every request records the `price_version` it was charged at.

---

## 6. Chat request

```json
{
  "model": "gemini-3.5-flash-lite",
  "messages": [
    {"role": "system", "content": "Answer briefly."},
    {"role": "user", "content": "What is an API gateway?"}
  ],
  "max_completion_tokens": 256,
  "stream": false
}
```

| field | type | required | rules |
|---|---|---|---|
| `model` | string | yes | An ID from `GET /v1/models`. |
| `messages` | array | yes | 1 to 50 items. Each has `role` (`system`, `user` or `assistant`) and non-blank `content` (text, at most 8,000 characters). All messages together: at most 32,000 characters. |
| `max_completion_tokens` | integer | no | 1 to 4,096, default 256, and no more than the model's `max_output_tokens`. The legacy name `max_tokens` also works. OpenAI models need at least 16. |
| `stream` | boolean | no | `true` for Server-Sent Events. Default `false`. |
| `stream_options` | object | no | Accepted for compatibility (`{"include_usage": true}`). Usage is always sent anyway. |

**Strict validation.** Unknown fields (for example `temperature`, `tools`, `n`) and wrong types (for example `"256"` as a string) return **422 `invalid_request`**, with `details` listing each bad field. Tollgate never silently ignores a field you sent.

**Not supported in v1:** tool/function calling, images, audio, file inputs, embeddings, multiple choices (`n`), sampling parameters such as `temperature` or `top_p`.

### Optional request headers

| header | purpose |
|---|---|
| `Idempotency-Key` | Your unique ID for this logical request (1–128 chars: letters, digits, `_ . : -`). **Strongly recommended.** See §9. |
| `X-Tollgate-Feature` | A label for cost reporting, for example `support-bot` (1–64 chars, same alphabet). Default `default`. |

---

## 7. Chat response

### JSON (`stream: false`)

```json
{
  "id": "chatcmpl-req_4f0c2b7e9a1d4c3b8e6f5a2d1c0b9a87",
  "object": "chat.completion",
  "created": 1762600000,
  "model": "gemini-3.5-flash-lite",
  "choices": [{"index": 0, "message": {"role": "assistant", "content": "An API gateway is..."}, "finish_reason": "stop"}],
  "usage": {"prompt_tokens": 14, "completion_tokens": 38, "total_tokens": 52}
}
```

`finish_reason` is `stop` (finished), `length` (hit `max_completion_tokens`) or `content_filter` (blocked by the provider's safety system). `usage` may also contain `prompt_tokens_details.cached_tokens`, `completion_tokens_details.reasoning_tokens` (thinking tokens, already included in `completion_tokens`) and `cache_write_tokens`, depending on the provider.

### Response headers

| header | when | meaning |
|---|---|---|
| `X-Request-ID` | every response | Unique ID. **Quote this to support.** |
| `X-Tollgate-Request-ID` | chat | Same value. Use it with `GET /v1/requests/{id}`. |
| `Idempotency-Key` | chat | The key in effect (yours, or the request ID if you sent none). |
| `X-Tollgate-Reservation-USD` | chat | Worst-case amount held before the call. |
| `X-Tollgate-Cost-USD` | chat, JSON only | Final cost of this request. |
| `X-Tollgate-Settlement` | chat, JSON only | Request state when the answer was sent (normally `usage_recorded`). |
| `X-Tollgate-Usage-Source` | chat | `provider_reported` (live) or `simulated` (test mode). |
| `X-Tollgate-Warden-ID`, `X-Tollgate-Control-Status` | chat, multi-node deployments | Which API node served you, and whether the control service was reachable. |
| `Retry-After` | 429 `rate_limit_exceeded` | Seconds to wait. |

---

## 8. Streaming

With `"stream": true` the response is `text/event-stream`. Each event is one line `data: <json>` followed by a blank line:

```
data: {"id":"chatcmpl-req_...","object":"chat.completion.chunk","created":1762600000,"model":"gemini-3.5-flash-lite","choices":[{"index":0,"delta":{"content":"An API gate"},"finish_reason":null}],"usage":null}

data: {"id":"chatcmpl-req_...","object":"chat.completion.chunk","created":1762600000,"model":"gemini-3.5-flash-lite","choices":[{"index":0,"delta":{},"finish_reason":"stop"}],"usage":null}

data: {"id":"chatcmpl-req_...","object":"chat.completion.chunk","created":1762600000,"model":"gemini-3.5-flash-lite","choices":[],"usage":{"prompt_tokens":14,"completion_tokens":38,"total_tokens":52}}

data: [DONE]
```

Rules for your parser:

1. Append `choices[0].delta.content` when present.
2. The **last chunk before `[DONE]`** has empty `choices` and the final `usage`. It is always sent.
3. `[DONE]` means success.
4. **Mid-stream failure:** if the provider fails after streaming started, you receive one event `data: {"error": {"code": "...", "message": "...", "request_id": "req_..."}}` and the connection closes **without** `[DONE]`. The HTTP status is already 200 at that point, so check every event for an `error` key. Treat the text received so far as incomplete, then read `GET /v1/requests/{request_id}` to see whether you were charged.

The cost header is not available on streams, because headers are sent before the answer. Read the cost from `GET /v1/requests/{id}` after the stream ends.

---

## 9. Idempotency: safe retries

Networks fail. If you resend a chat request after a timeout, you might pay for the same answer twice. To prevent that, send an `Idempotency-Key` that is unique for each **logical** request, and reuse it **only** when retrying that same request.

| you send | Tollgate returns |
|---|---|
| a new key | Normal processing. |
| the same key and the **same** body and feature label | **409 `duplicate_request`**. Nothing new is generated. `context.original_request_id` points to the first attempt. Read it with `GET /v1/requests/{id}`. |
| the same key with a **different** body or `X-Tollgate-Feature` | **409 `idempotency_conflict`**. Use a new key for a different request. |

Keys are remembered per customer, permanently. Without an `Idempotency-Key`, every call counts as a new request.

### Recommended retry policy

```
send request with Idempotency-Key K
├─ 2xx                         → done
├─ 409 duplicate_request       → GET /v1/requests/{context.original_request_id}; do not regenerate
├─ 429                         → wait Retry-After (or 1–2 s for concurrency_limit), resend with K
├─ 503 storage_unavailable /
│  authentication_unavailable /
│  warden_* / worker_unavailable → wait a few seconds, resend with K
├─ 502 / 503 / 504 provider_*  → GET /v1/requests/{X-Tollgate-Request-ID}:
│                                   released   → resend with a NEW key
│                                   unresolved → stop; contact support with the request ID
├─ network error, no response  → resend with K (Tollgate deduplicates)
└─ other 4xx                   → fix the request; do not retry unchanged
```

---

## 10. Billing, balance and request states

Before calling the provider, Tollgate **reserves** the worst case: your real input token count plus the full `max_completion_tokens`, at the model's prices. If your available balance cannot cover the reservation, you get **402 `insufficient_credits`** and nothing is called. A smaller `max_completion_tokens` needs a smaller reservation.

After the answer, the exact usage is recorded and the settlement worker charges it **exactly once**, usually within a second, then releases the unused part of the hold.

`GET /v1/balance`:

```json
{"frozen": false, "balance_usd": "4.998512", "reserved_usd": "0.000000",
 "available_usd": "4.998512", "spent_usd": "0.001488", "spend_cap_usd": null}
```

`available_usd = balance_usd − reserved_usd`. All money values are strings with exactly six decimals. Parse them as decimals, never as floats.

`GET /v1/requests/{request_id}` returns the request's `state`, token counts, `reserved_usd`, final `cost_usd` and `error_code`. Prompts and answers are **never stored**.

<!-- generated:states -->
| state | meaning |
|---|---|
| `reserved` | Credits are held; the provider has not been called yet. |
| `dispatched` | The provider call is in progress. |
| `usage_recorded` | Usage is saved; the settlement worker will charge it shortly. |
| `settled` | Final. The charge is in the ledger and the unused hold was released. |
| `released` | Final. Nothing was charged and the hold was returned. |
| `unresolved` | The outcome is uncertain. Credits stay held until the operator resolves it. Do not retry blindly. |
<!-- /generated:states -->

---

## 11. Errors

Every error uses one envelope:

```json
{"error": {
  "code": "insufficient_credits",
  "message": "Available credits cannot cover this request's reservation.",
  "request_id": "req_4f0c2b7e9a1d4c3b8e6f5a2d1c0b9a87",
  "context": {"required_usd": "0.002144", "available_usd": "0.000500"}
}}
```

- **Branch on `code`.** Messages may be reworded.
- `details` (only for `invalid_request`) lists `{field, message, type}` per bad field. Your submitted values are never echoed back.
- `context` gives machine-readable extras for some codes.
- An unknown code may appear in a future version. Handle it by HTTP status.

<!-- generated:errors -->
| HTTP | code | meaning | what to do |
|---|---|---|---|
| 400 | `content_length_mismatch` | Content-Length does not match the body that arrived. | Let your HTTP library set Content-Length. |
| 400 | `invalid_content_length` | The Content-Length header is not one non-negative integer. | Let your HTTP library set Content-Length. |
| 401 | `invalid_api_key` | The Authorization header is missing, malformed, revoked, or expired. | Send `Authorization: Bearer tg_...` with an active key. Ask the operator for a new key if it expired. |
| 402 | `insufficient_credits` | Available credits cannot cover the worst-case reservation for this request. | Top up, or lower `max_completion_tokens`. `context.required_usd` and `context.available_usd` show the gap. |
| 403 | `account_frozen` | The operator suspended new requests for this customer. | Contact the operator. |
| 403 | `model_not_allowed` | This key is not allowed to use the requested model. | Use a model listed by `GET /v1/models` for this key. |
| 403 | `spend_cap_exceeded` | The request would exceed the customer's lifetime spend cap. | Contact the operator to raise the cap. |
| 404 | `request_not_found` | No request with this ID belongs to your customer account. | Check the ID. You can only read your own requests. |
| 404 | `wrong_service` | This address serves a different role (generation vs administration). | Use the API base URL from your client pack. |
| 408 | `request_body_timeout` | The body did not arrive within the read deadline. | Retry on a stable connection. |
| 409 | `duplicate_request` | The same Idempotency-Key was already used with the same body. | Do not generate again. Read `GET /v1/requests/{context.original_request_id}`. |
| 409 | `idempotency_conflict` | The same Idempotency-Key was already used with a different body or feature label. | Use a new Idempotency-Key for a different request. |
| 409 | `reservation_unavailable` | The hold was released before dispatch. No provider call was made. | Retry with a new Idempotency-Key. |
| 413 | `request_too_large` | The request body exceeds the byte limit (default 262,144 bytes). | Send a smaller body. |
| 422 | `invalid_message_order` | The provider cannot accept this sequence of roles. | Start with system (optional) then user, and alternate user/assistant. |
| 422 | `invalid_request` | A field is missing, has the wrong type, or breaks a limit. `details` lists each field. | Fix the listed fields. Do not retry unchanged. |
| 422 | `model_limit_exceeded` | Input or output size is above the model's limit (OpenAI models also need `max_completion_tokens` of at least 16). | Shorten the prompt or change `max_completion_tokens`. |
| 422 | `unsupported_model` | The model ID is not enabled on this gateway. | Use a model listed by `GET /v1/models`. |
| 429 | `concurrency_limit` | Too many chat requests are running at once for this customer (default 5). | Wait for one to finish, then retry. |
| 429 | `rate_limit_exceeded` | Too many requests from this customer in the current minute (default 60). | Wait for the `Retry-After` seconds, then retry. |
| 500 | `internal_error` | Unexpected server error. Details are hidden. | Read `GET /v1/requests/{id}` before retrying; quote `X-Request-ID` to support. |
| 502 | `provider_rejected` | The AI provider rejected or failed the call. Provider failures use code `provider_*`, `invalid_provider_*`, `incomplete_provider_stream`, `missing_usage`, `openai_*`, `anthropic_*` or `gemini_*`. | Read `GET /v1/requests/{id}`. If `released`, retry with a new Idempotency-Key. If `unresolved`, contact support. |
| 503 | `authentication_unavailable` | Key verification storage is temporarily unavailable. | Retry after a short wait. |
| 503 | `control_unavailable` | The control service is down and this node is configured to fail closed. | Retry later. |
| 503 | `price_review_required` | The operator must review this model's price before it can be used. | Retry later or contact the operator. |
| 503 | `provider_unavailable` | The AI provider could not be reached. | Read `GET /v1/requests/{id}` before retrying. |
| 503 | `storage_unavailable` | The database is temporarily unavailable. | Retry with the same Idempotency-Key. |
| 503 | `warden_draining` | This API node is being taken out of service. | Retry; the load balancer will pick another node. |
| 503 | `warden_unavailable` | This API node is not registered as healthy. | Retry; the load balancer will pick another node. |
| 503 | `worker_unavailable` | Settlement is paused, so new generation is paused. | Retry later; another API node may be healthy. |
| 504 | `provider_timeout` | The provider did not finish before the deadline (default 180 s). | Read `GET /v1/requests/{id}`; credits stay held until the outcome is known. |
<!-- /generated:errors -->

---

## 12. Limits

Operator defaults. Your operator may configure different values.

| limit | default | error when exceeded |
|---|---|---|
| Requests per minute per customer (all `/v1` calls) | 60 | 429 `rate_limit_exceeded` |
| Concurrent chat requests per customer | 5 | 429 `concurrency_limit` |
| Request body | 262,144 bytes | 413 `request_too_large` |
| Messages per request | 50 | 422 `invalid_request` |
| Characters per message / per request | 8,000 / 32,000 | 422 `invalid_request` |
| `max_completion_tokens` | 1–4,096 and ≤ model `max_output_tokens` | 422 `invalid_request` / `model_limit_exceeded` |
| Provider deadline | 180 seconds | 504 `provider_timeout` |

---

## 13. Getting help

Send the operator:

1. the **`X-Request-ID`** (or `request_id` from the error body),
2. the time and the endpoint,
3. the error `code`.

**Never send your API key**, and never send prompts unless asked. Tollgate does not store prompts or answers, so support cannot see them anyway.

---

## 14. Versioning policy

- The path prefix `/v1` is the major version. Within `/v1` changes are **additive only**: new optional request fields, new response fields, new headers, new error codes.
- Your client **must ignore unknown response fields** and handle unknown error codes by HTTP status.
- Removing or renaming anything requires `/v2`, announced in advance, with `/v1` kept running during a migration period.
- The current specification version is in `info.version` of `openapi.json`.

---

## 15. FAQ

**Why did I get 402 when my balance looks positive?**
The reservation covers the full `max_completion_tokens`. Lower it, or top up. `context.required_usd` shows the amount needed.

**I retried and got 409 `duplicate_request`. Did I lose my answer?**
The first attempt ran. Tollgate does not store answer text, so read `GET /v1/requests/{original_request_id}` to see the cost and state. If you need the text, send a new request with a new key.

**Why was `temperature` rejected?**
v1 accepts only the fields in §6, so behaviour and cost stay predictable. Unknown fields are refused rather than silently ignored.

**Can I call Tollgate from a browser?**
No. That would expose your key. Call it from your backend.

**How fast is the charge final?**
Usually within a second (`usage_recorded` → `settled`). Your `available_usd` is correct immediately, because the unused hold is released at settlement and the reservation already covered the worst case.

**What does `unresolved` mean for my money?**
The provider's result could not be confirmed, so Tollgate keeps the reservation held instead of guessing. The operator checks the provider's records and either charges the confirmed usage or releases the hold. You are never charged more than the reservation.
