"""Self-service accounts and the client portal: approval, login, isolation and security checks."""

import tempfile
import unittest
from pathlib import Path

import httpx
from cryptography.fernet import Fernet

from app.config import Settings
from test_gateway import ADMIN_KEY, PROVIDER_KEY, create_app, create_simulator

ORIGIN = "http://127.0.0.1:8000"
OWNER = {"Authorization": "Bearer " + ADMIN_KEY}
PASSWORD = "correct horse battery"


class PortalTests(unittest.IsolatedAsyncioTestCase):
    async def start(self, **changes):
        self.tmp = tempfile.TemporaryDirectory()
        path = Path(self.tmp.name)
        settings = Settings(
            _env_file=None,
            environment="test",
            development_api_key=ADMIN_KEY,
            provider_api_key=PROVIDER_KEY,
            database_path=path / "portal.db",
            worker_enabled=False,
            credentials_key=Fernet.generate_key().decode(),
            **changes,
        )
        simulator = create_simulator(PROVIDER_KEY, path / "sim.db", delay=0)
        self.app = create_app(settings, provider_transport=httpx.ASGITransport(app=simulator))
        self.life = self.app.router.lifespan_context(self.app)
        await self.life.__aenter__()
        self.clients = []

    async def asyncTearDown(self):
        for client in self.clients:
            await client.aclose()
        await self.life.__aexit__(None, None, None)
        self.tmp.cleanup()

    def browser(self):
        client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url=ORIGIN)
        self.clients.append(client)
        return client

    async def signup(self, client, email, business="Tic tac toe"):
        return await client.post("/portal/signup", headers={"Origin": ORIGIN}, json={"business": business, "email": email, "password": PASSWORD})

    async def login(self, client, email):
        response = await client.post("/portal/login", headers={"Origin": ORIGIN}, json={"email": email, "password": PASSWORD})
        if response.status_code == 200:
            client.headers.update({"Origin": ORIGIN, "X-CSRF-Token": response.json()["csrf_token"]})
        return response

    async def approve(self, client, email):
        accounts = (await client.get("/admin/accounts", headers=OWNER)).json()["accounts"]
        account = next(row for row in accounts if row["email"] == email)
        result = await client.post(f"/admin/accounts/{account['id']}/state", headers=OWNER, json={"state": "active"})
        self.assertEqual(result.status_code, 200, result.text)
        return account

    async def test_signup_waits_for_approval_then_runs_a_tracked_request(self):
        await self.start()
        client = self.browser()
        created = await self.signup(client, "Dev@Example.com")
        self.assertEqual((created.status_code, created.json()["state"]), (201, "pending"))
        pending = await self.login(client, "dev@example.com")
        self.assertEqual(pending.json()["error"]["code"], "account_pending")
        account = await self.approve(client, "dev@example.com")
        self.assertEqual((await self.login(client, "dev@example.com")).status_code, 200)
        session = (await client.get("/portal/session")).json()
        self.assertEqual(session["business"], "Tic tac toe")
        issued = await client.post("/portal/keys", json={"label": "Game server", "allowed_models": ["sim-basic"], "expires_in_days": 30})
        self.assertEqual(issued.status_code, 201, issued.text)
        await client.post("/admin/credits", headers=OWNER, json={"customer_id": account["customer_id"], "amount_usd": "1.00", "reference": "budget"})
        answer = await client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer " + issued.json()["api_key"], "X-Tollgate-End-User": "player_7"},
            json={"model": "sim-basic", "messages": [{"role": "user", "content": "Next move?"}], "max_completion_tokens": 32},
        )
        self.assertEqual(answer.status_code, 200, answer.text)
        await client.post("/admin/settle", headers=OWNER)
        workspace = (await client.get("/portal/workspace")).json()
        self.assertEqual(len(workspace["requests"]), 1)
        self.assertEqual(workspace["end_users"][0]["end_user"], "player_7")
        self.assertEqual(workspace["keys"][0]["label"], "Game server")
        self.assertNotIn("key_hash", workspace["keys"][0])
        self.assertEqual(workspace["base_url"], ORIGIN + "/v1")

    async def test_accounts_cannot_see_or_change_each_other(self):
        await self.start(signup_mode="open")
        first, second = self.browser(), self.browser()
        await self.signup(first, "a@example.com", "Business A")
        await self.signup(second, "b@example.com", "Business B")
        await self.login(first, "a@example.com")
        await self.login(second, "b@example.com")
        key = (await first.post("/portal/keys", json={"label": "A", "allowed_models": ["sim-basic"]})).json()
        stolen = await second.delete("/portal/keys/" + key["id"])
        self.assertEqual(stolen.json()["error"]["code"], "key_not_found")
        self.assertEqual((await second.get("/portal/workspace")).json()["keys"], [])
        self.assertEqual(len((await first.get("/portal/workspace")).json()["keys"]), 1)

    async def test_writes_need_session_csrf_and_origin(self):
        await self.start(signup_mode="open")
        client = self.browser()
        body = {"label": "x", "allowed_models": ["sim-basic"]}
        self.assertEqual((await client.post("/portal/keys", json=body)).json()["error"]["code"], "portal_session_expired")
        await self.signup(client, "c@example.com")
        await self.login(client, "c@example.com")
        no_csrf = await client.post("/portal/keys", json=body, headers={"X-CSRF-Token": "wrong"})
        self.assertEqual(no_csrf.json()["error"]["code"], "csrf_failed")
        wrong_origin = await client.post("/portal/keys", json=body, headers={"Origin": "http://evil.test"})
        self.assertEqual(wrong_origin.json()["error"]["code"], "origin_rejected")
        foreign = await client.post("/portal/signup", headers={"Origin": "http://evil.test"}, json={"business": "Evil", "email": "e@example.com", "password": PASSWORD})
        self.assertEqual(foreign.json()["error"]["code"], "origin_rejected")

    async def test_signup_rules_and_wrong_password(self):
        await self.start(signup_mode="open")
        client = self.browser()
        self.assertEqual((await self.signup(client, "not-an-email")).json()["error"]["code"], "invalid_signup")
        short = await client.post("/portal/signup", headers={"Origin": ORIGIN}, json={"business": "Short", "email": "s@example.com", "password": "short"})
        self.assertEqual(short.json()["error"]["code"], "invalid_signup")
        self.assertEqual((await self.signup(client, "d@example.com")).status_code, 201)
        self.assertEqual((await self.signup(client, "D@example.com")).json()["error"]["code"], "email_taken")
        wrong = await client.post("/portal/login", headers={"Origin": ORIGIN}, json={"email": "d@example.com", "password": "not the password"})
        self.assertEqual(wrong.json()["error"]["code"], "invalid_login")
        unknown = await client.post("/portal/login", headers={"Origin": ORIGIN}, json={"email": "nobody@example.com", "password": PASSWORD})
        self.assertEqual(unknown.json()["error"]["code"], "invalid_login")
        with self.app.state.ledger.database.connection() as connection:
            stored = connection.execute("SELECT password_hash FROM accounts").fetchone()[0]
        self.assertTrue(stored.startswith("scrypt$"))
        self.assertNotIn(PASSWORD, stored)

    async def test_closed_signup_is_refused(self):
        await self.start(signup_mode="closed")
        client = self.browser()
        self.assertEqual((await self.signup(client, "x@example.com")).json()["error"]["code"], "signup_closed")

    async def test_suspension_signs_the_account_out(self):
        await self.start(signup_mode="open")
        client = self.browser()
        await self.signup(client, "f@example.com")
        await self.login(client, "f@example.com")
        self.assertEqual((await client.get("/portal/session")).status_code, 200)
        accounts = (await client.get("/admin/accounts", headers=OWNER)).json()["accounts"]
        await client.post(f"/admin/accounts/{accounts[0]['id']}/state", headers=OWNER, json={"state": "suspended"})
        self.assertEqual((await client.get("/portal/session")).status_code, 401)
        self.assertEqual((await self.login(client, "f@example.com")).json()["error"]["code"], "account_suspended")

    async def test_client_saves_its_own_provider_key(self):
        await self.start(signup_mode="open")
        client = self.browser()
        await self.signup(client, "g@example.com")
        await self.login(client, "g@example.com")
        saved = await client.put("/portal/provider-keys/simulator", json={"api_key": PROVIDER_KEY})
        self.assertEqual(saved.status_code, 200, saved.text)
        self.assertNotIn(PROVIDER_KEY, saved.text)
        workspace = (await client.get("/portal/workspace")).json()
        self.assertEqual([row["provider"] for row in workspace["provider_keys"]], ["simulator"])
        self.assertNotIn(PROVIDER_KEY, str(workspace))

    async def test_portal_page_is_served_with_strict_headers(self):
        await self.start()
        page = await self.browser().get("/portal")
        self.assertEqual(page.status_code, 200)
        self.assertIn("default-src 'self'", page.headers["content-security-policy"])
        script = await self.browser().get("/assets/portal.js")
        self.assertEqual(script.status_code, 200)


if __name__ == "__main__":
    unittest.main()
