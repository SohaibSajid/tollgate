"""Persistent settlement jobs and conservative recovery of interrupted requests."""

import asyncio
import logging

logger = logging.getLogger("tollgate.worker")


class SettlementWorker:
    def __init__(self, ledger, provider, settings, continuity=None):
        """Use the database queue so jobs survive process termination."""
        self.ledger, self.provider, self.settings = ledger, provider, settings
        self.continuity = continuity
        self.stop = asyncio.Event()

    async def tick(self) -> dict:
        """Settle ready money first; bound parallel recovery so slow providers cannot starve it."""
        settled = 0
        for request_id in await asyncio.to_thread(self.ledger.pending_jobs):
            settled += int(await asyncio.to_thread(self.ledger.settle, request_id))
        await asyncio.to_thread(self.ledger.heartbeat)
        if self.continuity:
            await asyncio.to_thread(self.continuity.close_expired)
        candidates = await asyncio.to_thread(
            self.ledger.recovery_candidates, self.settings.recovery_after_seconds
        )
        semaphore = asyncio.Semaphore(4)

        async def recover(row):
            async with semaphore:
                usage = await self.provider.lookup_usage(row["id"])
                if usage is not None:
                    await asyncio.to_thread(
                        self.ledger.record_usage,
                        row["id"],
                        usage["prompt_tokens"],
                        usage["completion_tokens"],
                        (usage.get("prompt_tokens_details") or {}).get(
                            "cached_tokens", 0
                        ),
                        usage.get("cache_write_tokens", 0),
                    )
                else:
                    await asyncio.to_thread(
                        self.ledger.unresolved, row["id"], "awaiting_provider_usage"
                    )
                await asyncio.to_thread(self.ledger.heartbeat)
                return int(usage is not None)

        results = await asyncio.gather(
            *(recover(row) for row in candidates), return_exceptions=True
        )
        recovered = sum(value for value in results if isinstance(value, int))
        for value in results:
            if isinstance(value, BaseException):
                logger.error("Recovery failed exception_type=%s", type(value).__name__)
        for request_id in await asyncio.to_thread(self.ledger.pending_jobs):
            settled += int(await asyncio.to_thread(self.ledger.settle, request_id))
        await asyncio.to_thread(self.ledger.heartbeat)
        return {"recovered": recovered, "settled": settled}

    async def run(self) -> None:
        """Run until shutdown; a failed tick leaves durable jobs available for retry."""
        while not self.stop.is_set():
            try:
                await self.tick()
            except Exception as exc:
                logger.error("Worker tick failed exception_type=%s", type(exc).__name__)
            try:
                await asyncio.wait_for(
                    self.stop.wait(), timeout=self.settings.worker_interval_seconds
                )
            except TimeoutError:
                pass


async def run_worker(settings):
    """Run settlement independently of the public API process."""
    from app.database import Database
    from app.services.ledger import Ledger
    from app.services.provider_client import ProviderClient
    from app.services.live_provider import LiveProviderClient
    from app.routing.providers import load_models
    from app.logging.request_log import configure_logging

    configure_logging()
    database = Database(
        settings.database_path,
        settings.database_url.get_secret_value() if settings.database_url else None,
    )
    await asyncio.to_thread(database.check_version)
    ledger = Ledger(database, settings.customer_concurrency)
    provider = (
        ProviderClient(settings)
        if settings.mode == "simulator"
        else LiveProviderClient(settings, load_models(settings), ledger)
    )
    from app.services.continuity import Continuity

    models = load_models(settings)
    continuity = Continuity(database, ledger, models, settings)
    worker = SettlementWorker(ledger, provider, settings, continuity)
    try:
        await worker.run()
    finally:
        await provider.close()
        await asyncio.to_thread(database.close)
