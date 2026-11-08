"""Shared error format used by endpoints and middleware."""

from pydantic import BaseModel, ConfigDict, Field
from starlette.responses import JSONResponse
from starlette.types import Scope


class ErrorDetail(BaseModel):
    """Identify one invalid field without including its submitted value."""

    model_config = ConfigDict(extra="forbid")
    field: str
    message: str
    type: str


class ErrorBody(BaseModel):
    """Give developers a stable code, readable message, and tracking ID."""

    model_config = ConfigDict(extra="forbid")
    code: str
    message: str
    request_id: str
    details: list[ErrorDetail] | None = Field(default=None, max_length=10)
    context: dict | None = None


class ErrorResponse(BaseModel):
    """Wrap every application error under the same top-level key."""

    model_config = ConfigDict(extra="forbid")
    error: ErrorBody


def error_response(
    scope: Scope,
    status_code: int,
    code: str,
    message: str,
    details: list[ErrorDetail] | None = None,
    headers: dict[str, str] | None = None,
    context: dict | None = None,
) -> JSONResponse:
    """Build an error response and attach its request ID to the headers."""
    request_id = scope.get("state", {}).get("request_id", "unavailable")
    body = ErrorResponse(
        error=ErrorBody(
            code=code,
            message=message,
            request_id=request_id,
            details=details,
            context=context,
        )
    )
    response_headers = dict(headers or {})
    response_headers["X-Request-ID"] = request_id
    response_headers["Cache-Control"] = "no-store"
    return JSONResponse(
        status_code=status_code,
        content=body.model_dump(exclude_none=True),
        headers=response_headers,
    )
