"""Record node attendance and fence expired processes before reserving money."""

import asyncio
import json
import logging
import uuid

from app.services.ledger import BillingError, Ledger

logger = logging.getLogger("tollgate.cluster")


class Cluster:
    def __init__(self, database, settings, role=None):
        self.database, self.settings = database, settings
        self.node_id = settings.node_id
        self.role = role or settings.service_role
        self.instance_id = uuid.uuid4().hex

    def now(self, connection):
        # One database clock avoids disagreements between hosts' clocks.
        sql = (
            "SELECT EXTRACT(EPOCH FROM clock_timestamp())"
            if self.database.url
            else "SELECT CAST(strftime('%s','now') AS REAL)"
        )
        return float(connection.execute(sql).fetchone()[0])

    def claim(self):
        """Reject duplicate live node IDs; keep an operator's drain flag on restart."""
        with self.database.transaction() as connection:
            now = self.now(connection)
            old = connection.execute(
                "SELECT * FROM cluster_nodes WHERE node_id=?", (self.node_id,)
            ).fetchone()
            if old and now - old["seen_at"] <= self.settings.node_stale_seconds:
                raise RuntimeError(
                    "Node ID is already active. Use a unique ID or wait for its lease to expire."
                )
            connection.execute(
                "INSERT INTO cluster_nodes(node_id,role,instance_id,seen_at,started_at) VALUES (?,?,?,?,?) "
                "ON CONFLICT(node_id) DO UPDATE SET role=excluded.role,instance_id=excluded.instance_id,seen_at=excluded.seen_at,started_at=excluded.started_at",
                (self.node_id, self.role, self.instance_id, now, now),
            )

    def beat(self):
        """Only the current process may refresh its unexpired lease."""
        with self.database.transaction() as connection:
            now = self.now(connection)
            changed = connection.execute(
                "UPDATE cluster_nodes SET seen_at=? WHERE node_id=? AND instance_id=? AND seen_at>=?",
                (
                    now,
                    self.node_id,
                    self.instance_id,
                    now - self.settings.node_stale_seconds,
                ),
            ).rowcount
            if changed != 1:
                raise RuntimeError(
                    "Node lease lost; restart this process before admitting requests."
                )

    def leave(self):
        with self.database.transaction() as connection:
            connection.execute(
                "UPDATE cluster_nodes SET seen_at=0 WHERE node_id=? AND instance_id=?",
                (self.node_id, self.instance_id),
            )

    async def run(self):
        while True:
            await asyncio.sleep(self.settings.heartbeat_seconds)
            try:
                await asyncio.to_thread(self.beat)
            except Exception as exc:
                logger.error(
                    "Heartbeat failed node=%s type=%s", self.node_id, type(exc).__name__
                )

    def require_admission(self, connection):
        """Check the lease, drain flag, and control policy inside the credit transaction."""
        now = self.now(connection)
        own = connection.execute(
            "SELECT * FROM cluster_nodes WHERE node_id=?", (self.node_id,)
        ).fetchone()
        if (
            not own
            or own["instance_id"] != self.instance_id
            or now - own["seen_at"] > self.settings.node_stale_seconds
        ):
            raise BillingError(
                503,
                "warden_unavailable",
                "This warden has lost its lease. Use the API address to reach a healthy warden.",
            )
        if own["draining"]:
            raise BillingError(
                503,
                "warden_draining",
                "This warden is draining. Existing requests may finish.",
            )
        worker = connection.execute(
            "SELECT seen_at FROM service_heartbeats WHERE name='worker'"
        ).fetchone()
        if not worker or now - worker[0] >= 60:
            raise BillingError(
                503,
                "worker_unavailable",
                "Settlement is unavailable. New generation is temporarily paused.",
            )
        control = connection.execute(
            "SELECT 1 FROM cluster_nodes WHERE role IN ('control','standalone') AND seen_at>=? AND draining=0 LIMIT 1",
            (now - self.settings.node_stale_seconds,),
        ).fetchone()
        degraded = not bool(control)
        if degraded and self.settings.control_failure_policy == "fail_closed":
            raise BillingError(
                503,
                "control_unavailable",
                "The control service is unavailable and this warden uses fail_closed.",
            )
        return {"warden_id": self.node_id, "control_degraded": degraded}

    def check_admission(self):
        with self.database.connection() as connection:
            return self.require_admission(connection)

    def drain(self, node_id, draining, reason):
        with self.database.transaction() as connection:
            row = connection.execute(
                "SELECT role FROM cluster_nodes WHERE node_id=?", (node_id,)
            ).fetchone()
            if not row or row["role"] != "warden":
                raise BillingError(
                    404, "warden_not_found", "Choose a registered warden."
                )
            connection.execute(
                "UPDATE cluster_nodes SET draining=? WHERE node_id=?",
                (int(draining), node_id),
            )
            Ledger.audit(
                connection,
                "warden_drain_changed",
                node_id,
                {"draining": draining, "reason": reason},
            )
        return {"node_id": node_id, "draining": draining}

    def snapshot(self):
        """Expose operational metadata, never credentials or request bodies."""
        with self.database.connection() as connection:
            now = self.now(connection)
            rows = [
                dict(row)
                for row in connection.execute(
                    "SELECT * FROM cluster_nodes ORDER BY role,node_id"
                )
            ]
            worker = connection.execute(
                "SELECT seen_at FROM service_heartbeats WHERE name='worker'"
            ).fetchone()
            pending = connection.execute(
                "SELECT COUNT(*) FROM settlement_jobs WHERE state='pending'"
            ).fetchone()[0]
            unresolved = connection.execute(
                "SELECT COUNT(*) FROM requests WHERE state='unresolved'"
            ).fetchone()[0]
            report = connection.execute(
                "SELECT checked_at,report_json FROM patrol_reports ORDER BY checked_at DESC LIMIT 1"
            ).fetchone()
        nodes = []
        for row in rows:
            age = max(0, now - row["seen_at"])
            status = (
                "offline"
                if age > self.settings.node_stale_seconds
                else "draining" if row["draining"] else "online"
            )
            nodes.append(
                {
                    "node_id": row["node_id"],
                    "role": row["role"],
                    "status": status,
                    "last_seen_seconds": age,
                    "started_at": row["started_at"],
                }
            )
        known = {row["node_id"] for row in nodes}
        for node in self.settings.expected_wardens:
            if node not in known:
                nodes.append(
                    {
                        "node_id": node,
                        "role": "warden",
                        "status": "missing",
                        "last_seen_seconds": None,
                        "started_at": None,
                    }
                )
        patrol_age = max(0, now - report["checked_at"]) if report else None
        return {
            "role": self.role,
            "node_id": self.node_id,
            "control_failure_policy": self.settings.control_failure_policy,
            "heartbeat_seconds": self.settings.heartbeat_seconds,
            "node_stale_seconds": self.settings.node_stale_seconds,
            "nodes": nodes,
            "pending_settlements": pending,
            "unresolved_requests": unresolved,
            "worker_recent": bool(worker and now - worker[0] < 60),
            "patrol_recent": patrol_age is not None
            and patrol_age <= self.settings.node_stale_seconds,
            "patrol": json.loads(report["report_json"]) if report else None,
        }
