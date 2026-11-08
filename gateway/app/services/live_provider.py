"""Real provider adapters. Normalize text streams and preserve reported usage."""

import asyncio
import codecs
import json
import logging
import re
import time
import httpx
from app.services.provider_client import ProviderFailure
from app.services.gemini_provider import GeminiProviderClient

logger = logging.getLogger("tollgate.providers")

# Use our own messages: provider error text can contain submitted content.
ERROR_HINTS = {
    "insufficient_quota": "Check the provider API account's billing and usage limits.",
    "credit_balance_exhausted": "The provider API account has exhausted its prepaid credits.",
    "billing_hard_limit_reached": "Check the provider API account's billing limit.",
    "organization_spend_limit_exceeded": "The provider organization has reached its spending limit.",
    "project_spend_limit_exceeded": "The provider project has reached its spending limit.",
    "organization_usage_limit_exceeded": "The provider organization has reached its approved usage limit.",
    "rate_limit_exceeded": "The provider request or token rate limit was reached.",
    "rate_limit_error": "The provider request or token rate limit was reached.",
    "slow_down": "The provider requires a slower request rate.",
    "invalid_api_key": "Check the provider API key configured on the gateway.",
    "authentication_error": "Check the provider API key configured on the gateway.",
    "permission_error": "Check the provider key permissions and model access.",
    "permission_denied": "Check the provider key permissions and model access.",
    "model_not_found": "Check the model name and the provider account's access to it.",
    "invalid_request_error": "The provider rejected a request parameter.",
    "invalid_prompt": "The provider rejected the prompt.",
    "context_length_exceeded": "The request exceeded the provider's context limit.",
    "server_error": "The provider encountered an internal error.",
    "internal_error": "The provider encountered an internal error.",
    "api_error": "The provider encountered an internal error.",
    "overloaded_error": "The provider is temporarily overloaded.",
    "server_is_overloaded": "The provider is temporarily overloaded.",
    "service_unavailable_error": "The provider is temporarily unavailable.",
}


def stream_failure(response, event, provider):
    """Expose recognized error codes and log identifiers, never raw provider messages."""
    result = event.get("response")
    error = (
        result.get("error") if isinstance(result, dict) else event.get("error", event)
    )
    error = error if isinstance(error, dict) else {}
    code = next(
        (
            value
            for value in (error.get("code"), error.get("type"))
            if isinstance(value, str) and value in ERROR_HINTS
        ),
        "unknown_error",
    )

    def safe_id(value):
        if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,200}", value):
            return value
        return "unavailable"

    event_type = (
        "response.failed" if event.get("type") == "response.failed" else "error"
    )
    logger.warning(
        "request_id=%s provider=%s event=%s provider_code=%s upstream_request_id=%s",
        safe_id(response.extensions.get("tollgate_request_id")),
        provider,
        event_type,
        code,
        safe_id(
            response.headers.get("x-request-id") or response.headers.get("request-id")
        ),
    )
    name = "OpenAI" if provider == "openai" else "Claude"
    hint = ERROR_HINTS.get(
        code, "Review the provider request using its identifier in the server log."
    )
    return ProviderFailure(
        provider + "_" + code if code != "unknown_error" else "provider_stream_error",
        f"{name} stream failed ({code}). {hint} "
        "Final usage is unconfirmed; credits remain reserved. "
        "Inspect request status before retrying.",
    )


def integer(value, name):
    if type(value) is not int or not 0 <= value <= 1_000_000:
        raise ProviderFailure(
            "invalid_provider_usage", "Provider usage contains invalid " + name + "."
        )
    return value


def openai_usage(raw):
    incoming = integer(raw.get("input_tokens"), "input tokens")
    outgoing = integer(raw.get("output_tokens"), "output tokens")
    cached = integer(
        (raw.get("input_tokens_details") or {}).get("cached_tokens", 0), "cached tokens"
    )
    if cached > incoming:
        raise ProviderFailure(
            "invalid_provider_usage", "Cached tokens exceed input tokens."
        )
    # Reasoning is already included in output_tokens; never add it a second time.
    return {
        "prompt_tokens": incoming,
        "completion_tokens": outgoing,
        "total_tokens": incoming + outgoing,
        "prompt_tokens_details": {"cached_tokens": cached},
        "cache_write_tokens": 0,
    }


def anthropic_usage(raw):
    normal = integer(raw.get("input_tokens"), "input tokens")
    cached = integer(raw.get("cache_read_input_tokens", 0), "cached tokens")
    written = integer(raw.get("cache_creation_input_tokens", 0), "cache write tokens")
    if written:
        cache = raw.get("cache_creation") or {}
        if (
            cache.get("ephemeral_1h_input_tokens", 0)
            or cache.get("ephemeral_5m_input_tokens", 0) != written
        ):
            raise ProviderFailure(
                "unsupported_cache_pricing",
                "Unexpected cache write duration requires operator review.",
            )
    outgoing = integer(raw.get("output_tokens"), "output tokens")
    incoming = normal + cached + written
    return {
        "prompt_tokens": incoming,
        "completion_tokens": outgoing,
        "total_tokens": incoming + outgoing,
        "prompt_tokens_details": {"cached_tokens": cached},
        "cache_write_tokens": written,
    }


async def sse_objects(response, maximum_bytes):
    """Parse UTF-8 and SSE boundaries correctly even when network chunks split characters."""
    decoder = codecs.getincrementaldecoder("utf-8")("strict")
    buffer, consumed = "", 0
    async for chunk in response.aiter_bytes():
        consumed += len(chunk)
        if consumed > maximum_bytes:
            raise ProviderFailure(
                "provider_response_too_large",
                "Provider response exceeded the configured byte limit.",
            )
        try:
            buffer += decoder.decode(chunk)
        except UnicodeDecodeError as exc:
            raise ProviderFailure(
                "invalid_provider_stream", "Provider stream contains invalid UTF-8."
            ) from exc
        # Normalize CRLF after joining chunks, including a CR/LF split across reads.
        buffer = buffer.replace("\r\n", "\n")
        while "\n\n" in buffer:
            frame, buffer = buffer.split("\n\n", 1)
            if len(frame.encode()) > 524288:
                raise ProviderFailure(
                    "provider_event_too_large", "Provider event exceeded its limit."
                )
            data = "\n".join(
                line[5:].lstrip()
                for line in frame.split("\n")
                if line.startswith("data:")
            )
            if not data or data == "[DONE]":
                continue
            try:
                obj = json.loads(data)
                if not isinstance(obj, dict):
                    raise ValueError()
            except ValueError as exc:
                raise ProviderFailure(
                    "invalid_provider_stream", "Provider sent a malformed event."
                ) from exc
            yield obj
        if len(buffer.encode()) > 524288:
            raise ProviderFailure(
                "provider_event_too_large", "Provider event exceeded its limit."
            )
    try:
        buffer += decoder.decode(b"", final=True)
    except UnicodeDecodeError as exc:
        raise ProviderFailure(
            "invalid_provider_stream", "Provider stream ended inside a UTF-8 character."
        ) from exc
    if buffer.strip():
        raise ProviderFailure(
            "incomplete_provider_stream", "Provider stream ended inside an event."
        )


class LiveProviderClient:
    def __init__(self, settings, models, ledger, transport=None):
        self.settings, self.models, self.ledger = settings, models, ledger
        self.client = httpx.AsyncClient(
            timeout=httpx.Timeout(
                settings.provider_timeout_seconds, connect=10, pool=5
            ),
            limits=httpx.Limits(max_connections=100, max_keepalive_connections=30),
            follow_redirects=False,
            trust_env=False,
            transport=transport,
        )
        self.gemini = GeminiProviderClient(self.client, settings, ledger, sse_objects)

    async def close(self):
        await self.client.aclose()

    def destination(self, provider):
        if provider == "openai":
            return "https://api.openai.com/v1", {
                "Authorization": "Bearer "
                + self.settings.openai_api_key.get_secret_value()
            }
        return "https://api.anthropic.com/v1", {
            "x-api-key": self.settings.anthropic_api_key.get_secret_value(),
            "anthropic-version": "2023-06-01",
        }

    def body(self, payload, spec):
        messages = [
            {"role": m["role"], "content": m["content"]} for m in payload["messages"]
        ]
        if spec.provider == "openai":
            return {"model": spec.upstream_model, "input": messages}
        system, conversation = [], []
        for message in messages:
            if message["role"] == "system":
                if conversation:
                    raise ProviderFailure(
                        "invalid_message_order",
                        "Claude system messages must precede the conversation.",
                        422,
                        True,
                    )
                system.append(message["content"])
            else:
                conversation.append(message)
        if not conversation or conversation[0]["role"] != "user":
            raise ProviderFailure(
                "invalid_message_order",
                "Claude requires the conversation to begin with a user message.",
                422,
                True,
            )
        result = {"model": spec.upstream_model, "messages": conversation}
        if system:
            result["system"] = "\n\n".join(system)
        return result

    async def bounded_json(self, method, url, headers, body=None, maximum=65536):
        async with self.client.stream(
            method, url, headers=headers, json=body
        ) as response:
            if response.status_code != 200:
                raise ProviderFailure(
                    "provider_preflight_failed",
                    "Provider rejected token counting or usage lookup. Check credentials, model access, and provider limits.",
                    503,
                    True,
                )
            data = bytearray()
            async for chunk in response.aiter_bytes():
                data.extend(chunk)
                if len(data) > maximum:
                    raise ProviderFailure(
                        "provider_response_too_large",
                        "Provider metadata exceeded its limit.",
                    )
            try:
                value = json.loads(data)
                if not isinstance(value, dict):
                    raise ValueError()
                return value
            except ValueError as exc:
                raise ProviderFailure(
                    "invalid_provider_response", "Provider returned invalid metadata."
                ) from exc

    async def count_tokens(self, payload, spec):
        """Count remotely; Claude gets a conservative full-context reservation for a hard budget."""
        if spec.provider == "gemini":
            return await self.gemini.count_tokens(payload, spec)
        base, headers = self.destination(spec.provider)
        route = (
            "/responses/input_tokens"
            if spec.provider == "openai"
            else "/messages/count_tokens"
        )
        try:
            async with asyncio.timeout(20):
                data = await self.bounded_json(
                    "POST", base + route, headers, self.body(payload, spec)
                )
            actual = integer(data.get("input_tokens"), "input count")
        except (httpx.HTTPError, TimeoutError) as exc:
            raise ProviderFailure(
                "provider_preflight_failed",
                "Could not count input tokens. No generation was dispatched.",
                503,
                True,
            ) from exc
        if (
            actual > spec.max_input_tokens
            or actual + payload["max_completion_tokens"] > spec.context_tokens
        ):
            raise ProviderFailure(
                "model_limit_exceeded",
                "Input exceeds the configured model limit.",
                422,
                True,
            )
        # Claude documents this count as an estimate. Reserving its whole context is
        # intentionally conservative; final usage refunds the unused reservation.
        return actual if spec.provider == "openai" else spec.context_tokens

    async def open(self, payload, request_id):
        spec = self.models[payload["model"]]
        if spec.provider == "gemini":
            return await self.gemini.open(payload, request_id, spec)
        base, headers = self.destination(spec.provider)
        headers["X-Client-Request-Id"] = request_id
        body = self.body(payload, spec)
        body["stream"] = (
            True  # Capture the provider ID early, including for JSON clients.
        )
        if spec.provider == "openai":
            path = "/responses"
            body.update(
                max_output_tokens=payload["max_completion_tokens"],
                store=self.settings.openai_store_responses,
                service_tier="default",
            )
        else:
            path = "/messages"
            body["max_tokens"] = payload["max_completion_tokens"]
        try:
            response = await self.client.send(
                self.client.build_request(
                    "POST", base + path, headers=headers, json=body
                ),
                stream=True,
            )
        except httpx.HTTPError as exc:
            raise ProviderFailure(
                "provider_unavailable",
                "Provider connection failed. The reservation remains held until the outcome is established.",
                503,
            ) from exc
        if response.status_code != 200:
            status = response.status_code
            await response.aclose()
            # Explicit rejected requests are distinct from ambiguous 5xx/network failures.
            no_charge = status in (400, 401, 403, 404, 413, 422, 429)
            raise ProviderFailure(
                "provider_rejected",
                "Provider rejected this request. Check model access, credentials, and rate limits.",
                429 if status == 429 else 502,
                no_charge,
            )
        response.extensions["tollgate_request_id"] = request_id
        return response

    async def events(self, response, model):
        """Turn malformed upstream fields into safe errors without dropping the reservation."""
        try:
            if self.models[model].provider == "gemini":
                async for event in self.gemini.events(response, model):
                    yield event
                return
            async for event in self._events(response, model):
                yield event
        except (KeyError, TypeError, ValueError, IndexError) as exc:
            raise ProviderFailure(
                "invalid_provider_stream",
                "Provider stream fields were invalid. Usage needs review.",
            ) from exc

    async def _events(self, response, model):
        spec, request_id = (
            self.models[model],
            response.extensions["tollgate_request_id"],
        )
        if "text/event-stream" not in response.headers.get("content-type", ""):
            raise ProviderFailure(
                "invalid_provider_stream", "Expected a streaming provider response."
            )
        created, raw_usage, finish, seen_delta = int(time.time()), {}, "stop", False

        def chunk(text=None, usage=None, reason=None):
            choices = (
                []
                if usage
                else [
                    {
                        "index": 0,
                        "delta": ({"content": text} if text is not None else {}),
                        "finish_reason": reason,
                    }
                ]
            )
            return {
                "id": "chatcmpl-" + request_id,
                "object": "chat.completion.chunk",
                "created": created,
                "model": model,
                "choices": choices,
                "usage": usage,
            }

        async for obj in sse_objects(response, self.settings.max_provider_bytes):
            kind = obj.get("type")
            if spec.provider == "openai":
                if kind == "response.created":
                    await asyncio.to_thread(
                        self.ledger.save_provider_id, request_id, obj["response"]["id"]
                    )
                elif kind in ("response.output_text.delta", "response.refusal.delta"):
                    text = obj.get("delta")
                    if not isinstance(text, str):
                        raise ProviderFailure(
                            "invalid_provider_stream",
                            "Provider text delta was invalid.",
                        )
                    yield chunk(text)
                elif kind in ("response.completed", "response.incomplete"):
                    result = obj["response"]
                    if result.get("service_tier") not in (None, "default"):
                        raise ProviderFailure(
                            "unexpected_price_tier",
                            "Provider used an unconfigured pricing tier; review this request.",
                        )
                    await asyncio.to_thread(
                        self.ledger.save_provider_id, request_id, result["id"]
                    )
                    if not isinstance(result.get("usage"), dict):
                        raise ProviderFailure(
                            "missing_usage", "Provider completion omitted usage."
                        )
                    finish = "length" if kind == "response.incomplete" else "stop"
                    yield chunk(reason=finish)
                    yield chunk(usage=openai_usage(result["usage"]))
                    yield None
                    return
                elif kind in ("error", "response.failed"):
                    raise stream_failure(response, obj, spec.provider)
            else:
                if kind == "message_start":
                    message = obj["message"]
                    await asyncio.to_thread(
                        self.ledger.save_provider_id, request_id, message["id"]
                    )
                    raw_usage = dict(message.get("usage", {}))
                elif kind == "content_block_delta":
                    delta = obj.get("delta", {})
                    if delta.get("type") == "text_delta":
                        if not isinstance(delta.get("text"), str):
                            raise ProviderFailure(
                                "invalid_provider_stream",
                                "Provider text delta was invalid.",
                            )
                        yield chunk(delta["text"])
                elif kind == "message_delta":
                    raw_usage.update(
                        obj.get("usage", {})
                    )  # These counters are cumulative.
                    reason = obj.get("delta", {}).get("stop_reason")
                    if reason and "output_tokens" in obj.get("usage", {}):
                        finish = "length" if reason == "max_tokens" else "stop"
                        seen_delta = True
                elif kind == "message_stop":
                    if not seen_delta:
                        raise ProviderFailure(
                            "missing_usage", "Claude stream ended without final usage."
                        )
                    yield chunk(reason=finish)
                    yield chunk(usage=anthropic_usage(raw_usage))
                    yield None
                    return
                elif kind == "error":
                    raise stream_failure(response, obj, spec.provider)
        raise ProviderFailure(
            "incomplete_provider_stream",
            "Provider stream ended without final usage. Credits remain reserved.",
        )

    async def completion(self, response, model):
        text, usage, reason = [], None, "stop"
        async for event in self.events(response, model):
            if event is None:
                break
            if event.get("usage"):
                usage = event["usage"]
            for choice in event["choices"]:
                text.append(choice["delta"].get("content", ""))
                reason = choice.get("finish_reason") or reason
        if usage is None:
            raise ProviderFailure(
                "missing_usage", "Provider did not report final usage."
            )
        return {
            "id": "chatcmpl-" + response.extensions["tollgate_request_id"],
            "object": "chat.completion",
            "created": int(time.time()),
            "model": model,
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": "".join(text)},
                    "finish_reason": reason,
                }
            ],
            "usage": usage,
        }

    async def lookup_usage(self, request_id):
        row = await asyncio.to_thread(self.ledger.request, request_id)
        if (
            row["provider"] != "openai"
            or not self.settings.openai_store_responses
            or not row["provider_response_id"]
        ):
            return None
        provider_id = row["provider_response_id"]
        if not provider_id.startswith("resp_") or not all(
            c.isalnum() or c in "_-" for c in provider_id
        ):
            return None
        base, headers = self.destination("openai")
        try:
            async with asyncio.timeout(15):
                data = await self.bounded_json(
                    "GET",
                    base + "/responses/" + provider_id,
                    headers,
                    maximum=self.settings.max_provider_bytes,
                )
            if (
                data.get("status") in ("completed", "incomplete")
                and data.get("service_tier") in (None, "default")
                and isinstance(data.get("usage"), dict)
            ):
                return openai_usage(data["usage"])
        except (httpx.HTTPError, ProviderFailure, TimeoutError, ValueError):
            pass
        return None
