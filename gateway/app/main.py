"""Run the local Tollgate API, dashboard, credit ledger, provider flow, and worker."""

import asyncio
import contextlib
import hashlib
import json
import logging
import sqlite3
import time
import csv
import io
from urllib.parse import urlsplit
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field
import psycopg
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated

import anyio
import httpx
from fastapi import FastAPI, Header, Request, Security
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from starlette.exceptions import HTTPException
from starlette.middleware import Middleware

from app.config import Settings, get_settings
from app.database import Database
from app.logging.request_log import RequestLogMiddleware, configure_logging
from app.middleware.request_id import RequestIDMiddleware
from app.middleware.request_size import RequestSizeMiddleware
from app.middleware.service_role import ServiceRoleMiddleware, allowed_path
from app.services.cluster import Cluster
from app.services.continuity import Continuity
from app.models.error import ErrorDetail, error_response
from app.models.request import (
    ChatCompletionRequest,
    CreditGrant,
    CustomerCreate,
    KeyCreate,
    SpendCap,
    Resolution,
    FreezeCustomer,
    CheckoutRequest,
    AllowanceRequest,
    OfflineSync,
    ContactUpdate,
)
from app.models.response import HealthResponse
from app.routing.providers import load_models, price_is_current
from app.security.sessions import Sessions
from app.services.live_provider import LiveProviderClient
from app.services.payments import Payments
from app.security.authentication import APIKeyAuthenticationMiddleware
from app.services.ledger import BillingError, Ledger
from app.services.pricing import micro_to_usd, simulated_input_tokens, usd_to_micro
from app.services.provider_client import ProviderClient, ProviderFailure
from app.services.worker import SettlementWorker

logger = logging.getLogger("tollgate.errors")
bearer = HTTPBearer(
    auto_error=False,
    description="Customer virtual key for /v1/. Owner admin key for /admin/.",
)
Credential = Annotated[HTTPAuthorizationCredentials | None, Security(bearer)]


def create_app(
    settings: Settings | None = None, *, provider_transport=None, payment_transport=None
) -> FastAPI:
    """Assemble one independent gateway; tests can inject a simulated HTTP transport."""
    settings = settings if settings is not None else get_settings()
    database = Database(
        settings.database_path,
        settings.database_url.get_secret_value() if settings.database_url else None,
    )
    cluster = Cluster(database, settings)
    ledger = Ledger(
        database,
        settings.customer_concurrency,
        admission_check=(
            cluster.require_admission if settings.service_role == "warden" else None
        ),
    )
    models_catalog = load_models(settings)
    sessions = Sessions(database, settings)
    payments = Payments(settings, ledger, transport=payment_transport)
    provider = (
        ProviderClient(settings, transport=provider_transport)
        if settings.mode == "simulator"
        else LiveProviderClient(
            settings, models_catalog, ledger, transport=provider_transport
        )
    )
    continuity = Continuity(database, ledger, models_catalog, settings)
    worker = SettlementWorker(ledger, provider, settings, continuity)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        """Initialize durable storage, start settlement, and close resources on shutdown."""
        if settings.environment != "test":
            configure_logging()
        await asyncio.to_thread(
            database.check_version
            if settings.environment == "production"
            else database.initialize
        )
        clustered = settings.service_role != "standalone"
        if clustered:
            await asyncio.to_thread(cluster.claim)
        heartbeat_task = asyncio.create_task(cluster.run()) if clustered else None
        task = (
            asyncio.create_task(worker.run())
            if settings.worker_enabled and settings.service_role != "control"
            else None
        )
        try:
            yield
        finally:
            worker.stop.set()
            if task:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            if heartbeat_task:
                heartbeat_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await heartbeat_task
                with contextlib.suppress(Exception):
                    await asyncio.to_thread(cluster.leave)
            await provider.close()
            await payments.close()
            await asyncio.to_thread(database.close)

    show_docs = settings.docs_enabled and settings.environment != "production"
    app = FastAPI(
        title="Tollgate",
        version="1.2.0",
        lifespan=lifespan,
        description="AI gateway with OpenAI, Anthropic, and Gemini text adapters, durable accounting, and operator recovery.",
        debug=False,
        redirect_slashes=False,
        docs_url="/docs" if show_docs else None,
        redoc_url=None,
        openapi_url="/openapi.json" if show_docs else None,
        middleware=[
            Middleware(
                RequestIDMiddleware, secure=settings.environment == "production"
            ),
            Middleware(
                TrustedHostMiddleware,
                allowed_hosts=(
                    [
                        urlsplit(settings.public_origin).hostname,
                        "127.0.0.1",
                        "localhost",
                        "test",
                        "testserver",
                    ]
                    if settings.environment != "production"
                    else [urlsplit(settings.public_origin).hostname, "127.0.0.1"]
                ),
            ),
            Middleware(RequestLogMiddleware),
            Middleware(ServiceRoleMiddleware, role=settings.service_role),
            Middleware(
                APIKeyAuthenticationMiddleware,
                admin_key=settings.development_api_key.get_secret_value(),
                ledger=ledger,
                sessions=sessions,
                settings=settings,
            ),
            Middleware(
                RequestSizeMiddleware,
                max_bytes=settings.max_request_bytes,
                timeout_seconds=settings.body_read_timeout_seconds,
            ),
        ],
    )
    app.state.ledger, app.state.worker, app.state.provider = ledger, worker, provider
    app.state.cluster = cluster
    app.state.sessions, app.state.payments, app.state.models = (
        sessions,
        payments,
        models_catalog,
    )

    @app.exception_handler(BillingError)
    async def billing_error(request: Request, exc: BillingError):
        """Explain credit, model-access, and retry conflicts with safe structured details."""
        return error_response(
            request.scope, exc.status, exc.code, exc.message, context=exc.context
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError):
        """Identify invalid fields without echoing submitted values or prompts."""
        details = [
            ErrorDetail(
                field=".".join(str(part)[:80] for part in item["loc"]),
                message=item["msg"][:300],
                type=item["type"],
            )
            for item in exc.errors()[:10]
        ]
        return error_response(
            request.scope,
            422,
            "invalid_request",
            "Check the fields listed in details.",
            details=details,
        )

    @app.exception_handler(HTTPException)
    async def http_error(request: Request, exc: HTTPException):
        """Preserve HTTP status and protocol headers in the common error envelope."""
        return error_response(
            request.scope,
            exc.status_code,
            f"http_{exc.status_code}",
            str(exc.detail),
            headers=exc.headers,
        )

    @app.exception_handler(psycopg.Error)
    @app.exception_handler(sqlite3.OperationalError)
    async def database_busy(request: Request, exc: sqlite3.OperationalError):
        """Return a retryable storage failure without exposing filesystem or SQL details."""
        logger.error("Storage operation failed request_id=%s", request.state.request_id)
        return error_response(
            request.scope,
            503,
            "storage_unavailable",
            "Storage is temporarily unavailable. Retry with the same Idempotency-Key.",
        )

    @app.exception_handler(ProviderFailure)
    async def preflight_error(request: Request, exc: ProviderFailure):
        return error_response(request.scope, exc.status, exc.code, exc.message)

    @app.exception_handler(Exception)
    async def unexpected_error(request: Request, exc: Exception):
        """Hide internal details while keeping an ID suitable for investigation."""
        logger.error(
            "Unhandled error request_id=%s exception_type=%s",
            request.scope.get("state", {}).get("request_id", "unavailable"),
            type(exc).__name__,
        )
        return error_response(
            request.scope,
            500,
            "internal_error",
            "An unexpected error occurred. Inspect the request status before retrying.",
        )

    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    async def dashboard():
        """Serve the local owner dashboard; its protected data APIs still require a key."""
        html = await asyncio.to_thread(
            Path(__file__).with_name("ui").joinpath("dashboard.html").read_text,
            encoding="utf-8",
        )
        return HTMLResponse(
            html,
            headers={
                "X-Content-Type-Options": "nosniff",
                "X-Frame-Options": "DENY",
                "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
            },
        )

    @app.get("/api-spec.json", include_in_schema=False)
    async def client_spec():
        """Publish the curated client specification; production still hides the internal schema."""
        from app.api_contract import build_client_openapi

        spec = build_client_openapi()
        spec["servers"] = [{"url": settings.public_origin, "description": "This gateway"}]
        return JSONResponse(spec, headers={"Cache-Control": "public, max-age=300"})

    @app.get("/health")
    async def health():
        """Check process liveness; this does not claim provider readiness."""
        return {
            "status": "ok",
            "service": settings.app_name,
            "mode": settings.mode,
            "version": "1.2.0",
            "role": settings.service_role,
        }

    @app.get("/admin/overview")
    async def overview(_credentials: Credential):
        """Return owner-visible balances, queue states, request metadata, and ledger entries."""
        return {
            "mode": settings.mode,
            "service_role": settings.service_role,
            "cluster": await asyncio.to_thread(cluster.snapshot),
            "payments_enabled": payments.enabled,
            "models": [
                {
                    **spec.public(),
                    "price_current": price_is_current(
                        spec, settings.price_max_age_days
                    ),
                }
                for spec in models_catalog.values()
            ],
            **await asyncio.to_thread(ledger.overview),
        }

    @app.post("/admin/customers", status_code=201)
    async def create_customer(payload: CustomerCreate, _credentials: Credential):
        """Create an empty customer wallet, without silently granting money."""
        return await asyncio.to_thread(ledger.create_customer, payload.name)

    @app.get("/admin/cluster")
    async def cluster_status(_credentials: Credential):
        return await asyncio.to_thread(cluster.snapshot)

    class DrainWarden(BaseModel):
        model_config = ConfigDict(extra="forbid", strict=True)
        draining: bool
        reason: str = Field(min_length=8, max_length=300)

    @app.post("/admin/wardens/{node_id}/drain")
    async def drain_warden(
        node_id: str, payload: DrainWarden, _credentials: Credential
    ):
        return await asyncio.to_thread(
            cluster.drain, node_id, payload.draining, payload.reason
        )

    @app.post("/admin/keys", status_code=201)
    async def create_key(payload: KeyCreate, _credentials: Credential):
        """Issue a customer-bound virtual key and show its secret exactly once."""
        if any(model not in models_catalog for model in payload.allowed_models):
            raise BillingError(
                422, "unsupported_model", "Choose models enabled on this gateway."
            )
        return await asyncio.to_thread(
            ledger.create_key,
            payload.customer_id,
            payload.label,
            payload.allowed_models,
            payload.expires_in_days,
        )

    @app.delete("/admin/keys/{key_id}")
    async def revoke_key(key_id: str, _credentials: Credential):
        """Block new requests from a key without disturbing its existing reservations."""
        await asyncio.to_thread(ledger.revoke_key, key_id)
        return {"status": "revoked"}

    @app.post("/admin/credits")
    async def grant_credits(payload: CreditGrant, _credentials: Credential):
        """Grant test credits idempotently using a caller-provided reference."""
        try:
            amount = usd_to_micro(payload.amount_usd)
        except ValueError as exc:
            raise BillingError(422, "invalid_amount", str(exc)) from exc
        return await asyncio.to_thread(
            ledger.grant, payload.customer_id, amount, payload.reference
        )

    @app.put("/admin/spend-cap")
    async def set_spend_cap(payload: SpendCap, _credentials: Credential):
        """Enforce a customer's lifetime spend cap alongside their prepaid balance."""
        try:
            amount = (
                None if payload.amount_usd is None else usd_to_micro(payload.amount_usd)
            )
        except ValueError as exc:
            raise BillingError(422, "invalid_amount", str(exc)) from exc
        return await asyncio.to_thread(ledger.set_cap, payload.customer_id, amount)

    @app.post("/admin/settle")
    async def settle_ready(_credentials: Credential):
        """Run ready settlement jobs manually without releasing uncertain requests."""
        count = 0
        for request_id in await asyncio.to_thread(ledger.pending_jobs):
            count += int(await asyncio.to_thread(ledger.settle, request_id))
        return {"settled": count}

    @app.get("/v1/models")
    async def models(request: Request, _credentials: Credential):
        """List only the models allowed by the authenticated customer's key."""
        allowed = request.state.identity["allowed_models"]
        return {
            "object": "list",
            "data": [
                spec.public()
                for name, spec in models_catalog.items()
                if name in allowed
            ],
        }

    @app.post("/v1/allowance", status_code=201)
    async def create_allowance(payload: AllowanceRequest, request: Request, _credentials: Credential):
        """Hold part of the wallet and return a signed token for the standby relay."""
        try:
            amount = usd_to_micro(payload.amount_usd)
        except ValueError as exc:
            raise BillingError(422, "invalid_amount", str(exc)) from exc
        if settings.service_role == "warden":
            await asyncio.to_thread(cluster.check_admission)
        return await asyncio.to_thread(
            continuity.issue,
            request.state.identity,
            amount,
            payload.ttl_minutes,
            payload.models,
            payload.include_direct,
        )

    @app.post("/v1/offline/sync")
    async def offline_sync(payload: OfflineSync, request: Request, _credentials: Credential):
        """Import usage records created while the primary system was unreachable."""
        return await asyncio.to_thread(
            continuity.sync,
            request.state.identity["customer_id"],
            payload.allowance_id,
            [r.model_dump() for r in payload.records],
            payload.close,
        )

    @app.put("/admin/customers/{customer_id}/contact")
    async def set_contact(customer_id: str, payload: ContactUpdate, _credentials: Credential):
        """Store where this customer receives outage and recovery notices."""
        return await asyncio.to_thread(
            continuity.set_contact, customer_id, payload.email, payload.phone
        )

    @app.get("/admin/contacts")
    async def contacts(_credentials: Credential):
        """Used by the outside watchdog to keep a local copy for use during an outage."""
        return {"contacts": await asyncio.to_thread(continuity.contacts)}

    @app.get("/admin/allowances")
    async def allowances(_credentials: Credential):
        return {"allowances": await asyncio.to_thread(continuity.allowances)}

    @app.get("/v1/balance")
    async def balance(request: Request, _credentials: Credential):
        """Return the caller's balance without accepting an arbitrary customer ID."""
        return await asyncio.to_thread(
            ledger.balance, request.state.identity["customer_id"]
        )

    @app.get("/v1/requests/{request_id}")
    async def request_status(
        request_id: str, request: Request, _credentials: Credential
    ):
        """Let a customer inspect only their own request and settlement metadata."""
        return await asyncio.to_thread(
            ledger.request, request_id, request.state.identity["customer_id"]
        )

    async def failed_provider(request_id: str, failure: ProviderFailure):
        """Release proven non-billable simulator rejections; retain ambiguous holds."""
        with anyio.CancelScope(shield=True):
            if failure.no_charge:
                await asyncio.to_thread(
                    ledger.release, request_id, failure.code, allow_dispatched=True
                )
            else:
                await asyncio.to_thread(ledger.unresolved, request_id, failure.code)

    async def save_usage(request_id: str, usage: dict):
        """Persist usage plus its queue job before announcing completion to the caller."""
        await asyncio.to_thread(
            ledger.record_usage,
            request_id,
            usage["prompt_tokens"],
            usage["completion_tokens"],
            (usage.get("prompt_tokens_details") or {}).get("cached_tokens", 0),
            usage.get("cache_write_tokens", 0),
        )
        record = await asyncio.to_thread(ledger.request, request_id)
        if record["error_code"] == "usage_exceeds_reservation":
            raise ProviderFailure(
                "usage_exceeds_reservation",
                "Reported usage exceeded the reservation; the request requires investigation.",
            )
        return record

    @app.post("/v1/chat/completions")
    async def chat(
        payload: ChatCompletionRequest,
        request: Request,
        _credentials: Credential,
        idempotency_key: Annotated[
            str | None,
            Header(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.:-]+$"),
        ] = None,
        x_tollgate_feature: Annotated[
            str, Header(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_.:-]+$")
        ] = "default",
    ):
        """Reserve atomically, dispatch once, and deliver a JSON answer or live SSE stream."""
        if settings.service_role == "warden":
            # Recheck under the credit lock later; this prevents unnecessary preflight I/O.
            await asyncio.to_thread(cluster.check_admission)
        spec = models_catalog.get(payload.model)
        if spec is None:
            raise BillingError(
                422, "unsupported_model", "Use a model listed by GET /v1/models."
            )
        if spec.id not in request.state.identity["allowed_models"]:
            raise BillingError(
                403, "model_not_allowed", "This key cannot use this model."
            )
        if not price_is_current(spec, settings.price_max_age_days):
            raise BillingError(
                503,
                "price_review_required",
                "The operator must review this model's pricing before new requests can run.",
            )
        if payload.max_completion_tokens > spec.max_output_tokens or (
            spec.provider == "openai" and payload.max_completion_tokens < 16
        ):
            raise BillingError(
                422,
                "model_limit_exceeded",
                "Output limit is unsupported for this model. OpenAI requires at least 16.",
            )
        if settings.mode == "simulator":
            input_bound = simulated_input_tokens(payload.messages)
            if input_bound > spec.max_input_tokens:
                raise BillingError(
                    422, "model_limit_exceeded", "Input exceeds this model's limit."
                )
        else:
            input_bound = await provider.count_tokens(payload.model_dump(), spec)
        request_id = request.state.request_id
        idem = idempotency_key or request_id
        fingerprint = hashlib.sha256(
            json.dumps(
                {"request": payload.model_dump(), "feature": x_tollgate_feature},
                sort_keys=True,
                ensure_ascii=False,
            ).encode()
        ).hexdigest()
        reservation = await asyncio.to_thread(
            ledger.reserve,
            request_id,
            request.state.identity,
            idem,
            fingerprint,
            spec,
            x_tollgate_feature,
            input_bound,
            spec.output_bound(payload.max_completion_tokens),
        )
        headers = {
            "X-Tollgate-Request-ID": request_id,
            "Idempotency-Key": idem,
            "X-Tollgate-Usage-Source": (
                "simulated" if settings.mode == "simulator" else "provider_reported"
            ),
            "X-Tollgate-Reservation-USD": micro_to_usd(reservation["reserved_micro"]),
        }
        if settings.service_role == "warden":
            headers["X-Tollgate-Warden-ID"] = settings.node_id
            headers["X-Tollgate-Control-Status"] = (
                "degraded" if reservation["control_degraded"] else "online"
            )
        if not await asyncio.to_thread(ledger.dispatch, request_id):
            raise BillingError(
                409,
                "reservation_unavailable",
                "The reservation was released before dispatch; no new provider call was made.",
            )
        deadline = asyncio.get_running_loop().time() + settings.provider_timeout_seconds
        try:
            async with asyncio.timeout_at(deadline):
                upstream = await provider.open(
                    payload.model_dump(exclude={"stream_options"}), request_id
                )
        except (ProviderFailure, TimeoutError) as exc:
            failure = (
                exc
                if isinstance(exc, ProviderFailure)
                else ProviderFailure(
                    "provider_timeout",
                    "The provider deadline expired; usage needs recovery.",
                    504,
                )
            )
            await failed_provider(request_id, failure)
            return error_response(
                request.scope,
                failure.status,
                failure.code,
                failure.message,
                headers=headers,
            )
        except BaseException:
            await failed_provider(
                request_id,
                ProviderFailure("interrupted_dispatch", "Dispatch was interrupted."),
            )
            raise

        if not payload.stream:
            try:
                async with asyncio.timeout_at(deadline):
                    answer = await provider.completion(upstream, payload.model)
                    record = await save_usage(request_id, answer["usage"])
                headers["X-Tollgate-Cost-USD"] = record["cost_usd"]
                headers["X-Tollgate-Settlement"] = record["state"]
                return JSONResponse(answer, headers=headers)
            except (ProviderFailure, httpx.HTTPError, TimeoutError) as exc:
                failure = (
                    exc
                    if isinstance(exc, ProviderFailure)
                    else ProviderFailure(
                        "provider_timeout",
                        "The provider result was interrupted; usage needs recovery.",
                        504,
                    )
                )
                await failed_provider(request_id, failure)
                return error_response(
                    request.scope,
                    failure.status,
                    failure.code,
                    failure.message,
                    headers=headers,
                )
            finally:
                with anyio.CancelScope(shield=True):
                    await upstream.aclose()
                    await asyncio.to_thread(
                        ledger.unresolved, request_id, "result_not_recorded"
                    )

        async def stream_response():
            """Forward chunks immediately, save final usage durably, and preserve uncertain holds."""
            usage_seen = False
            try:
                async with asyncio.timeout_at(deadline):
                    async for event in provider.events(upstream, payload.model):
                        if event is None:
                            if not usage_seen:
                                raise ProviderFailure(
                                    "missing_usage",
                                    "Stream finished without usage; settlement needs recovery.",
                                )
                            yield "data: [DONE]\n\n"
                            return
                        if event.get("usage") is not None:
                            await save_usage(request_id, event["usage"])
                            usage_seen = True
                        yield "data: " + json.dumps(event) + "\n\n"
            except (ProviderFailure, httpx.HTTPError, TimeoutError) as exc:
                failure = (
                    exc
                    if isinstance(exc, ProviderFailure)
                    else ProviderFailure(
                        "provider_timeout",
                        "The stream was interrupted; usage needs recovery.",
                        504,
                    )
                )
                await failed_provider(request_id, failure)
                yield "data: " + json.dumps(
                    {
                        "error": {
                            "code": failure.code,
                            "message": failure.message,
                            "request_id": request_id,
                        }
                    }
                ) + "\n\n"
            finally:
                with anyio.CancelScope(shield=True):
                    await upstream.aclose()
                    await asyncio.to_thread(
                        ledger.unresolved, request_id, "stream_ended_without_usage"
                    )

        return StreamingResponse(
            stream_response(),
            media_type="text/event-stream",
            headers={**headers, "X-Accel-Buffering": "no"},
        )

    @app.get("/assets/{name}", include_in_schema=False)
    async def asset(name: str):
        if name not in ("dashboard.css", "dashboard.js"):
            raise HTTPException(404, "Asset not found")
        return FileResponse(
            Path(__file__).with_name("ui") / name,
            headers={"X-Content-Type-Options": "nosniff", "Cache-Control": "no-cache"},
        )

    class Login(BaseModel):
        model_config = ConfigDict(extra="forbid", strict=True)
        key: str = Field(min_length=32, max_length=128)

    @app.post("/auth/login")
    async def login(payload: Login, request: Request):
        if request.headers.get("origin") != settings.public_origin:
            raise BillingError(
                403,
                "origin_rejected",
                "Open the dashboard using its configured public origin.",
            )
        raw, csrf = await asyncio.to_thread(sessions.login, payload.key)
        result = JSONResponse(
            {"csrf_token": csrf, "expires_in_seconds": settings.session_hours * 3600}
        )
        result.set_cookie(
            "tollgate_session",
            raw,
            httponly=True,
            secure=settings.public_origin.startswith("https://"),
            samesite="strict",
            max_age=settings.session_hours * 3600,
            path="/",
        )
        return result

    @app.get("/auth/session")
    async def current_session(request: Request):
        csrf = await asyncio.to_thread(
            sessions.get, request.cookies.get("tollgate_session", "")
        )
        if not csrf:
            raise BillingError(401, "session_expired", "Sign in with the owner key.")
        return {"csrf_token": csrf}

    @app.post("/auth/logout")
    async def logout(request: Request):
        if request.headers.get("origin") != settings.public_origin:
            raise BillingError(403, "origin_rejected", "Invalid origin.")
        raw = request.cookies.get("tollgate_session", "")
        csrf = await asyncio.to_thread(sessions.get, raw)
        if not csrf or request.headers.get("x-csrf-token") != csrf:
            raise BillingError(
                403, "csrf_failed", "Refresh the page before signing out."
            )
        await asyncio.to_thread(sessions.logout, raw)
        result = JSONResponse({"signed_out": True})
        result.delete_cookie("tollgate_session", path="/")
        return result

    @app.get("/ready")
    async def readiness():
        def check():
            with database.connection() as connection:
                connection.execute("SELECT 1")
                if settings.service_role == "control":
                    return True
                if settings.service_role == "warden":
                    cluster.require_admission(connection)
                row = connection.execute(
                    "SELECT seen_at FROM service_heartbeats WHERE name='worker'"
                ).fetchone()
                return bool(row and time.time() - row[0] < 60)

        try:
            ready = await asyncio.to_thread(check)
        except Exception:
            ready = False
        ready = ready and (
            settings.service_role == "control"
            or all(
                price_is_current(spec, settings.price_max_age_days)
                for spec in models_catalog.values()
            )
        )
        return JSONResponse({"ready": ready}, status_code=200 if ready else 503)

    @app.post("/admin/customers/{customer_id}/freeze")
    async def freeze(
        customer_id: str, payload: FreezeCustomer, _credentials: Credential
    ):
        return await asyncio.to_thread(
            ledger.freeze, customer_id, payload.frozen, payload.reason
        )

    @app.post("/admin/requests/{request_id}/resolve")
    async def resolve(request_id: str, payload: Resolution, _credentials: Credential):
        return await asyncio.to_thread(ledger.resolve, request_id, payload)

    @app.get("/admin/reconciliation")
    async def reconcile(_credentials: Credential):
        return await asyncio.to_thread(ledger.reconciliation)

    @app.get("/admin/ledger.csv")
    async def export_ledger(
        _credentials: Credential, before: int = 9223372036854775807
    ):
        def read_page():
            with database.connection() as connection:
                return [
                    dict(row)
                    for row in connection.execute(
                        "SELECT * FROM ledger WHERE id<? ORDER BY id DESC LIMIT 10000",
                        (before,),
                    )
                ]

        rows = await asyncio.to_thread(read_page)
        output = io.StringIO()
        columns = [
            "id",
            "customer_id",
            "kind",
            "amount_micro",
            "reference",
            "request_id",
            "created_at",
        ]
        writer = csv.DictWriter(output, fieldnames=columns)
        writer.writeheader()
        for row in rows:
            # Prevent spreadsheet formula execution from exported strings.
            writer.writerow(
                {
                    key: (
                        "'" + value
                        if isinstance(value, str)
                        and value[:1] in ("=", "+", "-", "@", "\t", "\r")
                        else value
                    )
                    for key, value in row.items()
                }
            )
        return StreamingResponse(
            iter([output.getvalue()]),
            media_type="text/csv",
            headers={
                "Content-Disposition": 'attachment; filename="tollgate-ledger.csv"',
                "X-Next-Before": str(rows[-1]["id"]) if len(rows) == 10000 else "",
            },
        )

    @app.get("/admin/metrics", include_in_schema=False)
    async def metrics(_credentials: Credential):
        snapshot = await asyncio.to_thread(ledger.overview)
        values = {
            "tollgate_customers": snapshot["totals"]["customers"],
            "tollgate_unresolved_requests": snapshot["unresolved"],
            "tollgate_worker_last_seen_seconds": snapshot["worker_last_seen"] or 0,
        }
        values.update(
            {
                "tollgate_settlements_" + key: snapshot["queue"].get(key, 0)
                for key in ("pending", "done", "blocked")
            }
        )
        body = (
            "\n".join(
                f"# TYPE {key} gauge\n{key} {value}" for key, value in values.items()
            )
            + "\n"
        )
        from fastapi.responses import PlainTextResponse

        return PlainTextResponse(body, media_type="text/plain; version=0.0.4")

    @app.post("/v1/billing/checkout")
    async def checkout(
        payload: CheckoutRequest, request: Request, _credentials: Credential
    ):
        return await payments.checkout(request.state.identity["customer_id"], payload)

    @app.post("/webhooks/stripe", include_in_schema=False)
    async def stripe_webhook(request: Request):
        return await asyncio.to_thread(
            payments.webhook,
            await request.body(),
            request.headers.get("stripe-signature", ""),
        )

    app.router.routes = [
        route
        for route in app.router.routes
        if allowed_path(settings.service_role, route.path)
    ]
    return app


app = create_app()
