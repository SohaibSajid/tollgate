"""Log request metadata while excluding prompts, keys, and response bodies."""

import logging
import time
from starlette.types import ASGIApp, Message, Receive, Scope, Send

logger = logging.getLogger("tollgate.requests")


def configure_logging() -> None:
    """Enable safe application logs once without modifying provider or server loggers."""
    root = logging.getLogger("tollgate")
    if not root.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(name)s %(levelname)s %(message)s")
        )
        root.addHandler(handler)
    root.setLevel(logging.INFO)
    root.propagate = False


class RequestLogMiddleware:
    def __init__(self, app: ASGIApp):
        """Keep only the next application; timing data remains local to each request."""
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Record status and elapsed time without recording submitted content."""
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        started, status = time.monotonic(), 500

        async def capture(message: Message):
            """Observe the status header without altering or buffering body chunks."""
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
            await send(message)

        try:
            await self.app(scope, receive, capture)
        finally:
            logger.info(
                "request_id=%s method=%s status=%s duration_ms=%.1f",
                scope.get("state", {}).get("request_id", "unavailable"),
                scope["method"],
                status,
                (time.monotonic() - started) * 1000,
            )
