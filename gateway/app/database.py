"""SQLite connections, durable schema, and short write transactions."""

import sqlite3
import re
import threading
from contextlib import contextmanager
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS customers (
    id TEXT PRIMARY KEY, name TEXT NOT NULL, created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS wallets (
    customer_id TEXT PRIMARY KEY REFERENCES customers(id),
    balance_micro INTEGER NOT NULL DEFAULT 0 CHECK(balance_micro >= 0),
    held_micro INTEGER NOT NULL DEFAULT 0 CHECK(held_micro >= 0 AND held_micro <= balance_micro),
    spent_micro INTEGER NOT NULL DEFAULT 0 CHECK(spent_micro >= 0),
    spend_cap_micro INTEGER CHECK(spend_cap_micro IS NULL OR spend_cap_micro >= 0)
);
CREATE TABLE IF NOT EXISTS api_keys (
    id TEXT PRIMARY KEY, customer_id TEXT NOT NULL REFERENCES customers(id),
    label TEXT NOT NULL, prefix TEXT NOT NULL, key_hash TEXT UNIQUE NOT NULL,
    allowed_models TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS requests (
    id TEXT PRIMARY KEY, customer_id TEXT NOT NULL REFERENCES customers(id),
    key_id TEXT NOT NULL REFERENCES api_keys(id), idempotency_key TEXT NOT NULL,
    request_hash TEXT NOT NULL, model TEXT NOT NULL, feature TEXT NOT NULL,
    state TEXT NOT NULL CHECK(state IN ('reserved','dispatched','unresolved','usage_recorded','settled','released')),
    reserve_micro INTEGER NOT NULL CHECK(reserve_micro >= 0),
    price_version TEXT NOT NULL, input_rate INTEGER NOT NULL, output_rate INTEGER NOT NULL,
    input_bound INTEGER NOT NULL, output_bound INTEGER NOT NULL,
    input_tokens INTEGER, output_tokens INTEGER, actual_micro INTEGER,
    error_code TEXT, created_at REAL NOT NULL, updated_at REAL NOT NULL,
    last_recovery_at REAL NOT NULL DEFAULT 0,
    UNIQUE(customer_id, idempotency_key)
);
CREATE INDEX IF NOT EXISTS request_customer_time ON requests(customer_id, created_at DESC);
CREATE INDEX IF NOT EXISTS request_recovery ON requests(state, updated_at, last_recovery_at);
CREATE TABLE IF NOT EXISTS ledger (
    id INTEGER PRIMARY KEY AUTOINCREMENT, customer_id TEXT NOT NULL REFERENCES customers(id),
    kind TEXT NOT NULL CHECK(kind IN ('grant','charge')),
    amount_micro INTEGER NOT NULL, reference TEXT NOT NULL,
    request_id TEXT REFERENCES requests(id), created_at REAL NOT NULL,
    UNIQUE(customer_id, kind, reference),
    CHECK((kind='grant' AND amount_micro > 0) OR (kind='charge' AND amount_micro <= 0))
);
CREATE TRIGGER IF NOT EXISTS ledger_no_update BEFORE UPDATE ON ledger
BEGIN SELECT RAISE(ABORT, 'Ledger entries are immutable'); END;
CREATE TRIGGER IF NOT EXISTS ledger_no_delete BEFORE DELETE ON ledger
BEGIN SELECT RAISE(ABORT, 'Ledger entries are immutable'); END;
CREATE TABLE IF NOT EXISTS settlement_jobs (
    request_id TEXT PRIMARY KEY REFERENCES requests(id),
    state TEXT NOT NULL DEFAULT 'pending' CHECK(state IN ('pending','done','blocked')),
    created_at REAL NOT NULL
);
"""


# Version 2 adds metadata only; existing wallet and ledger values are preserved.
ADDITIONS = {
    "wallets": {"frozen": "INTEGER NOT NULL DEFAULT 0"},
    "api_keys": {"expires_at": "REAL"},
    "requests": {
        "provider": "TEXT NOT NULL DEFAULT 'simulator'",
        "provider_response_id": "TEXT",
        "cached_tokens": "INTEGER NOT NULL DEFAULT 0",
        "cache_write_tokens": "INTEGER NOT NULL DEFAULT 0",
        "cached_rate": "INTEGER NOT NULL DEFAULT 0",
        "cache_write_rate": "INTEGER NOT NULL DEFAULT 0",
        "usage_evidence": "TEXT",
        "resolved_by": "TEXT",
    },
}
EXTRA_SCHEMA = """
CREATE TABLE IF NOT EXISTS cluster_nodes (
 node_id TEXT PRIMARY KEY, role TEXT NOT NULL, instance_id TEXT NOT NULL,
 seen_at REAL NOT NULL, started_at REAL NOT NULL,
 draining INTEGER NOT NULL DEFAULT 0 CHECK(draining IN (0,1))
);
CREATE TABLE IF NOT EXISTS patrol_reports (
 node_id TEXT PRIMARY KEY, checked_at REAL NOT NULL, report_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS audit_events (
 id INTEGER PRIMARY KEY AUTOINCREMENT, action TEXT NOT NULL, target TEXT NOT NULL,
 detail TEXT NOT NULL, created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
 token_hash TEXT PRIMARY KEY, csrf TEXT NOT NULL, admin_hash TEXT NOT NULL, expires_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS rate_windows (
 subject TEXT NOT NULL, bucket INTEGER NOT NULL, count INTEGER NOT NULL,
 PRIMARY KEY(subject, bucket)
);
CREATE TABLE IF NOT EXISTS service_heartbeats (
 name TEXT PRIMARY KEY, seen_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS payment_orders (
 id TEXT PRIMARY KEY, customer_id TEXT NOT NULL REFERENCES customers(id),
 amount_micro INTEGER NOT NULL CHECK(amount_micro > 0), reference TEXT NOT NULL,
 session_id TEXT UNIQUE, payment_intent TEXT UNIQUE, state TEXT NOT NULL DEFAULT 'pending',
 created_at REAL NOT NULL, UNIQUE(customer_id, reference)
);
CREATE TABLE IF NOT EXISTS payment_events (
 id TEXT PRIMARY KEY, event_type TEXT NOT NULL, created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS allowances (
 id TEXT PRIMARY KEY, customer_id TEXT NOT NULL REFERENCES customers(id),
 amount_micro INTEGER NOT NULL CHECK(amount_micro > 0),
 spent_micro INTEGER NOT NULL DEFAULT 0 CHECK(spent_micro >= 0 AND spent_micro <= amount_micro),
 models TEXT NOT NULL, expires_at REAL NOT NULL, created_at REAL NOT NULL,
 state TEXT NOT NULL DEFAULT 'active' CHECK(state IN ('active','closed')),
 direct INTEGER NOT NULL DEFAULT 0 CHECK(direct IN (0,1))
);
CREATE TABLE IF NOT EXISTS offline_records (
 allowance_id TEXT NOT NULL REFERENCES allowances(id), record_id TEXT NOT NULL,
 customer_id TEXT NOT NULL REFERENCES customers(id), model TEXT NOT NULL,
 input_tokens INTEGER NOT NULL, output_tokens INTEGER NOT NULL,
 cached_tokens INTEGER NOT NULL DEFAULT 0, cache_write_tokens INTEGER NOT NULL DEFAULT 0,
 cost_micro INTEGER NOT NULL, status TEXT NOT NULL,
 created_at REAL NOT NULL, imported_at REAL NOT NULL,
 PRIMARY KEY(allowance_id, record_id)
);
CREATE TABLE IF NOT EXISTS customer_contacts (
 customer_id TEXT PRIMARY KEY REFERENCES customers(id), email TEXT, phone TEXT
);
"""


class Record(dict):
    """Offer SQLite-compatible named and positional reads for PostgreSQL rows."""

    def __getitem__(self, key):
        return (
            list(self.values())[key]
            if isinstance(key, int)
            else super().__getitem__(key)
        )


class PGConnection:
    def __init__(self, connection):
        self.raw = connection

    def execute(self, sql, parameters=()):
        # All SQL is application-owned. Values always remain bound parameters.
        return self.raw.execute(sql.replace("?", "%s"), parameters)

    @property
    def in_transaction(self):
        from psycopg.pq import TransactionStatus

        return self.raw.info.transaction_status != TransactionStatus.IDLE


def pg_schema(sql):
    sql = re.sub(r"CREATE TRIGGER IF NOT EXISTS.*?END;", "", sql, flags=re.S)
    sql = sql.replace("INTEGER PRIMARY KEY AUTOINCREMENT", "BIGSERIAL PRIMARY KEY")
    return re.sub(
        r"\bREAL\b", "DOUBLE PRECISION", re.sub(r"\bINTEGER\b", "BIGINT", sql)
    )


class Database:
    def __init__(self, path: Path, url: str | None = None):
        self.path = path.resolve()
        self.url, self.pool = url, None
        self._pool_lock = threading.Lock()

    def _get_pool(self):
        from psycopg_pool import ConnectionPool
        from psycopg.rows import dict_row

        with self._pool_lock:
            if self.pool is None:

                def row_factory(cursor):
                    factory = dict_row(cursor)
                    return lambda values: Record(factory(values))

                self.pool = ConnectionPool(
                    self.url,
                    min_size=1,
                    max_size=16,
                    timeout=5,
                    kwargs={
                        "autocommit": True,
                        "row_factory": row_factory,
                        "options": "-c statement_timeout=10000 -c lock_timeout=5000",
                    },
                    open=False,
                )
                self.pool.open()
        return self.pool

    def connect(self):
        connection = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=5000")
        connection.execute("PRAGMA synchronous=FULL")
        return connection

    @contextmanager
    def connection(self):
        if self.url:
            with self._get_pool().connection() as connection:
                yield PGConnection(connection)
        else:
            connection = self.connect()
            try:
                yield connection
            finally:
                connection.close()

    @contextmanager
    def transaction(self):
        """Serialize brief money writes across processes; network I/O never holds this lock."""
        with self.connection() as connection:
            connection.execute("BEGIN" if self.url else "BEGIN IMMEDIATE")
            try:
                if self.url:
                    connection.execute("SELECT pg_advisory_xact_lock(721914620)")
                yield connection
                connection.execute("COMMIT")
            except BaseException:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
                raise

    def initialize(self):
        """Apply additive migrations under an exclusive database migration lock."""
        if not self.url:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.transaction() as connection:
            if self.url:
                connection.execute(
                    "CREATE TABLE IF NOT EXISTS schema_version (version BIGINT NOT NULL)"
                )
                row = connection.execute(
                    "SELECT version FROM schema_version"
                ).fetchone()
                version = row[0] if row else 0
            else:
                version = connection.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 1, 2, 3, 4, 5):
                raise RuntimeError(
                    "Unsupported database version; restore the matching application."
                )
            # sqlite executescript commits implicitly; execute individual DDL statements instead.
            base = re.sub(
                r"CREATE TRIGGER IF NOT EXISTS.*?END;", "", SCHEMA, flags=re.S
            )
            for statement in (
                pg_schema(base + EXTRA_SCHEMA) if self.url else base + EXTRA_SCHEMA
            ).split(";"):
                if statement.strip():
                    connection.execute(statement)
            if version < 2:
                for table, fields in ADDITIONS.items():
                    for field, definition in fields.items():
                        ddl = f"ALTER TABLE {table} ADD COLUMN {field} {definition}"
                        connection.execute(pg_schema(ddl) if self.url else ddl)
            if version < 3:
                connection.execute("ALTER TABLE requests ADD COLUMN warden_id TEXT")
                connection.execute(
                    "ALTER TABLE requests ADD COLUMN control_degraded INTEGER NOT NULL DEFAULT 0"
                )
            if version == 4:
                ddl = "ALTER TABLE allowances ADD COLUMN direct INTEGER NOT NULL DEFAULT 0"
                connection.execute(pg_schema(ddl) if self.url else ddl)
            for table in ("ledger", "audit_events"):
                if self.url:
                    connection.execute(
                        """CREATE OR REPLACE FUNCTION deny_immutable_change() RETURNS trigger
                     LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'Immutable accounting record'; END $$"""
                    )
                    connection.execute(
                        f"DROP TRIGGER IF EXISTS {table}_immutable ON {table}"
                    )
                    connection.execute(
                        f"CREATE TRIGGER {table}_immutable BEFORE UPDATE OR DELETE ON {table} FOR EACH ROW EXECUTE FUNCTION deny_immutable_change()"
                    )
                else:
                    for action in ("UPDATE", "DELETE"):
                        connection.execute(
                            f"CREATE TRIGGER IF NOT EXISTS {table}_no_{action.lower()} BEFORE {action} ON {table} BEGIN SELECT RAISE(ABORT, 'Immutable accounting record'); END;"
                        )
            if self.url:
                connection.execute("DELETE FROM schema_version")
                connection.execute("INSERT INTO schema_version VALUES (5)")
                if connection.execute(
                    "SELECT 1 FROM pg_roles WHERE rolname='tollgate_app'"
                ).fetchone():
                    connection.execute("GRANT USAGE ON SCHEMA public TO tollgate_app")
                    connection.execute(
                        "GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO tollgate_app"
                    )
                    connection.execute(
                        "GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO tollgate_app"
                    )
                    connection.execute(
                        "REVOKE UPDATE, DELETE, TRUNCATE ON ledger, audit_events FROM tollgate_app"
                    )
                    connection.execute(
                        "REVOKE INSERT, UPDATE, DELETE, TRUNCATE ON schema_version FROM tollgate_app"
                    )
            else:
                connection.execute("PRAGMA user_version=5")

    def check_version(self):
        with self.connection() as connection:
            try:
                row = connection.execute(
                    "SELECT version FROM schema_version"
                    if self.url
                    else "PRAGMA user_version"
                ).fetchone()
            except Exception:
                row = None
            if not row or row[0] != 5:
                raise RuntimeError(
                    "Run python manage.py migrate before starting this release."
                )

    def close(self):
        if self.pool:
            self.pool.close()
