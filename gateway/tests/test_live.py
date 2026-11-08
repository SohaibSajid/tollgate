"""Provider contract, session, money, and signed payment tests without paid calls."""

import asyncio
import hashlib
import hmac
import json
import os
import tempfile
import time
import unittest
import uuid
from dataclasses import replace
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from test_gateway import ADMIN_KEY, PROVIDER_KEY, create_app
import httpx
from app.config import Settings
from app.database import Database
from app.services.ledger import Ledger, BillingError
from app.services.live_provider import openai_usage, anthropic_usage
from app.services.pricing import calculate_cost
from app.models.request import Resolution
from app.routing.providers import MODELS


def stream(*events):
    return b"".join(
        b"event: test\r\ndata: " + json.dumps(event).encode() + b"\r\n\r\n"
        for event in events
    )


class LiveTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.behavior = "normal"
        self.failure_event = None
        self.calls = []
        self.settings = Settings(
            _env_file=None,
            environment="test",
            mode="live",
            development_api_key=ADMIN_KEY,
            provider_api_key=PROVIDER_KEY,
            openai_api_key="test-openai-provider-key",
            anthropic_api_key="test-anthropic-provider-key",
            database_path=Path(self.tmp.name) / "live.db",
            worker_enabled=False,
            stripe_secret_key="sk_test_placeholder",
            stripe_webhook_secret="whsec_test_placeholder",
            openai_store_responses=True,
        )
        self.app = create_app(
            self.settings, provider_transport=httpx.MockTransport(self.provider)
        )
        self.life = self.app.router.lifespan_context(self.app)
        await self.life.__aenter__()
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app),
            base_url="http://127.0.0.1:8000",
        )
        self.ledger = self.app.state.ledger
        self.customer = self.ledger.create_customer("Live test")
        self.key = self.ledger.create_key(
            self.customer["id"], "test", list(self.app.state.models)
        )["api_key"]
        self.ledger.grant(self.customer["id"], 1_000_000, "seed")

    async def asyncTearDown(self):
        await self.client.aclose()
        await self.life.__aexit__(None, None, None)
        self.tmp.cleanup()

    def provider(self, request):
        self.calls.append((request.method, request.url.path))
        if request.url.path.endswith("input_tokens") or request.url.path.endswith(
            "count_tokens"
        ):
            return httpx.Response(200, json={"input_tokens": 21})
        if request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "id": "resp_test",
                    "status": "completed",
                    "service_tier": "default",
                    "usage": {
                        "input_tokens": 21,
                        "output_tokens": 38,
                        "input_tokens_details": {"cached_tokens": 10},
                    },
                },
            )
        if self.behavior == "reject":
            return httpx.Response(429, json={"error": "rate"})
        if self.behavior == "ambiguous":
            return httpx.Response(503)
        if self.failure_event is not None:
            return httpx.Response(
                200,
                content=stream(self.failure_event),
                headers={"content-type": "text/event-stream", "x-request-id": "req_provider_test"},
            )
        if request.url.host == "api.openai.com":
            self.assertNotIn("test-openai-provider-key", request.content.decode())
            data = stream(
                {"type": "response.created", "response": {"id": "resp_test"}},
                {"type": "response.output_text.delta", "delta": "Hello world"},
            )
            if self.behavior != "broken":
                data += stream(
                    {
                        "type": "response.completed",
                        "response": {
                            "id": "resp_test",
                            "service_tier": "default",
                            "usage": {
                                "input_tokens": 21,
                                "output_tokens": 38,
                                "input_tokens_details": {"cached_tokens": 10},
                            },
                        },
                    }
                )
        else:
            data = stream(
                {
                    "type": "message_start",
                    "message": {
                        "id": "msg_test",
                        "usage": {"input_tokens": 21, "output_tokens": 1},
                    },
                },
                {
                    "type": "content_block_delta",
                    "delta": {"type": "text_delta", "text": "Hello Claude"},
                },
                {
                    "type": "message_delta",
                    "delta": {"stop_reason": "end_turn"},
                    "usage": {"output_tokens": 38},
                },
                {"type": "message_stop"},
            )
        return httpx.Response(
            200, content=data, headers={"content-type": "text/event-stream"}
        )

    async def chat(self, model="gpt-5.4-mini", stream=False, idem=None):
        return await self.client.post(
            "/v1/chat/completions",
            headers={
                "Authorization": "Bearer " + self.key,
                "Idempotency-Key": idem or uuid.uuid4().hex,
            },
            json={
                "model": model,
                "messages": [{"role": "user", "content": "Hi"}],
                "max_completion_tokens": 256,
                "stream": stream,
            },
        )

    async def test_openai_json_cached_usage_and_single_settlement(self):
        response = await self.chat()
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(
            response.json()["choices"][0]["message"]["content"], "Hello world"
        )
        request_id = response.headers["x-tollgate-request-id"]
        record = self.ledger.request(request_id)
        self.assertEqual(record["provider_response_id"], "resp_test")
        self.assertEqual(record["cost_usd"], "0.000180")
        self.assertEqual(record["cached_tokens"], 10)
        await self.app.state.worker.tick()
        await self.app.state.worker.tick()
        self.assertEqual(
            self.ledger.balance(self.customer["id"])["available_usd"], "0.999820"
        )
        self.assertTrue(self.ledger.reconciliation()["ok"])

    async def test_live_stream_and_duplicate_do_not_generate_twice(self):
        first = await self.chat(stream=True, idem="one-operation")
        self.assertEqual(first.status_code, 200)
        self.assertIn("Hello world", first.text)
        self.assertIn("[DONE]", first.text)
        second = await self.chat(stream=True, idem="one-operation")
        self.assertEqual(second.status_code, 409)
        self.assertEqual(self.calls.count(("POST", "/v1/responses")), 1)

    async def test_claude_reserves_context_but_charges_reported_usage(self):
        result = await self.chat(model="claude-haiku-4-5", stream=True)
        self.assertEqual(result.status_code, 200, result.text)
        self.assertIn("Hello Claude", result.text)
        record = self.ledger.request(result.headers["x-tollgate-request-id"])
        self.assertEqual(record["input_tokens"], 21)
        self.assertEqual(record["output_tokens"], 38)
        self.assertEqual(record["cost_usd"], "0.000211")
        self.assertEqual(record["reserved_usd"], "0.251280")
        await self.app.state.worker.tick()
        self.assertTrue(self.ledger.reconciliation()["ok"])

    async def test_rejected_request_releases_ambiguous_request_keeps_hold(self):
        self.behavior = "reject"
        result = await self.chat()
        self.assertEqual(result.status_code, 429)
        self.assertEqual(
            self.ledger.balance(self.customer["id"])["reserved_usd"], "0.000000"
        )
        self.behavior = "ambiguous"
        result = await self.chat()
        self.assertEqual(result.status_code, 502)
        self.assertNotEqual(
            self.ledger.balance(self.customer["id"])["reserved_usd"], "0.000000"
        )

    async def test_failed_response_explains_quota_and_preserves_hold(self):
        self.failure_event = {
            "type": "response.failed",
            "response": {"error": {"code": "insufficient_quota", "message": "private prompt sk-secret"}},
        }
        with self.assertLogs("tollgate.providers", level="WARNING") as captured:
            result = await self.chat(stream=True, idem="failed-stream-once")
        self.assertEqual(result.status_code, 200)
        self.assertIn("openai_insufficient_quota", result.text)
        self.assertIn("billing and usage limits", result.text)
        self.assertNotIn("[DONE]", result.text)
        logs = "\n".join(captured.output)
        self.assertIn("upstream_request_id=req_provider_test", logs)
        self.assertIn("provider_code=insufficient_quota", logs)
        self.assertNotIn("private prompt", logs + result.text)
        self.assertNotIn("sk-secret", logs + result.text)
        record = self.ledger.request(result.headers["x-tollgate-request-id"])
        self.assertEqual(record["state"], "unresolved")
        self.assertEqual(record["error_code"], "openai_insufficient_quota")
        self.assertIsNone(record["cost_usd"])
        self.assertNotEqual(self.ledger.balance(self.customer["id"])["reserved_usd"], "0.000000")
        duplicate = await self.chat(stream=True, idem="failed-stream-once")
        self.assertEqual(duplicate.status_code, 409)
        self.assertEqual(self.calls.count(("POST", "/v1/responses")), 1)
        self.assertTrue(self.ledger.reconciliation()["ok"])

    async def test_top_level_error_and_json_client_get_actionable_reason(self):
        self.failure_event = {"type": "error", "code": "rate_limit_exceeded", "message": "private text"}
        result = await self.chat()
        self.assertEqual(result.status_code, 502)
        self.assertEqual(result.json()["error"]["code"], "openai_rate_limit_exceeded")
        self.assertNotIn("private text", result.text)
        record = self.ledger.request(result.headers["x-tollgate-request-id"])
        self.assertEqual(record["state"], "unresolved")
        self.assertTrue(self.ledger.reconciliation()["ok"])

    async def test_claude_error_type_and_unknown_fields_remain_safe(self):
        cases = [
            ({"type": "error", "error": {"type": "overloaded_error"}}, "anthropic_overloaded_error"),
            ({"type": "error", "error": {"code": "private_secret", "message": "private_secret"}}, "provider_stream_error"),
            ({"type": "error", "error": {"code": [], "type": {}}}, "provider_stream_error"),
        ]
        for event, expected in cases:
            self.failure_event = event
            result = await self.chat(model="claude-haiku-4-5", stream=True)
            self.assertIn(expected, result.text)
            self.assertNotIn("private_secret", result.text)
            record = self.ledger.request(result.headers["x-tollgate-request-id"])
            self.assertEqual(record["state"], "unresolved")
            self.assertEqual(record["error_code"], expected)
        self.assertTrue(self.ledger.reconciliation()["ok"])

    async def test_broken_stream_recovers_using_saved_openai_id(self):
        self.behavior = "broken"
        result = await self.chat(stream=True)
        request_id = result.headers["x-tollgate-request-id"]
        self.assertIn("incomplete_provider_stream", result.text)
        self.assertEqual(self.ledger.request(request_id)["state"], "unresolved")
        with self.ledger.database.transaction() as connection:
            connection.execute(
                "UPDATE requests SET updated_at=0 WHERE id=?", (request_id,)
            )
        await self.app.state.worker.tick()
        self.assertEqual(self.ledger.request(request_id)["state"], "settled")

    async def test_sessions_csrf_and_logout(self):
        headers = {"Origin": "http://127.0.0.1:8000"}
        login = await self.client.post(
            "/auth/login", json={"key": ADMIN_KEY}, headers=headers
        )
        self.assertEqual(login.status_code, 200, login.text)
        self.assertIn("HttpOnly", login.headers["set-cookie"])
        self.assertEqual((await self.client.get("/admin/overview")).status_code, 200)
        result = await self.client.post("/admin/customers", json={"name": "blocked"})
        self.assertEqual(result.status_code, 403)
        headers["X-CSRF-Token"] = login.json()["csrf_token"]
        result = await self.client.post(
            "/admin/customers", json={"name": "allowed"}, headers=headers
        )
        self.assertEqual(result.status_code, 201)
        await self.client.post("/auth/logout", headers=headers)
        self.assertEqual((await self.client.get("/admin/overview")).status_code, 401)

    async def test_expired_key_and_frozen_wallet_cannot_generate(self):
        self.ledger.freeze(self.customer["id"], True, "Test suspension")
        result = await self.chat()
        self.assertEqual(result.status_code, 403)
        self.assertNotIn(("POST", "/v1/responses"), self.calls)
        self.ledger.freeze(self.customer["id"], False, "Test resumption")
        with self.ledger.database.transaction() as connection:
            connection.execute("UPDATE api_keys SET expires_at=1")
        self.assertEqual((await self.chat()).status_code, 401)

    async def test_price_age_blocks_dispatch(self):
        models = self.app.state.models
        original = models["gpt-5.4-mini"]
        models["gpt-5.4-mini"] = replace(original, reviewed_on="2020-01-01")
        self.assertEqual((await self.chat()).status_code, 503)
        self.assertEqual(self.calls, [])

    async def test_rate_limit_is_shared_and_readiness_checks_worker(self):
        with ThreadPoolExecutor(8) as pool:

            def attempt(_):
                try:
                    self.app.state.sessions.limit("one-test-budget", 3)
                    return 1
                except BillingError:
                    return 0

            self.assertEqual(sum(pool.map(attempt, range(20))), 3)
        self.assertEqual((await self.client.get("/ready")).status_code, 503)
        await self.app.state.worker.tick()
        self.assertEqual((await self.client.get("/ready")).status_code, 200)

    async def test_payment_signature_duplicate_and_amount_validation(self):
        payments = self.app.state.payments
        order = payments.order(self.customer["id"], 5_000_000, "checkout-one")
        obj = {
            "id": "cs_test_one",
            "metadata": {"tollgate_order_id": order["id"]},
            "mode": "payment",
            "currency": "usd",
            "amount_total": 500,
            "payment_status": "paid",
            "payment_intent": "pi_one",
        }
        event = {
            "id": "evt_one",
            "type": "checkout.session.completed",
            "livemode": False,
            "data": {"object": obj},
        }

        def signed(value, at=None):
            raw = json.dumps(value).encode()
            timestamp = str(int(time.time()) if at is None else at)
            signature = hmac.new(
                b"whsec_test_placeholder",
                timestamp.encode() + b"." + raw,
                hashlib.sha256,
            ).hexdigest()
            return raw, "t=" + timestamp + ",v1=" + signature

        with self.assertRaises(BillingError):
            payments.webhook(*signed(event, 1))
        event["data"]["object"]["amount_total"] = 501
        with self.assertRaises(BillingError):
            payments.webhook(*signed(event))
        event["data"]["object"]["amount_total"] = 500
        body, signature = signed(event)
        for _ in range(3):
            self.assertEqual(
                (
                    await self.client.post(
                        "/webhooks/stripe",
                        content=body,
                        headers={"stripe-signature": signature},
                    )
                ).status_code,
                200,
            )
        event["id"] = "evt_second_notification"
        payments.webhook(*signed(event))
        self.assertEqual(
            self.ledger.balance(self.customer["id"])["available_usd"], "6.000000"
        )
        self.assertTrue(self.ledger.reconciliation()["ok"])
        refund = {
            "id": "evt_refund",
            "type": "charge.refunded",
            "livemode": False,
            "data": {"object": {"payment_intent": "pi_one"}},
        }
        payments.webhook(*signed(refund))
        self.assertTrue(self.ledger.balance(self.customer["id"])["frozen"])

    async def test_operator_release_is_audited_and_cannot_double_release(self):
        self.behavior = "ambiguous"
        response = await self.chat()
        request_id = response.headers["x-tollgate-request-id"]
        decision = Resolution(
            action="release",
            evidence="Provider support confirmed no billable generation.",
        )
        self.ledger.resolve(request_id, decision)
        with self.assertRaises(BillingError):
            self.ledger.resolve(request_id, decision)
        self.assertTrue(self.ledger.reconciliation()["ok"])
        self.assertTrue(
            any(
                row["action"] == "reservation_released"
                for row in self.ledger.overview()["audit"]
            )
        )


@unittest.skipUnless(
    os.environ.get("TOLLGATE_TEST_POSTGRES_URL"),
    "PostgreSQL integration requires TOLLGATE_TEST_POSTGRES_URL",
)
class PostgresTests(unittest.TestCase):
    def test_real_postgres_concurrency_settlement_and_constraints(self):
        database = Database(Path("unused.db"), os.environ["TOLLGATE_TEST_POSTGRES_URL"])
        database.initialize()
        ledger = Ledger(database)
        customer = ledger.create_customer("Postgres integration " + uuid.uuid4().hex)
        key = ledger.create_key(customer["id"], "pg", ["sim-basic"])
        auth = ledger.authenticate(key["api_key"].encode())
        ledger.grant(customer["id"], 2090, "one-hold")

        def attempt(_):
            request_id = "req_" + uuid.uuid4().hex
            try:
                ledger.reserve(
                    request_id,
                    auth,
                    request_id,
                    "hash",
                    MODELS["sim-basic"],
                    "test",
                    21,
                    256,
                )
                return request_id
            except BillingError as error:
                self.assertEqual(error.code, "insufficient_credits")
                return None

        with ThreadPoolExecutor(10) as pool:
            accepted = [value for value in pool.map(attempt, range(30)) if value]
        self.assertEqual(len(accepted), 1)
        request_id = accepted[0]
        ledger.dispatch(request_id)
        ledger.record_usage(request_id, 21, 38)
        with ThreadPoolExecutor(8) as pool:
            self.assertEqual(sum(pool.map(ledger.settle, [request_id] * 24)), 1)
        self.assertTrue(ledger.reconciliation()["ok"])
        with self.assertRaises(Exception):
            with database.transaction() as connection:
                connection.execute(
                    "UPDATE ledger SET amount_micro=0 WHERE customer_id=?",
                    (customer["id"],),
                )
        database.close()


if __name__ == "__main__":
    unittest.main()
