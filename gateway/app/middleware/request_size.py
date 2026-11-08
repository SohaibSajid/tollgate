"""Bound API request bodies before FastAPI parses their JSON."""

import asyncio

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.models.error import error_response


class RequestSizeMiddleware:
    def __init__(self, app: ASGIApp, max_bytes: int, timeout_seconds: float) -> None:
        """Store the byte limit and total time allowed to receive a body."""
        self.app = app
        self.max_bytes = max_bytes
        self.timeout_seconds = timeout_seconds

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Read a bounded body, then pass its bytes to the next application."""
        if scope["type"] != "http" or not scope["path"].startswith(
            ("/v1/", "/admin/", "/auth/", "/webhooks/")
        ):
            await self.app(scope, receive, send)
            return

        lengths = [
            value
            for name, value in scope["headers"]
            if name.lower() == b"content-length"
        ]
        declared_length = None
        if lengths:
            if len(lengths) != 1 or len(lengths[0]) > 20 or not lengths[0].isdigit():
                response = error_response(
                    scope,
                    400,
                    "invalid_content_length",
                    "Content-Length must be one non-negative integer.",
                )
                await response(scope, receive, send)
                return
            declared_length = int(lengths[0])
            if declared_length > self.max_bytes:
                await self.reject_large_body(scope, receive, send)
                return

        # Count actual bytes too; a missing or incorrect header cannot bypass the limit.
        body = bytearray()
        too_large = False
        try:
            async with asyncio.timeout(self.timeout_seconds):
                while True:
                    message = await receive()
                    if message["type"] == "http.disconnect":
                        return
                    chunk = message.get("body", b"")
                    if len(body) + len(chunk) > self.max_bytes:
                        too_large = True
                        break
                    body.extend(chunk)
                    if not message.get("more_body", False):
                        break
        except TimeoutError:
            response = error_response(
                scope,
                408,
                "request_body_timeout",
                "The request body did not arrive within the allowed time.",
            )
            await response(scope, receive, send)
            return

        # Send errors outside the read deadline, so it cannot interrupt an error response.
        if too_large:
            await self.reject_large_body(scope, receive, send)
            return

        if declared_length is not None and declared_length != len(body):
            response = error_response(
                scope,
                400,
                "content_length_mismatch",
                "Content-Length does not match the received body size.",
            )
            await response(scope, receive, send)
            return

        body_bytes = bytes(body)
        del body
        delivered = False

        async def receive_body() -> Message:
            """Deliver the saved body once, then preserve disconnect notifications."""
            nonlocal delivered
            if not delivered:
                delivered = True
                return {"type": "http.request", "body": body_bytes, "more_body": False}
            return await receive()

        await self.app(scope, receive_body, send)

    async def reject_large_body(
        self, scope: Scope, receive: Receive, send: Send
    ) -> None:
        """Return a consistent 413 error without parsing an oversized body."""
        response = error_response(
            scope,
            413,
            "request_too_large",
            f"Request body must not exceed {self.max_bytes} bytes.",
        )
        await response(scope, receive, send)
