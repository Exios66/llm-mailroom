import structlog
import asyncio
from pathlib import Path

from pipeline.bins import review_dir, failed_dir, get_base_dir

logger = structlog.get_logger(__name__)

DEFAULT_SWEEP_INTERVAL = 300


class OpsMonitor:
    def __init__(self, sweep_interval: int | None = None):
        import os
        self.sweep_interval = sweep_interval or int(
            os.environ.get("OPS_MONITOR_INTERVAL_SECONDS", DEFAULT_SWEEP_INTERVAL)
        )
        self._running = False
        self._pause_file = get_base_dir() / "ops_monitor_paused"

    async def start(self):
        logger.info("ops_monitor_starting", interval=self.sweep_interval)
        self._running = True
        while self._running:
            try:
                await self._sweep()
            except Exception:
                logger.exception("ops_monitor_sweep_error")
            await asyncio.sleep(self.sweep_interval)

    async def _sweep(self):
        metrics = await self._gather_metrics()
        findings = await self._analyze_metrics(metrics)
        if findings.get("recommended_action") in ("alert", "pause_ingestion"):
            logger.warning(
                "ops_monitor_alert",
                severity=findings.get("severity"),
                action=findings.get("recommended_action"),
                findings=findings.get("findings", []),
            )
            if findings.get("recommended_action") == "pause_ingestion":
                self._pause_file.parent.mkdir(parents=True, exist_ok=True)
                self._pause_file.write_text("1")
                logger.critical("ops_monitor_paused_ingestion")

    async def _gather_metrics(self) -> dict:
        metrics = {
            "stuck_documents": [],
            "error_rates": {},
            "review_queue_size": 0,
            "failed_queue_size": 0,
        }

        try:
            catalog_data = await self._query_catalog()
            stuck_docs = catalog_data.get("stuck_documents", [])
            metrics["stuck_documents"] = len(stuck_docs) if isinstance(stuck_docs, list) else 0
            metrics["error_rates"] = catalog_data.get("error_rates", {})
        except Exception:
            logger.exception("catalog_query_failed")

        review = review_dir()
        if review.exists():
            metrics["review_queue_size"] = len(list(review.iterdir()))

        failed = failed_dir()
        if failed.exists():
            metrics["failed_queue_size"] = len(list(failed.iterdir()))

        return metrics

    async def _query_catalog(self) -> dict:
        try:
            from storage.catalog import get_stuck_documents, get_error_rate_by_doc_type
            stuck = await get_stuck_documents()
            errors = await get_error_rate_by_doc_type()
            return {
                "stuck_documents": stuck,
                "error_rates": errors,
            }
        except Exception:
            return {"stuck_documents": [], "error_rates": {}}

    async def _analyze_metrics(self, metrics: dict) -> dict:
        try:
            from agents.boss import BossAgent
            boss = BossAgent()
            return boss.analyze_system_metrics(metrics)
        except Exception:
            logger.exception("boss_analysis_error")
            return {
                "severity": "warning",
                "recommended_action": "alert",
                "findings": ["automated analysis failed"],
            }

    def stop(self):
        self._running = False
        logger.info("ops_monitor_stopped")

    @property
    def is_paused(self) -> bool:
        return self._pause_file.exists()


async def run_ops_monitor(sweep_interval: int | None = None):
    monitor = OpsMonitor(sweep_interval=sweep_interval)
    try:
        await monitor.start()
    except asyncio.CancelledError:
        monitor.stop()


if __name__ == "__main__":
    asyncio.run(run_ops_monitor())
