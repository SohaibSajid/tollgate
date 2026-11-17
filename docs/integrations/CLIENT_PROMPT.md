# Prompt for a client developer's AI coding assistant

Give this to any developer integrating Tollgate. They first sign up at `/portal`, add their own provider key and create a `tg_` key there ([CLIENT_PORTAL.md](../CLIENT_PORTAL.md)). Then they paste the prompt into Claude Code (or a similar assistant) inside **their own** backend project, after filling in the three values. Details behind each rule: [`INTEGRATION_MODES.md`](INTEGRATION_MODES.md), [`../API_GUIDE.md`](../API_GUIDE.md).

````text
Route all AI chat calls in this backend through our Tollgate gateway.

Values (store as server-side environment variables, never in frontend or mobile code, never log them):
TOLLGATE_BASE_URL=<base URL from the client pack, without /v1>
TOLLGATE_API_KEY=<tg_ key from the client pack>
TOLLGATE_MODEL=<an allowed model id from the client pack>

1. Find every place this backend calls an AI provider for text chat. Replace it with one shared function that sends
   POST {TOLLGATE_BASE_URL}/v1/chat/completions (OpenAI Chat Completions format).
   If the project already uses the OpenAI SDK, use it with base_url={TOLLGATE_BASE_URL}/v1 and max_retries=0.
2. Headers on every call: Authorization: Bearer <key>, Content-Type: application/json,
   Idempotency-Key: a new UUID per user message (reuse only when retrying that same message),
   X-Tollgate-Feature: a short label for the feature, e.g. "chat",
   X-Tollgate-End-User: the ID of the logged-in user from our own database (opaque ID, never email or name; allowed characters A-Z a-z 0-9 _ . : -, max 128).
3. Body with ONLY: model, messages ([{role: system|user|assistant, content: text}], 1-50 items, each <= 8000 chars,
   <= 32000 total), max_completion_tokens (1-4096), optional stream. No temperature, top_p, n, tools or other fields:
   Tollgate rejects them with 422. Embeddings, images, audio and tool calling are not supported; leave those calls unchanged.
4. Read the answer from choices[0].message.content and store the X-Tollgate-Request-ID response header with the message.
5. Errors: branch on error.code, log error.request_id, return a generic error to our users.
   401/403/422: do not retry. 402: out of credits, show a friendly message.
   429: wait Retry-After, resend with the same Idempotency-Key.
   409 duplicate_request: read GET /v1/requests/{context.original_request_id}; do not regenerate.
   503 storage_unavailable/authentication_unavailable/control_unavailable/warden_*/worker_unavailable/price_review_required: retry later with the same key.
   502/504 and 503 provider_*: read GET /v1/requests/{X-Tollgate-Request-ID} before any retry.
6. Optional fail-open (ask me first): if the connection to Tollgate fails (connect error or connect timeout), or it returns one of the 503 codes listed in step 5,
   call the provider directly with OUR OWN provider key and log the call for reconciliation. Never bypass 401/402/403/422/429.
7. Streaming (only if we stream): parse "data: <json>" events, append choices[0].delta.content, the last chunk before [DONE]
   has the usage, an event with an "error" key and no [DONE] means failure.
8. Add a startup check that GET {TOLLGATE_BASE_URL}/v1/models with the key returns 200. Add tests with a mocked Tollgate.
Keep the change targeted and tell me how to test it end to end.
````

After deploying, the developer sends one message through their app and asks the operator to confirm it appears in Tollgate → **Requests** as `settled`, with their user ID in the **End user** column.
