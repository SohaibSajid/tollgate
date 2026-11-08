"""Optional hosted Stripe Checkout with signed, idempotent wallet fulfillment."""

import asyncio
import hashlib
import hmac
import json
import time
import httpx
from app.services.ledger import BillingError, new_id
from app.services.pricing import usd_to_micro, MAX_WALLET_MICRO


class Payments:
    def __init__(self, settings, ledger, transport=None):
        self.settings, self.ledger = settings, ledger
        self.client = httpx.AsyncClient(
            timeout=20, follow_redirects=False, trust_env=False, transport=transport
        )

    @property
    def enabled(self):
        return bool(
            self.settings.stripe_secret_key.get_secret_value()
            and self.settings.stripe_webhook_secret.get_secret_value()
        )

    async def close(self):
        await self.client.aclose()

    def order(self, customer_id, amount, reference):
        with self.ledger.database.transaction() as connection:
            wallet = self.ledger._wallet(connection, customer_id)
            if wallet["frozen"]:
                raise BillingError(403, "account_frozen", "This account is suspended.")
            row = connection.execute(
                "SELECT * FROM payment_orders WHERE customer_id=? AND reference=?",
                (customer_id, reference),
            ).fetchone()
            if row:
                if row["amount_micro"] != amount:
                    raise BillingError(
                        409,
                        "reference_conflict",
                        "This checkout reference has a different amount.",
                    )
                return dict(row)
            order_id = new_id("order_")
            connection.execute(
                "INSERT INTO payment_orders(id,customer_id,amount_micro,reference,created_at) VALUES (?,?,?,?,?)",
                (order_id, customer_id, amount, reference, time.time()),
            )
        return {"id": order_id, "created_at": time.time()}

    async def checkout(self, customer_id, payload):
        if not self.enabled:
            raise BillingError(
                503,
                "payments_disabled",
                "Hosted payments are not configured. Contact the operator.",
            )
        amount = usd_to_micro(payload.amount_usd)
        order = await asyncio.to_thread(
            self.order, customer_id, amount, payload.reference
        )
        if time.time() - order["created_at"] > 20 * 3600:
            raise BillingError(
                409,
                "checkout_reference_expired",
                "Use a new reference for a new checkout.",
            )
        data = {
            "mode": "payment",
            "success_url": self.settings.public_origin + "/?payment=received",
            "cancel_url": self.settings.public_origin + "/?payment=cancelled",
            "client_reference_id": order["id"],
            "metadata[tollgate_order_id]": order["id"],
            "payment_intent_data[metadata][tollgate_order_id]": order["id"],
            "line_items[0][price_data][currency]": "usd",
            "line_items[0][price_data][unit_amount]": str(amount // 10000),
            "line_items[0][price_data][product_data][name]": "Tollgate prepaid AI credits",
            "line_items[0][quantity]": "1",
        }
        try:
            async with self.client.stream(
                "POST",
                "https://api.stripe.com/v1/checkout/sessions",
                data=data,
                headers={
                    "Authorization": "Bearer "
                    + self.settings.stripe_secret_key.get_secret_value(),
                    "Idempotency-Key": order["id"],
                },
            ) as response:
                content = bytearray()
                async for chunk in response.aiter_bytes():
                    content.extend(chunk)
                    if len(content) > 131072:
                        raise ValueError()
                result = json.loads(content)
                if (
                    response.status_code != 200
                    or not isinstance(result.get("url"), str)
                    or not result["url"].startswith("https://checkout.stripe.com/")
                ):
                    raise ValueError()
        except (httpx.HTTPError, ValueError) as exc:
            raise BillingError(
                503,
                "checkout_unavailable",
                "Could not create checkout. Retry with the same reference.",
            ) from exc
        await asyncio.to_thread(self.save_session, order["id"], result["id"])
        return {"checkout_url": result["url"], "order_id": order["id"]}

    def save_session(self, order_id, session_id):
        with self.ledger.database.transaction() as connection:
            row = connection.execute(
                "SELECT session_id FROM payment_orders WHERE id=?", (order_id,)
            ).fetchone()
            if row[0] not in (None, session_id):
                raise BillingError(
                    409,
                    "checkout_conflict",
                    "The checkout session does not match the saved order.",
                )
            connection.execute(
                "UPDATE payment_orders SET session_id=? WHERE id=?",
                (session_id, order_id),
            )

    def webhook(self, body: bytes, signature: str):
        """Verify raw bytes, timestamp, expected amount, and mode before changing a wallet."""
        if not self.enabled:
            raise BillingError(503, "payments_disabled", "Payments are not configured.")
        try:
            parts = [part.split("=", 1) for part in signature.split(",")]
            timestamps = [value for key, value in parts if key == "t"]
            signatures = [value for key, value in parts if key == "v1"]
            if len(timestamps) != 1 or abs(time.time() - int(timestamps[0])) > 300:
                raise ValueError()
            expected = hmac.new(
                self.settings.stripe_webhook_secret.get_secret_value().encode(),
                timestamps[0].encode() + b"." + body,
                hashlib.sha256,
            ).hexdigest()
            if not any(hmac.compare_digest(expected, value) for value in signatures):
                raise ValueError()
            event = json.loads(body)
            if (
                not isinstance(event["id"], str)
                or len(event["id"]) > 128
                or type(event["livemode"]) is not bool
                or event["livemode"] != self.settings.stripe_live_mode
            ):
                raise ValueError()
            obj, kind = event["data"]["object"], event["type"]
        except (ValueError, KeyError, TypeError) as exc:
            raise BillingError(
                400,
                "invalid_webhook",
                "Webhook signature, mode, or payload is invalid.",
            ) from exc
        with self.ledger.database.transaction() as connection:
            if connection.execute(
                "SELECT id FROM payment_events WHERE id=?", (event["id"],)
            ).fetchone():
                return {"received": True, "duplicate": True}
            if (
                kind
                in (
                    "checkout.session.completed",
                    "checkout.session.async_payment_succeeded",
                )
                and obj.get("payment_status") == "paid"
            ):
                order_id = obj.get("metadata", {}).get("tollgate_order_id")
                order = connection.execute(
                    "SELECT * FROM payment_orders WHERE id=?", (order_id,)
                ).fetchone()
                if (
                    not order
                    or obj.get("mode") != "payment"
                    or obj.get("currency") != "usd"
                    or type(obj.get("amount_total")) is not int
                    or obj["amount_total"] * 10000 != order["amount_micro"]
                    or order["session_id"] not in (None, obj.get("id"))
                ):
                    raise BillingError(
                        400,
                        "payment_mismatch",
                        "Payment does not match a pending Tollgate order.",
                    )
                wallet = self.ledger._wallet(connection, order["customer_id"])
                if order["state"] == "pending":
                    if (
                        wallet["balance_micro"] + order["amount_micro"]
                        > MAX_WALLET_MICRO
                    ):
                        raise BillingError(
                            409,
                            "wallet_limit",
                            "Payment requires operator review: wallet limit.",
                        )
                    reference = "stripe:" + obj["id"]
                    connection.execute(
                        "INSERT INTO ledger(customer_id,kind,amount_micro,reference,created_at) VALUES (?,'grant',?,?,?)",
                        (
                            order["customer_id"],
                            order["amount_micro"],
                            reference,
                            time.time(),
                        ),
                    )
                    connection.execute(
                        "UPDATE wallets SET balance_micro=balance_micro+? WHERE customer_id=?",
                        (order["amount_micro"], order["customer_id"]),
                    )
                    connection.execute(
                        "UPDATE payment_orders SET state='paid',session_id=?,payment_intent=? WHERE id=?",
                        (obj["id"], obj.get("payment_intent"), order_id),
                    )
                    self.ledger.audit(
                        connection,
                        "payment_credited",
                        order_id,
                        {
                            "event_id": event["id"],
                            "amount_micro": order["amount_micro"],
                        },
                    )
            elif kind in ("charge.refunded", "charge.dispute.created"):
                order = connection.execute(
                    "SELECT * FROM payment_orders WHERE payment_intent=?",
                    (obj.get("payment_intent"),),
                ).fetchone()
                if order:
                    connection.execute(
                        "UPDATE wallets SET frozen=1 WHERE customer_id=?",
                        (order["customer_id"],),
                    )
                    connection.execute(
                        "UPDATE payment_orders SET state='review' WHERE id=?",
                        (order["id"],),
                    )
                self.ledger.audit(
                    connection,
                    "payment_review_required",
                    order["id"] if order else event["id"],
                    {
                        "event_type": kind,
                        "event_id": event["id"],
                        "matched": bool(order),
                    },
                )
            connection.execute(
                "INSERT INTO payment_events VALUES (?,?,?)",
                (event["id"], kind, time.time()),
            )
        return {"received": True}
