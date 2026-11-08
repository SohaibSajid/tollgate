"""Bounded asynchronous HTTP calls and SSE parsing for the configured simulator."""

import codecs
import json

import httpx
from pydantic import ValidationError

from app.models.response import ChatCompletionResponse, TokenUsage


class ProviderFailure(Exception):
    def __init__(
        self, code: str, message: str, status: int = 502, no_charge: bool = False
    ):
        """Describe a safe gateway error and whether this simulator proved no charge."""
        super().__init__(message)
        self.code, self.message, self.status, self.no_charge = (
            code,
            message,
            status,
            no_charge,
        )


class ProviderClient:
    def __init__(self, settings, transport=None):
        """Reuse bounded connections; never auto-retry generation or follow redirects."""
        self.settings = settings
        self.client = httpx.AsyncClient(
            base_url=settings.provider_base_url,
            headers={
                "Authorization": "Bearer "
                + settings.provider_api_key.get_secret_value()
            },
            timeout=httpx.Timeout(settings.provider_timeout_seconds, connect=5, pool=5),
            limits=httpx.Limits(max_connections=30, max_keepalive_connections=10),
            follow_redirects=False,
            trust_env=False,
            transport=transport,
        )

    async def close(self) -> None:
        """Close pooled HTTP connections during application shutdown."""
        await self.client.aclose()

    async def open(self, payload: dict, request_id: str) -> httpx.Response:
        """Open a generation response without reading or buffering its whole body."""
        request = self.client.build_request(
            "POST",
            "/v1/chat/completions",
            json=payload,
            headers={"X-Tollgate-Request-ID": request_id},
        )
        try:
            response = await self.client.send(request, stream=True)
        except httpx.TimeoutException as exc:
            raise ProviderFailure(
                "provider_timeout",
                "The simulator did not respond in time; credits remain reserved pending recovery.",
                504,
            ) from exc
        except httpx.HTTPError as exc:
            raise ProviderFailure(
                "provider_unavailable",
                "The simulator could not be reached; the request outcome needs recovery.",
                503,
            ) from exc
        if response.status_code != 200:
            no_charge = response.headers.get("X-Simulator-No-Charge") == "true"
            await response.aclose()
            raise ProviderFailure(
                "provider_rejected",
                "The simulator rejected the request.",
                502,
                no_charge=no_charge,
            )
        return response

    async def completion(self, response: httpx.Response, model: str) -> dict:
        """Read a size-limited JSON answer and validate its reported usage."""
        body = bytearray()
        async for chunk in response.aiter_bytes():
            if len(body) + len(chunk) > self.settings.max_provider_bytes:
                raise ProviderFailure(
                    "provider_response_too_large",
                    "The simulator response exceeded its size limit.",
                )
            body.extend(chunk)
        try:
            parsed = ChatCompletionResponse.model_validate_json(bytes(body))
        except ValidationError as exc:
            raise ProviderFailure(
                "invalid_provider_response",
                "The simulator returned invalid JSON, fields, or token usage.",
            ) from exc
        if parsed.model != model:
            raise ProviderFailure(
                "provider_model_mismatch", "The simulator returned an unexpected model."
            )
        return parsed.model_dump()

    async def events(self, response: httpx.Response, model: str):
        """Parse bounded SSE frames while forwarding each event without full-response buffering."""
        if "text/event-stream" not in response.headers.get("content-type", ""):
            raise ProviderFailure(
                "invalid_provider_stream",
                "Expected an SSE response from the simulator.",
            )
        decoder = codecs.getincrementaldecoder("utf-8")("strict")
        buffer, received = "", 0
        async for chunk in response.aiter_bytes():
            received += len(chunk)
            if received > self.settings.max_provider_bytes:
                raise ProviderFailure(
                    "provider_response_too_large",
                    "The simulator stream exceeded its size limit.",
                )
            try:
                buffer += decoder.decode(chunk)
            except UnicodeDecodeError as exc:
                raise ProviderFailure(
                    "invalid_provider_stream",
                    "The simulator stream contained invalid text.",
                ) from exc
            while "\n\n" in buffer:
                frame, buffer = buffer.split("\n\n", 1)
                if len(frame) > 65_536:
                    raise ProviderFailure(
                        "invalid_provider_stream",
                        "A simulator stream event was too large.",
                    )
                data = "\n".join(
                    line[5:].lstrip()
                    for line in frame.splitlines()
                    if line.startswith("data:")
                )
                if not data:
                    continue
                if data == "[DONE]":
                    yield None
                    return
                try:
                    event = json.loads(data)
                    if (
                        event.get("model") != model
                        or event.get("object") != "chat.completion.chunk"
                        or not isinstance(event.get("choices"), list)
                    ):
                        raise ValueError("Unsupported stream shape")
                    if event.get("usage") is not None:
                        event["usage"] = TokenUsage.model_validate(
                            event["usage"]
                        ).model_dump()
                except (ValueError, AttributeError, ValidationError) as exc:
                    raise ProviderFailure(
                        "invalid_provider_stream",
                        "The simulator returned an invalid stream event.",
                    ) from exc
                yield event
            if len(buffer) > 65_536:
                raise ProviderFailure(
                    "invalid_provider_stream", "A simulator stream event was too large."
                )
        raise ProviderFailure(
            "incomplete_provider_stream",
            "The stream ended without a completion marker; usage may require recovery.",
        )

    async def lookup_usage(self, request_id: str) -> dict | None:
        """Ask the simulator's durable usage store; a missing result never proves no charge."""
        try:
            response = await self.client.get("/requests/" + request_id + "/usage")
            if response.status_code != 200 or len(response.content) > 4096:
                return None
            return TokenUsage.model_validate(response.json()["usage"]).model_dump()
        except (httpx.HTTPError, ValueError, KeyError, ValidationError):
            return None
