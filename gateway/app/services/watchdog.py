"""An outside observer: it runs on separate infrastructure and alerts customers.

It cannot live inside the wardens or the database it is watching. It keeps a local copy
of customer contacts, refreshed while the primary is healthy, so it can still send notices
when the primary and its database are completely down.
"""

import asyncio
import json
import logging
import time
from pathlib import Path

import httpx

from app.services.notifier import Notifier

logger = logging.getLogger("tollgate.watchdog")

OUTAGE_SUBJECT = "Tollgate service interruption"
OUTAGE_BODY = (
    "Our main service is currently unreachable. If your application uses the Tollgate "
    "resilient client, it keeps working automatically on its offline allowance. Your usage is "
    "recorded and will be settled when the main service returns. No action is needed from you."
)
RECOVERY_SUBJECT = "Tollgate service restored"
RECOVERY_BODY = (
    "The main service is back. Your application will upload the usage recorded during the "
    "interruption, and your balance will be updated after settlement."
)


class Watchdog:
    def __init__(self, settings, notifier: Notifier | None = None, transport=None):
        self.settings, self.transport = settings, transport
        self.notifier = notifier or Notifier(settings)
        self.path = Path(settings.watch_state_path)
        self.state = self._load()
        self.failures = 0

    def _load(self) -> dict:
        try:
            return json.loads(self.path.read_text())
        except (OSError, ValueError):
            return {"down": False, "contacts": [], "contacts_at": 0}

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_suffix(".tmp")
        temp.write_text(json.dumps(self.state))
        temp.replace(self.path)

    async def _healthy(self, client: httpx.AsyncClient) -> bool:
        try:
            return (await client.get(self.settings.watch_ready_url)).status_code == 200
        except httpx.HTTPError:
            return False

    async def _refresh_contacts(self, client: httpx.AsyncClient) -> None:
        if not self.settings.watch_control_url or time.time() - self.state["contacts_at"] < 300:
            return
        try:
            response = await client.get(
                self.settings.watch_control_url.rstrip("/") + "/admin/contacts",
                headers={"Authorization": "Bearer " + self.settings.development_api_key.get_secret_value()},
            )
            if response.status_code == 200:
                self.state["contacts"] = response.json()["contacts"]
                self.state["contacts_at"] = time.time()
                self._save()
        except (httpx.HTTPError, ValueError, KeyError):
            pass

    async def tick(self, client: httpx.AsyncClient) -> str | None:
        """Return 'outage' or 'recovery' when a notice was triggered, otherwise None."""
        if await self._healthy(client):
            self.failures = 0
            await self._refresh_contacts(client)
            if self.state["down"]:
                self.state["down"] = False
                self._save()
                await asyncio.to_thread(
                    self.notifier.notify, self.state["contacts"], RECOVERY_SUBJECT, RECOVERY_BODY
                )
                return "recovery"
            return None
        self.failures += 1
        if self.failures >= self.settings.watch_failures_to_alert and not self.state["down"]:
            self.state["down"] = True
            self._save()
            await asyncio.to_thread(
                self.notifier.notify, self.state["contacts"], OUTAGE_SUBJECT, OUTAGE_BODY
            )
            return "outage"
        return None

    async def run(self) -> None:
        async with httpx.AsyncClient(
            timeout=5, follow_redirects=False, trust_env=False, transport=self.transport
        ) as client:
            while True:
                try:
                    await self.tick(client)
                except Exception as exc:  # never let the watcher die
                    logger.error("Watchdog tick failed exception_type=%s", type(exc).__name__)
                await asyncio.sleep(self.settings.watch_interval_seconds)


async def run_watchdog(settings):
    if not settings.watch_ready_url:
        raise SystemExit("Set TOLLGATE_WATCH_READY_URL to the primary /ready address.")
    await Watchdog(settings).run()
