"""Exercise separate wardens sharing one real database without paid AI calls."""

import asyncio
import json
import sqlite3
import tempfile
import unittest
import uuid
from contextlib import AsyncExitStack, closing
from pathlib import Path
from unittest.mock import patch

import httpx

from test_gateway import ADMIN_KEY, PROVIDER_KEY, create_app, create_simulator
from app.config import Settings
from app.database import Database
from app.models.request import ChatCompletionRequest
from app.services.cluster import Cluster
from app.services.ledger import Ledger, BillingError
from app.services.patrol import Patrol
from app.services.pricing import calculate_cost, simulated_input_tokens


class ClusterTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name)
        self.stack = AsyncExitStack()
        self.settings = Settings(
            _env_file=None,
            environment="test",
            development_api_key=ADMIN_KEY,
            provider_api_key=PROVIDER_KEY,
            database_path=self.path / "shared.db",
            worker_enabled=False,
            provider_timeout_seconds=1,
            recovery_after_seconds=16,
        )
        self.simulator = create_simulator(
            PROVIDER_KEY, self.path / "provider.db", delay=0
        )
        self.control, self.owner = await self.start("control", "control-1")
        self.ledger = self.control.state.ledger
        self.customer = self.ledger.create_customer("API customer")["id"]
        self.key = self.ledger.create_key(self.customer, "Backend", ["sim-basic"])[
            "api_key"
        ]
        self.ledger.grant(self.customer, 1_000_000, "initial")
        self.ledger.heartbeat()
        self.one, self.api1 = await self.start("warden", "warden-1")
        self.two, self.api2 = await self.start("warden", "warden-2")
        self.headers = {"Authorization": "Bearer " + self.key}
        self.admin = {"Authorization": "Bearer " + ADMIN_KEY}

    async def start(self, role, node):
        settings = self.settings.model_copy(
            update={"service_role": role, "node_id": node}
        )
        app = create_app(
            settings, provider_transport=httpx.ASGITransport(app=self.simulator)
        )
        await self.stack.enter_async_context(app.router.lifespan_context(app))
        client = await self.stack.enter_async_context(
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
                base_url="http://testserver",
            )
        )
        return app, client

    async def asyncTearDown(self):
        await self.stack.aclose()
        self.temp.cleanup()

    def payload(self, stream=False):
        return {
            "model": "sim-basic",
            "messages": [{"role": "user", "content": "Explain credit holds"}],
            "max_completion_tokens": 64,
            "stream": stream,
        }

    async def generate(self, client=None, operation=None, stream=False):
        return await (client or self.api1).post(
            "/v1/chat/completions",
            json=self.payload(stream),
            headers={**self.headers, "Idempotency-Key": operation or uuid.uuid4().hex},
        )

    def provider_calls(self):
        with closing(sqlite3.connect(self.path / "provider.db")) as connection:
            return connection.execute("SELECT COUNT(*) FROM results").fetchone()[0]

    async def test_roles_block_admin_on_wardens_and_generation_on_control(self):
        for path in ("/", "/admin/overview", "/auth/session", "/webhooks/stripe"):
            response = await self.api1.get(path, headers=self.admin)
            self.assertEqual(response.status_code, 404, path)
        self.assertEqual((await self.generate(self.owner)).status_code, 404)
        self.assertEqual(
            (await self.owner.get("/admin/cluster", headers=self.admin)).status_code,
            200,
        )
        schema = (await self.api1.get("/openapi.json")).json()
        self.assertNotIn("/admin/keys", schema["paths"])
        self.assertIn("/v1/chat/completions", schema["paths"])

    async def test_control_outage_fail_open_preserves_api_and_accounting(self):
        self.control.state.cluster.leave()
        result = await self.generate()
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual(result.headers["X-Tollgate-Control-Status"], "degraded")
        row = self.ledger.request(result.headers["X-Tollgate-Request-ID"])
        self.assertEqual(row["warden_id"], "warden-1")
        self.assertEqual(row["control_degraded"], 1)
        await self.two.state.worker.tick()
        self.assertEqual(self.ledger.request(row["id"])["state"], "settled")
        self.assertTrue(self.ledger.reconciliation()["ok"])

    async def test_fail_closed_blocks_when_control_is_unavailable(self):
        self.one.state.cluster.settings = self.one.state.cluster.settings.model_copy(
            update={"control_failure_policy": "fail_closed"}
        )
        self.control.state.cluster.leave()
        response = await self.generate()
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["error"]["code"], "control_unavailable")
        self.assertEqual(self.provider_calls(), 0)
        self.assertEqual((await self.api1.get("/ready")).status_code, 503)

    async def test_fail_open_does_not_allow_invalid_credentials(self):
        self.control.state.cluster.leave()
        response = await self.api1.post(
            "/v1/chat/completions",
            json=self.payload(),
            headers={"Authorization": "Bearer " + "x" * 40},
        )
        self.assertEqual(response.status_code, 401)
        self.assertEqual(self.provider_calls(), 0)

    async def test_fail_open_does_not_allow_unfunded_generation(self):
        self.control.state.cluster.leave()
        customer = self.ledger.create_customer("Empty")["id"]
        key = self.ledger.create_key(customer, "Empty", ["sim-basic"])["api_key"]
        response = await self.api1.post(
            "/v1/chat/completions",
            json=self.payload(),
            headers={"Authorization": "Bearer " + key},
        )
        self.assertEqual(response.status_code, 402)
        self.assertEqual(self.provider_calls(), 0)

    async def test_two_wardens_cannot_reserve_the_same_money(self):
        customer = self.ledger.create_customer("One request only")["id"]
        key = self.ledger.create_key(customer, "Shared", ["sim-basic"])["api_key"]
        count = simulated_input_tokens(
            ChatCompletionRequest.model_validate(self.payload()).messages
        )
        required = calculate_cost(count, 64, 2_000_000, 8_000_000)
        self.ledger.grant(customer, required, "exact")
        responses = await asyncio.gather(
            *[
                api.post(
                    "/v1/chat/completions",
                    json=self.payload(),
                    headers={
                        "Authorization": "Bearer " + key,
                        "Idempotency-Key": str(i),
                    },
                )
                for i, api in enumerate([self.api1, self.api2] * 6)
            ]
        )
        codes = [r.status_code for r in responses]
        self.assertEqual(codes.count(200), 1, codes)
        self.assertEqual(codes.count(402), 11, codes)
        self.assertEqual(self.provider_calls(), 1)

    async def test_repeated_operation_across_wardens_is_not_generated_twice(self):
        responses = await asyncio.gather(
            self.generate(operation="same"), self.generate(self.api2, operation="same")
        )
        self.assertEqual(sorted(r.status_code for r in responses), [200, 409])
        self.assertEqual(self.provider_calls(), 1)

    async def test_other_warden_can_inspect_and_settle_a_request(self):
        result = await self.generate(stream=True)
        self.assertIn("[DONE]", result.text)
        rid = result.headers["X-Tollgate-Request-ID"]
        response = await self.api2.get("/v1/requests/" + rid, headers=self.headers)
        self.assertEqual(response.json()["warden_id"], "warden-1")
        await self.two.state.worker.tick()
        await self.one.state.worker.tick()
        self.assertEqual(
            len([r for r in self.ledger.overview()["ledger"] if r["kind"] == "charge"]),
            1,
        )

    async def test_drain_blocks_new_work_but_preserves_other_warden(self):
        response = await self.owner.post(
            "/admin/wardens/warden-1/drain",
            json={"draining": True, "reason": "Planned maintenance"},
            headers=self.admin,
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual((await self.api1.get("/ready")).status_code, 503)
        self.assertEqual(
            (await self.generate()).json()["error"]["code"], "warden_draining"
        )
        self.assertEqual((await self.generate(self.api2)).status_code, 200)
        self.control.state.cluster.drain("warden-1", False, "Maintenance finished")
        self.assertEqual((await self.api1.get("/ready")).status_code, 200)

    async def test_customer_cannot_drain_or_read_cluster(self):
        response = await self.owner.get("/admin/cluster", headers=self.headers)
        self.assertEqual(response.status_code, 401)
        response = await self.owner.post(
            "/admin/wardens/warden-1/drain",
            json={"draining": True, "reason": "User must not drain"},
            headers=self.headers,
        )
        self.assertEqual(response.status_code, 401)

    async def test_expired_lease_is_fenced_even_when_control_policy_is_open(self):
        self.one.state.cluster.leave()
        with self.assertRaises(RuntimeError):
            self.one.state.cluster.beat()
        self.assertEqual(
            (await self.generate()).json()["error"]["code"], "warden_unavailable"
        )
        replacement = Cluster(
            self.one.state.ledger.database, self.one.state.cluster.settings
        )
        replacement.claim()
        with self.assertRaises(BillingError):
            self.one.state.cluster.check_admission()
        replacement.leave()

    async def test_duplicate_live_node_id_cannot_replace_lease(self):
        duplicate = Cluster(
            self.one.state.ledger.database, self.one.state.cluster.settings
        )
        with self.assertRaises(RuntimeError):
            duplicate.claim()
        self.assertEqual((await self.generate()).status_code, 200)

    async def test_drain_between_preflight_and_reservation_is_rechecked(self):
        cluster = self.one.state.cluster
        original = cluster.check_admission

        def drain_after_preflight():
            result = original()
            self.control.state.cluster.drain("warden-1", True, "Drain during preflight")
            return result

        with patch.object(
            cluster, "check_admission", side_effect=drain_after_preflight
        ):
            result = await self.generate()
        self.assertEqual(result.status_code, 503)
        self.assertEqual(result.json()["error"]["code"], "warden_draining")
        self.assertEqual(self.provider_calls(), 0)
        self.assertEqual(self.ledger.balance(self.customer)["reserved_usd"], "0.000000")

    async def test_storage_failure_stops_admission_and_readiness(self):
        with patch.object(
            self.one.state.ledger.database,
            "connection",
            side_effect=sqlite3.OperationalError("offline"),
        ):
            self.assertEqual((await self.generate()).status_code, 503)
            self.assertEqual((await self.api1.get("/ready")).status_code, 503)
            self.assertEqual((await self.api1.get("/health")).status_code, 200)
        self.assertEqual(self.provider_calls(), 0)

    async def test_missing_settlement_worker_pauses_new_generation(self):
        with self.ledger.database.transaction() as connection:
            connection.execute(
                "UPDATE service_heartbeats SET seen_at=0 WHERE name='worker'"
            )
        response = await self.generate()
        self.assertEqual(response.json()["error"]["code"], "worker_unavailable")
        self.assertEqual(self.provider_calls(), 0)

    async def test_patrol_flags_missing_nodes_without_releasing_holds(self):
        result = await self.generate()
        before = self.ledger.balance(self.customer)
        self.two.state.cluster.leave()
        patrol = Patrol(
            self.ledger.database,
            self.settings.model_copy(
                update={
                    "node_id": "patrol-1",
                    "expected_wardens": ["warden-1", "warden-2", "warden-3"],
                }
            ),
        )
        patrol.cluster.claim()
        report = patrol.tick()
        self.assertIn(
            {"code": "node_offline", "node_id": "warden-2"}, report["findings"]
        )
        self.assertIn(
            {"code": "node_missing", "node_id": "warden-3"}, report["findings"]
        )
        self.assertEqual(before, self.ledger.balance(self.customer))
        self.assertEqual(
            self.ledger.request(result.headers["X-Tollgate-Request-ID"])["state"],
            "usage_recorded",
        )
        self.assertTrue(self.control.state.cluster.snapshot()["patrol_recent"])
        patrol.cluster.leave()

    async def test_revocation_is_visible_to_both_wardens(self):
        identity = self.ledger.authenticate(self.key.encode())
        self.ledger.revoke_key(identity["key_id"])
        self.assertEqual((await self.generate()).status_code, 401)
        self.assertEqual((await self.generate(self.api2)).status_code, 401)

    async def test_schema_three_migration_preserves_schema_two_balances(self):
        path = self.path / "old.db"
        database = Database(path)
        database.initialize()
        ledger = Ledger(database)
        customer = ledger.create_customer("Existing")["id"]
        ledger.grant(customer, 900_000, "existing")
        with database.connection() as connection:
            connection.execute("ALTER TABLE requests DROP COLUMN warden_id")
            connection.execute("ALTER TABLE requests DROP COLUMN control_degraded")
            connection.execute("DROP TABLE cluster_nodes")
            connection.execute("DROP TABLE patrol_reports")
            connection.execute("PRAGMA user_version=2")
        database.initialize()
        database.initialize()
        database.check_version()
        self.assertEqual(ledger.balance(customer)["balance_usd"], "0.900000")
        self.assertTrue(ledger.reconciliation()["ok"])


if __name__ == "__main__":
    unittest.main()
