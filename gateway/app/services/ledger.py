"""Customer keys, atomic reservations, immutable charges, and the durable work queue."""

import hashlib
import json
import secrets
import time

from app.database import Database
from app.services.pricing import MAX_WALLET_MICRO, calculate_cost, micro_to_usd


class BillingError(Exception):
    def __init__(
        self, status: int, code: str, message: str, context: dict | None = None
    ):
        """Carry safe API error details without including credentials or prompt text."""
        super().__init__(message)
        self.status, self.code, self.message = status, code, message
        self.context = context or {}


def new_id(prefix: str) -> str:
    """Generate an unpredictable identifier for a customer or key record."""
    return prefix + secrets.token_hex(12)


class Ledger:
    def __init__(self, database: Database, concurrency: int = 5, admission_check=None):
        """Keep database access separate from HTTP and provider code."""
        self.database = database
        self.concurrency = concurrency
        self.admission_check = admission_check

    def create_customer(self, name: str) -> dict:
        """Create a customer and an empty wallet in a single transaction."""
        customer_id = new_id("cus_")
        with self.database.transaction() as connection:
            connection.execute(
                "INSERT INTO customers VALUES (?,?,?)", (customer_id, name, time.time())
            )
            connection.execute(
                "INSERT INTO wallets(customer_id) VALUES (?)", (customer_id,)
            )
            self.audit(connection, "customer_created", customer_id, {"name": name})
        return {"id": customer_id, "name": name}

    def create_key(
        self,
        customer_id: str,
        label: str,
        allowed_models: list[str],
        expires_in_days: int | None = None,
    ) -> dict:
        """Return the secret once; retain only its SHA-256 hash and short prefix."""
        raw_key, key_id = "tg_" + secrets.token_urlsafe(32), new_id("key_")
        with self.database.transaction() as connection:
            self._wallet(connection, customer_id)
            connection.execute(
                "INSERT INTO api_keys(id,customer_id,label,prefix,key_hash,allowed_models,created_at) VALUES (?,?,?,?,?,?,?)",
                (
                    key_id,
                    customer_id,
                    label,
                    raw_key[:10],
                    hashlib.sha256(raw_key.encode()).hexdigest(),
                    json.dumps(allowed_models),
                    time.time(),
                ),
            )
            connection.execute(
                "UPDATE api_keys SET expires_at=? WHERE id=?",
                (
                    (
                        None
                        if expires_in_days is None
                        else time.time() + expires_in_days * 86400
                    ),
                    key_id,
                ),
            )
            self.audit(
                connection,
                "key_issued",
                key_id,
                {"customer_id": customer_id, "models": allowed_models},
            )
        return {
            "id": key_id,
            "customer_id": customer_id,
            "api_key": raw_key,
            "prefix": raw_key[:10],
            "allowed_models": allowed_models,
        }

    def authenticate(self, token: bytes) -> dict | None:
        """Resolve an active high-entropy customer key without storing plaintext keys."""
        digest = hashlib.sha256(token).hexdigest()
        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT id,customer_id,allowed_models FROM api_keys WHERE key_hash=? AND active=1 AND (expires_at IS NULL OR expires_at>?)",
                (digest, time.time()),
            ).fetchone()
        if row is None:
            return None
        return {
            "key_id": row["id"],
            "customer_id": row["customer_id"],
            "allowed_models": json.loads(row["allowed_models"]),
        }

    def revoke_key(self, key_id: str) -> None:
        """Prevent new reservations; already-dispatched requests still settle normally."""
        with self.database.transaction() as connection:
            if (
                connection.execute(
                    "UPDATE api_keys SET active=0 WHERE id=?", (key_id,)
                ).rowcount
                != 1
            ):
                raise BillingError(404, "key_not_found", "API key was not found.")
            self.audit(connection, "key_revoked", key_id, {})

    def grant(self, customer_id: str, amount: int, reference: str) -> dict:
        """Add credits once per reference and reject conflicting retries."""
        if not 0 < amount <= MAX_WALLET_MICRO:
            raise BillingError(
                422,
                "invalid_amount",
                "Credit amount must be positive and within the wallet limit.",
            )
        with self.database.transaction() as connection:
            wallet = self._wallet(connection, customer_id)
            old = connection.execute(
                "SELECT amount_micro FROM ledger WHERE customer_id=? AND kind='grant' AND reference=?",
                (customer_id, reference),
            ).fetchone()
            if old:
                if old[0] != amount:
                    raise BillingError(
                        409,
                        "reference_conflict",
                        "This reference was already used for a different credit amount.",
                    )
            else:
                if wallet["balance_micro"] + amount > MAX_WALLET_MICRO:
                    raise BillingError(
                        422,
                        "wallet_limit",
                        "This grant would exceed the local wallet limit.",
                    )
                connection.execute(
                    "INSERT INTO ledger(customer_id,kind,amount_micro,reference,created_at) VALUES (?,'grant',?,?,?)",
                    (customer_id, amount, reference, time.time()),
                )
                connection.execute(
                    "UPDATE wallets SET balance_micro=balance_micro+? WHERE customer_id=?",
                    (amount, customer_id),
                )
            self.audit(
                connection,
                "credit_granted",
                customer_id,
                {"amount_micro": amount, "reference": reference},
            )
        return self.balance(customer_id)

    def set_cap(self, customer_id: str, cap: int | None) -> dict:
        """Set a lifetime spend cap without invalidating already committed spending."""
        with self.database.transaction() as connection:
            wallet = self._wallet(connection, customer_id)
            if cap is not None and cap < wallet["spent_micro"] + wallet["held_micro"]:
                raise BillingError(
                    409,
                    "cap_below_committed",
                    "Cap must cover settled spending plus active reservations.",
                )
            connection.execute(
                "UPDATE wallets SET spend_cap_micro=? WHERE customer_id=?",
                (cap, customer_id),
            )
            self.audit(connection, "cap_changed", customer_id, {"cap_micro": cap})
        return self.balance(customer_id)

    def reserve(
        self,
        request_id: str,
        auth: dict,
        idempotency_key: str,
        request_hash: str,
        spec,
        feature: str,
        input_bound: int,
        output_bound: int,
    ) -> dict:
        """Check key, model, balance, cap, and uniqueness while atomically creating a hold."""
        amount = calculate_cost(
            input_bound,
            output_bound,
            max(spec.input_rate, spec.cache_write_rate),
            spec.output_rate,
        )
        now, customer_id = time.time(), auth["customer_id"]
        with self.database.transaction() as connection:
            admission = self.admission_check(connection) if self.admission_check else {}
            # Recheck revocation inside the transaction, not only during authentication.
            key = connection.execute(
                "SELECT * FROM api_keys WHERE id=? AND customer_id=? AND active=1 AND (expires_at IS NULL OR expires_at>?)",
                (auth["key_id"], customer_id, now),
            ).fetchone()
            if key is None:
                raise BillingError(
                    401, "invalid_api_key", "API key is no longer active."
                )
            if spec.id not in json.loads(key["allowed_models"]):
                raise BillingError(
                    403,
                    "model_not_allowed",
                    "This API key cannot use the requested model.",
                )
            old = connection.execute(
                "SELECT * FROM requests WHERE customer_id=? AND idempotency_key=?",
                (customer_id, idempotency_key),
            ).fetchone()
            if old:
                code = (
                    "duplicate_request"
                    if old["request_hash"] == request_hash
                    else "idempotency_conflict"
                )
                raise BillingError(
                    409,
                    code,
                    "This idempotency key already exists. Inspect the original request instead of generating again.",
                    {"original_request_id": old["id"], "state": old["state"]},
                )
            wallet = self._wallet(connection, customer_id)
            if wallet["frozen"]:
                raise BillingError(
                    403, "account_frozen", "This customer's new requests are suspended."
                )
            active = connection.execute(
                "SELECT COUNT(*) FROM requests WHERE customer_id=? AND state IN ('reserved','dispatched')",
                (customer_id,),
            ).fetchone()[0]
            if active >= self.concurrency:
                raise BillingError(
                    429,
                    "concurrency_limit",
                    "Wait for an active request to finish before starting another.",
                )
            if wallet["balance_micro"] - wallet["held_micro"] < amount:
                raise BillingError(
                    402,
                    "insufficient_credits",
                    "Available credits cannot cover this request's reservation.",
                    {
                        "required_usd": micro_to_usd(amount),
                        "available_usd": micro_to_usd(
                            wallet["balance_micro"] - wallet["held_micro"]
                        ),
                    },
                )
            cap = wallet["spend_cap_micro"]
            if (
                cap is not None
                and wallet["spent_micro"] + wallet["held_micro"] + amount > cap
            ):
                raise BillingError(
                    403,
                    "spend_cap_exceeded",
                    "This request would exceed the customer's lifetime spend cap.",
                )
            connection.execute(
                "UPDATE wallets SET held_micro=held_micro+? WHERE customer_id=?",
                (amount, customer_id),
            )
            connection.execute(
                "INSERT INTO requests(id,customer_id,key_id,idempotency_key,request_hash,model,feature,state,reserve_micro,price_version,input_rate,output_rate,input_bound,output_bound,created_at,updated_at) VALUES (?,?,?,?,?,?,?,'reserved',?,?,?,?,?,?,?,?)",
                (
                    request_id,
                    customer_id,
                    auth["key_id"],
                    idempotency_key,
                    request_hash,
                    spec.id,
                    feature,
                    amount,
                    spec.price_version,
                    spec.input_rate,
                    spec.output_rate,
                    input_bound,
                    output_bound,
                    now,
                    now,
                ),
            )
            connection.execute(
                "UPDATE requests SET provider=?,cached_rate=?,cache_write_rate=? WHERE id=?",
                (spec.provider, spec.cached_rate, spec.cache_write_rate, request_id),
            )
            connection.execute(
                "UPDATE requests SET warden_id=?,control_degraded=? WHERE id=?",
                (
                    admission.get("warden_id"),
                    int(admission.get("control_degraded", False)),
                    request_id,
                ),
            )
        return {"request_id": request_id, "reserved_micro": amount, **admission}

    def dispatch(self, request_id: str) -> bool:
        """Mark dispatch before network I/O; a released hold can never be dispatched."""
        with self.database.transaction() as connection:
            return (
                connection.execute(
                    "UPDATE requests SET state='dispatched',updated_at=? WHERE id=? AND state='reserved'",
                    (time.time(), request_id),
                ).rowcount
                == 1
            )

    def record_usage(
        self,
        request_id: str,
        input_tokens: int,
        output_tokens: int,
        cached_tokens: int = 0,
        cache_write_tokens: int = 0,
        *,
        evidence: str | None = None
    ) -> None:
        """Save reported usage and its settlement job together, preventing a lost queue job."""
        if (
            type(input_tokens) is not int
            or type(output_tokens) is not int
            or type(cached_tokens) is not int
            or type(cache_write_tokens) is not int
            or min(input_tokens, output_tokens, cached_tokens, cache_write_tokens) < 0
            or cached_tokens + cache_write_tokens > input_tokens
        ):
            raise ValueError("Usage must contain non-negative integers.")
        with self.database.transaction() as connection:
            row = self._request(connection, request_id)
            if evidence is not None:
                if row["state"] != "unresolved":
                    raise BillingError(
                        409,
                        "not_unresolved",
                        "Only unresolved requests accept operator usage evidence.",
                    )
                connection.execute(
                    "UPDATE requests SET usage_evidence=?,resolved_by='operator' WHERE id=?",
                    (evidence, request_id),
                )
                self.audit(
                    connection, "usage_attested", request_id, {"evidence": evidence}
                )
            if row["input_tokens"] is not None:
                if (
                    row["input_tokens"],
                    row["output_tokens"],
                    row["cached_tokens"],
                    row["cache_write_tokens"],
                ) != (input_tokens, output_tokens, cached_tokens, cache_write_tokens):
                    raise BillingError(
                        409,
                        "usage_conflict",
                        "Conflicting usage requires investigation.",
                    )
                return
            if row["state"] not in ("dispatched", "unresolved"):
                raise BillingError(
                    409,
                    "invalid_request_state",
                    "Usage cannot be recorded for this request state.",
                )
            cost = calculate_cost(
                input_tokens,
                output_tokens,
                row["input_rate"],
                row["output_rate"],
                cached_tokens,
                row["cached_rate"],
                cache_write_tokens,
                row["cache_write_rate"],
            )
            within_bound = (
                input_tokens <= row["input_bound"]
                and output_tokens <= row["output_bound"]
                and cost <= row["reserve_micro"]
            )
            state = "usage_recorded" if within_bound else "unresolved"
            error = None if within_bound else "usage_exceeds_reservation"
            connection.execute(
                "UPDATE requests SET state=?,input_tokens=?,output_tokens=?,actual_micro=?,error_code=?,updated_at=? WHERE id=?",
                (
                    state,
                    input_tokens,
                    output_tokens,
                    cost,
                    error,
                    time.time(),
                    request_id,
                ),
            )
            connection.execute(
                "UPDATE requests SET cached_tokens=?,cache_write_tokens=? WHERE id=?",
                (cached_tokens, cache_write_tokens, request_id),
            )
            connection.execute(
                "INSERT INTO settlement_jobs VALUES (?,?,?) ON CONFLICT(request_id) DO UPDATE SET state=excluded.state",
                (request_id, "pending" if within_bound else "blocked", time.time()),
            )

    def settle(self, request_id: str) -> bool:
        """Charge once and clear the entire hold in the same durable transaction."""
        with self.database.transaction() as connection:
            row = self._request(connection, request_id)
            if row["state"] != "usage_recorded":
                return False
            connection.execute(
                "INSERT INTO ledger(customer_id,kind,amount_micro,reference,request_id,created_at) VALUES (?,'charge',?,?,?,?)",
                (
                    row["customer_id"],
                    -row["actual_micro"],
                    request_id,
                    request_id,
                    time.time(),
                ),
            )
            connection.execute(
                "UPDATE wallets SET balance_micro=balance_micro-?,held_micro=held_micro-?,spent_micro=spent_micro+? WHERE customer_id=?",
                (
                    row["actual_micro"],
                    row["reserve_micro"],
                    row["actual_micro"],
                    row["customer_id"],
                ),
            )
            connection.execute(
                "UPDATE requests SET state='settled',updated_at=? WHERE id=?",
                (time.time(), request_id),
            )
            connection.execute(
                "UPDATE settlement_jobs SET state='done' WHERE request_id=?",
                (request_id,),
            )
        return True

    def release(
        self, request_id: str, reason: str, *, allow_dispatched: bool = False
    ) -> bool:
        """Release only undispatched work or an explicitly proven non-billable simulator rejection."""
        with self.database.transaction() as connection:
            row = self._request(connection, request_id)
            states = ("reserved", "dispatched") if allow_dispatched else ("reserved",)
            if row["state"] not in states:
                return False
            connection.execute(
                "UPDATE wallets SET held_micro=held_micro-? WHERE customer_id=?",
                (row["reserve_micro"], row["customer_id"]),
            )
            connection.execute(
                "UPDATE requests SET state='released',error_code=?,updated_at=? WHERE id=?",
                (reason, time.time(), request_id),
            )
        return True

    def unresolved(self, request_id: str, reason: str) -> None:
        """Keep the hold when dispatch may have incurred a cost but usage is missing."""
        with self.database.transaction() as connection:
            connection.execute(
                "UPDATE requests SET state='unresolved',error_code=?,updated_at=? WHERE id=? AND state='dispatched'",
                (reason, time.time(), request_id),
            )

    def pending_jobs(self, limit: int = 50) -> list[str]:
        """Read a bounded queue batch; each job is still rechecked inside settlement."""
        with self.database.connection() as connection:
            return [
                row[0]
                for row in connection.execute(
                    "SELECT request_id FROM settlement_jobs WHERE state='pending' ORDER BY created_at LIMIT ?",
                    (limit,),
                )
            ]

    def recovery_candidates(self, stale_seconds: float) -> list[dict]:
        """Release stale undispatched holds and schedule bounded lookups for uncertain work."""
        now = time.time()
        with self.database.transaction() as connection:
            stale = connection.execute(
                "SELECT * FROM requests WHERE state='reserved' AND updated_at<? LIMIT 50",
                (now - stale_seconds,),
            ).fetchall()
            for row in stale:
                connection.execute(
                    "UPDATE wallets SET held_micro=held_micro-? WHERE customer_id=?",
                    (row["reserve_micro"], row["customer_id"]),
                )
                connection.execute(
                    "UPDATE requests SET state='released',error_code='never_dispatched',updated_at=? WHERE id=? AND state='reserved'",
                    (now, row["id"]),
                )
            rows = connection.execute(
                "SELECT * FROM requests WHERE state IN ('dispatched','unresolved') AND input_tokens IS NULL AND updated_at<? AND last_recovery_at<? ORDER BY last_recovery_at LIMIT 20",
                (now - stale_seconds, now - 10),
            ).fetchall()
            for row in rows:
                connection.execute(
                    "UPDATE requests SET last_recovery_at=? WHERE id=?",
                    (now, row["id"]),
                )
        return [dict(row) for row in rows]

    def balance(self, customer_id: str) -> dict:
        """Expose posted, reserved, available, spent, and cap amounts separately."""
        with self.database.connection() as connection:
            row = self._wallet(connection, customer_id)
            return self._public_wallet(row)

    def overview(self) -> dict:
        """Load a consistent dashboard snapshot without returning secrets or prompts."""
        with self.database.connection() as connection:
            connection.execute(
                "BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY"
                if self.database.url
                else "BEGIN"
            )
            customers = []
            for row in connection.execute(
                "SELECT c.*,w.* FROM customers c JOIN wallets w ON c.id=w.customer_id ORDER BY c.created_at DESC LIMIT 200"
            ):
                customers.append(
                    {"id": row["id"], "name": row["name"], **self._public_wallet(row)}
                )
            keys = [
                dict(row)
                for row in connection.execute(
                    "SELECT id,customer_id,label,prefix,allowed_models,active,expires_at,created_at FROM api_keys ORDER BY created_at DESC LIMIT 200"
                )
            ]
            requests = [
                self._public_request(row)
                for row in connection.execute(
                    "SELECT * FROM requests ORDER BY created_at DESC LIMIT 100"
                )
            ]
            entries = [
                dict(row)
                for row in connection.execute(
                    "SELECT * FROM ledger ORDER BY id DESC LIMIT 100"
                )
            ]
            features = [
                dict(row)
                for row in connection.execute(
                    "SELECT feature,COUNT(*) AS requests,COALESCE(SUM(actual_micro),0) AS cost_micro FROM requests WHERE state='settled' GROUP BY feature ORDER BY cost_micro DESC LIMIT 100"
                )
            ]
            jobs = {
                row[0]: row[1]
                for row in connection.execute(
                    "SELECT state,COUNT(*) FROM settlement_jobs GROUP BY state"
                ).fetchall()
            }
            totals = dict(
                connection.execute(
                    "SELECT COALESCE(SUM(balance_micro-held_micro),0) AS available,COALESCE(SUM(held_micro),0) AS reserved,COALESCE(SUM(spent_micro),0) AS spent,COUNT(*) AS customers FROM wallets"
                ).fetchone()
            )
            audit = [
                dict(row)
                for row in connection.execute(
                    "SELECT * FROM audit_events ORDER BY id DESC LIMIT 100"
                )
            ]
            heartbeat = connection.execute(
                "SELECT seen_at FROM service_heartbeats WHERE name='worker'"
            ).fetchone()
            unresolved = connection.execute(
                "SELECT COUNT(*) FROM requests WHERE state='unresolved'"
            ).fetchone()[0]
            connection.execute("COMMIT")
        for entry in entries:
            entry["amount_usd"] = micro_to_usd(entry["amount_micro"])
        return {
            "totals": {
                key: (value if key == "customers" else micro_to_usd(int(value)))
                for key, value in totals.items()
            },
            "audit": audit,
            "unresolved": unresolved,
            "worker_last_seen": heartbeat[0] if heartbeat else None,
            "customers": customers,
            "keys": keys,
            "requests": requests,
            "ledger": entries,
            "features": features,
            "queue": jobs,
        }

    def request(self, request_id: str, customer_id: str | None = None) -> dict:
        """Read a request while enforcing customer ownership when used by customer APIs."""
        with self.database.connection() as connection:
            row = self._request(connection, request_id)
            if customer_id is not None and row["customer_id"] != customer_id:
                raise BillingError(404, "request_not_found", "Request was not found.")
            return self._public_request(row)

    @staticmethod
    def _wallet(connection, customer_id: str):
        """Find an existing wallet or raise a safe not-found error."""
        row = connection.execute(
            "SELECT * FROM wallets WHERE customer_id=?", (customer_id,)
        ).fetchone()
        if row is None:
            raise BillingError(404, "customer_not_found", "Customer was not found.")
        return row

    @staticmethod
    def _request(connection, request_id: str):
        """Find a billing request by its stable identifier."""
        row = connection.execute(
            "SELECT * FROM requests WHERE id=?", (request_id,)
        ).fetchone()
        if row is None:
            raise BillingError(404, "request_not_found", "Request was not found.")
        return row

    @staticmethod
    def _public_wallet(row) -> dict:
        """Use decimal strings in JSON so browsers do not round stored money."""
        return {
            "frozen": bool(row["frozen"]),
            "balance_usd": micro_to_usd(row["balance_micro"]),
            "reserved_usd": micro_to_usd(row["held_micro"]),
            "available_usd": micro_to_usd(row["balance_micro"] - row["held_micro"]),
            "spent_usd": micro_to_usd(row["spent_micro"]),
            "spend_cap_usd": (
                None
                if row["spend_cap_micro"] is None
                else micro_to_usd(row["spend_cap_micro"])
            ),
        }

    @staticmethod
    def _public_request(row) -> dict:
        """Omit fingerprints and credentials from customer-visible request metadata."""
        result = {
            key: row[key]
            for key in (
                "id",
                "customer_id",
                "model",
                "feature",
                "state",
                "price_version",
                "input_tokens",
                "output_tokens",
                "error_code",
                "provider",
                "provider_response_id",
                "warden_id",
                "control_degraded",
                "cached_tokens",
                "cache_write_tokens",
                "resolved_by",
                "created_at",
            )
        }
        result["reserved_usd"] = micro_to_usd(row["reserve_micro"])
        result["cost_usd"] = (
            None if row["actual_micro"] is None else micro_to_usd(row["actual_micro"])
        )
        result["usage_source"] = (
            "simulated" if row["provider"] == "simulator" else "provider_reported"
        )
        return result

    @staticmethod
    def audit(connection, action: str, target: str, detail: dict):
        connection.execute(
            "INSERT INTO audit_events(action,target,detail,created_at) VALUES (?,?,?,?)",
            (action, target, json.dumps(detail, sort_keys=True), time.time()),
        )

    def save_provider_id(self, request_id: str, provider_id: str):
        if not isinstance(provider_id, str) or not 1 <= len(provider_id) <= 200:
            raise ValueError("Invalid provider response ID")
        with self.database.transaction() as connection:
            row = self._request(connection, request_id)
            if (
                row["provider_response_id"]
                and row["provider_response_id"] != provider_id
            ):
                raise BillingError(
                    409, "provider_id_conflict", "Provider identifiers disagree."
                )
            connection.execute(
                "UPDATE requests SET provider_response_id=? WHERE id=?",
                (provider_id, request_id),
            )

    def freeze(self, customer_id: str, frozen: bool, reason: str):
        with self.database.transaction() as connection:
            self._wallet(connection, customer_id)
            connection.execute(
                "UPDATE wallets SET frozen=? WHERE customer_id=?",
                (int(frozen), customer_id),
            )
            self.audit(
                connection,
                "account_frozen" if frozen else "account_unfrozen",
                customer_id,
                {"reason": reason},
            )
        return self.balance(customer_id)

    def resolve(self, request_id: str, payload):
        """Resolve only uncertain outcomes, keeping operator evidence in the immutable audit trail."""
        if payload.action == "record_usage":
            self.record_usage(
                request_id,
                payload.input_tokens,
                payload.output_tokens,
                payload.cached_tokens,
                payload.cache_write_tokens,
                evidence=payload.evidence,
            )
            self.settle(request_id)
        else:
            with self.database.transaction() as connection:
                row = self._request(connection, request_id)
                if row["state"] != "unresolved":
                    raise BillingError(
                        409,
                        "not_unresolved",
                        "Only unresolved work may be released by an operator.",
                    )
                connection.execute(
                    "UPDATE wallets SET held_micro=held_micro-? WHERE customer_id=?",
                    (row["reserve_micro"], row["customer_id"]),
                )
                connection.execute(
                    "UPDATE requests SET state='released',error_code='operator_release',usage_evidence=?,resolved_by='operator',updated_at=? WHERE id=?",
                    (payload.evidence, time.time(), request_id),
                )
                connection.execute(
                    "UPDATE settlement_jobs SET state='done' WHERE request_id=?",
                    (request_id,),
                )
                self.audit(
                    connection,
                    "reservation_released",
                    request_id,
                    {
                        "evidence": payload.evidence,
                        "known_provider_cost_micro": row["actual_micro"],
                    },
                )
        return self.request(request_id)

    def heartbeat(self):
        with self.database.transaction() as connection:
            now = float(
                connection.execute(
                    "SELECT EXTRACT(EPOCH FROM clock_timestamp())"
                    if self.database.url
                    else "SELECT CAST(strftime('%s','now') AS REAL)"
                ).fetchone()[0]
            )
            connection.execute(
                "INSERT INTO service_heartbeats VALUES ('worker',?) ON CONFLICT(name) DO UPDATE SET seen_at=excluded.seen_at",
                (now,),
            )
            connection.execute(
                "DELETE FROM sessions WHERE expires_at<?", (time.time(),)
            )
            connection.execute(
                "DELETE FROM rate_windows WHERE bucket<?", (int(time.time() // 60) - 2,)
            )

    def reconciliation(self):
        """Check posted money against immutable entries and holds against unsettled requests."""
        with self.database.connection() as connection:
            connection.execute(
                "BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY"
                if self.database.url
                else "BEGIN"
            )
            rows = connection.execute(
                """SELECT w.customer_id,w.balance_micro,w.held_micro,
                COALESCE((SELECT SUM(l.amount_micro) FROM ledger l WHERE l.customer_id=w.customer_id),0) AS ledger_balance,
                COALESCE((SELECT SUM(r.reserve_micro) FROM requests r WHERE r.customer_id=w.customer_id AND r.state NOT IN ('settled','released')),0) AS request_holds,
                COALESCE((SELECT SUM(a.amount_micro-a.spent_micro) FROM allowances a WHERE a.customer_id=w.customer_id AND a.state='active'),0) AS allowance_holds
                FROM wallets w"""
            ).fetchall()
            connection.execute("COMMIT")
        failures = [
            dict(r)
            for r in rows
            if r["balance_micro"] != r["ledger_balance"]
            or r["held_micro"] != r["request_holds"] + r["allowance_holds"]
        ]
        return {"ok": not failures, "wallets_checked": len(rows), "failures": failures}
