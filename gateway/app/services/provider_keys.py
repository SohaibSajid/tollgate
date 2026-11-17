"""Encrypted provider keys that each customer business brings to the gateway."""

import re
import time

from cryptography.fernet import Fernet, InvalidToken

from app.services.ledger import BillingError, Ledger

PROVIDERS = ("openai", "anthropic", "gemini")
KEY_PATTERN = re.compile(r"[A-Za-z0-9_.-]{20,512}")


def unseal(settings, sealed: str) -> str:
    secret = settings.credentials_key.get_secret_value()
    if not secret:
        raise ValueError("provider_key_unavailable")
    try:
        return Fernet(secret.encode()).decrypt(sealed.encode()).decode()
    except InvalidToken as exc:
        raise ValueError("provider_key_unavailable") from exc


class ProviderKeys:
    def __init__(self, database, settings):
        self.database = database
        secret = settings.credentials_key.get_secret_value()
        self.fernet = Fernet(secret.encode()) if secret else None
        self.providers = PROVIDERS + (("simulator",) if settings.mode == "simulator" else ())

    def save(self, customer_id: str, provider: str, value: str) -> dict:
        if self.fernet is None:
            raise BillingError(
                503,
                "credentials_key_missing",
                "Set TOLLGATE_CREDENTIALS_KEY (python manage.py credentials-key) and restart before saving provider keys.",
            )
        if provider not in self.providers:
            raise BillingError(
                422, "unsupported_provider", "Use one of: " + ", ".join(self.providers) + "."
            )
        if KEY_PATTERN.fullmatch(value) is None:
            raise BillingError(
                422, "invalid_provider_key", "The provider key has an unexpected format."
            )
        if value.startswith("tg_"):
            raise BillingError(
                422,
                "invalid_provider_key",
                "This is a Tollgate customer key. Save the customer's own provider key, for example a Gemini key starting with AIza.",
            )
        hint, now = value[-4:], time.time()
        secret = self.fernet.encrypt(value.encode()).decode()
        with self.database.transaction() as connection:
            if not connection.execute(
                "SELECT 1 FROM customers WHERE id=?", (customer_id,)
            ).fetchone():
                raise BillingError(404, "customer_not_found", "Customer was not found.")
            connection.execute(
                "DELETE FROM provider_keys WHERE customer_id=? AND provider=?",
                (customer_id, provider),
            )
            connection.execute(
                "INSERT INTO provider_keys(customer_id,provider,secret,hint,created_at) VALUES (?,?,?,?,?)",
                (customer_id, provider, secret, hint, now),
            )
            Ledger.audit(
                connection, "provider_key_saved", customer_id, {"provider": provider, "hint": hint}
            )
        return {"customer_id": customer_id, "provider": provider, "hint": hint, "created_at": now}

    def remove(self, customer_id: str, provider: str) -> dict:
        with self.database.transaction() as connection:
            removed = connection.execute(
                "DELETE FROM provider_keys WHERE customer_id=? AND provider=?",
                (customer_id, provider),
            ).rowcount
            if not removed:
                raise BillingError(
                    404, "provider_key_not_found", "No provider key is saved for this customer and provider."
                )
            Ledger.audit(connection, "provider_key_removed", customer_id, {"provider": provider})
        return {"status": "removed"}

    def sealed(self, customer_id: str, provider: str) -> str | None:
        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT secret FROM provider_keys WHERE customer_id=? AND provider=?",
                (customer_id, provider),
            ).fetchone()
        return None if row is None else row[0]

    def get(self, customer_id: str, provider: str) -> str | None:
        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT secret FROM provider_keys WHERE customer_id=? AND provider=?",
                (customer_id, provider),
            ).fetchone()
        if row is None:
            return None
        if self.fernet is None:
            raise BillingError(
                503,
                "provider_key_unavailable",
                "The gateway cannot read this account's saved provider key. Contact the operator.",
            )
        try:
            return self.fernet.decrypt(row[0].encode()).decode()
        except InvalidToken as exc:
            raise BillingError(
                503,
                "provider_key_unavailable",
                "The gateway cannot read this account's saved provider key. Contact the operator.",
            ) from exc

    def listing(self) -> list[dict]:
        with self.database.connection() as connection:
            return [
                dict(row)
                for row in connection.execute(
                    "SELECT customer_id,provider,hint,created_at FROM provider_keys ORDER BY created_at DESC LIMIT 200"
                )
            ]
