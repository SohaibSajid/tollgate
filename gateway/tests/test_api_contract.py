"""The published client specification must match the code and the running server."""

import contextlib
import json
import os
import re
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

from app.config import Settings

ADMIN_KEY, PROVIDER_KEY = "admin_" + "a" * 40, "provider_" + "b" * 40
ROOT = Path(__file__).resolve().parents[2]
SPEC_PATH = ROOT / "docs" / "api" / "openapi.json"
_import_temp = tempfile.TemporaryDirectory()
sys.path.insert(0, str(ROOT))
with patch.dict(
    os.environ,
    {
        "TOLLGATE_DEVELOPMENT_API_KEY": ADMIN_KEY,
        "TOLLGATE_PROVIDER_API_KEY": PROVIDER_KEY,
        "TOLLGATE_SIMULATOR_DATABASE_PATH": str(Path(_import_temp.name) / "s.db"),
    },
):
    from app import api_contract as contract
    from app.main import create_app
    from simulator.main import create_simulator


def documented_codes(spec, method, path, status):
    response = spec["paths"][path][method.lower()]["responses"][str(status)]
    return set(response["content"]["application/json"]["examples"])


class SpecificationFileTests(unittest.TestCase):
    def setUp(self):
        self.spec = contract.build_client_openapi()

    def test_committed_file_matches_code(self):
        committed = json.loads(SPEC_PATH.read_text(encoding="utf-8"))
        self.assertEqual(
            committed,
            json.loads(json.dumps(self.spec)),
            "Run: python manage.py export-openapi",
        )

    def test_guide_tables_match_code(self):
        guide = (ROOT / "docs" / "API_GUIDE.md").read_text(encoding="utf-8")
        for name in contract.GUIDE_BLOCKS:
            self.assertIn(f"<!-- generated:{name} -->", guide)
        self.assertEqual(guide, contract.render_guide(guide), "Run: python manage.py export-openapi")

    def test_every_provider_usage_shape_matches_schema(self):
        from app.services.gemini_provider import gemini_usage
        from app.services.live_provider import anthropic_usage, openai_usage

        shapes = [
            gemini_usage({"promptTokenCount": 9, "candidatesTokenCount": 3, "thoughtsTokenCount": 2, "cachedContentTokenCount": 1, "totalTokenCount": 14}),
            openai_usage({"input_tokens": 9, "output_tokens": 5, "input_tokens_details": {"cached_tokens": 2}}),
            anthropic_usage({"input_tokens": 9, "output_tokens": 5, "cache_read_input_tokens": 1}),
        ]
        for usage in shapes:
            contract.Usage.model_validate(usage)
            contract.ChatCompletionChunk.model_validate({"id": "x", "object": "chat.completion.chunk", "created": 1, "model": "m", "choices": [], "usage": usage})

    def test_every_operation_is_a_real_route(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = Settings(
                _env_file=None,
                environment="test",
                development_api_key=ADMIN_KEY,
                provider_api_key=PROVIDER_KEY,
                database_path=Path(directory) / "w.db",
                worker_enabled=False,
            )
            app = create_app(settings)
            routes = {
                (method, route.path)
                for route in app.routes
                for method in getattr(route, "methods", ()) or ()
            }
        self.assertTrue(contract.CLIENT_OPERATIONS)
        for operation in contract.CLIENT_OPERATIONS:
            self.assertIn(operation, routes)

    def test_examples_validate_against_models(self):
        models = {
            "HealthOut": contract.HealthOut,
            "ReadyOut": contract.ReadyOut,
            "ModelList": contract.ModelList,
            "ChatCompletion": contract.ChatCompletion,
            "Balance": contract.Balance,
            "RequestStatus": contract.RequestStatus,
            "ErrorResponse": contract.ErrorResponse,
        }
        checked = 0
        for path in self.spec["paths"].values():
            for operation in path.values():
                body = operation.get("requestBody")
                if body:
                    for example in body["content"]["application/json"]["examples"].values():
                        contract.ChatCompletionRequest.model_validate(example["value"])
                        checked += 1
                for response in operation["responses"].values():
                    content = response["content"].get("application/json", {})
                    name = content["schema"]["$ref"].rsplit("/", 1)[1]
                    examples = [content["example"]] if "example" in content else []
                    examples += [item["value"] for item in content.get("examples", {}).values()]
                    for example in examples:
                        models[name].model_validate(example)
                        checked += 1
        self.assertGreater(checked, 40)
        for frame in contract.STREAM_EXAMPLE.strip().split("\n\n")[:-1]:
            contract.ChatCompletionChunk.model_validate_json(frame[len("data: ") :])

    def test_every_documented_code_exists_in_source(self):
        source = "\n".join(
            path.read_text(encoding="utf-8")
            for path in (ROOT / "gateway" / "app").rglob("*.py")
            if path.name != "api_contract.py"
        )
        for code in contract.ERRORS:
            self.assertRegex(source, r'"' + re.escape(code) + r'"', code)

    def test_error_statuses_are_consistent(self):
        for path, methods in self.spec["paths"].items():
            for method, operation in methods.items():
                for status, response in operation["responses"].items():
                    for code in response["content"].get("application/json", {}).get("examples", {}):
                        self.assertEqual(str(contract.ERRORS[code][0]), status, (path, code))


class LiveContractTests(unittest.IsolatedAsyncioTestCase):
    spec = contract.build_client_openapi()

    @contextlib.asynccontextmanager
    async def server(self, scenario="normal", credit="1.00", **overrides):
        with tempfile.TemporaryDirectory() as directory:
            simulator = create_simulator(PROVIDER_KEY, Path(directory) / "sim.db", scenario=scenario, delay=0)
            settings = Settings(
                _env_file=None,
                environment="test",
                development_api_key=ADMIN_KEY,
                provider_api_key=PROVIDER_KEY,
                database_path=Path(directory) / "wallet.db",
                worker_enabled=False,
                max_request_bytes=4096,
                provider_timeout_seconds=2,
                **overrides,
            )
            app = create_app(settings, provider_transport=httpx.ASGITransport(app=simulator))
            async with app.router.lifespan_context(app):
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
                    base_url="http://testserver",
                ) as client:
                    owner = {"Authorization": "Bearer " + ADMIN_KEY}
                    customer = (await client.post("/admin/customers", json={"name": "Contract"}, headers=owner)).json()["id"]
                    issued = await client.post(
                        "/admin/keys",
                        json={"customer_id": customer, "allowed_models": ["sim-basic"], "expires_in_days": 14},
                        headers=owner,
                    )
                    self.assertEqual(issued.status_code, 201)
                    if credit != "0":
                        granted = await client.post(
                            "/admin/credits",
                            json={"customer_id": customer, "amount_usd": credit, "reference": "contract"},
                            headers=owner,
                        )
                        self.assertEqual(granted.status_code, 200)
                    auth = {"Authorization": "Bearer " + issued.json()["api_key"]}
                    yield client, auth, owner, customer

    @staticmethod
    def body(**changes):
        return {"model": "sim-basic", "messages": [{"role": "user", "content": "Hello contract"}], "max_completion_tokens": 64, **changes}

    def assert_error(self, response, method, path, status, code):
        self.assertEqual(response.status_code, status, response.text)
        error = contract.ErrorResponse.model_validate(response.json()).error
        self.assertEqual(error.code, code)
        self.assertEqual(error.request_id, response.headers["X-Request-ID"])
        self.assertIn(code, documented_codes(self.spec, method, path, status))
        return error

    async def test_status_endpoints(self):
        async with self.server() as (client, *_):
            health = await client.get("/health")
            self.assertEqual(health.status_code, 200)
            contract.HealthOut.model_validate(health.json())
            ready = await client.get("/ready")
            self.assertEqual(ready.status_code, 503)
            contract.ReadyOut.model_validate(ready.json())

    async def test_server_publishes_the_same_specification(self):
        async with self.server() as (client, *_):
            served = await client.get("/api-spec.json")
            self.assertEqual(served.status_code, 200)
            body = served.json()
            self.assertEqual(body["servers"], [{"url": "http://127.0.0.1:8000", "description": "This gateway"}])
            body["servers"] = self.spec["servers"]
            self.assertEqual(body, json.loads(json.dumps(self.spec)))

    async def test_success_responses_match_schemas(self):
        async with self.server() as (client, auth, *_):
            models = await client.get("/v1/models", headers=auth)
            self.assertEqual([m.id for m in contract.ModelList.model_validate(models.json()).data], ["sim-basic"])
            answer = await client.post("/v1/chat/completions", json=self.body(), headers=auth)
            self.assertEqual(answer.status_code, 200, answer.text)
            contract.ChatCompletion.model_validate(answer.json())
            documented = self.spec["paths"]["/v1/chat/completions"]["post"]["responses"]["200"]["headers"]
            for header in ("X-Request-ID", "X-Tollgate-Request-ID", "Idempotency-Key", "X-Tollgate-Reservation-USD", "X-Tollgate-Usage-Source", "X-Tollgate-Cost-USD", "X-Tollgate-Settlement"):
                self.assertIn(header, answer.headers)
                self.assertIn(header, documented)
            status = await client.get("/v1/requests/" + answer.headers["X-Tollgate-Request-ID"], headers=auth)
            contract.RequestStatus.model_validate(status.json())
            contract.Balance.model_validate((await client.get("/v1/balance", headers=auth)).json())

    async def test_stream_matches_schema_and_always_ends_with_usage(self):
        async with self.server() as (client, auth, *_):
            response = await client.post("/v1/chat/completions", json=self.body(stream=True), headers=auth)
            self.assertEqual(response.status_code, 200)
            self.assertIn("text/event-stream", response.headers["content-type"])
            frames = [f[len("data: ") :] for f in response.text.strip().split("\n\n")]
            self.assertEqual(frames[-1], "[DONE]")
            chunks = [contract.ChatCompletionChunk.model_validate_json(f) for f in frames[:-1]]
            self.assertIsNotNone(chunks[-1].usage)
            self.assertTrue(all(c.usage is None for c in chunks[:-1]))
            text = "".join(c.choices[0].delta.content or "" for c in chunks if c.choices)
            self.assertIn("simulated", text)

    async def test_mid_stream_failure_sends_documented_error_event(self):
        async with self.server(scenario="disconnect") as (client, auth, *_):
            response = await client.post("/v1/chat/completions", json=self.body(stream=True), headers=auth)
            last = response.text.strip().split("\n\n")[-1][len("data: ") :]
            error = contract.StreamError.model_validate_json(last).error
            self.assertEqual(error.request_id, response.headers["X-Tollgate-Request-ID"])
            status = await client.get("/v1/requests/" + error.request_id, headers=auth)
            self.assertEqual(status.json()["state"], "unresolved")

    async def test_billing_is_exactly_once(self):
        async with self.server() as (client, auth, owner, _):
            answer = await client.post("/v1/chat/completions", json=self.body(), headers={**auth, "Idempotency-Key": "once-1"})
            request_id = answer.headers["X-Tollgate-Request-ID"]
            cost = answer.headers["X-Tollgate-Cost-USD"]
            before = (await client.get("/v1/requests/" + request_id, headers=auth)).json()
            self.assertEqual(before["state"], "usage_recorded")
            self.assertEqual((await client.post("/admin/settle", headers=owner)).json(), {"settled": 1})
            self.assertEqual((await client.post("/admin/settle", headers=owner)).json(), {"settled": 0})
            after = (await client.get("/v1/requests/" + request_id, headers=auth)).json()
            self.assertEqual((after["state"], after["cost_usd"]), ("settled", cost))
            wallet = (await client.get("/v1/balance", headers=auth)).json()
            micro = int(cost.replace(".", ""))
            self.assertEqual(wallet["spent_usd"], cost)
            self.assertEqual(wallet["reserved_usd"], "0.000000")
            self.assertEqual(int(wallet["available_usd"].replace(".", "")), 1_000_000 - micro)

    async def test_documented_error_paths(self):
        chat = "/v1/chat/completions"
        async with self.server() as (client, auth, owner, customer):
            self.assert_error(await client.get("/v1/balance"), "GET", "/v1/balance", 401, "invalid_api_key")
            self.assert_error(await client.get("/v1/balance", headers={"Authorization": "Bearer tg_" + "x" * 43}), "GET", "/v1/balance", 401, "invalid_api_key")
            self.assert_error(await client.post(chat, json=self.body(model="sim-deep"), headers=auth), "POST", chat, 403, "model_not_allowed")
            self.assert_error(await client.post(chat, json=self.body(model="no-such-model"), headers=auth), "POST", chat, 422, "unsupported_model")
            self.assert_error(await client.get("/v1/requests/req_" + "0" * 32, headers=auth), "GET", "/v1/requests/{request_id}", 404, "request_not_found")
            invalid = self.assert_error(await client.post(chat, json={"model": "sim-basic"}, headers=auth), "POST", chat, 422, "invalid_request")
            self.assertTrue(invalid.details)
            self.assert_error(await client.post(chat, json=self.body(temperature=0.2), headers=auth), "POST", chat, 422, "invalid_request")
            self.assert_error(await client.post(chat, json=self.body(max_completion_tokens="64"), headers=auth), "POST", chat, 422, "invalid_request")
            self.assert_error(await client.post(chat, content=b"{" + b" " * 5000 + b"}", headers={**auth, "Content-Type": "application/json"}), "POST", chat, 413, "request_too_large")
            first = await client.post(chat, json=self.body(), headers={**auth, "Idempotency-Key": "same"})
            duplicate = self.assert_error(await client.post(chat, json=self.body(), headers={**auth, "Idempotency-Key": "same"}), "POST", chat, 409, "duplicate_request")
            self.assertEqual(duplicate.context["original_request_id"], first.headers["X-Tollgate-Request-ID"])
            self.assert_error(await client.post(chat, json=self.body(), headers={**auth, "Idempotency-Key": "same", "X-Tollgate-Feature": "other"}), "POST", chat, 409, "idempotency_conflict")
            await client.put("/admin/spend-cap", json={"customer_id": customer, "amount_usd": "0.001"}, headers=owner)
            self.assert_error(await client.post(chat, json=self.body(max_completion_tokens=4000), headers=auth), "POST", chat, 403, "spend_cap_exceeded")
            await client.put("/admin/spend-cap", json={"customer_id": customer, "amount_usd": None}, headers=owner)
            await client.post(f"/admin/customers/{customer}/freeze", json={"frozen": True, "reason": "contract test"}, headers=owner)
            self.assert_error(await client.post(chat, json=self.body(), headers=auth), "POST", chat, 403, "account_frozen")

    async def test_insufficient_credits_reports_the_gap(self):
        async with self.server(credit="0.000100") as (client, auth, *_):
            error = self.assert_error(await client.post("/v1/chat/completions", json=self.body(), headers=auth), "POST", "/v1/chat/completions", 402, "insufficient_credits")
            self.assertEqual(set(error.context), {"required_usd", "available_usd"})

    async def test_rate_limit_sends_retry_after(self):
        fixed = 1_800_000_030.0
        with patch("app.security.sessions.time") as clock:
            clock.time.return_value = fixed
            async with self.server(customer_requests_per_minute=2) as (client, auth, *_):
                for _ in range(2):
                    self.assertEqual((await client.get("/v1/balance", headers=auth)).status_code, 200)
                limited = await client.get("/v1/balance", headers=auth)
                error = self.assert_error(limited, "GET", "/v1/balance", 429, "rate_limit_exceeded")
                self.assertEqual(limited.headers["Retry-After"], "30")
                self.assertEqual(error.context["retry_after_seconds"], 30)

    async def test_revoked_and_expired_keys_are_rejected(self):
        async with self.server() as (client, auth, owner, customer):
            issued = (await client.post("/admin/keys", json={"customer_id": customer, "allowed_models": ["sim-basic"], "expires_in_days": 1}, headers=owner)).json()
            key = {"Authorization": "Bearer " + issued["api_key"]}
            self.assertEqual((await client.get("/v1/balance", headers=key)).status_code, 200)
            with patch("app.services.ledger.time") as clock:
                clock.time.return_value = time.time() + 2 * 86400
                self.assert_error(await client.get("/v1/balance", headers=key), "GET", "/v1/balance", 401, "invalid_api_key")
            self.assertEqual((await client.delete("/admin/keys/" + issued["id"], headers=owner)).status_code, 200)
            self.assert_error(await client.get("/v1/balance", headers=key), "GET", "/v1/balance", 401, "invalid_api_key")
            self.assertEqual((await client.get("/v1/balance", headers=auth)).status_code, 200)

    async def test_customer_key_cannot_use_admin_endpoints(self):
        async with self.server() as (client, auth, *_):
            for path in ("/admin/overview", "/admin/customers"):
                response = await client.get(path, headers=auth) if path.endswith("overview") else await client.post(path, json={"name": "x"}, headers=auth)
                self.assertEqual(response.status_code, 401)
                self.assertEqual(response.json()["error"]["code"], "invalid_api_key")

if __name__ == "__main__":
    unittest.main()
