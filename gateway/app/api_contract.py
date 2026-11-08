"""Client API contract: strict response models, error catalog, and the published OpenAPI 3.1 document."""

import copy
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field
from pydantic.json_schema import models_json_schema

from app.models.error import ErrorResponse
from app.models.request import (
    MAX_MESSAGE_CHARACTERS,
    MAX_MESSAGES,
    MAX_OUTPUT_TOKENS,
    MAX_TOTAL_CHARACTERS,
    ChatCompletionRequest,
)

API_VERSION = "1.2.0"
REF = "#/components/schemas/{model}"
Money = Field(pattern=r"^-?[0-9]+\.[0-9]{6}$", examples=["0.998512"])


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class HealthOut(Strict):
    status: Literal["ok"]
    service: str
    mode: Literal["simulator", "live"]
    version: str
    role: Literal["standalone", "control", "warden"]


class ReadyOut(Strict):
    ready: bool


class ModelOut(Strict):
    id: str
    object: Literal["model"]
    owned_by: Literal["simulator", "openai", "anthropic", "gemini"]
    pricing_source: Literal["test_prices", "free_tier_assumed", "configured_provider_rates"]
    price_version: str
    reviewed_on: str
    input_usd_per_million: str = Money
    output_usd_per_million: str = Money
    cached_usd_per_million: str = Money
    max_output_tokens: int
    max_input_tokens: int


class ModelList(Strict):
    object: Literal["list"]
    data: list[ModelOut]


class PromptTokenDetails(Strict):
    cached_tokens: int = Field(ge=0)


class CompletionTokenDetails(Strict):
    reasoning_tokens: int = Field(ge=0)


class Usage(Strict):
    prompt_tokens: int = Field(ge=0)
    completion_tokens: int = Field(ge=0)
    total_tokens: int = Field(ge=0)
    prompt_tokens_details: PromptTokenDetails | None = None
    completion_tokens_details: CompletionTokenDetails | None = None
    cache_write_tokens: int | None = Field(default=None, ge=0)


class AssistantMessage(Strict):
    role: Literal["assistant"]
    content: str


class Choice(Strict):
    index: Literal[0]
    message: AssistantMessage
    finish_reason: Literal["stop", "length", "content_filter"]


class ChatCompletion(Strict):
    id: str
    object: Literal["chat.completion"]
    created: int
    model: str
    choices: list[Choice] = Field(min_length=1, max_length=1)
    usage: Usage


class Delta(Strict):
    content: str | None = None


class ChunkChoice(Strict):
    index: Literal[0]
    delta: Delta
    finish_reason: Literal["stop", "length", "content_filter"] | None


class ChatCompletionChunk(Strict):
    id: str
    object: Literal["chat.completion.chunk"]
    created: int
    model: str
    choices: list[ChunkChoice] = Field(max_length=1)
    usage: Usage | None


class StreamErrorBody(Strict):
    code: str
    message: str
    request_id: str


class StreamError(Strict):
    error: StreamErrorBody


class Balance(Strict):
    frozen: bool
    balance_usd: str = Money
    reserved_usd: str = Money
    available_usd: str = Money
    spent_usd: str = Money
    spend_cap_usd: str | None = Money


RequestState = Literal[
    "reserved", "dispatched", "usage_recorded", "settled", "released", "unresolved"
]


class RequestStatus(Strict):
    id: str
    customer_id: str
    model: str
    feature: str
    state: RequestState
    price_version: str
    input_tokens: int | None
    output_tokens: int | None
    error_code: str | None
    provider: str | None
    provider_response_id: str | None
    warden_id: str | None
    control_degraded: bool | int
    cached_tokens: int | None
    cache_write_tokens: int | None
    resolved_by: str | None
    created_at: float
    reserved_usd: str = Money
    cost_usd: str | None = Money
    usage_source: Literal["simulated", "provider_reported"]


REQUEST_STATES = {
    "reserved": "Credits are held; the provider has not been called yet.",
    "dispatched": "The provider call is in progress.",
    "usage_recorded": "Usage is saved; the settlement worker will charge it shortly.",
    "settled": "Final. The charge is in the ledger and the unused hold was released.",
    "released": "Final. Nothing was charged and the hold was returned.",
    "unresolved": "The outcome is uncertain. Credits stay held until the operator resolves it. Do not retry blindly.",
}

PROVIDER_CODE_NOTE = (
    "Provider failures use code `provider_*`, `invalid_provider_*`, `incomplete_provider_stream`, "
    "`missing_usage`, `openai_*`, `anthropic_*` or `gemini_*`."
)

ERRORS = {
    "invalid_request": (422, "A field is missing, has the wrong type, or breaks a limit. `details` lists each field.", "Fix the listed fields. Do not retry unchanged."),
    "invalid_api_key": (401, "The Authorization header is missing, malformed, revoked, or expired.", "Send `Authorization: Bearer tg_...` with an active key. Ask the operator for a new key if it expired."),
    "insufficient_credits": (402, "Available credits cannot cover the worst-case reservation for this request.", "Top up, or lower `max_completion_tokens`. `context.required_usd` and `context.available_usd` show the gap."),
    "model_not_allowed": (403, "This key is not allowed to use the requested model.", "Use a model listed by `GET /v1/models` for this key."),
    "account_frozen": (403, "The operator suspended new requests for this customer.", "Contact the operator."),
    "spend_cap_exceeded": (403, "The request would exceed the customer's lifetime spend cap.", "Contact the operator to raise the cap."),
    "request_not_found": (404, "No request with this ID belongs to your customer account.", "Check the ID. You can only read your own requests."),
    "wrong_service": (404, "This address serves a different role (generation vs administration).", "Use the API base URL from your client pack."),
    "duplicate_request": (409, "The same Idempotency-Key was already used with the same body.", "Do not generate again. Read `GET /v1/requests/{context.original_request_id}`."),
    "idempotency_conflict": (409, "The same Idempotency-Key was already used with a different body or feature label.", "Use a new Idempotency-Key for a different request."),
    "reservation_unavailable": (409, "The hold was released before dispatch. No provider call was made.", "Retry with a new Idempotency-Key."),
    "request_too_large": (413, "The request body exceeds the byte limit (default 262,144 bytes).", "Send a smaller body."),
    "invalid_content_length": (400, "The Content-Length header is not one non-negative integer.", "Let your HTTP library set Content-Length."),
    "content_length_mismatch": (400, "Content-Length does not match the body that arrived.", "Let your HTTP library set Content-Length."),
    "request_body_timeout": (408, "The body did not arrive within the read deadline.", "Retry on a stable connection."),
    "unsupported_model": (422, "The model ID is not enabled on this gateway.", "Use a model listed by `GET /v1/models`."),
    "model_limit_exceeded": (422, "Input or output size is above the model's limit (OpenAI models also need `max_completion_tokens` of at least 16).", "Shorten the prompt or change `max_completion_tokens`."),
    "invalid_message_order": (422, "The provider cannot accept this sequence of roles.", "Start with system (optional) then user, and alternate user/assistant."),
    "rate_limit_exceeded": (429, "Too many requests from this customer in the current minute (default 60).", "Wait for the `Retry-After` seconds, then retry."),
    "concurrency_limit": (429, "Too many chat requests are running at once for this customer (default 5).", "Wait for one to finish, then retry."),
    "internal_error": (500, "Unexpected server error. Details are hidden.", "Read `GET /v1/requests/{id}` before retrying; quote `X-Request-ID` to support."),
    "provider_rejected": (502, "The AI provider rejected or failed the call. " + PROVIDER_CODE_NOTE, "Read `GET /v1/requests/{id}`. If `released`, retry with a new Idempotency-Key. If `unresolved`, contact support."),
    "price_review_required": (503, "The operator must review this model's price before it can be used.", "Retry later or contact the operator."),
    "storage_unavailable": (503, "The database is temporarily unavailable.", "Retry with the same Idempotency-Key."),
    "authentication_unavailable": (503, "Key verification storage is temporarily unavailable.", "Retry after a short wait."),
    "provider_unavailable": (503, "The AI provider could not be reached.", "Read `GET /v1/requests/{id}` before retrying."),
    "worker_unavailable": (503, "Settlement is paused, so new generation is paused.", "Retry later; another API node may be healthy."),
    "warden_draining": (503, "This API node is being taken out of service.", "Retry; the load balancer will pick another node."),
    "warden_unavailable": (503, "This API node is not registered as healthy.", "Retry; the load balancer will pick another node."),
    "control_unavailable": (503, "The control service is down and this node is configured to fail closed.", "Retry later."),
    "provider_timeout": (504, "The provider did not finish before the deadline (default 180 s).", "Read `GET /v1/requests/{id}`; credits stay held until the outcome is known."),
}

COMMON = ["invalid_api_key", "wrong_service", "rate_limit_exceeded", "internal_error", "storage_unavailable", "authentication_unavailable"]
BODY = ["request_too_large", "invalid_content_length", "content_length_mismatch", "request_body_timeout", "invalid_request"]
CHAT = COMMON + BODY + [
    "insufficient_credits", "model_not_allowed", "account_frozen", "spend_cap_exceeded",
    "duplicate_request", "idempotency_conflict", "reservation_unavailable",
    "unsupported_model", "model_limit_exceeded", "invalid_message_order", "concurrency_limit",
    "provider_rejected", "price_review_required", "provider_unavailable", "worker_unavailable",
    "warden_draining", "warden_unavailable", "control_unavailable", "provider_timeout",
]

EXAMPLE_CONTEXT = {
    "insufficient_credits": {"required_usd": "0.002144", "available_usd": "0.000500"},
    "duplicate_request": {"original_request_id": "req_4f0c2b7e9a1d4c3b8e6f5a2d1c0b9a87", "state": "settled"},
    "idempotency_conflict": {"original_request_id": "req_4f0c2b7e9a1d4c3b8e6f5a2d1c0b9a87", "state": "settled"},
    "rate_limit_exceeded": {"retry_after_seconds": 42},
}

RATE_HEADERS = {"Retry-After": {"description": "Seconds to wait before retrying (rate_limit_exceeded only).", "schema": {"type": "integer", "minimum": 1, "maximum": 60}}}
REQUEST_ID_HEADER = {"X-Request-ID": {"description": "Unique ID for this HTTP request. Quote it to support.", "schema": {"type": "string", "pattern": "^req_[0-9a-f]{32}$"}}}


def error_example(code: str) -> dict:
    status, when, _ = ERRORS[code]
    body = {"code": code, "message": when.split(". ")[0].rstrip(".") + ".", "request_id": "req_4f0c2b7e9a1d4c3b8e6f5a2d1c0b9a87"}
    if code == "invalid_request":
        body["details"] = [{"field": "body.messages", "message": "Field required", "type": "missing"}]
    if code in EXAMPLE_CONTEXT:
        body["context"] = EXAMPLE_CONTEXT[code]
    return {"error": body}


def error_responses(codes: list[str]) -> dict:
    by_status: dict[int, list[str]] = {}
    for code in codes:
        by_status.setdefault(ERRORS[code][0], []).append(code)
    result = {}
    for status in sorted(by_status):
        names = by_status[status]
        headers = dict(REQUEST_ID_HEADER)
        if "rate_limit_exceeded" in names:
            headers.update(RATE_HEADERS)
        result[str(status)] = {
            "description": "Error codes: " + ", ".join(f"`{name}`" for name in names) + ". See the error catalog in `info.description`.",
            "headers": headers,
            "content": {
                "application/json": {
                    "schema": {"$ref": REF.format(model="ErrorResponse")},
                    "examples": {name: {"summary": ERRORS[name][1], "value": error_example(name)} for name in names},
                }
            },
        }
    return result


def ok(schema: str, description: str, example: dict, headers: dict | None = None) -> dict:
    return {
        "200": {
            "description": description,
            "headers": {**REQUEST_ID_HEADER, **(headers or {})},
            "content": {"application/json": {"schema": {"$ref": REF.format(model=schema)}, "example": example}},
        }
    }


EXAMPLE_REQUEST_ID = "req_4f0c2b7e9a1d4c3b8e6f5a2d1c0b9a87"

EXAMPLES = {
    "health": {"status": "ok", "service": "Tollgate", "mode": "live", "version": API_VERSION, "role": "standalone"},
    "models": {
        "object": "list",
        "data": [
            {
                "id": "sim-basic", "object": "model", "owned_by": "simulator", "pricing_source": "test_prices",
                "price_version": "sim-test-v1", "reviewed_on": "", "input_usd_per_million": "2.000000",
                "output_usd_per_million": "8.000000", "cached_usd_per_million": "0.000000",
                "max_output_tokens": 4096, "max_input_tokens": 40000,
            }
        ],
    },
    "completion": {
        "id": "chatcmpl-" + EXAMPLE_REQUEST_ID, "object": "chat.completion", "created": 1762600000, "model": "sim-basic",
        "choices": [{"index": 0, "message": {"role": "assistant", "content": "An API gateway is a single entry point..."}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 14, "completion_tokens": 38, "total_tokens": 52},
    },
    "balance": {"frozen": False, "balance_usd": "1.000000", "reserved_usd": "0.000000", "available_usd": "0.999668", "spent_usd": "0.000332", "spend_cap_usd": None},
    "request": {
        "id": EXAMPLE_REQUEST_ID, "customer_id": "cus_3a5f0e1d2c4b6a7980f1e2d3", "model": "sim-basic", "feature": "default",
        "state": "settled", "price_version": "sim-test-v1", "input_tokens": 14, "output_tokens": 38, "error_code": None,
        "provider": "simulator", "provider_response_id": None, "warden_id": None, "control_degraded": False,
        "cached_tokens": 0, "cache_write_tokens": 0, "resolved_by": None, "created_at": 1762600000.25,
        "reserved_usd": "0.002144", "cost_usd": "0.000332", "usage_source": "simulated",
    },
}

STREAM_EXAMPLE = (
    'data: {"id":"chatcmpl-req_4f0c...","object":"chat.completion.chunk","created":1762600000,"model":"sim-basic",'
    '"choices":[{"index":0,"delta":{"content":"An API gate"},"finish_reason":null}],"usage":null}\n\n'
    'data: {"id":"chatcmpl-req_4f0c...","object":"chat.completion.chunk","created":1762600000,"model":"sim-basic",'
    '"choices":[{"index":0,"delta":{},"finish_reason":"stop"}],"usage":null}\n\n'
    'data: {"id":"chatcmpl-req_4f0c...","object":"chat.completion.chunk","created":1762600000,"model":"sim-basic",'
    '"choices":[],"usage":{"prompt_tokens":14,"completion_tokens":38,"total_tokens":52}}\n\n'
    "data: [DONE]\n\n"
)

CHAT_HEADERS = {
    "X-Tollgate-Request-ID": {"description": "Same value as X-Request-ID. Use it with `GET /v1/requests/{request_id}`.", "schema": {"type": "string"}},
    "Idempotency-Key": {"description": "The idempotency key in effect (yours, or the request ID if you sent none).", "schema": {"type": "string"}},
    "X-Tollgate-Reservation-USD": {"description": "Worst-case amount held before the provider call.", "schema": {"type": "string"}},
    "X-Tollgate-Usage-Source": {"description": "`simulated` in simulator mode, `provider_reported` in live mode.", "schema": {"type": "string", "enum": ["simulated", "provider_reported"]}},
    "X-Tollgate-Warden-ID": {"description": "API node that served the request (multi-node deployments only).", "schema": {"type": "string"}},
    "X-Tollgate-Control-Status": {"description": "`online`, or `degraded` if the control service was unreachable (multi-node only).", "schema": {"type": "string", "enum": ["online", "degraded"]}},
}
JSON_ONLY_HEADERS = {
    "X-Tollgate-Cost-USD": {"description": "Final cost of this request (JSON responses only).", "schema": {"type": "string"}},
    "X-Tollgate-Settlement": {"description": "Request state when the response was sent, normally `usage_recorded`.", "schema": {"type": "string"}},
}


def error_catalog_markdown() -> str:
    rows = ["| HTTP | code | meaning | what to do |", "|---|---|---|---|"]
    for code, (status, when, action) in sorted(ERRORS.items(), key=lambda item: (item[1][0], item[0])):
        rows.append(f"| {status} | `{code}` | {when} | {action} |")
    return "\n".join(rows)


def states_markdown() -> str:
    rows = ["| state | meaning |", "|---|---|"]
    rows += [f"| `{state}` | {meaning} |" for state, meaning in REQUEST_STATES.items()]
    return "\n".join(rows)


DESCRIPTION = f"""Tollgate is a prepaid AI gateway. Your application calls Tollgate with a customer API key (`tg_...`) instead of holding AI provider keys. Every chat request reserves its worst-case cost from your credits, calls the AI provider once, then charges the exact reported usage once and returns the unused hold.

The chat endpoint is compatible with the OpenAI Chat Completions format for **text only**. Official OpenAI SDKs work by setting `base_url` to `<your base URL>/v1`.

## Conventions
- **Authentication:** `Authorization: Bearer tg_...` on every `/v1/` call. `/health` and `/ready` need no key.
- **Money:** decimal strings in USD with exactly six decimal places (micro-USD), never floats.
- **Errors:** every error has the same JSON envelope `{{"error": {{"code", "message", "request_id", "details"?, "context"?}}}}`. Branch on `code`, never on `message`.
- **Request IDs:** every response carries `X-Request-ID`. Quote it when contacting support.
- **Idempotency:** send `Idempotency-Key` on chat requests so a network retry cannot generate (and charge) twice.
- **Unknown fields are rejected** with `invalid_request`. Strict types: `"256"` is not an integer.
- **Out of scope:** tools/function calling, images, audio, embeddings, `n > 1`, temperature and other sampling parameters.

## Limits (operator defaults)
| limit | default |
|---|---|
| requests per minute per customer | 60 |
| concurrent chat requests per customer | 5 |
| request body | 262,144 bytes |
| messages per request | {MAX_MESSAGES} |
| characters per message | {MAX_MESSAGE_CHARACTERS:,} |
| characters per request (all messages) | {MAX_TOTAL_CHARACTERS:,} |
| `max_completion_tokens` | 1 to {MAX_OUTPUT_TOKENS:,} (default 256), and no more than the model's `max_output_tokens` |
| provider deadline | 180 seconds |

## Request states
{states_markdown()}

## Error catalog
{error_catalog_markdown()}

## Versioning
This document describes API version {API_VERSION}. Within `/v1`, changes are additive only: new optional request fields, new response fields, new error codes and new headers. Clients must ignore unknown response fields and treat unknown error codes by HTTP status. Removing or renaming anything requires a new path prefix (`/v2`) announced in advance.
"""


def schemas() -> dict:
    output = [
        (model, "serialization")
        for model in (HealthOut, ReadyOut, ModelList, ChatCompletion, ChatCompletionChunk, StreamError, Balance, RequestStatus, ErrorResponse)
    ]
    _, result = models_json_schema(output + [(ChatCompletionRequest, "validation")], ref_template=REF)
    defs = result["$defs"]
    request = defs["ChatCompletionRequest"]
    request["properties"]["max_completion_tokens"]["description"] = (
        "Maximum output tokens. The legacy name `max_tokens` is accepted as an alias. The reservation is based on this value, so smaller values need less available credit."
    )
    request["properties"]["stream"]["description"] = "If true, the response is a `text/event-stream` of `ChatCompletionChunk` events ending with `data: [DONE]`."
    request["properties"]["stream_options"]["description"] = "Accepted for OpenAI compatibility. The final usage chunk is always sent, with or without it."
    request["properties"]["model"]["description"] = "A model ID from `GET /v1/models`."
    return defs


def operation(op_id: str, summary: str, description: str, tag: str, responses: dict, secured: bool = True, **extra) -> dict:
    result = {"operationId": op_id, "summary": summary, "description": description, "tags": [tag], "responses": responses, **extra}
    result["security"] = [{"CustomerKey": []}] if secured else []
    return result


def build_client_openapi() -> dict:
    header_param = lambda name, description, pattern, max_length: {
        "name": name, "in": "header", "required": False, "description": description,
        "schema": {"type": "string", "minLength": 1, "maxLength": max_length, "pattern": pattern},
    }
    chat_ok = {
        "description": "Answer generated. JSON when `stream` is false; Server-Sent Events when `stream` is true.",
        "headers": {**REQUEST_ID_HEADER, **CHAT_HEADERS, **JSON_ONLY_HEADERS},
        "content": {
            "application/json": {"schema": {"$ref": REF.format(model="ChatCompletion")}, "example": EXAMPLES["completion"]},
            "text/event-stream": {
                "schema": {"type": "string", "description": "Each event is `data: <ChatCompletionChunk JSON>` followed by a blank line. The stream ends with `data: [DONE]`. If the provider fails mid-stream, a single `data: <StreamError JSON>` event is sent instead of `[DONE]` and the connection closes."},
                "example": STREAM_EXAMPLE,
            },
        },
    }
    chat_errors = error_responses(CHAT)
    for status in chat_errors.values():
        status["headers"] = {**status["headers"], **{k: v for k, v in CHAT_HEADERS.items() if k != "X-Tollgate-Warden-ID"}}
        status["description"] += " Tollgate headers are present only when the failure happened after the reservation."
    paths = {
        "/health": {"get": operation("getHealth", "Liveness check", "Confirms the process is running. It does not check the database or provider.", "Status", ok("HealthOut", "Process is alive.", EXAMPLES["health"]), secured=False)},
        "/ready": {
            "get": operation(
                "getReady", "Readiness check",
                "Returns 200 when this node can accept generation: the database answers, the settlement worker is alive, and every model price is current. Load balancers should route only to ready nodes.",
                "Status",
                {
                    **ok("ReadyOut", "Ready.", {"ready": True}),
                    "503": {"description": "Not ready. Do not route traffic here.", "headers": REQUEST_ID_HEADER, "content": {"application/json": {"schema": {"$ref": REF.format(model="ReadyOut")}, "example": {"ready": False}}}},
                },
                secured=False,
            )
        },
        "/v1/models": {"get": operation("listModels", "List models this key may use", "Returns only the models in this key's allow-list, with current prices in USD per million tokens.", "Models", {**ok("ModelList", "Models available to this key.", EXAMPLES["models"]), **error_responses(COMMON)})},
        "/v1/chat/completions": {
            "post": operation(
                "createChatCompletion", "Create a chat completion",
                "Reserves the worst-case cost, calls the provider once, and records the exact usage. "
                "The charge is settled into the ledger by the settlement worker a moment later; `GET /v1/requests/{request_id}` shows the final state.\n\n"
                "**Retry rule:** retry only with the same `Idempotency-Key`. If you get `duplicate_request`, the first attempt already ran: read its status instead of generating again.",
                "Chat",
                {"200": chat_ok, **chat_errors},
                parameters=[
                    header_param("Idempotency-Key", "Your unique ID for this logical request (for example a UUID). Reusing it with the same body returns `duplicate_request`; with a different body, `idempotency_conflict`.", r"^[A-Za-z0-9_.:-]+$", 128),
                    header_param("X-Tollgate-Feature", "Optional label for cost reporting (for example `support-bot`). Defaults to `default`. It is part of the idempotency fingerprint.", r"^[A-Za-z0-9_.:-]+$", 64),
                ],
                requestBody={
                    "required": True,
                    "content": {
                        "application/json": {
                            "schema": {"$ref": REF.format(model="ChatCompletionRequest")},
                            "examples": {
                                "json": {"summary": "Single JSON answer", "value": {"model": "sim-basic", "messages": [{"role": "system", "content": "Answer in one paragraph."}, {"role": "user", "content": "Explain an API gateway."}], "max_completion_tokens": 256}},
                                "stream": {"summary": "Streaming answer", "value": {"model": "sim-basic", "messages": [{"role": "user", "content": "Explain an API gateway."}], "max_completion_tokens": 256, "stream": True}},
                            },
                        }
                    },
                },
            )
        },
        "/v1/balance": {"get": operation("getBalance", "Read your credit balance", "`available_usd` = `balance_usd` − `reserved_usd`. `spent_usd` is lifetime settled spending.", "Billing", {**ok("Balance", "Current wallet.", EXAMPLES["balance"]), **error_responses(COMMON)})},
        "/v1/requests/{request_id}": {
            "get": operation(
                "getRequest", "Read one request's status and cost",
                "Use the `X-Tollgate-Request-ID` from a chat response. You can only read your own requests.",
                "Billing",
                {**ok("RequestStatus", "Request metadata. Prompts and answers are never stored.", EXAMPLES["request"]), **error_responses(COMMON + ["request_not_found"])},
                parameters=[{"name": "request_id", "in": "path", "required": True, "schema": {"type": "string", "pattern": "^req_[0-9a-f]{32}$"}}],
            )
        },
    }
    return {
        "openapi": "3.1.0",
        "info": {"title": "Tollgate Client API", "version": API_VERSION, "description": DESCRIPTION, "contact": {"name": "Tollgate operator support (see your client pack)"}},
        "servers": [{"url": "https://api.example.com", "description": "Replace with the base URL from your client pack."}, {"url": "http://127.0.0.1:8000", "description": "Local development"}],
        "tags": [
            {"name": "Status", "description": "Health checks. No key needed."},
            {"name": "Models", "description": "What this key can use, and prices."},
            {"name": "Chat", "description": "Text generation (OpenAI-compatible)."},
            {"name": "Billing", "description": "Balance and per-request cost."},
        ],
        "paths": paths,
        "components": {
            "schemas": copy.deepcopy(schemas()),
            "securitySchemes": {"CustomerKey": {"type": "http", "scheme": "bearer", "bearerFormat": "tg_ virtual key", "description": "Customer API key issued by the operator. It is shown once at issue time."}},
        },
        "security": [{"CustomerKey": []}],
    }


CLIENT_OPERATIONS = [
    (method.upper(), path)
    for path, methods in build_client_openapi()["paths"].items()
    for method in methods
]


GUIDE_BLOCKS = {"states": states_markdown, "errors": error_catalog_markdown}


def render_guide(text: str) -> str:
    for name, render in GUIDE_BLOCKS.items():
        start, end = f"<!-- generated:{name} -->", f"<!-- /generated:{name} -->"
        head, _, rest = text.partition(start)
        _, _, tail = rest.partition(end)
        text = f"{head}{start}\n{render()}\n{end}{tail}"
    return text
