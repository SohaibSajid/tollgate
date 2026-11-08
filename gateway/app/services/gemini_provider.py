"""Gemini text streaming for an operator-confirmed free-tier project."""

import asyncio
import json
import logging
import re
import time

import httpx

from app.services.provider_client import ProviderFailure

logger = logging.getLogger("tollgate.providers")
BASE_URL = "https://generativelanguage.googleapis.com/v1beta/models/"
ERRORS = {
    "INVALID_ARGUMENT": (
        "invalid_request",
        "Gemini rejected a request parameter or API key.",
    ),
    "UNAUTHENTICATED": (
        "authentication_failed",
        "Check the Gemini API key saved on the gateway.",
    ),
    "PERMISSION_DENIED": (
        "access_denied",
        "Check the Gemini key restrictions, project access, and regional availability.",
    ),
    "NOT_FOUND": (
        "model_unavailable",
        "This Gemini model is unavailable to your project.",
    ),
    "RESOURCE_EXHAUSTED": (
        "quota_exhausted",
        "Gemini's free quota or rate limit was reached. Check the limits in AI Studio and wait for a reset. Tollgate will not switch providers.",
    ),
    "FAILED_PRECONDITION": (
        "project_not_ready",
        "Check that AI Studio lists your project as Free tier and that this model is available in your region.",
    ),
    "INTERNAL": ("server_error", "Gemini encountered an internal error."),
    "UNAVAILABLE": ("unavailable", "Gemini is temporarily unavailable."),
    "DEADLINE_EXCEEDED": ("timeout", "Gemini exceeded its response deadline."),
}
HTTP_CODES = {
    400: "INVALID_ARGUMENT",
    401: "UNAUTHENTICATED",
    403: "PERMISSION_DENIED",
    404: "NOT_FOUND",
    429: "RESOURCE_EXHAUSTED",
    500: "INTERNAL",
    503: "UNAVAILABLE",
    504: "DEADLINE_EXCEEDED",
}


def token_count(value, name):
    """Reject missing, negative, or non-integer usage before settlement."""
    if type(value) is not int or not 0 <= value <= 2_000_000:
        raise ProviderFailure(
            "gemini_invalid_usage", "Gemini reported invalid " + name + "."
        )
    return value


def gemini_usage(raw):
    """Use final cumulative counters, adding thinking to output exactly once."""
    incoming = token_count(raw.get("promptTokenCount", 0), "input tokens")
    visible = token_count(raw.get("candidatesTokenCount", 0), "candidate tokens")
    thoughts = token_count(raw.get("thoughtsTokenCount", 0), "thinking tokens")
    cached = token_count(raw.get("cachedContentTokenCount", 0), "cached tokens")
    total = token_count(raw.get("totalTokenCount"), "total tokens")
    tools = token_count(raw.get("toolUsePromptTokenCount", 0), "tool tokens")
    if tools or cached > incoming or total != incoming + visible + thoughts:
        raise ProviderFailure(
            "gemini_invalid_usage",
            "Gemini usage totals did not match the supported text request.",
        )
    return {
        "prompt_tokens": incoming,
        "completion_tokens": visible + thoughts,
        "total_tokens": total,
        "prompt_tokens_details": {"cached_tokens": cached},
        "completion_tokens_details": {"reasoning_tokens": thoughts},
        "cache_write_tokens": 0,
    }


class GeminiProviderClient:
    def __init__(self, client, settings, ledger, parse_events):
        self.client, self.settings, self.ledger = client, settings, ledger
        self.parse_events = parse_events

    def headers(self):
        """Keep the provider secret in a header, never in URLs or client responses."""
        return {"x-goog-api-key": self.settings.gemini_api_key.get_secret_value()}

    def body(self, payload):
        """Map text chat roles and leading system instructions to Gemini contents."""
        contents, system = [], []
        for message in payload["messages"]:
            if message["role"] == "system":
                if contents:
                    raise ProviderFailure(
                        "gemini_message_order",
                        "Put system messages before the conversation.",
                        422,
                        True,
                    )
                system.append({"text": message["content"]})
                continue
            role = "model" if message["role"] == "assistant" else "user"
            part = {"text": message["content"]}
            if contents and contents[-1]["role"] == role:
                contents[-1]["parts"].append(part)
            else:
                contents.append({"role": role, "parts": [part]})
        if (
            not contents
            or contents[0]["role"] != "user"
            or contents[-1]["role"] != "user"
        ):
            raise ProviderFailure(
                "gemini_message_order",
                "Gemini conversations must start and end with a user message.",
                422,
                True,
            )
        body = {
            "contents": contents,
            "generationConfig": {
                "candidateCount": 1,
                "maxOutputTokens": payload["max_completion_tokens"],
                "thinkingConfig": {
                    "thinkingLevel": "MINIMAL",
                    "includeThoughts": False,
                },
            },
        }
        if system:
            body["systemInstruction"] = {"parts": system}
        return body

    def failure(self, raw, response, phase, *, no_charge=False):
        """Report safe error categories without echoing raw Google error messages."""
        error = raw.get("error", {}) if isinstance(raw, dict) else {}
        status = error.get("status") if isinstance(error, dict) else None
        if not isinstance(status, str) or status not in ERRORS:
            status = HTTP_CODES.get(response.status_code, "UNKNOWN")
        code, message = ERRORS.get(
            status,
            (
                "provider_error",
                "Gemini returned an unrecognized error. Inspect the request status.",
            ),
        )
        request_id = response.extensions.get("tollgate_request_id", "preflight")
        provider_id = response.headers.get("x-request-id", "unavailable")
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,200}", provider_id):
            provider_id = "unavailable"
        logger.warning(
            "request_id=%s provider=gemini phase=%s provider_code=%s upstream_request_id=%s",
            request_id,
            phase,
            status,
            provider_id,
        )
        if phase == "count":
            message += " No generation was dispatched."
        elif not no_charge:
            message += (
                " Final usage is unconfirmed; inspect this request before retrying."
            )
        return ProviderFailure(
            "gemini_" + code,
            message,
            429 if status == "RESOURCE_EXHAUSTED" else 502,
            no_charge,
        )

    async def error_body(self, response):
        """Read a small error object; raw messages stay out of logs and storage."""
        data = bytearray()
        try:
            async for chunk in response.aiter_bytes():
                data.extend(chunk)
                if len(data) > 16384:
                    return {}
            value = json.loads(data)
            return value if isinstance(value, dict) else {}
        except (ValueError, httpx.HTTPError):
            return {}

    async def count_tokens(self, payload, spec):
        """Count the same complete input that generation will receive."""
        request = {
            "generateContentRequest": {
                "model": "models/" + spec.upstream_model,
                **self.body(payload),
            }
        }
        try:
            async with asyncio.timeout(20):
                async with self.client.stream(
                    "POST",
                    BASE_URL + spec.upstream_model + ":countTokens",
                    headers=self.headers(),
                    json=request,
                ) as response:
                    if response.status_code != 200:
                        raise self.failure(
                            await self.error_body(response),
                            response,
                            "count",
                            no_charge=True,
                        )
                    data = bytearray()
                    async for chunk in response.aiter_bytes():
                        data.extend(chunk)
                        if len(data) > 65536:
                            raise ProviderFailure(
                                "gemini_metadata_too_large",
                                "Gemini token-count metadata exceeded its limit.",
                                502,
                                True,
                            )
                    try:
                        value = json.loads(data)
                        if not isinstance(value, dict):
                            raise ValueError()
                    except ValueError as exc:
                        raise ProviderFailure(
                            "gemini_invalid_count",
                            "Gemini returned invalid token-count metadata.",
                            502,
                            True,
                        ) from exc
        except (httpx.HTTPError, TimeoutError) as exc:
            raise ProviderFailure(
                "gemini_preflight_unavailable",
                "Could not contact Gemini for token counting. No generation was dispatched.",
                503,
                True,
            ) from exc
        count = token_count(value.get("totalTokens"), "input count")
        if count > spec.max_input_tokens:
            raise ProviderFailure(
                "model_limit_exceeded",
                "Input exceeds Tollgate's Gemini token limit.",
                422,
                True,
            )
        # Keep a conservative text input ceiling; final reported usage remains authoritative.
        return spec.max_input_tokens

    async def open(self, payload, request_id, spec):
        """Dispatch once to the fixed Gemini endpoint; never fall back to a paid provider."""
        try:
            response = await self.client.send(
                self.client.build_request(
                    "POST",
                    BASE_URL + spec.upstream_model + ":streamGenerateContent",
                    params={"alt": "sse"},
                    headers=self.headers(),
                    json=self.body(payload),
                ),
                stream=True,
            )
        except httpx.HTTPError as exc:
            raise ProviderFailure(
                "gemini_unavailable",
                "Gemini connection failed. The request outcome needs review.",
                503,
            ) from exc
        response.extensions["tollgate_request_id"] = request_id
        if response.status_code != 200:
            try:
                raw = await self.error_body(response)
                rejected = response.status_code in (400, 401, 403, 404, 413, 422, 429)
                raise self.failure(raw, response, "generation", no_charge=rejected)
            finally:
                await response.aclose()
        return response

    async def events(self, response, model):
        """Forward text immediately and commit only final usage after a clean stream end."""
        if "text/event-stream" not in response.headers.get("content-type", ""):
            raise ProviderFailure(
                "gemini_invalid_stream", "Gemini did not return an event stream."
            )
        request_id = response.extensions["tollgate_request_id"]
        created, finish, final_usage, saved_id = int(time.time()), None, None, None

        def chunk(text=None, usage=None, reason=None):
            return {
                "id": "chatcmpl-" + request_id,
                "object": "chat.completion.chunk",
                "created": created,
                "model": model,
                "choices": (
                    []
                    if usage
                    else [
                        {
                            "index": 0,
                            "delta": {"content": text} if text is not None else {},
                            "finish_reason": reason,
                        }
                    ]
                ),
                "usage": usage,
            }

        async for obj in self.parse_events(response, self.settings.max_provider_bytes):
            if "error" in obj:
                raise self.failure(obj, response, "stream")
            provider_id = obj.get("responseId")
            if provider_id is not None and provider_id != saved_id:
                if not isinstance(provider_id, str) or not 1 <= len(provider_id) <= 200:
                    raise ProviderFailure(
                        "gemini_invalid_stream",
                        "Gemini returned an invalid response identifier.",
                    )
                await asyncio.to_thread(
                    self.ledger.save_provider_id, request_id, provider_id
                )
                saved_id = provider_id
            candidates = obj.get("candidates", [])
            if not isinstance(candidates, list) or len(candidates) > 1:
                raise ProviderFailure(
                    "gemini_invalid_stream",
                    "Gemini returned an unexpected candidate count.",
                )
            feedback = obj.get("promptFeedback", {})
            if not isinstance(feedback, dict):
                raise ProviderFailure(
                    "gemini_invalid_stream", "Gemini returned invalid prompt feedback."
                )
            if feedback.get("blockReason"):
                raise ProviderFailure(
                    "gemini_prompt_blocked",
                    "Gemini blocked this prompt. Try a different test message.",
                    422,
                )
            if candidates:
                candidate = candidates[0]
                if not isinstance(candidate, dict) or candidate.get("index", 0) != 0:
                    raise ProviderFailure(
                        "gemini_invalid_stream", "Gemini returned an invalid candidate."
                    )
                content = candidate.get("content", {})
                if not isinstance(content, dict) or not isinstance(
                    content.get("parts", []), list
                ):
                    raise ProviderFailure(
                        "gemini_invalid_stream", "Gemini returned invalid text content."
                    )
                for part in content.get("parts", []):
                    if not isinstance(part, dict):
                        raise ProviderFailure(
                            "gemini_invalid_stream",
                            "Gemini returned an invalid text part.",
                        )
                    if part.get("thought") is True:
                        continue
                    if "text" not in part or not isinstance(part["text"], str):
                        raise ProviderFailure(
                            "gemini_unsupported_content",
                            "Gemini returned content outside this text-only integration.",
                        )
                    if finish is not None:
                        raise ProviderFailure(
                            "gemini_invalid_stream",
                            "Gemini sent text after its final candidate.",
                        )
                    if part["text"]:
                        yield chunk(text=part["text"])
                reason = candidate.get("finishReason")
                if reason:
                    reasons = {
                        "STOP": "stop",
                        "MAX_TOKENS": "length",
                        "SAFETY": "content_filter",
                        "RECITATION": "content_filter",
                        "BLOCKLIST": "content_filter",
                        "PROHIBITED_CONTENT": "content_filter",
                        "SPII": "content_filter",
                        "LANGUAGE": "content_filter",
                    }
                    if reason not in reasons:
                        raise ProviderFailure(
                            "gemini_finish_unknown",
                            "Gemini stopped for an unsupported reason. Inspect the request.",
                        )
                    if finish is not None and finish != reasons[reason]:
                        raise ProviderFailure(
                            "gemini_invalid_stream",
                            "Gemini reported conflicting completion reasons.",
                        )
                    finish = reasons[reason]
            if "usageMetadata" in obj and finish is not None:
                if not isinstance(obj["usageMetadata"], dict):
                    raise ProviderFailure(
                        "gemini_invalid_usage",
                        "Gemini returned invalid usage metadata.",
                    )
                final_usage = gemini_usage(obj["usageMetadata"])
        if finish is None or final_usage is None:
            raise ProviderFailure(
                "gemini_missing_usage",
                "Gemini ended without a final result and usage. Inspect this request before retrying.",
            )
        yield chunk(reason=finish)
        yield chunk(usage=final_usage)
        yield None
