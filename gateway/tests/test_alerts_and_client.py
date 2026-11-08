"""Resilient client (3 tiers), email/SMS notifier and outside watchdog. Needs only httpx."""

import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "client"))
from resilient_client import AllowanceExhausted, OutageError, ResilientTollgate, UnknownOutcome  # noqa: E402

from app.services.notifier import Notifier  # noqa: E402
from app.services.watchdog import Watchdog  # noqa: E402


class Secret:
    def __init__(self, value):
        self.value = value

    def get_secret_value(self):
        return self.value


GPT_PRICE = {"provider": "openai", "upstream_model": "gpt-up", "input_rate": 750_000, "output_rate": 4_500_000,
             "cached_rate": 75_000, "cache_write_rate": 0, "max_output_tokens": 4096}
OPENAI_ANSWER = {"choices": [{"message": {"content": "direct answer"}}],
                 "usage": {"prompt_tokens": 1000, "completion_tokens": 180}}
RELAY_ANSWER = {"choices": [{"message": {"content": "hello"}}], "usage": {"prompt_tokens": 10, "completion_tokens": 5}}


class ClientTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.up, self.relay_up, self.calls, self.uploaded = False, True, [], []

        def handler(request: httpx.Request) -> httpx.Response:
            host = request.url.host
            self.calls.append(host)
            if host == "primary.test":
                if not self.up:
                    raise httpx.ConnectError("down", request=request)
                self.uploaded.append(json.loads(request.content))
                return httpx.Response(200, json={"accepted": 1, "duplicates": 0, "rejected": []})
            if host == "relay.test":
                if not self.relay_up:
                    raise httpx.ConnectError("down", request=request)
                return httpx.Response(200, json=RELAY_ANSWER, headers={
                    "X-Tollgate-Record-ID": "rly_abcdef123456", "X-Tollgate-Record-Time": "1700000000.5",
                    "X-Tollgate-Record-Sig": "s" * 30})
            if host == "api.openai.com":
                if self.provider_error:
                    raise self.provider_error('provider too slow', request=request)
                return httpx.Response(200, json=OPENAI_ANSWER)
            raise AssertionError(host)

        self.provider_error = None
        self.client = ResilientTollgate(
            "tg_key", "http://primary.test", "http://relay.test", journal_path=str(Path(self.temp.name) / "j.db"),
            transport=httpx.MockTransport(handler), models=["gpt-test"], direct_failover=True,
        )
        self.add_allowance(amount=100_000, direct=True)

    def add_allowance(self, amount, direct):
        self.client.db.execute(
            "INSERT OR REPLACE INTO allowance(id,token,expires_at,relay_url,models,closed,amount_micro,prices_json,direct_json) VALUES (?,?,?,?,?,0,?,?,?)",
            ("alw_1", "tga1.a.b", 9999999999, None, '["gpt-test"]', amount, json.dumps({"gpt-test": GPT_PRICE}),
             json.dumps({"keys": {"openai": "sk-restricted"}}) if direct else None),
        )

    def tearDown(self):
        self.client.close()
        self.temp.cleanup()

    MSG = [{"role": "user", "content": "hi"}]

    def test_relay_tier_is_used_first_and_prompt_is_journaled(self):
        seen = []
        self.client.on_failover = seen.append
        answer = self.client.chat(self.MSG, "gpt-test")
        self.assertEqual(answer["choices"][0]["message"]["content"], "hello")
        self.assertEqual((seen, self.calls), (["primary_unreachable"], ["primary.test", "relay.test"]))
        row = self.client.db.execute("SELECT * FROM calls").fetchone()
        self.assertEqual((row["via"], json.loads(row["prompt"])[0]["content"], json.loads(row["response"])), ("relay", "hi", "hello"))
        self.assertEqual(self.client.pending_sync(), 1)

    def test_goes_straight_to_the_provider_when_relay_is_down_too(self):
        self.relay_up = False
        answer = self.client.chat(self.MSG, "gpt-test")
        self.assertEqual((answer["choices"][0]["message"]["content"], answer["tollgate_via"]), ("direct answer", "direct"))
        self.assertEqual(self.calls, ["primary.test", "relay.test", "api.openai.com"])
        row = self.client.db.execute("SELECT * FROM calls").fetchone()
        self.assertEqual((row["via"], row["cost_micro"]), ("direct", 1560))
        self.assertTrue(row["record_id"].startswith("dir_"))

    def test_direct_only_order_skips_the_relay(self):
        self.client.order = ("direct",)
        self.client.chat(self.MSG, "gpt-test")
        self.assertEqual(self.calls, ["primary.test", "api.openai.com"])

    def test_history_is_uploaded_after_recovery_then_not_again(self):
        self.relay_up = False
        self.client.chat(self.MSG, "gpt-test")
        self.client.chat(self.MSG, "gpt-test")
        self.up = True
        self.client.sync(close=True)
        self.assertEqual(self.client.pending_sync(), 0)
        records = self.uploaded[0]["records"]
        self.assertEqual((len(records), records[0]["sig"], records[0]["input_tokens"]), (2, "", 1000))
        self.assertTrue(self.uploaded[0]["close"])
        self.client.sync()
        self.assertEqual(len(self.uploaded), 1)

    def test_local_allowance_guard_stops_overspending(self):
        self.relay_up = False
        self.add_allowance(amount=2_000, direct=True)  # one call costs 1,560; worst case is higher than what is left
        self.client.chat(self.MSG, "gpt-test")
        with self.assertRaises(AllowanceExhausted):
            self.client.chat(self.MSG, "gpt-test")

    def test_direct_is_never_used_without_opt_in(self):
        self.relay_up = False
        self.add_allowance(amount=100_000, direct=False)
        with self.assertRaises(OutageError):
            self.client.chat(self.MSG, "gpt-test")
        self.assertNotIn("api.openai.com", self.calls)

    def test_provider_read_timeout_is_not_retried_or_hidden(self):
        self.relay_up = False
        self.provider_error = httpx.ReadTimeout
        with self.assertRaises(UnknownOutcome):
            self.client.chat(self.MSG, "gpt-test")
        self.assertEqual(self.calls.count("api.openai.com"), 1)

    def test_no_allowance_raises_outage_error(self):
        self.client.db.execute("DELETE FROM allowance")
        with self.assertRaises(OutageError):
            self.client.chat(self.MSG, "gpt-test")


class FakeSMTP:
    sent = []

    def __init__(self, *_a, **_k):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *_a):
        return False

    def starttls(self):
        pass

    def login(self, *_a):
        pass

    def send_message(self, message):
        FakeSMTP.sent.append((message["To"], message["Subject"]))


def alert_settings(temp, **changes):
    values = dict(
        smtp_host="smtp.test", smtp_port=587, smtp_username="", smtp_password=Secret(""), smtp_from="noreply@tollgate.test",
        sms_webhook_url="http://sms.test/send", sms_webhook_token=Secret("tok"),
        watch_ready_url="http://primary.test/ready", watch_control_url="http://control.test", watch_interval_seconds=1,
        watch_failures_to_alert=3, watch_state_path=Path(temp) / "w.json", development_api_key=Secret("o" * 40),
    )
    values.update(changes)
    return SimpleNamespace(**values)


class AlertTests(unittest.IsolatedAsyncioTestCase):
    async def test_notifier_sends_email_and_sms(self):
        calls = []

        def sms(request):
            calls.append((json.loads(request.content), request.headers["authorization"]))
            return httpx.Response(200)

        with tempfile.TemporaryDirectory() as temp:
            FakeSMTP.sent = []
            result = Notifier(alert_settings(temp), transport=httpx.MockTransport(sms), smtp_factory=FakeSMTP).notify(
                [{"email": "a@example.com", "phone": "+923001234567"}, {"email": None, "phone": None}], "Subject", "Body")
        self.assertEqual(result, {"email": 1, "sms": 1, "failed": 0})
        self.assertEqual(FakeSMTP.sent, [("a@example.com", "Subject")])
        self.assertEqual((calls[0][0]["to"], calls[0][1]), ("+923001234567", "Bearer tok"))

    async def test_notifier_failures_are_counted_not_raised(self):
        with tempfile.TemporaryDirectory() as temp:
            bad = httpx.MockTransport(lambda r: httpx.Response(500))
            result = Notifier(alert_settings(temp, smtp_host=""), transport=bad).notify([{"email": "a@b.co", "phone": "+923001234567"}], "S", "B")
        # email is not configured and the SMS webhook returns 500: both undelivered notices are counted as failed
        self.assertEqual(result, {"email": 0, "sms": 0, "failed": 2})

    async def test_watchdog_alerts_once_per_outage_and_once_on_recovery(self):
        class Recorder:
            def __init__(self):
                self.sent = []

            def notify(self, contacts, subject, body):
                self.sent.append((len(contacts), subject))
                return {}

        state = {"up": False}

        def handler(request):
            if request.url.path == "/ready":
                return httpx.Response(200 if state["up"] else 503)
            return httpx.Response(200, json={"contacts": [{"email": "new@example.com"}, {"email": "b@example.com"}]})

        with tempfile.TemporaryDirectory() as temp:
            settings = alert_settings(temp)
            Path(settings.watch_state_path).write_text(json.dumps({"down": False, "contacts": [{"email": "a@example.com"}], "contacts_at": 9999999999}))
            recorder = Recorder()
            dog = Watchdog(settings, notifier=recorder)
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                outcomes = [await dog.tick(client) for _ in range(5)]
                state["up"] = True
                outcomes += [await dog.tick(client), await dog.tick(client)]
            saved = json.loads(Path(settings.watch_state_path).read_text())
        self.assertEqual(outcomes, [None, None, "outage", None, None, "recovery", None])
        self.assertEqual([s for _, s in recorder.sent], ["Tollgate service interruption", "Tollgate service restored"])
        self.assertEqual(recorder.sent[0][0], 1)  # outage used the cached contact list while control was unreachable
        self.assertFalse(saved["down"])

    async def test_watchdog_keeps_working_with_contacts_cached_before_the_outage(self):
        def handler(request):
            if request.url.path == "/ready":
                return httpx.Response(200)
            return httpx.Response(200, json={"contacts": [{"email": "fresh@example.com"}]})

        with tempfile.TemporaryDirectory() as temp:
            settings = alert_settings(temp)
            dog = Watchdog(settings, notifier=SimpleNamespace(notify=lambda *a: {}))
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                await dog.tick(client)
            saved = json.loads(Path(settings.watch_state_path).read_text())
        self.assertEqual(saved["contacts"], [{"email": "fresh@example.com"}])


if __name__ == "__main__":
    unittest.main()
