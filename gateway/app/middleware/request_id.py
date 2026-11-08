"""Assign a separate tracking ID to every HTTP request."""

from uuid import uuid4

from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send


class RequestIDMiddleware:
    def __init__(self, app: ASGIApp, secure: bool = False) -> None:
        """Keep a reference to the next middleware or application."""
        self.app = app
        self.secure = secure

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Create an ID before processing and add it to the outgoing response."""
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        # Generate our own ID so a caller cannot impersonate another request.
        request_id = "req_" + uuid4().hex
        scope.setdefault("state", {})["request_id"] = request_id

        async def send_with_request_id(message: Message) -> None:
            """Add headers without buffering or changing response-body chunks."""
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                headers["X-Request-ID"] = request_id
                headers["Cache-Control"] = "no-store"
                headers["X-Content-Type-Options"] = "nosniff"
                headers["X-Frame-Options"] = "DENY"
                headers["Referrer-Policy"] = "no-referrer"
                headers["Permissions-Policy"] = (
                    "camera=(), microphone=(), geolocation=()"
                )
                if self.secure:
                    headers["Strict-Transport-Security"] = "max-age=31536000"
            await send(message)

        # The ID lives in this call, not on self, so concurrent requests stay separate.
        await self.app(scope, receive, send_with_request_id)
