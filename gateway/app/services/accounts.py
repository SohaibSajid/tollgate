"""Self-service client accounts: sign-up, operator approval, password login and portal sessions."""

import hashlib
import hmac
import json
import re
import secrets
import time

from app.services.ledger import BillingError, Ledger, new_id

EMAIL = re.compile(r"[^@\s]{1,64}@[^@\s]{1,190}\.[A-Za-z]{2,24}")
SCRYPT = {"n": 2**14, "r": 8, "p": 1, "dklen": 32}


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, **SCRYPT)
    return "scrypt$" + salt.hex() + "$" + digest.hex()


def check_password(password: str, stored: str) -> bool:
    _, salt, expected = stored.split("$")
    actual = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), **SCRYPT)
    return hmac.compare_digest(actual.hex(), expected)


def token_hash(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


DUMMY_HASH = hash_password(secrets.token_urlsafe(16))


class Accounts:
    def __init__(self, database, ledger: Ledger, settings):
        self.database, self.ledger, self.settings = database, ledger, settings

    def signup(self, business: str, email: str, password: str) -> dict:
        if self.settings.signup_mode == "closed":
            raise BillingError(403, "signup_closed", "Sign-up is closed. Contact the operator.")
        email = email.strip().lower()
        business = business.strip()
        if not 2 <= len(business) <= 80:
            raise BillingError(422, "invalid_signup", "Enter a business name of 2 to 80 characters.")
        if len(email) > 254 or EMAIL.fullmatch(email) is None:
            raise BillingError(422, "invalid_signup", "Enter a valid email address.")
        if not 10 <= len(password) <= 128:
            raise BillingError(422, "invalid_signup", "Use a password of 10 to 128 characters.")
        with self.database.connection() as connection:
            if connection.execute("SELECT 1 FROM accounts WHERE email=?", (email,)).fetchone():
                raise BillingError(409, "email_taken", "An account with this email already exists.")
        password_hash = hash_password(password)
        state = "active" if self.settings.signup_mode == "open" else "pending"
        customer = self.ledger.create_customer(business)
        account_id = new_id("acc_")
        with self.database.transaction() as connection:
            connection.execute(
                "INSERT INTO accounts(id,customer_id,email,password_hash,state,created_at) VALUES (?,?,?,?,?,?)",
                (account_id, customer["id"], email, password_hash, state, time.time()),
            )
            Ledger.audit(connection, "account_signup", account_id, {"customer_id": customer["id"], "state": state})
        return {"account_id": account_id, "customer_id": customer["id"], "state": state}

    def login(self, email: str, password: str) -> tuple[str, str]:
        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT id,password_hash,state FROM accounts WHERE email=?", (email.strip().lower(),)
            ).fetchone()
        if row is None:
            check_password(password, DUMMY_HASH)
            raise BillingError(401, "invalid_login", "The email or password is incorrect.")
        if not check_password(password, row["password_hash"]):
            raise BillingError(401, "invalid_login", "The email or password is incorrect.")
        if row["state"] == "pending":
            raise BillingError(403, "account_pending", "Your account is waiting for the operator's approval.")
        if row["state"] == "suspended":
            raise BillingError(403, "account_suspended", "Your account is suspended. Contact the operator.")
        raw, csrf, now = secrets.token_urlsafe(32), secrets.token_urlsafe(32), time.time()
        with self.database.transaction() as connection:
            connection.execute("DELETE FROM portal_sessions WHERE expires_at<?", (now,))
            connection.execute(
                "INSERT INTO portal_sessions(token_hash,csrf,account_id,expires_at) VALUES (?,?,?,?)",
                (token_hash(raw), csrf, row["id"], now + self.settings.session_hours * 3600),
            )
        return raw, csrf

    def session(self, raw: str) -> dict | None:
        if not 20 <= len(raw) <= 128:
            return None
        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT s.csrf,a.id,a.customer_id,a.email,c.name FROM portal_sessions s JOIN accounts a ON a.id=s.account_id JOIN customers c ON c.id=a.customer_id WHERE s.token_hash=? AND s.expires_at>? AND a.state='active'",
                (token_hash(raw), time.time()),
            ).fetchone()
        if row is None:
            return None
        return {"csrf": row[0], "account_id": row[1], "customer_id": row[2], "email": row[3], "business": row[4]}

    def logout(self, raw: str) -> None:
        with self.database.transaction() as connection:
            connection.execute("DELETE FROM portal_sessions WHERE token_hash=?", (token_hash(raw),))

    def listing(self) -> list[dict]:
        with self.database.connection() as connection:
            return [
                dict(row)
                for row in connection.execute(
                    "SELECT a.id,a.customer_id,a.email,a.state,a.created_at,c.name AS business FROM accounts a JOIN customers c ON c.id=a.customer_id ORDER BY a.created_at DESC LIMIT 200"
                )
            ]

    def set_state(self, account_id: str, state: str) -> dict:
        if state not in ("active", "suspended"):
            raise BillingError(422, "invalid_state", "Use active or suspended.")
        with self.database.transaction() as connection:
            if not connection.execute(
                "UPDATE accounts SET state=? WHERE id=?", (state, account_id)
            ).rowcount:
                raise BillingError(404, "account_not_found", "Account was not found.")
            if state == "suspended":
                connection.execute("DELETE FROM portal_sessions WHERE account_id=?", (account_id,))
            Ledger.audit(connection, "account_" + state, account_id, {})
        return {"id": account_id, "state": state}

    def workspace(self, customer_id: str) -> dict:
        with self.database.connection() as connection:
            keys = [
                dict(row)
                for row in connection.execute(
                    "SELECT id,label,prefix,allowed_models,active,expires_at,created_at FROM api_keys WHERE customer_id=? ORDER BY created_at DESC LIMIT 100",
                    (customer_id,),
                )
            ]
            requests = [
                Ledger._public_request(row)
                for row in connection.execute(
                    "SELECT * FROM requests WHERE customer_id=? ORDER BY created_at DESC LIMIT 100",
                    (customer_id,),
                )
            ]
            end_users = [
                dict(row)
                for row in connection.execute(
                    "SELECT end_user,COUNT(*) AS requests,COALESCE(SUM(input_tokens),0) AS input_tokens,COALESCE(SUM(output_tokens),0) AS output_tokens,COALESCE(SUM(actual_micro),0) AS cost_micro FROM requests WHERE customer_id=? AND state='settled' AND end_user IS NOT NULL GROUP BY end_user ORDER BY cost_micro DESC,requests DESC LIMIT 100",
                    (customer_id,),
                )
            ]
            provider_keys = [
                dict(row)
                for row in connection.execute(
                    "SELECT provider,hint,created_at FROM provider_keys WHERE customer_id=? ORDER BY provider",
                    (customer_id,),
                )
            ]
        for key in keys:
            key["allowed_models"] = json.loads(key["allowed_models"])
        return {
            "balance": self.ledger.balance(customer_id),
            "keys": keys,
            "requests": requests,
            "end_users": end_users,
            "provider_keys": provider_keys,
        }

    def owns_key(self, customer_id: str, key_id: str) -> bool:
        with self.database.connection() as connection:
            return bool(
                connection.execute(
                    "SELECT 1 FROM api_keys WHERE id=? AND customer_id=?", (key_id, customer_id)
                ).fetchone()
            )
