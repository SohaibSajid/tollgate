"""Authenticate before body parsing; enforce separate owner and customer privileges."""

import asyncio
import hashlib
from http.cookies import SimpleCookie
from secrets import compare_digest
from app.models.error import error_response
from app.services.ledger import BillingError


class APIKeyAuthenticationMiddleware:
    def __init__(self, app, admin_key, ledger, sessions=None, settings=None):
        self.app, self.ledger, self.sessions, self.settings = (
            app,
            ledger,
            sessions,
            settings,
        )
        self.admin_digest = hashlib.sha256(admin_key.encode()).digest()

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        path = scope["path"]
        if not path.startswith(("/v1/", "/admin/", "/auth/")):
            return await self.app(scope, receive, send)
        headers = {name.lower(): value for name, value in scope["headers"]}
        values = [
            value
            for name, value in scope["headers"]
            if name.lower() == b"authorization"
        ]
        token = b""
        if len(values) == 1 and len(values[0]) <= 256:
            scheme, _, candidate = values[0].partition(b" ")
            if scheme.lower() == b"bearer" and 32 <= len(candidate) <= 128:
                token = candidate
        try:
            if self.sessions and path.startswith("/auth/"):
                address = str(scope.get("client", ("unknown",))[0])
                await asyncio.to_thread(
                    self.sessions.limit,
                    "login:" + hashlib.sha256(address.encode()).hexdigest(),
                    20 if path == "/auth/login" else 120,
                )
                return await self.app(scope, receive, send)
            is_admin = path.startswith("/admin/")
            if is_admin:
                valid = bool(token) and compare_digest(
                    hashlib.sha256(token).digest(), self.admin_digest
                )
                if not valid and self.sessions and not values:
                    cookie = SimpleCookie()
                    try:
                        cookie.load(headers.get(b"cookie", b"").decode("latin-1"))
                        raw = (
                            cookie["tollgate_session"].value
                            if "tollgate_session" in cookie
                            else ""
                        )
                    except Exception:
                        raw = ""
                    csrf = await asyncio.to_thread(self.sessions.get, raw)
                    valid = csrf is not None
                    if valid and scope["method"] not in ("GET", "HEAD", "OPTIONS"):
                        origin = headers.get(b"origin", b"").decode("latin-1")
                        supplied = headers.get(b"x-csrf-token", b"").decode("latin-1")
                        if origin != self.settings.public_origin or not compare_digest(
                            supplied, csrf
                        ):
                            raise BillingError(
                                403,
                                "csrf_failed",
                                "Refresh the dashboard and retry from its configured origin.",
                            )
                if not valid:
                    raise BillingError(
                        401,
                        "invalid_api_key",
                        "Sign in with the owner key for this endpoint.",
                    )
                if self.sessions:
                    await asyncio.to_thread(
                        self.sessions.limit,
                        "admin",
                        self.settings.admin_requests_per_minute,
                    )
            else:
                identity = (
                    await asyncio.to_thread(self.ledger.authenticate, token)
                    if token
                    else None
                )
                if not identity:
                    raise BillingError(
                        401,
                        "invalid_api_key",
                        "Use an active customer virtual key for this endpoint.",
                    )
                scope.setdefault("state", {})["identity"] = identity
                if self.sessions:
                    await asyncio.to_thread(
                        self.sessions.limit,
                        "customer:" + identity["customer_id"],
                        self.settings.customer_requests_per_minute,
                    )
        except BillingError as exc:
            response = error_response(
                scope,
                exc.status,
                exc.code,
                exc.message,
                context=exc.context,
                headers=(
                    {"Retry-After": str(exc.context.get("retry_after_seconds", 60))}
                    if exc.status == 429
                    else None
                ),
            )
            return await response(scope, receive, send)
        except Exception:
            response = error_response(
                scope,
                503,
                "authentication_unavailable",
                "Authentication storage is temporarily unavailable.",
            )
            return await response(scope, receive, send)
        await self.app(scope, receive, send)
