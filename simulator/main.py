"""A local fake AI provider with durable usage records and controllable failures."""

import asyncio
import contextlib
import hashlib
import json
import secrets
import sqlite3
import time
from pathlib import Path
from typing import Literal

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parent


@contextlib.contextmanager
def open_results(database_path):
    """Commit on success, roll back on error, and always release the file."""
    connection = sqlite3.connect(database_path, timeout=5)
    try:
        with connection:
            yield connection
    finally:
        connection.close()


class SimulatorSettings(BaseSettings):
    """Read the simulator's key and fault controls from the same gateway/.env file."""

    model_config = SettingsConfigDict(
        env_file=ROOT.parent / "gateway" / ".env",
        env_prefix="TOLLGATE_",
        extra="ignore",
    )
    provider_api_key: SecretStr
    simulator_scenario: Literal[
        "normal", "slow", "reject", "malformed", "disconnect", "missing_usage"
    ] = "normal"
    simulator_delay_seconds: float = Field(default=0.1, ge=0, le=180)
    simulator_database_path: Path = ROOT / "data" / "simulator.db"


class Message(BaseModel):
    """Accept bounded text messages; tools, images, and audio are outside this MVP."""

    model_config = ConfigDict(extra="forbid", strict=True)
    role: Literal["system", "user", "assistant"]
    content: str = Field(min_length=1, max_length=8000)


class CompletionRequest(BaseModel):
    """Use the same deliberately small chat subset as the gateway."""

    model_config = ConfigDict(extra="forbid", strict=True)
    model: Literal["sim-basic", "sim-deep"]
    messages: list[Message] = Field(min_length=1, max_length=50)
    max_completion_tokens: int = Field(default=256, ge=1, le=4096)
    stream: bool = False


def create_simulator(
    key: str, database_path: Path, scenario: str = "normal", delay: float = 0.1
) -> FastAPI:
    """Build a simulator with test-injectable storage and failure behavior."""
    database_path = database_path.resolve()
    database_path.parent.mkdir(parents=True, exist_ok=True)
    with open_results(database_path) as connection:
        connection.execute(
            "CREATE TABLE IF NOT EXISTS results(id TEXT PRIMARY KEY,request_hash TEXT NOT NULL,response TEXT NOT NULL)"
        )
    app = FastAPI(title="Tollgate simulator: no real AI or real provider charges")

    async def authenticate(authorization: str = Header(default="")):
        """Protect usage records with the separate gateway-to-simulator bearer key."""
        expected = hashlib.sha256(("Bearer " + key).encode()).digest()
        supplied = hashlib.sha256(authorization.encode()).digest()
        if not secrets.compare_digest(expected, supplied):
            raise HTTPException(401, "Invalid simulator key")

    def save_result(request_id: str, fingerprint: str, response: dict) -> dict:
        """Persist generation usage before sending any answer bytes to the gateway."""
        with open_results(database_path) as connection:
            connection.execute(
                "INSERT INTO results VALUES (?,?,?) ON CONFLICT(id) DO NOTHING",
                (request_id, fingerprint, json.dumps(response)),
            )
            row = connection.execute(
                "SELECT request_hash,response FROM results WHERE id=?", (request_id,)
            ).fetchone()
            if row[0] != fingerprint:
                raise HTTPException(
                    409, "Simulator request ID was reused for different input"
                )
        return json.loads(row[1])

    def read_result(request_id: str):
        """Read durable usage so the gateway can recover after its own crash."""
        with open_results(database_path) as connection:
            row = connection.execute(
                "SELECT response FROM results WHERE id=?", (request_id,)
            ).fetchone()
        return None if row is None else json.loads(row[0])

    @app.get("/health")
    async def health():
        """Expose the simulator's selected fault scenario for local debugging."""
        return {"status": "ok", "simulated": True, "scenario": scenario}

    @app.get("/requests/{request_id}/usage", dependencies=[Depends(authenticate)])
    async def usage(request_id: str):
        """Return final stored usage; absence is not a promise that no request will arrive."""
        result = await asyncio.to_thread(read_result, request_id)
        if result is None:
            raise HTTPException(404, "Usage is not available")
        return {"usage": result["usage"], "usage_source": "simulated"}

    @app.post("/v1/chat/completions", dependencies=[Depends(authenticate)])
    async def complete(
        payload: CompletionRequest,
        x_tollgate_request_id: str = Header(min_length=1, max_length=128),
    ):
        """Generate deterministic test text and fake counts using a documented rule."""
        if scenario == "reject":
            return JSONResponse(
                status_code=503,
                content={"error": "Deliberate non-billable simulator rejection"},
                headers={"X-Simulator-No-Charge": "true"},
            )
        input_tokens = 4 + sum(
            3 + (len(message.content.encode("utf-8")) + 3) // 4
            for message in payload.messages
        )
        if input_tokens > 40_000:
            return JSONResponse(
                status_code=400,
                content={"error": "Simulator input limit"},
                headers={"X-Simulator-No-Charge": "true"},
            )
        text = (
            "This is a simulated AI answer. Tollgate forwarded your request, reserved credits, "
            "and will settle the reported test usage. No real AI model was called."
        )
        if payload.model == "sim-deep":
            text += " This second test model has a separate price version with higher test rates."
        content = text[: payload.max_completion_tokens * 4]
        output_tokens = (len(content.encode()) + 3) // 4
        result = {
            "id": "chatcmpl-" + x_tollgate_request_id,
            "object": "chat.completion",
            "created": int(time.time()),
            "model": payload.model,
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": content},
                    "finish_reason": "length" if len(content) < len(text) else "stop",
                }
            ],
            "usage": {
                "prompt_tokens": input_tokens,
                "completion_tokens": output_tokens,
                "total_tokens": input_tokens + output_tokens,
            },
        }
        fingerprint = hashlib.sha256(payload.model_dump_json().encode()).hexdigest()
        result = await asyncio.to_thread(
            save_result, x_tollgate_request_id, fingerprint, result
        )
        await asyncio.sleep(delay)
        if scenario == "malformed":
            return JSONResponse({"deliberately": "wrong response shape"})
        if not payload.stream:
            if scenario == "missing_usage":
                return JSONResponse(
                    {key: value for key, value in result.items() if key != "usage"}
                )
            return result

        async def events():
            """Emit SSE pieces; chunks are transport pieces, not individual tokens."""
            common = {key: result[key] for key in ("id", "created", "model")}
            common["object"] = "chat.completion.chunk"
            content = result["choices"][0]["message"]["content"]
            for offset in range(0, len(content), 12):
                event = {
                    **common,
                    "choices": [
                        {
                            "index": 0,
                            "delta": {"content": content[offset : offset + 12]},
                            "finish_reason": None,
                        }
                    ],
                    "usage": None,
                }
                yield "data: " + json.dumps(event) + "\n\n"
                if scenario == "disconnect":
                    return
                await asyncio.sleep(0.02)
            yield "data: " + json.dumps(
                {
                    **common,
                    "choices": [
                        {
                            "index": 0,
                            "delta": {},
                            "finish_reason": result["choices"][0]["finish_reason"],
                        }
                    ],
                    "usage": None,
                }
            ) + "\n\n"
            if scenario != "missing_usage":
                yield "data: " + json.dumps(
                    {**common, "choices": [], "usage": result["usage"]}
                ) + "\n\n"
            yield "data: [DONE]\n\n"

        return StreamingResponse(
            events(),
            media_type="text/event-stream",
            headers={"X-Usage-Source": "simulated"},
        )

    return app


settings = SimulatorSettings()
app = create_simulator(
    settings.provider_api_key.get_secret_value(),
    settings.simulator_database_path,
    settings.simulator_scenario,
    settings.simulator_delay_seconds,
)
