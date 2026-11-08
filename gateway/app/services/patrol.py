"""Monitor the cluster independently; never refund or repeat an AI request."""

import asyncio
import json
import logging

from app.services.cluster import Cluster

logger = logging.getLogger("tollgate.patrol")


class Patrol:
    def __init__(self, database, settings):
        self.cluster = Cluster(database, settings, role="patrol")

    def tick(self):
        """Persist missing-node and accounting-backlog findings for the operator."""
        self.cluster.beat()
        state = self.cluster.snapshot()
        findings = [
            {"code": "node_" + row["status"], "node_id": row["node_id"]}
            for row in state["nodes"]
            if row["status"] in ("offline", "missing")
        ]
        if not any(
            row["role"] in ("control", "standalone") and row["status"] == "online"
            for row in state["nodes"]
        ):
            findings.append({"code": "control_unavailable"})
        if not state["worker_recent"]:
            findings.append({"code": "settlement_worker_stale"})
        if state["unresolved_requests"]:
            findings.append(
                {"code": "usage_needs_review", "count": state["unresolved_requests"]}
            )
        report = {
            "findings": findings,
            "pending_settlements": state["pending_settlements"],
            "node_count": len(state["nodes"]),
        }
        with self.cluster.database.transaction() as connection:
            connection.execute(
                "INSERT INTO patrol_reports VALUES (?,?,?) ON CONFLICT(node_id) DO UPDATE SET checked_at=excluded.checked_at,report_json=excluded.report_json",
                (
                    self.cluster.node_id,
                    self.cluster.now(connection),
                    json.dumps(report, sort_keys=True),
                ),
            )
        return report

    async def run(self):
        previous = None
        while True:
            try:
                report = await asyncio.to_thread(self.tick)
                signature = json.dumps(report["findings"], sort_keys=True)
                if signature != previous:
                    logger.info("Patrol findings=%s", signature)
                    previous = signature
            except Exception as exc:
                logger.error("Patrol tick failed type=%s", type(exc).__name__)
            await asyncio.sleep(self.cluster.settings.heartbeat_seconds)


async def run_patrol(settings):
    from app.database import Database
    from app.logging.request_log import configure_logging

    configure_logging()
    database = Database(
        settings.database_path,
        settings.database_url.get_secret_value() if settings.database_url else None,
    )
    patrol = Patrol(database, settings)
    await asyncio.to_thread(database.check_version)
    await asyncio.to_thread(patrol.cluster.claim)
    try:
        await patrol.run()
    finally:
        try:
            await asyncio.to_thread(patrol.cluster.leave)
        finally:
            await asyncio.to_thread(database.close)
