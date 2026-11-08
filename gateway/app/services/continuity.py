"""Offline continuity: signed spending allowances, safe settlement of relay records, contacts.

How it works (see docs/OFFLINE.md):
1. While the system is healthy, a customer's app asks a warden for an *allowance*.
   The money is moved from "available" into a hold (wallets.held_micro) and a
   signed token is returned. The hold means offline spending can never overdraw.
2. If the primary system is unreachable, the app presents the token to the standby
   relay, which verifies the signature offline and spends only inside the allowance.
3. When the primary returns, the app uploads the relay's usage records. Each record
   is re-priced here from the token counts, charged once, and the unused hold is
   released when the allowance is closed.
"""

import base64
import hashlib
import hmac
import json
import time

from app.services.ledger import BillingError, Ledger, new_id
from app.services.pricing import calculate_cost, micro_to_usd

PREFIX = "tga1"
CLOCK_SKEW_SECONDS = 300
MAX_RECORDS_PER_SYNC = 500


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _mac(secret: str, body: str) -> str:
    return _b64(hmac.new(secret.encode(), f"{PREFIX}.{body}".encode(), hashlib.sha256).digest())


def sign_allowance(secret: str, claims: dict) -> str:
    """Return a tamper-evident token the relay can verify without any database."""
    body = _b64(json.dumps(claims, sort_keys=True, separators=(",", ":")).encode())
    return f"{PREFIX}.{body}.{_mac(secret, body)}"


def verify_allowance(secret: str, token: str, now: float | None = None) -> dict:
    """Validate signature and expiry; raise ValueError with a safe reason otherwise."""
    parts = token.split(".")
    if len(secret) < 32 or len(parts) != 3 or parts[0] != PREFIX:
        raise ValueError("invalid_allowance")
    if not hmac.compare_digest(parts[2], _mac(secret, parts[1])):
        raise ValueError("invalid_allowance")
    try:
        claims = json.loads(_unb64(parts[1]))
        expires = float(claims["exp"])
        claims["aid"], claims["cid"], int(claims["amt"]), list(claims["models"])
    except (ValueError, KeyError, TypeError) as exc:
        raise ValueError("invalid_allowance") from exc
    if expires <= (time.time() if now is None else now):
        raise ValueError("allowance_expired")
    return claims


RECORD_FIELDS = (
    "record_id",
    "model",
    "input_tokens",
    "output_tokens",
    "cached_tokens",
    "cache_write_tokens",
    "created_at",
)


def sign_record(secret: str, allowance_id: str, record: dict) -> str:
    """The relay signs the token counts it observed so a customer cannot edit them."""
    body = json.dumps(
        {"aid": allowance_id, **{k: record[k] for k in RECORD_FIELDS}},
        sort_keys=True,
        separators=(",", ":"),
    )
    return _b64(hmac.new(secret.encode(), f"rec.{body}".encode(), hashlib.sha256).digest())


class Continuity:
    def __init__(self, database, ledger: Ledger, models: dict, settings):
        self.database, self.ledger, self.models, self.settings = (
            database,
            ledger,
            models,
            settings,
        )

    @property
    def enabled(self) -> bool:
        return len(self.settings.allowance_secret.get_secret_value()) >= 32

    def _secret(self) -> str:
        if not self.enabled:
            raise BillingError(
                503,
                "offline_mode_disabled",
                "Offline continuity is not configured on this deployment.",
            )
        return self.settings.allowance_secret.get_secret_value()

    # ----- issuing -----
    def issue(
        self,
        identity: dict,
        amount_micro: int,
        ttl_minutes: int,
        models: list[str],
        include_direct: bool = False,
    ):
        """Hold money for offline use and sign an allowance for the standby relay."""
        secret = self._secret()
        s = self.settings
        if not 0 < amount_micro <= s.allowance_max_micro:
            raise BillingError(
                422,
                "invalid_amount",
                f"Allowance must be between $0.000001 and ${micro_to_usd(s.allowance_max_micro)}.",
            )
        if not 5 <= ttl_minutes <= s.allowance_max_ttl_minutes:
            raise BillingError(
                422,
                "invalid_ttl",
                f"Allowance lifetime must be 5 to {s.allowance_max_ttl_minutes} minutes.",
            )
        allowed = [m for m in models if m in self.models and m in identity["allowed_models"]]
        if not allowed or len(allowed) != len(set(models)):
            raise BillingError(
                403, "model_not_allowed", "This key cannot use one of the requested models."
            )
        direct_keys = self._direct_keys(identity["customer_id"], allowed) if include_direct else None
        now = time.time()
        customer_id, allowance_id = identity["customer_id"], new_id("alw_")
        expires = now + ttl_minutes * 60
        with self.database.transaction() as c:
            wallet = Ledger._wallet(c, customer_id)
            if wallet["frozen"]:
                raise BillingError(403, "account_frozen", "This customer is suspended.")
            if amount_micro > wallet["balance_micro"] - wallet["held_micro"]:
                raise BillingError(
                    402, "insufficient_credits", "Available credits cannot cover this allowance."
                )
            cap = wallet["spend_cap_micro"]
            if cap is not None and (
                wallet["spent_micro"] + wallet["held_micro"] + amount_micro > cap
            ):
                raise BillingError(
                    403, "spend_cap_exceeded", "This allowance would exceed the spend cap."
                )
            active = c.execute(
                "SELECT COUNT(*) FROM allowances WHERE customer_id=? AND state='active' AND expires_at>?",
                (customer_id, now),
            ).fetchone()[0]
            if active >= s.allowance_max_active:
                raise BillingError(
                    409, "too_many_allowances", "Close or let an older allowance expire first."
                )
            c.execute(
                "UPDATE wallets SET held_micro=held_micro+? WHERE customer_id=?",
                (amount_micro, customer_id),
            )
            c.execute(
                "INSERT INTO allowances(id,customer_id,amount_micro,models,expires_at,created_at,direct) VALUES (?,?,?,?,?,?,?)",
                (
                    allowance_id,
                    customer_id,
                    amount_micro,
                    json.dumps(allowed),
                    expires,
                    now,
                    1 if direct_keys else 0,
                ),
            )
            Ledger.audit(
                c,
                "allowance_issued",
                allowance_id,
                {
                    "customer_id": customer_id,
                    "amount_micro": amount_micro,
                    "models": allowed,
                    "direct": bool(direct_keys),
                },
            )
        token = sign_allowance(
            secret,
            {
                "aid": allowance_id,
                "cid": customer_id,
                "amt": amount_micro,
                "exp": expires,
                "iat": now,
                "models": allowed,
            },
        )
        response = {
            "allowance_id": allowance_id,
            "token": token,
            "amount_micro": amount_micro,
            "prices": {m: self._price(m) for m in allowed},
            "amount_usd": micro_to_usd(amount_micro),
            "expires_at": expires,
            "models": allowed,
            "relay_url": self.settings.relay_url or None,
        }
        if direct_keys:
            response["direct"] = {"keys": direct_keys, "expires_at": expires}
        return response

    def _price(self, model_id: str) -> dict:
        spec = self.models[model_id]
        return {
            "provider": spec.provider,
            "upstream_model": spec.upstream_model,
            "input_rate": spec.input_rate,
            "output_rate": spec.output_rate,
            "cached_rate": spec.cached_rate,
            "cache_write_rate": spec.cache_write_rate,
            "max_output_tokens": spec.max_output_tokens,
        }

    def _direct_keys(self, customer_id: str, models: list[str]) -> dict:
        """Restricted emergency keys, only for customers the owner approved by ID."""
        s = self.settings
        if not s.emergency_direct_enabled:
            raise BillingError(403, "direct_mode_disabled", "Direct emergency access is not enabled.")
        if customer_id not in s.direct_customers:
            raise BillingError(403, "direct_not_permitted", "This customer is not approved for direct emergency access.")
        available = {
            "openai": s.emergency_openai_key.get_secret_value(),
            "anthropic": s.emergency_anthropic_key.get_secret_value(),
            "gemini": s.emergency_gemini_key.get_secret_value(),
        }
        keys = {}
        for model_id in models:
            provider = self.models[model_id].provider
            if provider not in available or not available[provider]:
                raise BillingError(
                    422,
                    "no_emergency_key",
                    f"No emergency key is configured for the {provider} provider.",
                )
            keys[provider] = available[provider]
        return keys

    # ----- settling -----
    def sync(self, customer_id: str, allowance_id: str, records: list[dict], close: bool):
        """Import relay records once each; re-price from token counts; never trust relay money."""
        if len(records) > MAX_RECORDS_PER_SYNC:
            raise BillingError(422, "too_many_records", "Send at most 500 records per sync.")
        accepted = duplicates = 0
        rejected: list[dict] = []
        now = time.time()
        with self.database.transaction() as c:
            a = c.execute(
                "SELECT * FROM allowances WHERE id=? AND customer_id=?",
                (allowance_id, customer_id),
            ).fetchone()
            if a is None:
                raise BillingError(404, "allowance_not_found", "Allowance was not found.")
            Ledger._wallet(c, customer_id)
            spent, models = a["spent_micro"], json.loads(a["models"])
            for r in records:
                if c.execute(
                    "SELECT 1 FROM offline_records WHERE allowance_id=? AND record_id=?",
                    (allowance_id, r["record_id"]),
                ).fetchone():
                    duplicates += 1
                    continue
                spec, reason, charged = self.models.get(r["model"]), None, 0
                is_direct = r["record_id"].startswith("dir_")
                if is_direct and not a["direct"]:
                    reason = "direct_not_enabled"
                elif not is_direct and not hmac.compare_digest(
                    r.get("sig", ""), sign_record(self._secret(), allowance_id, r)
                ):
                    reason = "bad_signature"
                elif a["state"] == "closed":
                    reason = "allowance_closed"
                elif spec is None or r["model"] not in models:
                    reason = "model_not_allowed"
                elif r["created_at"] > a["expires_at"] + CLOCK_SKEW_SECONDS:
                    reason = "after_expiry"
                elif r["created_at"] < a["created_at"] - CLOCK_SKEW_SECONDS:
                    reason = "before_issue"
                else:
                    try:
                        cost = calculate_cost(
                            r["input_tokens"],
                            r["output_tokens"],
                            spec.input_rate,
                            spec.output_rate,
                            r["cached_tokens"],
                            spec.cached_rate,
                            r["cache_write_tokens"],
                            spec.cache_write_rate,
                        )
                    except ValueError:
                        reason = "invalid_usage"
                if reason is None:
                    charged = min(cost, a["amount_micro"] - spent)
                    status = ("settled" if charged == cost else "capped") + (
                        "_unverified" if is_direct else ""
                    )
                    spent += charged
                    c.execute(
                        "INSERT INTO ledger(customer_id,kind,amount_micro,reference,created_at) VALUES (?,'charge',?,?,?)",
                        (customer_id, -charged, f"offline:{allowance_id}:{r['record_id']}", now),
                    )
                    c.execute(
                        "UPDATE wallets SET balance_micro=balance_micro-?,held_micro=held_micro-?,spent_micro=spent_micro+? WHERE customer_id=?",
                        (charged, charged, charged, customer_id),
                    )
                    accepted += 1
                else:
                    status = f"rejected:{reason}"
                    rejected.append({"record_id": r["record_id"], "reason": reason})
                c.execute(
                    "INSERT INTO offline_records VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        allowance_id,
                        r["record_id"],
                        customer_id,
                        r["model"],
                        r["input_tokens"],
                        r["output_tokens"],
                        r["cached_tokens"],
                        r["cache_write_tokens"],
                        charged,
                        status,
                        r["created_at"],
                        now,
                    ),
                )
            c.execute("UPDATE allowances SET spent_micro=? WHERE id=?", (spent, allowance_id))
            if close and a["state"] == "active":
                self._close(c, a["id"], customer_id, a["amount_micro"] - spent, "customer")
            Ledger.audit(
                c,
                "offline_sync",
                allowance_id,
                {"accepted": accepted, "duplicates": duplicates, "rejected": len(rejected)},
            )
        return {
            "accepted": accepted,
            "duplicates": duplicates,
            "rejected": rejected,
            "closed": bool(close),
            "balance": self.ledger.balance(customer_id),
        }

    @staticmethod
    def _close(c, allowance_id: str, customer_id: str, remainder: int, who: str):
        """Release the unused part of the hold exactly once."""
        c.execute(
            "UPDATE wallets SET held_micro=held_micro-? WHERE customer_id=?",
            (remainder, customer_id),
        )
        c.execute("UPDATE allowances SET state='closed' WHERE id=?", (allowance_id,))
        Ledger.audit(
            c, "allowance_closed", allowance_id, {"released_micro": remainder, "by": who}
        )

    def close_expired(self) -> int:
        """Release holds of allowances whose sync grace period has ended."""
        cutoff = time.time() - self.settings.allowance_sync_grace_seconds
        closed = 0
        with self.database.transaction() as c:
            for a in c.execute(
                "SELECT * FROM allowances WHERE state='active' AND expires_at<?", (cutoff,)
            ).fetchall():
                self._close(
                    c, a["id"], a["customer_id"], a["amount_micro"] - a["spent_micro"], "sweeper"
                )
                closed += 1
        return closed

    # ----- contacts -----
    def set_contact(self, customer_id: str, email: str | None, phone: str | None):
        with self.database.transaction() as c:
            Ledger._wallet(c, customer_id)
            c.execute("DELETE FROM customer_contacts WHERE customer_id=?", (customer_id,))
            c.execute(
                "INSERT INTO customer_contacts VALUES (?,?,?)", (customer_id, email, phone)
            )
            Ledger.audit(c, "contact_set", customer_id, {"email": bool(email), "phone": bool(phone)})
        return {"customer_id": customer_id, "email": email, "phone": phone}

    def contacts(self) -> list[dict]:
        with self.database.connection() as c:
            rows = c.execute(
                "SELECT c.id,c.name,k.email,k.phone FROM customers c JOIN customer_contacts k ON k.customer_id=c.id"
            ).fetchall()
        return [
            {"customer_id": r["id"], "name": r["name"], "email": r["email"], "phone": r["phone"]}
            for r in rows
        ]

    def allowances(self) -> list[dict]:
        with self.database.connection() as c:
            rows = c.execute(
                "SELECT * FROM allowances ORDER BY created_at DESC LIMIT 100"
            ).fetchall()
        return [
            {
                "id": r["id"],
                "customer_id": r["customer_id"],
                "amount_usd": micro_to_usd(r["amount_micro"]),
                "spent_usd": micro_to_usd(r["spent_micro"]),
                "state": r["state"],
                "direct": bool(r["direct"]),
                "expires_at": r["expires_at"],
            }
            for r in rows
        ]
