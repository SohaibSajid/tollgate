"""Keep administration off wardens and generation off the control service."""

from app.models.error import error_response


def allowed_path(role, path):
    if role == "warden":
        return path.startswith("/v1/") or path in (
            "/health",
            "/ready",
            "/docs",
            "/openapi.json",
        )
    if role == "control":
        return not path.startswith("/v1/")
    return True


class ServiceRoleMiddleware:
    def __init__(self, app, role):
        self.app, self.role = app, role

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and not allowed_path(self.role, scope["path"]):
            response = error_response(
                scope,
                404,
                "wrong_service",
                "Use the API address for generation and the control address for administration.",
            )
            return await response(scope, receive, send)
        return await self.app(scope, receive, send)
