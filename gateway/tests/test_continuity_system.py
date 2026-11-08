"""Primary + relay working together over real ASGI apps (needs the full FastAPI stack; no paid AI calls)."""

import tempfile
import unittest
from contextlib import AsyncExitStack
from pathlib import Path

import httpx

from test_gateway import ADMIN_KEY, PROVIDER_KEY, create_app, create_simulator
from app.config import Settings
from app.relay import create_relay_app

SECRET = "t" * 48


def make_settings(path, **changes):
    return Settings(
        _env_file=None,
        environment="test",
        development_api_key=ADMIN_KEY,
        provider_api_key=PROVIDER_KEY,
        database_path=path / "shared.db",
        relay_database_path=path / "relay.db",
        allowance_secret=SECRET,
        worker_enabled=False,
        provider_timeout_seconds=1,
        **changes,
    )


class RelayCycleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name)
        self.stack = AsyncExitStack()
        self.settings = make_settings(self.path)
        self.simulator = create_simulator(PROVIDER_KEY, self.path / "provider.db", delay=0)
        transport = httpx.ASGITransport(app=self.simulator)
        self.primary = create_app(self.settings, provider_transport=transport)
        self.relay = create_relay_app(self.settings, provider_transport=transport)
        await self.stack.enter_async_context(self.primary.router.lifespan_context(self.primary))
        await self.stack.enter_async_context(self.relay.router.lifespan_context(self.relay))
        self.api = await self.stack.enter_async_context(
            httpx.AsyncClient(transport=httpx.ASGITransport(app=self.primary, raise_app_exceptions=False), base_url="http://testserver")
        )
        self.rly = await self.stack.enter_async_context(
            httpx.AsyncClient(transport=httpx.ASGITransport(app=self.relay, raise_app_exceptions=False), base_url="http://testserver")
        )
        ledger = self.primary.state.ledger
        self.customer = ledger.create_customer("Sohaib")["id"]
        self.key = ledger.create_key(self.customer, "app", ["sim-basic"])["api_key"]
        ledger.grant(self.customer, 1_000_000, "start")
        self.auth = {"Authorization": "Bearer " + self.key}
        self.body = {"model": "sim-basic", "messages": [{"role": "user", "content": "Explain credit holds"}], "max_completion_tokens": 64}

    async def asyncTearDown(self):
        await self.stack.aclose()
        self.temp.cleanup()

    async def allowance(self, amount="0.40"):
        response = await self.api.post("/v1/allowance", headers=self.auth, json={"amount_usd": amount, "ttl_minutes": 60, "models": ["sim-basic"]})
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()

    async def test_outage_cycle_charges_exactly_once(self):
        allowance = await self.allowance()
        bearer = {"Authorization": "Bearer " + allowance["token"]}
        answer = await self.rly.post("/v1/chat/completions", headers=bearer, json=self.body)
        self.assertEqual(answer.status_code, 200, answer.text)
        self.assertEqual(answer.headers["X-Tollgate-Mode"], "relay")
        records = (await self.rly.get("/v1/offline/records", headers=bearer)).json()
        self.assertEqual(len(records["records"]), 1)
        sync = {"allowance_id": allowance["allowance_id"], "records": records["records"], "close": True}
        first = await self.api.post("/v1/offline/sync", headers=self.auth, json=sync)
        again = await self.api.post("/v1/offline/sync", headers=self.auth, json=sync)
        self.assertEqual((first.json()["accepted"], again.json()["accepted"]), (1, 0))
        wallet = (await self.api.get("/v1/balance", headers=self.auth)).json()
        self.assertEqual(wallet["reserved_usd"], "0.000000")
        self.assertNotEqual(wallet["spent_usd"], "0.000000")
        reconcile = await self.api.get("/admin/reconciliation", headers={"Authorization": "Bearer " + ADMIN_KEY})
        self.assertTrue(reconcile.json()["ok"])

    async def test_relay_refuses_forged_expired_or_exhausted(self):
        forged = await self.rly.post("/v1/chat/completions", headers={"Authorization": "Bearer tga1.x.y"}, json=self.body)
        self.assertEqual(forged.status_code, 401)
        tiny = await self.allowance("0.0004")
        spent = await self.rly.post("/v1/chat/completions", headers={"Authorization": "Bearer " + tiny["token"]}, json=self.body)
        self.assertEqual(spent.status_code, 402)
        self.assertEqual(spent.json()["error"]["code"], "allowance_exhausted")

    async def test_relay_rejects_model_outside_allowance(self):
        allowance = await self.allowance()
        body = {**self.body, "model": "sim-deep"}
        response = await self.rly.post("/v1/chat/completions", headers={"Authorization": "Bearer " + allowance["token"]}, json=body)
        self.assertEqual(response.status_code, 403)

    async def test_warden_role_serves_allowance_but_control_does_not(self):
        control = create_app(self.settings.model_copy(update={"service_role": "control", "node_id": "control-1"}))
        async with control.router.lifespan_context(control):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=control), base_url="http://testserver") as client:
                response = await client.post("/v1/allowance", headers=self.auth, json={"amount_usd": "0.10", "ttl_minutes": 60, "models": ["sim-basic"]})
        self.assertEqual(response.status_code, 404)


if __name__ == "__main__":
    unittest.main()
