"""Resilient Tollgate client: your users never notice a Tollgate outage.

Request path, chosen automatically on every call
------------------------------------------------
1. Primary: Tollgate wardens (normal path: reserve, provider, settle).
2. Relay:   the standby relay, using a signed allowance (if a relay is configured).
3. Direct:  straight to the AI provider with a restricted emergency key (only if the owner
            approved this customer and `direct_failover=True`). Last resort.

Nobody has to do anything: if tier 1 is provably unreachable the next tier is tried at once.
Every prompt and answer is saved in YOUR OWN local database (journal.db); Tollgate itself never
stores them. When Tollgate is back, `sync()` uploads token counts so the wallet is charged once
and the unused allowance is released.

Safety rules
------------
* It moves to the next tier only when the previous one provably did not run the request
  (connection refused, DNS failure, connect timeout, or HTTP 503). After a read timeout or a
  504 the request may already be charged, so it raises UnknownOutcome instead of risking a repeat.
* Spending in tiers 2 and 3 is capped by the allowance, checked locally before every call.
* Direct mode uses a provider key that left the gateway. Use a restricted key with a spending
  limit set at the provider, and protect journal.db like a password file.
"""

import json
import sqlite3
import time
import uuid

import httpx

import direct_providers as direct


class OutageError(RuntimeError):
    """Every available tier was unreachable."""


class UnknownOutcome(RuntimeError):
    """A tier may have run this request; check before retrying."""


class AllowanceExhausted(RuntimeError):
    """The offline allowance cannot cover this call."""


class _TierUnavailable(Exception):
    """This tier provably did not run the request; try the next one."""


SCHEMA = """
CREATE TABLE IF NOT EXISTS calls (
 id TEXT PRIMARY KEY, created_at REAL NOT NULL, via TEXT NOT NULL, model TEXT NOT NULL,
 prompt TEXT NOT NULL, response TEXT, input_tokens INTEGER, output_tokens INTEGER,
 cached_tokens INTEGER NOT NULL DEFAULT 0, cache_write_tokens INTEGER NOT NULL DEFAULT 0,
 record_id TEXT, allowance_id TEXT, synced INTEGER NOT NULL DEFAULT 0, error TEXT,
 relay_time REAL, relay_sig TEXT, cost_micro INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS allowance (
 id TEXT PRIMARY KEY, token TEXT NOT NULL, expires_at REAL NOT NULL, relay_url TEXT,
 models TEXT NOT NULL, closed INTEGER NOT NULL DEFAULT 0,
 amount_micro INTEGER NOT NULL DEFAULT 0, prices_json TEXT, direct_json TEXT
)
"""
EXTRA_COLUMNS = {
    "calls": {"relay_time": "REAL", "relay_sig": "TEXT", "cost_micro": "INTEGER NOT NULL DEFAULT 0",
              "cache_write_tokens": "INTEGER NOT NULL DEFAULT 0"},
    "allowance": {"amount_micro": "INTEGER NOT NULL DEFAULT 0", "prices_json": "TEXT", "direct_json": "TEXT"},
}
CONNECT_ERRORS = (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout)


class ResilientTollgate:
    def __init__(
        self,
        api_key,
        primary_url,
        relay_url="",
        journal_path="journal.db",
        models=("gemini-3.5-flash-lite",),
        allowance_usd="1.00",
        allowance_minutes=240,
        direct_failover=False,
        failover_order=("relay", "direct"),
        on_failover=None,
        transport=None,
    ):
        self.key, self.primary, self.relay = api_key, primary_url.rstrip("/"), relay_url.rstrip("/")
        self.models, self.allowance_usd, self.allowance_minutes = list(models), allowance_usd, allowance_minutes
        self.direct_failover, self.order, self.on_failover = direct_failover, tuple(failover_order), on_failover
        self.http = httpx.Client(timeout=httpx.Timeout(60, connect=3), transport=transport, trust_env=False)
        self.db = sqlite3.connect(journal_path, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        for statement in SCHEMA.split(";"):
            if statement.strip():
                self.db.execute(statement)
        for table, columns in EXTRA_COLUMNS.items():
            existing = {r[1] for r in self.db.execute(f"PRAGMA table_info({table})")}
            for name, definition in columns.items():
                if name not in existing:
                    self.db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")

    def close(self):
        self.http.close()
        self.db.close()

    # ------------------------------------------------------------------ allowance
    def _current_allowance(self):
        return self.db.execute(
            "SELECT * FROM allowance WHERE closed=0 AND expires_at>? ORDER BY expires_at DESC LIMIT 1",
            (time.time() + 60,),
        ).fetchone()

    def refresh_allowance(self, force=False):
        """Fetch a fresh allowance while Tollgate is healthy. Call this on a timer (e.g. every 30 min)."""
        current = self._current_allowance()
        if current and not force and current["expires_at"] - time.time() > self.allowance_minutes * 30:
            return dict(current)
        response = self.http.post(
            f"{self.primary}/v1/allowance",
            headers={"Authorization": f"Bearer {self.key}"},
            json={
                "amount_usd": self.allowance_usd,
                "ttl_minutes": self.allowance_minutes,
                "models": self.models,
                "include_direct": self.direct_failover,
            },
        )
        response.raise_for_status()
        body = response.json()
        self.db.execute(
            "INSERT OR REPLACE INTO allowance(id,token,expires_at,relay_url,models,closed,amount_micro,prices_json,direct_json) VALUES (?,?,?,?,?,0,?,?,?)",
            (
                body["allowance_id"],
                body["token"],
                body["expires_at"],
                body.get("relay_url"),
                json.dumps(body["models"]),
                body.get("amount_micro", 0),
                json.dumps(body.get("prices", {})),
                json.dumps(body["direct"]) if body.get("direct") else None,
            ),
        )
        return body

    def _spent(self, allowance_id):
        return self.db.execute(
            "SELECT COALESCE(SUM(cost_micro),0) FROM calls WHERE allowance_id=? AND via IN ('relay','direct')",
            (allowance_id,),
        ).fetchone()[0]

    def _guard(self, allowance, model, messages, max_tokens):
        prices = json.loads(allowance["prices_json"] or "{}")
        price = prices.get(model)
        if price is None or not allowance["amount_micro"]:
            return price
        if self._spent(allowance["id"]) + direct.worst_case_micro(messages, max_tokens, price) > allowance["amount_micro"]:
            raise AllowanceExhausted("The offline allowance cannot cover this call.")
        return price

    # ------------------------------------------------------------------ calls
    def chat(self, messages, model, max_completion_tokens=256):
        call_id = uuid.uuid4().hex
        payload = {"model": model, "messages": messages, "max_completion_tokens": max_completion_tokens}
        self.db.execute(
            "INSERT INTO calls(id,created_at,via,model,prompt) VALUES (?,?,?,?,?)",
            (call_id, time.time(), "pending", model, json.dumps(messages)),
        )
        try:
            response = self.http.post(
                f"{self.primary}/v1/chat/completions",
                headers={"Authorization": f"Bearer {self.key}", "Idempotency-Key": call_id},
                json=payload,
            )
            if response.status_code != 503:
                if response.status_code != 200:
                    self._fail(call_id, f"http_{response.status_code}")
                    response.raise_for_status()
                body = response.json()
                self._store(call_id, "primary", body["choices"][0]["message"]["content"], body["usage"],
                            response.headers.get("X-Tollgate-Request-ID"), None, 0, synced=1)
                return body
            reason = "primary_refused"
        except CONNECT_ERRORS:
            reason = "primary_unreachable"
        except httpx.HTTPStatusError:
            raise
        except httpx.HTTPError as exc:
            self._fail(call_id, "unknown_outcome")
            raise UnknownOutcome(f"The primary may have processed call {call_id}: {type(exc).__name__}") from exc
        return self._failover(call_id, payload, reason)

    def _failover(self, call_id, payload, reason):
        allowance = self._current_allowance()
        if allowance is None:
            self._fail(call_id, "no_allowance")
            raise OutageError("Tollgate is down and no valid offline allowance is held.")
        if self.on_failover:
            self.on_failover(reason)
        model, messages, max_tokens = payload["model"], payload["messages"], payload["max_completion_tokens"]
        try:
            price = self._guard(allowance, model, messages, max_tokens)
        except AllowanceExhausted:
            self._fail(call_id, "allowance_exhausted")
            raise
        for tier in self.order:
            try:
                if tier == "relay" and self.relay:
                    return self._via_relay(call_id, payload, allowance, price)
                if tier == "direct" and self.direct_failover and allowance["direct_json"] and price:
                    return self._via_direct(call_id, payload, allowance, price)
            except _TierUnavailable:
                continue
        self._fail(call_id, "all_tiers_unavailable")
        raise OutageError("Tollgate, the standby relay and direct access are all unavailable.")

    def _via_relay(self, call_id, payload, allowance, price):
        try:
            response = self.http.post(
                f"{self.relay}/v1/chat/completions",
                headers={"Authorization": f"Bearer {allowance['token']}"},
                json=payload,
            )
        except CONNECT_ERRORS as exc:
            raise _TierUnavailable from exc
        except httpx.HTTPError as exc:
            self._fail(call_id, "unknown_outcome", "relay")
            raise UnknownOutcome(f"The relay may have processed call {call_id}.") from exc
        if response.status_code == 503:
            raise _TierUnavailable
        if response.status_code != 200:
            self._fail(call_id, f"http_{response.status_code}", "relay")
            response.raise_for_status()
        body = response.json()
        usage = self._normal(body["usage"])
        cost = direct.cost_micro(usage, price) if price else 0
        self._store(call_id, "relay", body["choices"][0]["message"]["content"], body["usage"],
                    response.headers.get("X-Tollgate-Record-ID"), allowance["id"], cost,
                    relay_time=float(response.headers["X-Tollgate-Record-Time"]) if "X-Tollgate-Record-Time" in response.headers else None,
                    relay_sig=response.headers.get("X-Tollgate-Record-Sig"))
        return body

    def _via_direct(self, call_id, payload, allowance, price):
        keys = json.loads(allowance["direct_json"])["keys"]
        provider = price["provider"]
        if provider not in keys:
            raise _TierUnavailable
        url, headers, request_body = direct.build_request(
            provider, price["upstream_model"], payload["messages"], payload["max_completion_tokens"], keys[provider]
        )
        try:
            response = self.http.post(url, headers=headers, json=request_body, timeout=httpx.Timeout(120, connect=5))
        except CONNECT_ERRORS as exc:
            raise _TierUnavailable from exc
        except httpx.HTTPError as exc:
            self._fail(call_id, "unknown_outcome", "direct")
            raise UnknownOutcome(f"The provider may have processed call {call_id}.") from exc
        if response.status_code != 200:
            self._fail(call_id, f"provider_http_{response.status_code}", "direct")
            response.raise_for_status()
        try:
            text, usage = direct.parse_response(provider, response.json())
        except ValueError as exc:
            self._fail(call_id, "unusable_provider_answer", "direct")
            raise UnknownOutcome("The provider answered, but usage could not be read; the call may be charged.") from exc
        body = {
            "choices": [{"message": {"role": "assistant", "content": text}}],
            "usage": {"prompt_tokens": usage["prompt_tokens"], "completion_tokens": usage["completion_tokens"],
                      "prompt_tokens_details": {"cached_tokens": usage["cached_tokens"]},
                      "cache_write_tokens": usage["cache_write_tokens"]},
            "tollgate_via": "direct",
        }
        self._store(call_id, "direct", text, body["usage"], "dir_" + call_id, allowance["id"], direct.cost_micro(usage, price))
        return body

    @staticmethod
    def _normal(usage):
        return {
            "prompt_tokens": usage["prompt_tokens"],
            "completion_tokens": usage["completion_tokens"],
            "cached_tokens": (usage.get("prompt_tokens_details") or {}).get("cached_tokens", 0),
            "cache_write_tokens": usage.get("cache_write_tokens", 0),
        }

    def _store(self, call_id, via, text, usage, record_id, allowance_id, cost, synced=0, relay_time=None, relay_sig=None):
        u = self._normal(usage)
        self.db.execute(
            "UPDATE calls SET via=?,response=?,input_tokens=?,output_tokens=?,cached_tokens=?,cache_write_tokens=?,record_id=?,allowance_id=?,synced=?,cost_micro=?,relay_time=?,relay_sig=? WHERE id=?",
            (via, json.dumps(text), u["prompt_tokens"], u["completion_tokens"], u["cached_tokens"],
             u["cache_write_tokens"], record_id, allowance_id, synced, cost, relay_time, relay_sig, call_id),
        )

    def _fail(self, call_id, error, via="failed"):
        self.db.execute("UPDATE calls SET via=?,error=? WHERE id=?", (via, error, call_id))

    # ------------------------------------------------------------------ settle after recovery
    def sync(self, close=False):
        """Upload relay and direct usage to Tollgate. Safe to repeat: each record counts once."""
        rows = self.db.execute(
            "SELECT * FROM calls WHERE synced=0 AND record_id IS NOT NULL AND allowance_id IS NOT NULL "
            "AND ((via='relay' AND relay_sig IS NOT NULL) OR via='direct')"
        ).fetchall()
        grouped = {}
        for row in rows:
            grouped.setdefault(row["allowance_id"], []).append(row)
        results = []
        for allowance_id, calls in grouped.items():
            records = [
                {
                    "record_id": c["record_id"],
                    "model": c["model"],
                    "input_tokens": c["input_tokens"],
                    "output_tokens": c["output_tokens"],
                    "cached_tokens": c["cached_tokens"],
                    "cache_write_tokens": c["cache_write_tokens"],
                    "created_at": c["created_at"] if c["via"] == "direct" else c["relay_time"],
                    "sig": "" if c["via"] == "direct" else c["relay_sig"],
                }
                for c in calls
            ]
            response = self.http.post(
                f"{self.primary}/v1/offline/sync",
                headers={"Authorization": f"Bearer {self.key}"},
                json={"allowance_id": allowance_id, "records": records, "close": close},
            )
            response.raise_for_status()
            self.db.executemany("UPDATE calls SET synced=1 WHERE id=?", [(c["id"],) for c in calls])
            results.append(response.json())
        return results

    def pending_sync(self):
        return self.db.execute(
            "SELECT COUNT(*) FROM calls WHERE via IN ('relay','direct') AND synced=0"
        ).fetchone()[0]
