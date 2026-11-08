"""Offline continuity money rules: allowances, signed records, settlement, release."""

import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from app.database import Database
from app.routing.providers import MODELS, ModelSpec
from app.services.continuity import (
    Continuity,
    sign_allowance,
    sign_record,
    verify_allowance,
)
from app.services.ledger import BillingError, Ledger

SECRET = "s" * 48


class Secret:
    def __init__(self, value):
        self.value = value

    def get_secret_value(self):
        return self.value


def settings(**changes):
    values = dict(
        allowance_secret=Secret(SECRET),
        allowance_max_micro=5_000_000,
        allowance_max_ttl_minutes=1440,
        allowance_max_active=3,
        allowance_sync_grace_seconds=3600,
        relay_url="http://relay.test",
        emergency_direct_enabled=False,
        direct_customers=[],
        emergency_openai_key=Secret("sk-restricted-openai-test"),
        emergency_anthropic_key=Secret(""),
        emergency_gemini_key=Secret(""),
    )
    values.update(changes)
    return SimpleNamespace(**values)


class ContinuityCoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.database = Database(Path(self.temp.name) / "main.db")
        self.database.initialize()
        self.ledger = Ledger(self.database)
        self.cont = Continuity(self.database, self.ledger, MODELS, settings())
        self.customer = self.ledger.create_customer("Sohaib")["id"]
        self.key = self.ledger.create_key(self.customer, "app", ["sim-basic"])
        self.identity = {"customer_id": self.customer, "allowed_models": ["sim-basic"]}
        self.ledger.grant(self.customer, 1_000_000, "start")

    def tearDown(self):
        self.temp.cleanup()

    def record(self, allowance, rid, tokens=(1000, 100), when=None):
        record = {
            "record_id": rid,
            "model": "sim-basic",
            "input_tokens": tokens[0],
            "output_tokens": tokens[1],
            "cached_tokens": 0,
            "cache_write_tokens": 0,
            "created_at": when or time.time(),
        }
        record["sig"] = sign_record(SECRET, allowance["allowance_id"], record)
        return record

    def issue(self, amount=400_000, minutes=60):
        return self.cont.issue(self.identity, amount, minutes, ["sim-basic"])

    def test_token_round_trip_and_tamper_detection(self):
        token = sign_allowance(SECRET, {"aid": "a", "cid": "c", "amt": 5, "exp": time.time() + 60, "models": ["m"]})
        self.assertEqual(verify_allowance(SECRET, token)["aid"], "a")
        head, body, sig = token.split(".")
        forged = ".".join([head, body[:-2] + "AA", sig])
        with self.assertRaises(ValueError):
            verify_allowance(SECRET, forged)
        with self.assertRaises(ValueError):
            verify_allowance("x" * 48, token)
        with self.assertRaises(ValueError):
            verify_allowance(SECRET, token, now=time.time() + 3600)

    def test_issue_holds_money_and_reconciles(self):
        allowance = self.issue()
        wallet = self.ledger.balance(self.customer)
        self.assertEqual(wallet["reserved_usd"], "0.400000")
        self.assertEqual(wallet["available_usd"], "0.600000")
        self.assertTrue(self.ledger.reconciliation()["ok"])
        self.assertEqual(verify_allowance(SECRET, allowance["token"])["amt"], 400_000)

    def test_issue_refuses_more_than_available_and_too_many(self):
        with self.assertRaises(BillingError) as raised:
            self.cont.issue(self.identity, 1_000_001, 60, ["sim-basic"])
        self.assertEqual(raised.exception.code, "insufficient_credits")
        for _ in range(3):
            self.issue(10_000)
        with self.assertRaises(BillingError) as raised:
            self.issue(10_000)
        self.assertEqual(raised.exception.code, "too_many_allowances")

    def test_sync_charges_once_and_is_idempotent(self):
        allowance = self.issue()
        rec = self.record(allowance, "rly_00000001")
        first = self.cont.sync(self.customer, allowance["allowance_id"], [rec], close=False)
        again = self.cont.sync(self.customer, allowance["allowance_id"], [rec], close=False)
        self.assertEqual((first["accepted"], again["accepted"], again["duplicates"]), (1, 0, 1))
        # 1000 in x $2/M + 100 out x $8/M = 0.002 + 0.0008
        self.assertEqual(self.ledger.balance(self.customer)["spent_usd"], "0.002800")
        self.assertEqual(self.ledger.balance(self.customer)["reserved_usd"], "0.397200")
        self.assertTrue(self.ledger.reconciliation()["ok"])

    def test_edited_token_counts_are_rejected(self):
        allowance = self.issue()
        rec = self.record(allowance, "rly_00000002")
        rec["input_tokens"] = 1  # customer tries to under-report
        result = self.cont.sync(self.customer, allowance["allowance_id"], [rec], close=False)
        self.assertEqual(result["rejected"][0]["reason"], "bad_signature")
        self.assertEqual(self.ledger.balance(self.customer)["spent_usd"], "0.000000")

    def test_usage_beyond_allowance_is_capped_not_overdrawn(self):
        allowance = self.issue(amount=3_000)  # $0.003
        recs = [self.record(allowance, f"rly_0000010{i}") for i in range(2)]  # 2800 each
        result = self.cont.sync(self.customer, allowance["allowance_id"], recs, close=True)
        self.assertEqual(result["accepted"], 2)
        self.assertEqual(self.ledger.balance(self.customer)["spent_usd"], "0.003000")
        self.assertEqual(self.ledger.balance(self.customer)["reserved_usd"], "0.000000")
        self.assertTrue(self.ledger.reconciliation()["ok"])

    def test_close_releases_remainder_once(self):
        allowance = self.issue()
        self.cont.sync(self.customer, allowance["allowance_id"], [], close=True)
        self.cont.sync(self.customer, allowance["allowance_id"], [], close=True)
        wallet = self.ledger.balance(self.customer)
        self.assertEqual((wallet["reserved_usd"], wallet["available_usd"]), ("0.000000", "1.000000"))
        late = self.record(allowance, "rly_00000200")
        result = self.cont.sync(self.customer, allowance["allowance_id"], [late], close=False)
        self.assertEqual(result["rejected"][0]["reason"], "allowance_closed")

    def test_sweeper_releases_expired_allowance_after_grace(self):
        cont = Continuity(self.database, self.ledger, MODELS, settings(allowance_sync_grace_seconds=60))
        allowance = cont.issue(self.identity, 250_000, 5, ["sim-basic"])
        with self.database.transaction() as c:
            c.execute("UPDATE allowances SET expires_at=? WHERE id=?", (time.time() - 120, allowance["allowance_id"]))
        self.assertEqual(cont.close_expired(), 1)
        self.assertEqual(cont.close_expired(), 0)
        self.assertEqual(self.ledger.balance(self.customer)["reserved_usd"], "0.000000")
        self.assertTrue(self.ledger.reconciliation()["ok"])

    def test_other_customer_cannot_sync_allowance(self):
        allowance = self.issue()
        other = self.ledger.create_customer("Other")["id"]
        with self.assertRaises(BillingError) as raised:
            self.cont.sync(other, allowance["allowance_id"], [], close=False)
        self.assertEqual(raised.exception.status, 404)

    def test_disabled_without_strong_secret(self):
        cont = Continuity(self.database, self.ledger, MODELS, settings(allowance_secret=Secret("short")))
        with self.assertRaises(BillingError) as raised:
            cont.issue(self.identity, 1000, 60, ["sim-basic"])
        self.assertEqual(raised.exception.code, "offline_mode_disabled")

    def test_contacts_are_stored_and_listed(self):
        self.cont.set_contact(self.customer, "sohaib@example.com", "+923001234567")
        self.cont.set_contact(self.customer, "new@example.com", None)
        self.assertEqual(self.cont.contacts()[0]["email"], "new@example.com")
        self.assertIsNone(self.cont.contacts()[0]["phone"])

    # ----- direct emergency access -----
    def direct_setup(self, customers=None, **changes):
        models = dict(MODELS)
        models["gpt-test"] = ModelSpec(
            "gpt-test", 750_000, 4_500_000, provider="openai", upstream_model="gpt-upstream", cached_rate=75_000
        )
        models["claude-test"] = ModelSpec("claude-test", 1_000_000, 5_000_000, provider="anthropic", upstream_model="claude-upstream")
        cont = Continuity(
            self.database,
            self.ledger,
            models,
            settings(
                emergency_direct_enabled=True,
                direct_customers=[self.customer] if customers is None else customers,
                **changes,
            ),
        )
        identity = {"customer_id": self.customer, "allowed_models": ["gpt-test", "claude-test", "sim-basic"]}
        return cont, identity

    def test_direct_keys_are_off_by_default_and_per_customer(self):
        with self.assertRaises(BillingError) as raised:
            self.cont.issue(self.identity, 10_000, 60, ["sim-basic"], include_direct=True)
        self.assertEqual(raised.exception.code, "direct_mode_disabled")
        cont, identity = self.direct_setup(customers=["cus_other"])
        with self.assertRaises(BillingError) as raised:
            cont.issue(identity, 10_000, 60, ["gpt-test"], include_direct=True)
        self.assertEqual(raised.exception.code, "direct_not_permitted")

    def test_direct_issue_returns_only_needed_provider_keys_and_prices(self):
        cont, identity = self.direct_setup()
        result = cont.issue(identity, 100_000, 60, ["gpt-test"], include_direct=True)
        self.assertEqual(list(result["direct"]["keys"]), ["openai"])
        self.assertEqual(result["prices"]["gpt-test"]["upstream_model"], "gpt-upstream")
        self.assertTrue(self.ledger.reconciliation()["ok"])
        self.assertTrue(cont.allowances()[0]["direct"])
        plain = cont.issue(identity, 10_000, 60, ["gpt-test"])
        self.assertNotIn("direct", plain)

    def test_direct_issue_needs_a_key_for_every_provider(self):
        cont, identity = self.direct_setup()
        with self.assertRaises(BillingError) as raised:
            cont.issue(identity, 10_000, 60, ["claude-test"], include_direct=True)
        self.assertEqual(raised.exception.code, "no_emergency_key")

    def test_direct_records_are_charged_as_unverified_and_capped(self):
        cont, identity = self.direct_setup()
        allowance = cont.issue(identity, 2_000, 60, ["gpt-test"], include_direct=True)
        rec = lambda rid: {  # noqa: E731
            "record_id": rid, "model": "gpt-test", "input_tokens": 1000, "output_tokens": 100,
            "cached_tokens": 0, "cache_write_tokens": 0, "created_at": time.time(), "sig": "",
        }
        result = cont.sync(self.customer, allowance["allowance_id"], [rec("dir_" + "a" * 32), rec("dir_" + "b" * 32)], close=True)
        self.assertEqual(result["accepted"], 2)
        # 1000 x $0.75/M + 100 x $4.50/M = 1200 micro each; the second is capped at 800 left
        self.assertEqual(self.ledger.balance(self.customer)["spent_usd"], "0.002000")
        with self.database.connection() as c:
            statuses = sorted(r[0] for r in c.execute("SELECT status FROM offline_records"))
        self.assertEqual(statuses, ["capped_unverified", "settled_unverified"])
        self.assertTrue(self.ledger.reconciliation()["ok"])

    def test_direct_records_rejected_on_a_non_direct_allowance(self):
        allowance = self.issue()
        rec = {"record_id": "dir_" + "c" * 32, "model": "sim-basic", "input_tokens": 10, "output_tokens": 10,
               "cached_tokens": 0, "cache_write_tokens": 0, "created_at": time.time(), "sig": ""}
        result = self.cont.sync(self.customer, allowance["allowance_id"], [rec], close=False)
        self.assertEqual(result["rejected"][0]["reason"], "direct_not_enabled")

    def test_unsigned_non_direct_record_is_rejected(self):
        allowance = self.issue()
        rec = {"record_id": "rly_" + "d" * 32, "model": "sim-basic", "input_tokens": 10, "output_tokens": 10,
               "cached_tokens": 0, "cache_write_tokens": 0, "created_at": time.time(), "sig": ""}
        result = self.cont.sync(self.customer, allowance["allowance_id"], [rec], close=False)
        self.assertEqual(result["rejected"][0]["reason"], "bad_signature")

    def test_schema_four_database_gains_the_direct_column(self):
        with self.database.connection() as c:
            c.execute("ALTER TABLE allowances DROP COLUMN direct")
            c.execute("PRAGMA user_version=4")
        self.database.initialize()
        self.database.initialize()
        self.database.check_version()
        self.assertIn("direct", self.cont.allowances()[0] if self.cont.allowances() else {"direct": 0})
        with self.database.connection() as c:
            columns = [r[1] for r in c.execute("PRAGMA table_info(allowances)")]
        self.assertIn("direct", columns)

    def test_schema_five_preserves_existing_balance(self):
        with self.database.connection() as c:
            for t in ("allowances", "offline_records", "customer_contacts"):
                c.execute(f"DROP TABLE {t}")
            c.execute("PRAGMA user_version=3")
        self.database.initialize()
        self.database.check_version()
        self.assertEqual(self.ledger.balance(self.customer)["balance_usd"], "1.000000")


if __name__ == "__main__":
    unittest.main()
