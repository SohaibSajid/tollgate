"""Standby relay: keeps serving customers when the wardens and main database are down.

Run it on separate infrastructure (another host, region or provider). It needs only:
the provider keys, the shared allowance secret, and a small local SQLite file. It never
connects to the main database. Spending is limited to the signed allowance in the
caller's token, so an outage can never make a wallet go negative.
"""

import asyncio
import contextlib
import hashlib
import json
import sqlite3
import time
from contextlib import asynccontextmanager, contextmanager

import anyio
import httpx
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.middleware import Middleware

from app.config import Settings, get_settings
from app.middleware.request_id import RequestIDMiddleware
from app.middleware.request_size import RequestSizeMiddleware
from app.models.error import error_response
from app.models.request import ChatCompletionRequest
from app.routing.providers import load_models, price_is_current
from app.services.continuity import sign_record, verify_allowance
from app.services.live_provider import LiveProviderClient
from app.services.pricing import calculate_cost, micro_to_usd, simulated_input_tokens
from app.services.provider_client import ProviderClient, ProviderFailure

SCHEMA = """
CREATE TABLE IF NOT EXISTS relay_allowances (
 aid TEXT PRIMARY KEY, customer_id TEXT NOT NULL, amount_micro INTEGER NOT NULL,
 spent_micro INTEGER NOT NULL DEFAULT 0, reserved_micro INTEGER NOT NULL DEFAULT 0,
 expires_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS relay_journal (
 record_id TEXT PRIMARY KEY, aid TEXT NOT NULL REFERENCES relay_allowances(aid),
 model TEXT NOT NULL, state TEXT NOT NULL CHECK(state IN ('reserved','done','unresolved','released')),
 reserve_micro INTEGER NOT NULL, input_tokens INTEGER, output_tokens INTEGER,
 cached_tokens INTEGER NOT NULL DEFAULT 0, cache_write_tokens INTEGER NOT NULL DEFAULT 0,
 cost_micro INTEGER, created_at REAL NOT NULL
)
"""


class RelayError(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


class _NoLedger:
    """The relay has no ledger; provider-id bookkeeping is simply skipped."""

    def save_provider_id(self, *_args):
        return None

    def request(self, *_args):
        raise LookupError("The relay does not recover usage.")


class RelayStore:
    def __init__(self, path):
        self.path = path

    def connect(self):
        connection = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA synchronous=FULL")
        return connection

    @contextmanager
    def transaction(self):
        connection = self.connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.execute("COMMIT")
        except BaseException:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()

    def initialize(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.transaction() as c:
            for statement in SCHEMA.split(";"):
                if statement.strip():
                    c.execute(statement)

    def reserve(self, claims: dict, record_id: str, model: str, reserve_micro: int):
        """Hold part of the allowance locally before the provider is called."""
        with self.transaction() as c:
            row = c.execute(
                "SELECT * FROM relay_allowances WHERE aid=?", (claims["aid"],)
            ).fetchone()
            if row is None:
                c.execute(
                    "INSERT INTO relay_allowances VALUES (?,?,?,0,0,?)",
                    (claims["aid"], claims["cid"], claims["amt"], claims["exp"]),
                )
                spent = reserved = 0
            else:
                spent, reserved = row["spent_micro"], row["reserved_micro"]
            if spent + reserved + reserve_micro > claims["amt"]:
                raise RelayError(
                    402, "allowance_exhausted", "The offline spending allowance cannot cover this request."
                )
            c.execute(
                "UPDATE relay_allowances SET reserved_micro=reserved_micro+? WHERE aid=?",
                (reserve_micro, claims["aid"]),
            )
            c.execute(
                "INSERT INTO relay_journal(record_id,aid,model,state,reserve_micro,created_at) VALUES (?,?,?,'reserved',?,?)",
                (record_id, claims["aid"], model, reserve_micro, time.time()),
            )

    def complete(self, record_id: str, usage: dict, spec):
        details = usage.get("prompt_tokens_details") or {}
        cached, written = details.get("cached_tokens", 0), usage.get("cache_write_tokens", 0)
        cost = calculate_cost(
            usage["prompt_tokens"],
            usage["completion_tokens"],
            spec.input_rate,
            spec.output_rate,
            cached,
            spec.cached_rate,
            written,
            spec.cache_write_rate,
        )
        with self.transaction() as c:
            row = c.execute("SELECT * FROM relay_journal WHERE record_id=?", (record_id,)).fetchone()
            if row is None or row["state"] not in ("reserved", "unresolved"):
                return cost, None
            c.execute(
                "UPDATE relay_journal SET state='done',input_tokens=?,output_tokens=?,cached_tokens=?,cache_write_tokens=?,cost_micro=? WHERE record_id=?",
                (usage["prompt_tokens"], usage["completion_tokens"], cached, written, cost, record_id),
            )
            c.execute(
                "UPDATE relay_allowances SET reserved_micro=reserved_micro-?,spent_micro=spent_micro+? WHERE aid=?",
                (row["reserve_micro"], cost, row["aid"]),
            )
        return cost, {
            "record_id": record_id,
            "model": row["model"],
            "input_tokens": usage["prompt_tokens"],
            "output_tokens": usage["completion_tokens"],
            "cached_tokens": cached,
            "cache_write_tokens": written,
            "created_at": row["created_at"],
        }

    def fail(self, record_id: str, no_charge: bool):
        """A proven non-billable failure frees the hold; anything doubtful stays held."""
        with self.transaction() as c:
            row = c.execute("SELECT * FROM relay_journal WHERE record_id=?", (record_id,)).fetchone()
            if row is None or row["state"] != "reserved":
                return
            if no_charge:
                c.execute("UPDATE relay_journal SET state='released' WHERE record_id=?", (record_id,))
                c.execute(
                    "UPDATE relay_allowances SET reserved_micro=reserved_micro-? WHERE aid=?",
                    (row["reserve_micro"], row["aid"]),
                )
            else:
                c.execute("UPDATE relay_journal SET state='unresolved' WHERE record_id=?", (record_id,))

    def records(self, aid: str) -> dict:
        with self.transaction() as c:
            a = c.execute("SELECT * FROM relay_allowances WHERE aid=?", (aid,)).fetchone()
            rows = c.execute(
                "SELECT * FROM relay_journal WHERE aid=? AND state='done' ORDER BY created_at", (aid,)
            ).fetchall()
        return {
            "allowance_id": aid,
            "records": [
                {
                    "record_id": r["record_id"],
                    "model": r["model"],
                    "input_tokens": r["input_tokens"],
                    "output_tokens": r["output_tokens"],
                    "cached_tokens": r["cached_tokens"],
                    "cache_write_tokens": r["cache_write_tokens"],
                    "created_at": r["created_at"],
                }
                for r in rows
            ],
            "unresolved": 0 if a is None else self._unresolved(aid),
        }

    def _unresolved(self, aid: str) -> int:
        with self.transaction() as c:
            return c.execute(
                "SELECT COUNT(*) FROM relay_journal WHERE aid=? AND state IN ('reserved','unresolved')",
                (aid,),
            ).fetchone()[0]

    def remaining(self, aid: str, amount: int) -> int:
        with self.transaction() as c:
            row = c.execute("SELECT * FROM relay_allowances WHERE aid=?", (aid,)).fetchone()
        return amount if row is None else amount - row["spent_micro"] - row["reserved_micro"]


def create_relay_app(settings: Settings | None = None, *, provider_transport=None) -> FastAPI:
    settings = settings if settings is not None else get_settings()
    secret = settings.allowance_secret.get_secret_value()
    if len(secret) < 32:
        raise RuntimeError("Set TOLLGATE_ALLOWANCE_SECRET (at least 32 characters) on the relay.")
    store = RelayStore(settings.relay_database_path)
    models = load_models(settings)
    provider = (
        ProviderClient(settings, transport=provider_transport)
        if settings.mode == "simulator"
        else LiveProviderClient(settings, models, _NoLedger(), transport=provider_transport)
    )

    @asynccontextmanager
    async def lifespan(_app):
        await asyncio.to_thread(store.initialize)
        try:
            yield
        finally:
            await provider.close()

    app = FastAPI(
        title="Tollgate standby relay",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        middleware=[
            Middleware(RequestIDMiddleware, secure=settings.environment == "production"),
            Middleware(
                RequestSizeMiddleware,
                max_bytes=settings.max_request_bytes,
                timeout_seconds=settings.body_read_timeout_seconds,
            ),
        ],
    )

    @app.exception_handler(RelayError)
    async def relay_error(request: Request, exc: RelayError):
        return error_response(request.scope, exc.status, exc.code, exc.message)

    @app.exception_handler(ProviderFailure)
    async def provider_error(request: Request, exc: ProviderFailure):
        return error_response(request.scope, exc.status, exc.code, exc.message)

    @app.exception_handler(RequestValidationError)
    async def invalid(request: Request, _exc: RequestValidationError):
        return error_response(request.scope, 422, "invalid_request", "The request body is invalid.")

    def claims_from(request: Request) -> dict:
        header = request.headers.get("authorization", "")
        if not header.lower().startswith("bearer "):
            raise RelayError(401, "invalid_allowance", "Send the signed allowance as a bearer token.")
        try:
            return verify_allowance(secret, header[7:].strip())
        except ValueError as exc:
            raise RelayError(401, str(exc), "The allowance is invalid or expired.") from exc

    @app.get("/health")
    async def health():
        return {"status": "ok", "service": "relay"}

    @app.get("/ready")
    async def ready():
        try:
            await asyncio.to_thread(store.initialize)
        except sqlite3.Error:
            return JSONResponse({"status": "not_ready"}, status_code=503)
        return {"status": "ready"}

    @app.get("/v1/offline/records")
    async def offline_records(request: Request):
        """Return this allowance's usage records, ready to upload to the primary system."""
        claims = claims_from(request)
        data = await asyncio.to_thread(store.records, claims["aid"])
        for record in data["records"]:
            record["sig"] = sign_record(secret, claims["aid"], record)
        return data

    @app.post("/v1/chat/completions")
    async def chat(payload: ChatCompletionRequest, request: Request):
        claims = claims_from(request)
        spec = models.get(payload.model)
        if spec is None or spec.id not in claims["models"]:
            raise RelayError(403, "model_not_allowed", "This allowance does not cover that model.")
        if not price_is_current(spec, settings.price_max_age_days):
            raise RelayError(503, "price_review_required", "This model's price review is out of date.")
        if payload.max_completion_tokens > spec.max_output_tokens or (
            spec.provider == "openai" and payload.max_completion_tokens < 16
        ):
            raise RelayError(422, "model_limit_exceeded", "Output limit is unsupported for this model.")
        if settings.mode == "simulator":
            input_bound = simulated_input_tokens(payload.messages)
            if input_bound > spec.max_input_tokens:
                raise RelayError(422, "model_limit_exceeded", "Input exceeds this model's limit.")
        else:
            input_bound = await provider.count_tokens(payload.model_dump(), spec)
        reserve = calculate_cost(
            input_bound,
            spec.output_bound(payload.max_completion_tokens),
            max(spec.input_rate, spec.cache_write_rate),
            spec.output_rate,
        )
        record_id = "rly_" + hashlib.sha256(request.state.request_id.encode()).hexdigest()[:32]
        await asyncio.to_thread(store.reserve, claims, record_id, spec.id, reserve)
        headers = {
            "X-Tollgate-Mode": "relay",
            "X-Tollgate-Record-ID": record_id,
            "X-Tollgate-Reservation-USD": micro_to_usd(reserve),
        }
        deadline = asyncio.get_running_loop().time() + settings.provider_timeout_seconds
        try:
            async with asyncio.timeout_at(deadline):
                upstream = await provider.open(
                    payload.model_dump(exclude={"stream_options"}), record_id
                )
        except (ProviderFailure, TimeoutError) as exc:
            failure = (
                exc
                if isinstance(exc, ProviderFailure)
                else ProviderFailure("provider_timeout", "The provider deadline expired.", 504)
            )
            await asyncio.to_thread(store.fail, record_id, failure.no_charge)
            return error_response(request.scope, failure.status, failure.code, failure.message, headers=headers)

        if not payload.stream:
            try:
                async with asyncio.timeout_at(deadline):
                    answer = await provider.completion(upstream, payload.model)
                cost, record = await asyncio.to_thread(store.complete, record_id, answer["usage"], spec)
                headers["X-Tollgate-Cost-USD"] = micro_to_usd(cost)
                if record:
                    headers["X-Tollgate-Record-Time"] = repr(record["created_at"])
                    headers["X-Tollgate-Record-Sig"] = sign_record(secret, claims["aid"], record)
                return JSONResponse(answer, headers=headers)
            except (ProviderFailure, httpx.HTTPError, TimeoutError) as exc:
                failure = (
                    exc
                    if isinstance(exc, ProviderFailure)
                    else ProviderFailure("provider_timeout", "The provider result was interrupted.", 504)
                )
                await asyncio.to_thread(store.fail, record_id, failure.no_charge)
                return error_response(request.scope, failure.status, failure.code, failure.message, headers=headers)
            finally:
                with anyio.CancelScope(shield=True):
                    await upstream.aclose()
                    await asyncio.to_thread(store.fail, record_id, False)

        async def stream_response():
            seen = False
            try:
                async with asyncio.timeout_at(deadline):
                    async for event in provider.events(upstream, payload.model):
                        if event is None:
                            if not seen:
                                raise ProviderFailure("missing_usage", "Stream finished without usage.")
                            yield "data: [DONE]\n\n"
                            return
                        if event.get("usage") is not None:
                            await asyncio.to_thread(store.complete, record_id, event["usage"], spec)
                            seen = True
                        yield "data: " + json.dumps(event) + "\n\n"
            except (ProviderFailure, httpx.HTTPError, TimeoutError) as exc:
                failure = (
                    exc
                    if isinstance(exc, ProviderFailure)
                    else ProviderFailure("provider_timeout", "The stream was interrupted.", 504)
                )
                await asyncio.to_thread(store.fail, record_id, failure.no_charge)
                yield "data: " + json.dumps(
                    {"error": {"code": failure.code, "message": failure.message, "record_id": record_id}}
                ) + "\n\n"
            finally:
                with anyio.CancelScope(shield=True):
                    await upstream.aclose()
                    await asyncio.to_thread(store.fail, record_id, False)

        return StreamingResponse(
            stream_response(),
            media_type="text/event-stream",
            headers={**headers, "X-Accel-Buffering": "no"},
        )

    app.state.store = store
    return app
