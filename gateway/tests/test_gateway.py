"""Correctness tests: run from gateway/ with python -m unittest discover -s tests -v."""

import asyncio
import contextlib
import hashlib
import json
import multiprocessing
import os
import sqlite3
import sys
import tempfile
import time
import unittest
import uuid
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

import httpx
from pydantic import ValidationError

from app.config import Settings
from app.database import Database
from app.models.request import ChatCompletionRequest
from app.routing.providers import MODELS, ModelSpec
from app.services.ledger import BillingError, Ledger
from app.services.pricing import (
    calculate_cost,
    micro_to_usd,
    simulated_input_tokens,
    usd_to_micro,
)

ADMIN_KEY, PROVIDER_KEY = "admin_" + "a" * 40, "provider_" + "b" * 40
_import_temp = tempfile.TemporaryDirectory()
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
with patch.dict(
    os.environ,
    {
        "TOLLGATE_DEVELOPMENT_API_KEY": ADMIN_KEY,
        "TOLLGATE_PROVIDER_API_KEY": PROVIDER_KEY,
        "TOLLGATE_SIMULATOR_DATABASE_PATH": str(
            Path(_import_temp.name) / "import-simulator.db"
        ),
    },
):
    from app.main import create_app
    from simulator.main import create_simulator


def reserve_in_process(arguments):
    """Use a separate process and connection to contend for the same wallet."""
    path, auth = arguments
    ledger = Ledger(Database(Path(path)))
    try:
        ledger.reserve(
            "req_" + uuid.uuid4().hex,
            auth,
            uuid.uuid4().hex,
            "same-test-prompt",
            ModelSpec("sim-basic", 1_000_000, 1_000_000),
            "concurrency",
            400_000,
            400_000,
        )
        return "accepted"
    except BillingError as exc:
        return exc.code


class LedgerTests(unittest.TestCase):
    def setUp(self):
        """Create real temporary disk storage and a funded customer for each test."""
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "wallet.db"
        self.database = Database(self.path)
        self.database.initialize()
        self.ledger = Ledger(self.database)
        self.customer = self.ledger.create_customer("Test customer")["id"]
        self.key = self.ledger.create_key(self.customer, "Test", ["sim-basic"])
        self.auth = self.ledger.authenticate(self.key["api_key"].encode())
        self.ledger.grant(self.customer, 1_000_000, "initial")

    def tearDown(self):
        """Remove only this test's temporary database."""
        self.temp.cleanup()

    def reserve(self, request_id="req_test"):
        """Reserve $0.80 against a $1 wallet using explicit test rates."""
        return self.ledger.reserve(
            request_id,
            self.auth,
            request_id,
            "fingerprint",
            ModelSpec("sim-basic", 1_000_000, 1_000_000),
            "test",
            400_000,
            400_000,
        )

    def test_concurrent_processes_admit_only_one_affordable_request(self):
        """Independent processes must not both spend the same available credits."""
        with ProcessPoolExecutor(
            max_workers=4, mp_context=multiprocessing.get_context("spawn")
        ) as pool:
            outcomes = list(
                pool.map(reserve_in_process, [(str(self.path), self.auth)] * 16)
            )
        self.assertEqual(outcomes.count("accepted"), 1)
        self.assertEqual(outcomes.count("insufficient_credits"), 15)
        balance = self.ledger.balance(self.customer)
        self.assertEqual(balance["reserved_usd"], "0.800000")
        self.assertEqual(balance["available_usd"], "0.200000")

    def test_repeated_settlement_charges_once(self):
        """Many workers retrying one job must produce exactly one charge."""
        self.reserve()
        self.assertTrue(self.ledger.dispatch("req_test"))
        self.ledger.record_usage("req_test", 100_000, 200_000)
        with ThreadPoolExecutor(max_workers=12) as pool:
            results = list(
                pool.map(lambda _: self.ledger.settle("req_test"), range(32))
            )
        self.assertEqual(sum(results), 1)
        self.assertEqual(
            self.ledger.balance(self.customer)["available_usd"], "0.700000"
        )
        self.assertEqual(self.ledger.balance(self.customer)["reserved_usd"], "0.000000")
        self.assertEqual(
            len(
                [
                    row
                    for row in self.ledger.overview()["ledger"]
                    if row["kind"] == "charge"
                ]
            ),
            1,
        )

    def test_settlement_rolls_back_as_one_operation(self):
        """A failure after ledger insertion must not leave a partial debit."""
        self.reserve()
        self.ledger.dispatch("req_test")
        self.ledger.record_usage("req_test", 100_000, 200_000)
        with self.database.connection() as connection:
            connection.execute(
                "CREATE TRIGGER fail_wallet BEFORE UPDATE ON wallets BEGIN SELECT RAISE(ABORT,'test failure'); END"
            )
        with self.assertRaises(sqlite3.IntegrityError):
            self.ledger.settle("req_test")
        self.assertEqual(self.ledger.request("req_test")["state"], "usage_recorded")
        self.assertEqual(self.ledger.balance(self.customer)["balance_usd"], "1.000000")
        self.assertEqual(len(self.ledger.overview()["ledger"]), 1)
        with self.database.connection() as connection:
            connection.execute("DROP TRIGGER fail_wallet")
        self.assertTrue(self.ledger.settle("req_test"))

    def test_grants_and_requests_are_idempotent(self):
        """Retry references cannot create duplicate credits or duplicate holds."""
        with ThreadPoolExecutor(max_workers=8) as pool:
            list(
                pool.map(
                    lambda _: self.ledger.grant(self.customer, 123, "same-grant"),
                    range(20),
                )
            )
        self.assertEqual(self.ledger.balance(self.customer)["balance_usd"], "1.000123")
        with self.assertRaises(BillingError):
            self.ledger.grant(self.customer, 124, "same-grant")
        self.reserve()
        with self.assertRaises(BillingError) as error:
            self.reserve()
        self.assertEqual(error.exception.code, "duplicate_request")
        self.assertEqual(self.ledger.balance(self.customer)["reserved_usd"], "0.800000")

    def test_undispatched_expiry_and_dispatch_fence(self):
        """A reclaimed undispatched reservation can never proceed to provider dispatch."""
        self.reserve()
        with self.database.transaction() as connection:
            connection.execute("UPDATE requests SET updated_at=0 WHERE id='req_test'")
        self.ledger.recovery_candidates(1)
        self.assertEqual(self.ledger.request("req_test")["state"], "released")
        self.assertFalse(self.ledger.dispatch("req_test"))
        self.assertEqual(
            self.ledger.balance(self.customer)["available_usd"], "1.000000"
        )

    def test_unknown_dispatch_keeps_credits_reserved(self):
        """Elapsed time cannot justify refunding work that may already be billable."""
        self.reserve()
        self.ledger.dispatch("req_test")
        self.ledger.unresolved("req_test", "timeout")
        with self.database.transaction() as connection:
            connection.execute("UPDATE requests SET updated_at=0 WHERE id='req_test'")
        candidates = self.ledger.recovery_candidates(1)
        self.assertEqual(len(candidates), 1)
        self.assertFalse(self.ledger.release("req_test", "expired"))
        self.assertEqual(self.ledger.balance(self.customer)["reserved_usd"], "0.800000")

    def test_excess_usage_blocks_settlement(self):
        """Unexpected usage must stay visible without overcharging the wallet."""
        self.reserve()
        self.ledger.dispatch("req_test")
        self.ledger.record_usage("req_test", 400_001, 400_000)
        self.assertFalse(self.ledger.settle("req_test"))
        self.assertEqual(
            self.ledger.request("req_test")["error_code"], "usage_exceeds_reservation"
        )
        self.assertEqual(self.ledger.balance(self.customer)["reserved_usd"], "0.800000")

    def test_revocation_model_rules_and_caps(self):
        """Account controls are rechecked inside the reservation transaction."""
        self.ledger.set_cap(self.customer, 700_000)
        with self.assertRaises(BillingError) as error:
            self.reserve()
        self.assertEqual(error.exception.code, "spend_cap_exceeded")
        self.ledger.set_cap(self.customer, None)
        self.ledger.revoke_key(self.key["id"])
        with self.assertRaises(BillingError) as error:
            self.reserve()
        self.assertEqual(error.exception.code, "invalid_api_key")

    def test_reopened_database_preserves_queue_and_immutable_ledger(self):
        """A new ledger instance resumes a saved job and entries remain append-only."""
        self.reserve()
        self.ledger.dispatch("req_test")
        self.ledger.record_usage("req_test", 100_000, 200_000)
        reopened = Ledger(Database(self.path))
        self.assertEqual(reopened.pending_jobs(), ["req_test"])
        reopened.settle("req_test")
        with self.database.connection() as connection:
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute("DELETE FROM ledger")
            total = connection.execute(
                "SELECT SUM(amount_micro) FROM ledger WHERE customer_id=?",
                (self.customer,),
            ).fetchone()[0]
            wallet = connection.execute(
                "SELECT balance_micro FROM wallets WHERE customer_id=?",
                (self.customer,),
            ).fetchone()[0]
            self.assertEqual(total, wallet)

    def test_integer_pricing_and_version_snapshot(self):
        """Exact decimal conversion and saved rates must survive later catalog changes."""
        self.assertEqual(usd_to_micro("0.000001"), 1)
        self.assertEqual(micro_to_usd(1), "0.000001")
        self.assertEqual(calculate_cost(1, 0, 500_000, 0), 1)
        self.assertEqual(calculate_cost(1000, 200, 2_000_000, 8_000_000), 3600)
        for invalid in ("0.0000001", "-1", "1e3", "NaN"):
            with self.assertRaises(ValueError):
                usd_to_micro(invalid)
        self.reserve()
        self.ledger.dispatch("req_test")
        self.ledger.record_usage("req_test", 100_000, 200_000)
        self.assertEqual(self.ledger.request("req_test")["cost_usd"], "0.300000")


class GatewayTests(unittest.IsolatedAsyncioTestCase):
    @contextlib.asynccontextmanager
    async def workspace(self, scenario="normal", initial_credit="1.00"):
        """Run an isolated gateway and simulator against real temporary disk files."""
        with tempfile.TemporaryDirectory() as directory:
            simulator_path = Path(directory) / "simulator.db"
            simulator = create_simulator(
                PROVIDER_KEY, simulator_path, scenario=scenario, delay=0
            )
            settings = Settings(
                _env_file=None,
                environment="test",
                development_api_key=ADMIN_KEY,
                provider_api_key=PROVIDER_KEY,
                database_path=Path(directory) / "wallet.db",
                worker_enabled=False,
                max_request_bytes=4096,
                provider_timeout_seconds=1,
                recovery_after_seconds=16,
            )
            app = create_app(
                settings, provider_transport=httpx.ASGITransport(app=simulator)
            )
            async with app.router.lifespan_context(app):
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
                    base_url="http://testserver",
                ) as client:
                    owner = {"Authorization": "Bearer " + ADMIN_KEY}
                    customer = (
                        await client.post(
                            "/admin/customers",
                            json={"name": "Test customer"},
                            headers=owner,
                        )
                    ).json()["id"]
                    key = (
                        await client.post(
                            "/admin/keys",
                            json={
                                "customer_id": customer,
                                "allowed_models": ["sim-basic"],
                            },
                            headers=owner,
                        )
                    ).json()["api_key"]
                    if initial_credit != "0":
                        grant = await client.post(
                            "/admin/credits",
                            json={
                                "customer_id": customer,
                                "amount_usd": initial_credit,
                                "reference": "initial",
                            },
                            headers=owner,
                        )
                        self.assertEqual(grant.status_code, 200)
                    yield client, app, customer, key, owner, simulator_path

    @staticmethod
    def payload(stream=False):
        """Keep the baseline request consistent across HTTP integration cases."""
        return {
            "model": "sim-basic",
            "messages": [{"role": "user", "content": "Hello Tollgate"}],
            "max_completion_tokens": 256,
            "stream": stream,
        }

    async def test_json_usage_cost_and_durable_settlement(self):
        """A real HTTP adapter flow must replace the old 501 with an answer and charge."""
        async with self.workspace() as (client, app, customer, key, owner, _):
            response = await client.post(
                "/v1/chat/completions",
                json=self.payload(),
                headers={
                    "Authorization": "Bearer " + key,
                    "Idempotency-Key": "json-demo",
                },
            )
            self.assertEqual(response.status_code, 200, response.text)
            self.assertIn(
                "simulated", response.json()["choices"][0]["message"]["content"]
            )
            usage = response.json()["usage"]
            expected = calculate_cost(
                usage["prompt_tokens"], usage["completion_tokens"], 2_000_000, 8_000_000
            )
            self.assertEqual(
                response.headers["X-Tollgate-Cost-USD"], micro_to_usd(expected)
            )
            request_id = response.headers["X-Tollgate-Request-ID"]
            self.assertEqual(
                app.state.ledger.request(request_id)["state"], "usage_recorded"
            )
            self.assertEqual((await app.state.worker.tick())["settled"], 1)
            self.assertEqual(
                app.state.ledger.balance(customer)["available_usd"],
                micro_to_usd(1_000_000 - expected),
            )
            retry = await client.post(
                "/v1/chat/completions",
                json=self.payload(),
                headers={
                    "Authorization": "Bearer " + key,
                    "Idempotency-Key": "json-demo",
                },
            )
            self.assertEqual(retry.status_code, 409)
            self.assertEqual(
                retry.json()["error"]["context"]["original_request_id"], request_id
            )

    async def test_sse_final_usage_is_settled(self):
        """Streaming must deliver content, final usage, and the completion marker."""
        async with self.workspace() as (client, app, customer, key, *_):
            response = await client.post(
                "/v1/chat/completions",
                json=self.payload(True),
                headers={"Authorization": "Bearer " + key},
            )
            self.assertEqual(response.status_code, 200, response.text)
            self.assertIn("text/event-stream", response.headers["content-type"])
            self.assertIn("[DONE]", response.text)
            self.assertIn('"prompt_tokens"', response.text)
            await app.state.worker.tick()
            self.assertEqual(
                app.state.ledger.request(response.headers["X-Tollgate-Request-ID"])[
                    "state"
                ],
                "settled",
            )

    async def test_concurrent_http_requests_do_not_overspend(self):
        """Only one request may reserve the entire affordable amount before any settlement."""
        payload = self.payload()
        tokens = simulated_input_tokens(
            ChatCompletionRequest.model_validate(payload).messages
        )
        required = calculate_cost(tokens, 256, 2_000_000, 8_000_000)
        async with self.workspace(initial_credit=micro_to_usd(required)) as (
            client,
            app,
            customer,
            key,
            owner,
            simulator_path,
        ):
            responses = await asyncio.gather(
                *(
                    client.post(
                        "/v1/chat/completions",
                        json=payload,
                        headers={
                            "Authorization": "Bearer " + key,
                            "Idempotency-Key": str(uuid.uuid4()),
                        },
                    )
                    for _ in range(20)
                )
            )
            self.assertEqual(
                [response.status_code for response in responses].count(200), 1
            )
            self.assertEqual(
                [response.status_code for response in responses].count(402), 19
            )
            with contextlib.closing(sqlite3.connect(simulator_path)) as connection:
                self.assertEqual(
                    connection.execute("SELECT COUNT(*) FROM results").fetchone()[0], 1
                )

    async def test_interrupted_stream_recovers_from_durable_provider_usage(self):
        """An interrupted answer stays reserved until stored simulator usage is recovered."""
        async with self.workspace("disconnect") as (client, app, customer, key, *_):
            response = await client.post(
                "/v1/chat/completions",
                json=self.payload(True),
                headers={"Authorization": "Bearer " + key},
            )
            request_id = response.headers["X-Tollgate-Request-ID"]
            self.assertIn("incomplete_provider_stream", response.text)
            self.assertEqual(
                app.state.ledger.request(request_id)["state"], "unresolved"
            )
            with app.state.ledger.database.transaction() as connection:
                connection.execute(
                    "UPDATE requests SET updated_at=0 WHERE id=?", (request_id,)
                )
            result = await app.state.worker.tick()
            self.assertEqual(result, {"recovered": 1, "settled": 1})
            self.assertEqual(
                app.state.ledger.balance(customer)["reserved_usd"], "0.000000"
            )

    async def test_rejection_releases_but_malformed_reply_requires_recovery(self):
        """Release only an explicit no-charge rejection, not an invalid response after work."""
        for scenario in ("reject", "malformed", "missing_usage"):
            async with self.workspace(scenario) as (client, app, customer, key, *_):
                response = await client.post(
                    "/v1/chat/completions",
                    json=self.payload(),
                    headers={"Authorization": "Bearer " + key},
                )
                self.assertEqual(response.status_code, 502, response.text)
                record = app.state.ledger.request(
                    response.headers["X-Tollgate-Request-ID"]
                )
                self.assertEqual(
                    record["state"],
                    "released" if scenario == "reject" else "unresolved",
                )

    async def test_admin_customer_separation_and_model_access(self):
        """Admin credentials must not generate answers, and customer keys cannot administer."""
        async with self.workspace() as (client, app, customer, key, owner, _):
            self.assertEqual(
                (
                    await client.post(
                        "/v1/chat/completions", json=self.payload(), headers=owner
                    )
                ).status_code,
                401,
            )
            self.assertEqual(
                (
                    await client.get(
                        "/admin/overview", headers={"Authorization": "Bearer " + key}
                    )
                ).status_code,
                401,
            )
            denied = await client.post(
                "/v1/chat/completions",
                json={**self.payload(), "model": "sim-deep"},
                headers={"Authorization": "Bearer " + key},
            )
            self.assertEqual(denied.status_code, 403)
            overview = (await client.get("/admin/overview", headers=owner)).text
            self.assertNotIn(key, overview)
            self.assertNotIn(ADMIN_KEY, overview)
            self.assertNotIn(PROVIDER_KEY, overview)

    async def test_customer_cannot_inspect_another_customers_request(self):
        """Stable request IDs do not bypass ownership checks."""
        async with self.workspace() as (client, app, customer, key, owner, _):
            response = await client.post(
                "/v1/chat/completions",
                json=self.payload(),
                headers={"Authorization": "Bearer " + key},
            )
            other = app.state.ledger.create_customer("Other customer")["id"]
            other_key = app.state.ledger.create_key(other, "Other", ["sim-basic"])[
                "api_key"
            ]
            denied = await client.get(
                "/v1/requests/" + response.headers["X-Tollgate-Request-ID"],
                headers={"Authorization": "Bearer " + other_key},
            )
            self.assertEqual(denied.status_code, 404)

    async def test_body_limits_authentication_and_validation(self):
        """Reject invalid or oversized input without creating a billing reservation."""
        async with self.workspace() as (client, app, customer, key, owner, _):
            unauthorized = await client.post(
                "/v1/chat/completions", content=b"x" * 5000
            )
            self.assertEqual(unauthorized.status_code, 401)
            oversized = await client.post(
                "/v1/chat/completions",
                content=b"x" * 5000,
                headers={"Authorization": "Bearer " + key},
            )
            self.assertEqual(oversized.status_code, 413)
            invalid = await client.post(
                "/v1/chat/completions",
                json={**self.payload(), "max_completion_tokens": True},
                headers={"Authorization": "Bearer " + key},
            )
            self.assertEqual(invalid.status_code, 422)
            self.assertEqual(app.state.ledger.overview()["requests"], [])
            page = await client.get("/")
            self.assertEqual(page.status_code, 200)
            self.assertIn('id="mode-badge"', page.text)
        self.assertNotIn("unsafe-inline", page.headers["Content-Security-Policy"])


if __name__ == "__main__":
    unittest.main()
