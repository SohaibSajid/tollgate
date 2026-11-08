"""Terminate real child processes to verify durable commits and rollback recovery."""

import multiprocessing
import os
import tempfile
import unittest
from pathlib import Path

from app.database import Database
from app.services.ledger import Ledger
from app.routing.providers import ModelSpec


def crash_child(path, identity, stage):
    """Exit without cleanup at a selected billing boundary, as a killed process would."""
    database = Database(Path(path))
    ledger = Ledger(database)
    ledger.reserve(
        "req_crash",
        identity,
        "crash-idem",
        "crash-fingerprint",
        ModelSpec("sim-basic", 1_000_000, 1_000_000),
        "crash-test",
        400_000,
        400_000,
    )
    if stage == "reserved":
        os._exit(19)
    ledger.dispatch("req_crash")
    if stage == "dispatched":
        os._exit(19)
    ledger.record_usage("req_crash", 100_000, 200_000)
    if stage == "usage_recorded":
        os._exit(19)
    # Leave an uncommitted ledger insert and verify SQLite removes it after the crash.
    connection = database.connect()
    connection.execute("BEGIN IMMEDIATE")
    connection.execute(
        "INSERT INTO ledger(customer_id,kind,amount_micro,reference,request_id,created_at) VALUES (?,'charge',-300000,'req_crash','req_crash',0)",
        (identity["customer_id"],),
    )
    os._exit(19)


class CrashRecoveryTests(unittest.TestCase):
    def test_crash_boundaries_preserve_money_and_jobs(self):
        """A new process must safely handle pre-dispatch, unknown, queued, and partial work."""
        for stage in ("reserved", "dispatched", "usage_recorded", "during_settlement"):
            with self.subTest(stage=stage), tempfile.TemporaryDirectory() as directory:
                database = Database(Path(directory) / "wallet.db")
                database.initialize()
                ledger = Ledger(database)
                customer = ledger.create_customer("Crash test")["id"]
                key = ledger.create_key(customer, "Test", ["sim-basic"])["api_key"]
                identity = ledger.authenticate(key.encode())
                ledger.grant(customer, 1_000_000, "initial")
                process = multiprocessing.get_context("spawn").Process(
                    target=crash_child, args=(str(database.path), identity, stage)
                )
                process.start()
                process.join(timeout=20)
                if process.is_alive():
                    process.terminate()
                    process.join()
                    self.fail("Crash test child did not finish")
                self.assertEqual(process.exitcode, 19)
                reopened = Ledger(Database(database.path))
                with database.transaction() as connection:
                    connection.execute(
                        "UPDATE requests SET updated_at=0 WHERE id='req_crash'"
                    )
                reopened.recovery_candidates(1)
                if stage == "reserved":
                    self.assertFalse(reopened.dispatch("req_crash"))
                    self.assertEqual(
                        reopened.balance(customer)["available_usd"], "1.000000"
                    )
                elif stage == "dispatched":
                    self.assertFalse(reopened.release("req_crash", "timeout"))
                    self.assertEqual(
                        reopened.balance(customer)["reserved_usd"], "0.800000"
                    )
                else:
                    self.assertEqual(reopened.pending_jobs(), ["req_crash"])
                    self.assertTrue(reopened.settle("req_crash"))
                    self.assertEqual(
                        reopened.balance(customer)["available_usd"], "0.700000"
                    )
                    self.assertEqual(
                        reopened.balance(customer)["reserved_usd"], "0.000000"
                    )
                    charges = [
                        row
                        for row in reopened.overview()["ledger"]
                        if row["kind"] == "charge"
                    ]
                    self.assertEqual(len(charges), 1)


if __name__ == "__main__":
    unittest.main()
