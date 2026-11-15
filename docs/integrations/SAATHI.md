# Connecting a Dockerised backend (Saathi) to Tollgate

Architecture: **phone → Saathi backend (Docker) → Tollgate → Gemini.** The `tg_` key lives only in the backend environment. Tollgate sends no CORS headers, so a browser or app cannot call it directly. Authority: [`docs/api/openapi.json`](../api/openapi.json). Full detail: [`docs/API_GUIDE.md`](../API_GUIDE.md).

## 1. Base URL and key

| item | value |
|---|---|
| Base URL from a container | `http://host.docker.internal:8100` |
| Chat endpoint | `POST /v1/chat/completions` |
| Models for your key | `GET /v1/models` |
| Balance | `GET /v1/balance` |
| Request status | `GET /v1/requests/{request_id}` |
| Auth header | `Authorization: Bearer tg_...` (missing, wrong, revoked or expired key: 401 `invalid_api_key`) |

Keep both values in the backend `.env`, for example `TOLLGATE_BASE_URL` and `TOLLGATE_API_KEY`. Never pass them to Flutter.

## 2. Request

Headers:

| header | rule |
|---|---|
| `Authorization` | required |
| `Content-Type: application/json` | required |
| `Idempotency-Key` | optional but strongly recommended. 1–128 characters from `A-Za-z0-9_.:-`. One new UUID per user message. |
| `X-Tollgate-Feature` | optional cost label, 1–64 characters, same alphabet, default `default` (for example `saathi-chat`) |

Body, **allowed fields only**. Any other field (`temperature`, `top_p`, `n`, `tools`, `user`) returns 422 `invalid_request`.

| field | rule |
|---|---|
| `model` | required, an id from `GET /v1/models` (for example `gemini-3.5-flash-lite`) |
| `messages` | required, 1–50 items, each `{"role": "system"\|"user"\|"assistant", "content": "<text>"}`; content 1–8,000 characters, at most 32,000 in total |
| `max_completion_tokens` | optional integer 1–4096, default 256 (`max_tokens` is accepted as an alias) |
| `stream` | optional boolean, default `false` |
| `stream_options` | optional, only `{"include_usage": true}`; usage is sent anyway |

Gemini conversations: system messages first, then alternating user/assistant, starting and ending with a user message. Otherwise 422 `invalid_message_order`.

```json
{"model": "gemini-3.5-flash-lite",
 "messages": [{"role": "system", "content": "Answer briefly."}, {"role": "user", "content": "Hello"}],
 "max_completion_tokens": 256}
```

Text only. Not supported: embeddings, tools/function calling, images, audio, files, sampling parameters.

## 3. Response

`choices[0].message.content` holds the answer; `usage` holds token counts. Useful headers: `X-Tollgate-Request-ID` (store it with the Saathi message), `X-Tollgate-Cost-USD` (JSON only), `X-Tollgate-Reservation-USD`.

## 4. Idempotency

| you send | result |
|---|---|
| new key | normal processing |
| same key, same body and feature | 409 `duplicate_request`; nothing is generated; `context.original_request_id` points to the first attempt |
| same key, different body or feature | 409 `idempotency_conflict` |

Tollgate never replays the stored answer and never retries generation itself. The OpenAI SDK retries automatically, so either send an `Idempotency-Key` or set `max_retries=0`.

## 5. Errors to handle

Branch on `error.code`, not the message. Always log `error.request_id`.

| HTTP | code | backend action |
|---|---|---|
| 401 | `invalid_api_key` | configuration problem; alert, do not retry |
| 402 | `insufficient_credits` | tell the operator to top up, or lower `max_completion_tokens` |
| 403 | `account_frozen`, `model_not_allowed`, `spend_cap_exceeded` | do not retry; operator action |
| 409 | `duplicate_request` | `GET /v1/requests/{context.original_request_id}`; do not regenerate |
| 409 | `idempotency_conflict` | use a new key |
| 422 | `invalid_request`, `invalid_message_order`, `model_limit_exceeded`, `unsupported_model` | fix the request; do not retry unchanged |
| 429 | `rate_limit_exceeded` | wait `Retry-After` seconds, resend with the same key |
| 429 | `concurrency_limit` | wait 1–2 seconds, resend with the same key |
| 503 | `storage_unavailable`, `authentication_unavailable`, `worker_unavailable`, `warden_*` | wait a few seconds, resend with the same key |
| 503 | `price_review_required` | operator must re-review prices; show a friendly message |
| 502, 503, 504 | `provider_*` | `GET /v1/requests/{X-Tollgate-Request-ID}`: `released` means retry with a new key; `unresolved` means stop and tell the operator |
| other | unknown code | handle by HTTP status |

Error body: `{"error": {"code", "message", "request_id", "context"}}`. Return a generic error to the phone; never forward Tollgate's body or key.

## 6. Streaming

Set `"stream": true`. The response is `text/event-stream`, one `data: <json>` event per chunk.

1. Append `choices[0].delta.content` when present.
2. The last chunk before `[DONE]` has empty `choices` and the final `usage`.
3. `[DONE]` means success.
4. After streaming starts, HTTP status is already 200. An event with an `error` key and no `[DONE]` means failure; treat the text as incomplete and read `GET /v1/requests/{request_id}`.

`X-Tollgate-Cost-USD` is not sent on streams; read the cost from `GET /v1/requests/{id}`.

## 7. Verify a request in the dashboard

1. Open the dashboard and sign in with the owner key.
2. Go to **Requests** and find the row by `X-Tollgate-Request-ID`, customer **Saathi app**, or feature label.
3. The state should be `settled`, with input and output tokens filled in. A momentary `usage_recorded` means the worker has not run yet (`python manage.py worker` must be running).
4. Prompts and answers are never stored, so only metadata appears.

Reachability check from inside the Saathi container (replace `saathi-api` with your service name):

```powershell
docker compose exec saathi-api curl -s http://host.docker.internal:8100/health
```
