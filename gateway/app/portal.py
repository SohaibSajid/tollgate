"""Client portal: businesses sign up, sign in and manage their own keys and usage."""

import asyncio
import hashlib
import secrets
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from app.services.ledger import BillingError

COOKIE = "tollgate_portal"
PAGE_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
}


class Signup(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    business: str = Field(min_length=1, max_length=80)
    email: str = Field(min_length=3, max_length=254)
    password: str = Field(min_length=1, max_length=128)


class PortalLogin(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    email: str = Field(min_length=3, max_length=254)
    password: str = Field(min_length=1, max_length=128)


class PortalKey(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    label: str = Field(min_length=1, max_length=80)
    allowed_models: list[str] = Field(min_length=1, max_length=20)
    expires_in_days: int | None = Field(default=30, ge=1, le=365)


class OwnProviderKey(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    api_key: str = Field(min_length=20, max_length=512)


def add_portal(app: FastAPI, *, settings, accounts, ledger, keys, sessions, models):
    def check_origin(request: Request):
        if request.headers.get("origin") != settings.public_origin:
            raise BillingError(403, "origin_rejected", "Open the portal using its configured public origin.")

    async def throttle(request: Request, name: str, maximum: int):
        address = str(request.scope.get("client", ("unknown",))[0])
        await asyncio.to_thread(
            sessions.limit, name + ":" + hashlib.sha256(address.encode()).hexdigest(), maximum
        )

    async def account(request: Request, write: bool = False) -> dict:
        found = await asyncio.to_thread(accounts.session, request.cookies.get(COOKIE, ""))
        if found is None:
            raise BillingError(401, "portal_session_expired", "Sign in to the portal again.")
        if write:
            check_origin(request)
            if not secrets.compare_digest(request.headers.get("x-csrf-token", ""), found["csrf"]):
                raise BillingError(403, "csrf_failed", "Refresh the portal and try again.")
        await asyncio.to_thread(
            sessions.limit, "portal:" + found["account_id"], settings.admin_requests_per_minute
        )
        return found

    @app.get("/portal", response_class=HTMLResponse, include_in_schema=False)
    async def portal_page():
        html = await asyncio.to_thread(
            Path(__file__).with_name("ui").joinpath("portal.html").read_text, encoding="utf-8"
        )
        return HTMLResponse(html, headers=PAGE_HEADERS)

    @app.post("/portal/signup", status_code=201, include_in_schema=False)
    async def portal_signup(payload: Signup, request: Request):
        check_origin(request)
        await throttle(request, "portal-signup", 10)
        result = await asyncio.to_thread(
            accounts.signup, payload.business, payload.email, payload.password
        )
        return {"state": result["state"]}

    @app.post("/portal/login", include_in_schema=False)
    async def portal_login(payload: PortalLogin, request: Request):
        check_origin(request)
        await throttle(request, "portal-login", 20)
        raw, csrf = await asyncio.to_thread(accounts.login, payload.email, payload.password)
        response = JSONResponse({"csrf_token": csrf})
        response.set_cookie(
            COOKIE,
            raw,
            httponly=True,
            secure=settings.public_origin.startswith("https://"),
            samesite="strict",
            max_age=settings.session_hours * 3600,
            path="/",
        )
        return response

    @app.get("/portal/session", include_in_schema=False)
    async def portal_session(request: Request):
        found = await account(request)
        return {
            "csrf_token": found["csrf"],
            "email": found["email"],
            "business": found["business"],
            "customer_id": found["customer_id"],
        }

    @app.post("/portal/logout", include_in_schema=False)
    async def portal_logout(request: Request):
        await account(request, write=True)
        await asyncio.to_thread(accounts.logout, request.cookies.get(COOKIE, ""))
        response = JSONResponse({"signed_out": True})
        response.delete_cookie(COOKIE, path="/")
        return response

    @app.get("/portal/workspace", include_in_schema=False)
    async def portal_workspace(request: Request):
        found = await account(request)
        data = await asyncio.to_thread(accounts.workspace, found["customer_id"])
        data["models"] = [spec.public() for spec in models.values()]
        data["provider_key_policy"] = settings.provider_key_policy
        data["base_url"] = settings.public_origin + "/v1"
        return data

    @app.post("/portal/keys", status_code=201, include_in_schema=False)
    async def portal_create_key(payload: PortalKey, request: Request):
        found = await account(request, write=True)
        if any(model not in models for model in payload.allowed_models):
            raise BillingError(422, "unsupported_model", "Choose models enabled on this gateway.")
        return await asyncio.to_thread(
            ledger.create_key,
            found["customer_id"],
            payload.label,
            payload.allowed_models,
            payload.expires_in_days,
        )

    @app.delete("/portal/keys/{key_id}", include_in_schema=False)
    async def portal_revoke_key(key_id: str, request: Request):
        found = await account(request, write=True)
        if not await asyncio.to_thread(accounts.owns_key, found["customer_id"], key_id):
            raise BillingError(404, "key_not_found", "API key was not found.")
        await asyncio.to_thread(ledger.revoke_key, key_id)
        return {"status": "revoked"}

    @app.put("/portal/provider-keys/{provider}", include_in_schema=False)
    async def portal_save_provider_key(provider: str, payload: OwnProviderKey, request: Request):
        found = await account(request, write=True)
        return await asyncio.to_thread(keys.save, found["customer_id"], provider, payload.api_key)

    @app.delete("/portal/provider-keys/{provider}", include_in_schema=False)
    async def portal_remove_provider_key(provider: str, request: Request):
        found = await account(request, write=True)
        return await asyncio.to_thread(keys.remove, found["customer_id"], provider)
