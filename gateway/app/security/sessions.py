"""Revocable dashboard sessions and database-backed rate limits."""

import hashlib
import secrets
import time
from app.services.ledger import BillingError


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


class Sessions:
    def __init__(self, database, settings):
        self.database, self.settings = database, settings
        self.admin_hash = digest(settings.development_api_key.get_secret_value())

    def valid_owner(self, token: str) -> bool:
        return secrets.compare_digest(digest(token), self.admin_hash)

    def login(self, token: str):
        if not self.valid_owner(token):
            raise BillingError(401, "invalid_admin_key", "The owner key is incorrect.")
        raw, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        with self.database.transaction() as connection:
            connection.execute(
                "INSERT INTO sessions VALUES (?,?,?,?)",
                (
                    digest(raw),
                    csrf,
                    self.admin_hash,
                    time.time() + self.settings.session_hours * 3600,
                ),
            )
        return raw, csrf

    def get(self, raw: str):
        if not 20 <= len(raw) <= 128:
            return None
        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT csrf FROM sessions WHERE token_hash=? AND expires_at>? AND admin_hash=?",
                (digest(raw), time.time(), self.admin_hash),
            ).fetchone()
        return row[0] if row else None

    def logout(self, raw: str):
        with self.database.transaction() as connection:
            connection.execute(
                "DELETE FROM sessions WHERE token_hash=?", (digest(raw),)
            )

    def limit(self, subject: str, maximum: int):
        """Atomically count a fixed-minute window shared by all gateway processes."""
        bucket = int(time.time() // 60)
        with self.database.transaction() as connection:
            row = connection.execute(
                "SELECT count FROM rate_windows WHERE subject=? AND bucket=?",
                (subject, bucket),
            ).fetchone()
            if row and row[0] >= maximum:
                raise BillingError(
                    429,
                    "rate_limit_exceeded",
                    "Too many requests. Wait before retrying.",
                    {"retry_after_seconds": 60 - int(time.time()) % 60},
                )
            connection.execute(
                "INSERT INTO rate_windows VALUES (?,?,1) ON CONFLICT(subject,bucket) DO UPDATE SET count=rate_windows.count+1",
                (subject, bucket),
            )
