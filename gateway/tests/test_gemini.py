"""Exercise Gemini through the public gateway without calling a paid service."""

import json
import tempfile
import unittest
import uuid
from pathlib import Path

import httpx

from app.config import Settings
from app.routing.providers import load_models
from app.services.gemini_provider import gemini_usage
from app.services.provider_client import ProviderFailure
from test_gateway import ADMIN_KEY, PROVIDER_KEY, create_app

MODEL = "gemini-3.5-flash-lite"
SECRET = "test-gemini-key-that-must-stay-private"


class Fragments(httpx.AsyncByteStream):
    def __init__(self, data):
        self.data = data

    async def __aiter__(self):
        for position in range(0, len(self.data), 7):
            yield self.data[position : position + 7]


def sse(*events):
    return b"".join(
        b"data: " + json.dumps(event, ensure_ascii=False).encode() + b"\r\n\r\n"
        for event in events
    )


class GeminiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.behavior, self.calls, self.bodies = "normal", [], []
        self.settings = Settings(
            _env_file=None,
            environment="test",
            mode="live",
            development_api_key=ADMIN_KEY,
            provider_api_key=PROVIDER_KEY,
            gemini_api_key=SECRET,
            openai_api_key="old-openai-key-unused",
            enabled_providers=["gemini"],
            worker_enabled=False,
            database_path=Path(self.tmp.name) / "gemini.db",
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
        self.customer = self.ledger.create_customer("Free Gemini test")
        self.key = self.ledger.create_key(self.customer["id"], "Gemini only", [MODEL])[
            "api_key"
        ]

    async def asyncTearDown(self):
        await self.client.aclose()
        await self.life.__aexit__(None, None, None)
        self.tmp.cleanup()

    def provider(self, request):
        self.assertEqual(request.url.host, "generativelanguage.googleapis.com")
        self.assertEqual(request.headers["x-goog-api-key"], SECRET)
        self.assertNotIn(SECRET, str(request.url))
        self.assertNotIn(SECRET, request.content.decode())
        body = json.loads(request.content)
        self.calls.append(request.url.path)
        self.bodies.append(body)
        if request.url.path.endswith(":countTokens"):
            if self.behavior == "count_reject":
                return httpx.Response(
                    403,
                    json={"error": {"status": "PERMISSION_DENIED", "message": SECRET}},
                )
            return httpx.Response(
                200, json={"totalTokens": 40001 if self.behavior == "oversize" else 20}
            )
        self.assertTrue(request.url.path.endswith(":streamGenerateContent"))
        self.assertEqual(request.url.params.get("alt"), "sse")
        self.assertNotIn("tools", body)
        if self.behavior == "quota":
            return httpx.Response(
                429, json={"error": {"status": "RESOURCE_EXHAUSTED", "message": SECRET}}
            )
        if self.behavior == "server_error":
            return httpx.Response(503, json={"error": {"status": "UNAVAILABLE"}})
        usage = {
            "promptTokenCount": 20,
            "candidatesTokenCount": 3,
            "thoughtsTokenCount": 5,
            "cachedContentTokenCount": 4,
            "totalTokenCount": 28,
        }
        events = [
            {
                "responseId": "gemini_response_123",
                "candidates": [
                    {
                        "index": 0,
                        "content": {
                            "parts": [{"text": "private thinking", "thought": True}]
                        },
                    }
                ],
            },
            {
                "candidates": [{"content": {"parts": [{"text": "Hello Gemini 😊"}]}}],
                "usageMetadata": {
                    "promptTokenCount": 20,
                    "candidatesTokenCount": 1,
                    "totalTokenCount": 21,
                },
            },
        ]
        if self.behavior == "blocked":
            events = [{"promptFeedback": {"blockReason": "SAFETY"}}]
        elif self.behavior == "stream_error":
            events.append(
                {"error": {"status": "RESOURCE_EXHAUSTED", "message": SECRET}}
            )
        elif self.behavior != "broken":
            reason = "MAX_TOKENS" if self.behavior == "length" else "STOP"
            events.append({"candidates": [{"finishReason": reason}]})
            if self.behavior != "missing_usage":
                if self.behavior == "bad_usage":
                    usage["totalTokenCount"] = 99
                if self.behavior == "thinking":
                    usage.update(thoughtsTokenCount=350, totalTokenCount=373)
                events.append({"usageMetadata": usage})
        data = sse(*events)
        if self.behavior == "truncated":
            data += b'data: {"unfinished":'
        return httpx.Response(
            200, stream=Fragments(data), headers={"content-type": "text/event-stream"}
        )

    async def chat(self, *, stream=False, idem=None, messages=None):
        return await self.client.post(
            "/v1/chat/completions",
            headers={
                "Authorization": "Bearer " + self.key,
                "Idempotency-Key": idem or uuid.uuid4().hex,
            },
            json={
                "model": MODEL,
                "messages": messages or [{"role": "user", "content": "Hello"}],
                "max_completion_tokens": 256,
                "stream": stream,
            },
        )

    async def test_json_real_usage_zero_cost_without_funding(self):
        result = await self.chat()
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual(
            result.json()["choices"][0]["message"]["content"], "Hello Gemini 😊"
        )
        self.assertEqual(result.json()["usage"]["completion_tokens"], 8)
        self.assertEqual(
            result.json()["usage"]["completion_tokens_details"]["reasoning_tokens"], 5
        )
        record = self.ledger.request(result.headers["x-tollgate-request-id"])
        self.assertEqual(record["provider_response_id"], "gemini_response_123")
        self.assertEqual(record["cost_usd"], "0.000000")
        self.assertEqual(record["reserved_usd"], "0.000000")
        await self.app.state.worker.tick()
        await self.app.state.worker.tick()
        self.assertEqual(self.ledger.request(record["id"])["state"], "settled")
        entries = [
            row
            for row in self.ledger.overview()["ledger"]
            if row["request_id"] == record["id"]
        ]
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["amount_usd"], "0.000000")
        self.assertTrue(self.ledger.reconciliation()["ok"])

    async def test_stream_fragmentation_cumulative_usage_and_duplicate(self):
        first = await self.chat(stream=True, idem="one-gemini-request")
        self.assertEqual(first.status_code, 200)
        self.assertIn("[DONE]", first.text)
        self.assertNotIn("private thinking", first.text)
        events = [
            json.loads(line[6:])
            for line in first.text.splitlines()
            if line.startswith("data: {")
        ]
        usages = [event["usage"] for event in events if event.get("usage")]
        self.assertEqual(len(usages), 1)
        self.assertEqual(usages[0]["total_tokens"], 28)
        duplicate = await self.chat(stream=True, idem="one-gemini-request")
        self.assertEqual(duplicate.status_code, 409)
        self.assertEqual(
            sum(path.endswith(":streamGenerateContent") for path in self.calls), 1
        )

    async def test_count_includes_system_and_same_generation_contents(self):
        result = await self.chat(
            messages=[
                {"role": "system", "content": "Be brief"},
                {"role": "user", "content": "Hi"},
                {"role": "assistant", "content": "Hello"},
                {"role": "user", "content": "Explain APIs"},
            ]
        )
        self.assertEqual(result.status_code, 200, result.text)
        counted = self.bodies[0]["generateContentRequest"]
        generation = self.bodies[1]
        self.assertEqual(counted.pop("model"), "models/" + MODEL)
        self.assertEqual(counted, generation)
        self.assertEqual(
            generation["systemInstruction"]["parts"][0]["text"], "Be brief"
        )
        self.assertEqual(generation["contents"][1]["role"], "model")

    async def test_quota_rejection_is_clear_and_never_falls_back(self):
        self.behavior = "quota"
        result = await self.chat(stream=True)
        self.assertEqual(result.status_code, 429)
        self.assertIn("gemini_quota_exhausted", result.text)
        self.assertNotIn(SECRET, result.text)
        self.assertEqual(
            self.ledger.request(result.headers["x-tollgate-request-id"])["state"],
            "released",
        )
        self.assertEqual(len(self.calls), 2)

    async def test_bad_preflight_never_dispatches_generation(self):
        for behavior, status in (("count_reject", 502), ("oversize", 422)):
            self.behavior = behavior
            self.calls.clear()
            result = await self.chat()
            self.assertEqual(result.status_code, status, result.text)
            self.assertEqual(len(self.calls), 1)
            self.assertTrue(self.calls[0].endswith(":countTokens"))
            self.assertNotIn(SECRET, result.text)
        self.assertEqual(self.ledger.overview()["requests"], [])

    async def test_uncertain_streams_do_not_settle_partial_usage(self):
        for behavior in (
            "broken",
            "missing_usage",
            "bad_usage",
            "truncated",
            "stream_error",
            "server_error",
        ):
            self.behavior = behavior
            result = await self.chat(stream=True)
            self.assertNotIn("[DONE]", result.text)
            self.assertNotIn(SECRET, result.text)
            record = self.ledger.request(result.headers["x-tollgate-request-id"])
            self.assertEqual(record["state"], "unresolved", behavior)
            self.assertIsNone(record["cost_usd"])
        self.assertTrue(self.ledger.reconciliation()["ok"])

    async def test_blocked_prompt_has_a_visible_error(self):
        self.behavior = "blocked"
        result = await self.chat(stream=True)
        self.assertIn("gemini_prompt_blocked", result.text)
        self.assertNotIn("[DONE]", result.text)

    async def test_thinking_is_counted_once_within_model_ceiling(self):
        self.behavior = "thinking"
        result = await self.chat()
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual(result.json()["usage"]["completion_tokens"], 353)
        await self.app.state.worker.tick()
        record = self.ledger.request(result.headers["x-tollgate-request-id"])
        self.assertEqual(record["state"], "settled")
        self.assertEqual(record["cost_usd"], "0.000000")

    async def test_length_finish_is_preserved(self):
        self.behavior = "length"
        result = await self.chat()
        self.assertEqual(result.json()["choices"][0]["finish_reason"], "length")

    async def test_model_selection_and_free_rates_are_explicit(self):
        result = await self.client.get(
            "/v1/models", headers={"Authorization": "Bearer " + self.key}
        )
        self.assertEqual([row["id"] for row in result.json()["data"]], [MODEL])
        self.assertEqual(
            result.json()["data"][0]["pricing_source"], "free_tier_assumed"
        )
        rows = json.loads(self.settings.model_catalog_path.read_text())
        rows[-1]["input_usd_per_million"] = "0.30"
        path = Path(self.tmp.name) / "bad-prices.json"
        path.write_text(json.dumps(rows))
        with self.assertRaises(ValueError):
            load_models(self.settings.model_copy(update={"model_catalog_path": path}))

    async def test_unsupported_message_order_has_no_provider_call(self):
        result = await self.chat(
            messages=[{"role": "assistant", "content": "Continue"}]
        )
        self.assertEqual(result.status_code, 422)
        self.assertEqual(self.calls, [])

    async def test_usage_validation_rejects_boolean_and_tool_counters(self):
        for usage in (
            {"promptTokenCount": True, "totalTokenCount": 1},
            {"promptTokenCount": 1, "toolUsePromptTokenCount": 2, "totalTokenCount": 3},
        ):
            with self.assertRaises(ProviderFailure):
                gemini_usage(usage)


if __name__ == "__main__":
    unittest.main()
