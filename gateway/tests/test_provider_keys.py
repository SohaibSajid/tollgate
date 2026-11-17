"""Customer-owned provider keys: encrypted storage and use on every provider call."""

import tempfile
import unittest
import uuid
from pathlib import Path

import httpx
from cryptography.fernet import Fernet
from pydantic import ValidationError

from app.config import Settings
from app.database import Database
from app.services.ledger import BillingError, Ledger
from app.services.provider_keys import ProviderKeys
from test_gateway import ADMIN_KEY, PROVIDER_KEY, create_app
from test_gemini import MODEL, Fragments, sse

CUSTOMER_KEY = "AIza-customer-own-gemini-key-0001"
DEPLOYMENT_KEY = "deployment-gemini-key-must-not-be-used"


def settings_for(path, **changes):
    return Settings(
        _env_file=None,
        environment="test",
        development_api_key=ADMIN_KEY,
        provider_api_key=PROVIDER_KEY,
        database_path=path,
        worker_enabled=False,
        **changes,
    )


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "keys.db"
        self.database = Database(self.path)
        self.database.initialize()
        self.customer = Ledger(self.database).create_customer("Acme")["id"]
        self.secret = Fernet.generate_key().decode()
        self.keys = ProviderKeys(self.database, settings_for(self.path, credentials_key=self.secret))

    def tearDown(self):
        self.tmp.cleanup()

    def code(self, call, *args):
        with self.assertRaises(BillingError) as caught:
            call(*args)
        return caught.exception.code

    def test_saved_key_is_encrypted_and_only_a_hint_is_shown(self):
        saved = self.keys.save(self.customer, "gemini", CUSTOMER_KEY)
        self.assertEqual(saved["hint"], "0001")
        self.assertNotIn(CUSTOMER_KEY, str(saved))
        with self.database.connection() as connection:
            stored = connection.execute("SELECT secret FROM provider_keys").fetchone()[0]
            audit = " ".join(row[0] for row in connection.execute("SELECT detail FROM audit_events"))
        self.assertNotIn(CUSTOMER_KEY, stored)
        self.assertNotIn(CUSTOMER_KEY, audit)
        self.assertEqual(self.keys.get(self.customer, "gemini"), CUSTOMER_KEY)
        listing = self.keys.listing()
        self.assertEqual([(row["provider"], row["hint"]) for row in listing], [("gemini", "0001")])
        self.assertNotIn("secret", listing[0])

    def test_replace_and_remove(self):
        self.keys.save(self.customer, "gemini", CUSTOMER_KEY)
        self.keys.save(self.customer, "gemini", CUSTOMER_KEY + "-new")
        self.assertEqual(self.keys.get(self.customer, "gemini"), CUSTOMER_KEY + "-new")
        self.assertEqual(len(self.keys.listing()), 1)
        self.keys.remove(self.customer, "gemini")
        self.assertIsNone(self.keys.get(self.customer, "gemini"))
        self.assertEqual(self.code(self.keys.remove, self.customer, "gemini"), "provider_key_not_found")

    def test_bad_input_is_rejected(self):
        self.assertEqual(self.code(self.keys.save, self.customer, "mistral", CUSTOMER_KEY), "unsupported_provider")
        self.assertEqual(self.code(self.keys.save, self.customer, "gemini", "short"), "invalid_provider_key")
        self.assertEqual(self.code(self.keys.save, self.customer, "gemini", "has spaces in the key value"), "invalid_provider_key")
        self.assertEqual(self.code(self.keys.save, self.customer, "gemini", "tg_" + "x" * 43), "invalid_provider_key")
        self.assertEqual(self.code(self.keys.save, "cus_missing", "gemini", CUSTOMER_KEY), "customer_not_found")

    def test_missing_or_changed_credentials_key(self):
        plain = ProviderKeys(self.database, settings_for(self.path))
        self.assertEqual(self.code(plain.save, self.customer, "gemini", CUSTOMER_KEY), "credentials_key_missing")
        self.keys.save(self.customer, "gemini", CUSTOMER_KEY)
        self.assertEqual(self.code(plain.get, self.customer, "gemini"), "provider_key_unavailable")
        other = ProviderKeys(self.database, settings_for(self.path, credentials_key=Fernet.generate_key().decode()))
        self.assertEqual(self.code(other.get, self.customer, "gemini"), "provider_key_unavailable")

    def test_malformed_credentials_key_is_rejected_at_startup(self):
        with self.assertRaises(ValidationError):
            settings_for(self.path, credentials_key="not-a-valid-key")


class GeminiCustomerKeyTests(unittest.IsolatedAsyncioTestCase):
    async def start(self, **changes):
        self.tmp = tempfile.TemporaryDirectory()
        self.used = []
        settings = settings_for(
            Path(self.tmp.name) / "byok.db",
            mode="live",
            enabled_providers=["gemini"],
            credentials_key=Fernet.generate_key().decode(),
            **changes,
        )
        self.app = create_app(settings, provider_transport=httpx.MockTransport(self.provider))
        self.life = self.app.router.lifespan_context(self.app)
        await self.life.__aenter__()
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url="http://127.0.0.1:8000")
        ledger = self.app.state.ledger
        self.customer = ledger.create_customer("Bring your own key")["id"]
        self.key = ledger.create_key(self.customer, "App", [MODEL])["api_key"]
        self.owner = {"Authorization": "Bearer " + ADMIN_KEY}

    async def asyncTearDown(self):
        await self.client.aclose()
        await self.life.__aexit__(None, None, None)
        self.tmp.cleanup()

    def provider(self, request):
        self.used.append(request.headers["x-goog-api-key"])
        if request.url.path.endswith(":countTokens"):
            return httpx.Response(200, json={"totalTokens": 20})
        events = [
            {"responseId": "gemini_byok_1", "candidates": [{"content": {"parts": [{"text": "Hi"}]}}]},
            {"candidates": [{"finishReason": "STOP"}]},
            {"usageMetadata": {"promptTokenCount": 20, "candidatesTokenCount": 1, "totalTokenCount": 21}},
        ]
        return httpx.Response(200, stream=Fragments(sse(*events)), headers={"content-type": "text/event-stream"})

    async def chat(self):
        return await self.client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer " + self.key, "Idempotency-Key": uuid.uuid4().hex},
            json={"model": MODEL, "messages": [{"role": "user", "content": "Hello"}], "max_completion_tokens": 64},
        )

    async def key_source(self, response):
        status = await self.client.get(
            "/v1/requests/" + response.headers["X-Tollgate-Request-ID"],
            headers={"Authorization": "Bearer " + self.key},
        )
        return status.json()["key_source"]

    async def test_customer_key_is_used_for_counting_and_generation(self):
        await self.start(provider_key_policy="customer_only", gemini_api_key=DEPLOYMENT_KEY)
        saved = await self.client.put(
            f"/admin/customers/{self.customer}/provider-keys/gemini",
            json={"api_key": CUSTOMER_KEY},
            headers=self.owner,
        )
        self.assertEqual(saved.status_code, 200, saved.text)
        self.assertNotIn(CUSTOMER_KEY, saved.text)
        answer = await self.chat()
        self.assertEqual(answer.status_code, 200, answer.text)
        self.assertEqual(self.used, [CUSTOMER_KEY, CUSTOMER_KEY])
        self.assertEqual(await self.key_source(answer), "customer")
        overview = await self.client.get("/admin/overview", headers=self.owner)
        self.assertNotIn(CUSTOMER_KEY, overview.text)
        self.assertEqual(overview.json()["provider_keys"][0]["hint"], "0001")

    async def test_customer_only_refuses_without_a_key_and_needs_no_deployment_key(self):
        await self.start(provider_key_policy="customer_only")
        refused = await self.chat()
        self.assertEqual(refused.status_code, 403, refused.text)
        self.assertEqual(refused.json()["error"]["code"], "provider_key_missing")
        self.assertEqual(self.used, [])

    async def test_customer_first_falls_back_to_the_deployment_key(self):
        await self.start(gemini_api_key=DEPLOYMENT_KEY)
        answer = await self.chat()
        self.assertEqual(answer.status_code, 200, answer.text)
        self.assertEqual(self.used, [DEPLOYMENT_KEY, DEPLOYMENT_KEY])
        self.assertEqual(await self.key_source(answer), "deployment")


if __name__ == "__main__":
    unittest.main()
