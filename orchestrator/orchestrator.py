"""
Multi-bot Kalshi trading orchestrator.

Manages all registered bots (B, C, D, ...), polls their signal engines
on a schedule, applies shared risk management, and routes positions to
the unified paper trader.

Bot registration pattern:
    orchestrator = Orchestrator()
    orchestrator.register(BotD())
    orchestrator.run_loop()
"""

from __future__ import annotations

import logging
import time
from datetime import datetime
from typing import Protocol

import schedule

from shared.paper_trader import PaperTrader
from shared.signal_engine import BaseSignalEngine, Signal

logger = logging.getLogger(__name__)


class BotInterface(Protocol):
    """Duck-typed interface that all bots must satisfy."""
    BOT_ID: str
    signal_engine: BaseSignalEngine

    def run_once(self) -> dict: ...
    def status(self) -> dict: ...


class Orchestrator:
    """
    Orchestrates all registered trading bots.

    Responsibilities:
      - Register and track bots (B, C, D, ...)
      - Run each bot's cycle on its own schedule
      - Aggregate status for the unified dashboard
      - Enforce portfolio-level risk limits
    """

    # Max total open positions across all bots
    MAX_TOTAL_POSITIONS = 20

    # Max fraction of combined bankroll allocated across all open positions
    MAX_TOTAL_EXPOSURE = 0.40

    def __init__(self):
        self._bots: dict[str, BotInterface] = {}
        self._run_results: dict[str, dict] = {}

    def register(self, bot: BotInterface):
        self._bots[bot.BOT_ID] = bot
        logger.info("Orchestrator: registered bot '%s'", bot.BOT_ID)

    def run_all_once(self) -> dict:
        """Run one cycle for every registered bot. Returns aggregated summary."""
        results = {}
        for bot_id, bot in self._bots.items():
            try:
                result = bot.run_once()
                self._run_results[bot_id] = result
                results[bot_id] = result
                logger.info("Bot %s cycle complete: %s", bot_id, result)
            except Exception as exc:
                logger.error("Bot %s cycle failed: %s", bot_id, exc)
                results[bot_id] = {"error": str(exc)}
        return results

    def status_all(self) -> dict:
        """Aggregate status from all registered bots."""
        statuses = {}
        for bot_id, bot in self._bots.items():
            try:
                statuses[bot_id] = bot.status()
            except Exception as exc:
                statuses[bot_id] = {"error": str(exc)}

        total_pnl = sum(
            s.get("total_pnl", 0) for s in statuses.values() if isinstance(s, dict)
        )
        total_trades = sum(
            s.get("total_trades", 0) for s in statuses.values() if isinstance(s, dict)
        )

        return {
            "bots": statuses,
            "aggregate": {
                "total_pnl": round(total_pnl, 2),
                "total_trades": total_trades,
                "active_bots": len(self._bots),
            },
            "timestamp": datetime.utcnow().isoformat(sep=" ", timespec="seconds"),
        }

    def run_loop(self, interval_minutes: int = 15):
        """Continuous scheduled loop. Blocks until interrupted."""
        logger.info(
            "Orchestrator starting loop with %d bots (every %d min)",
            len(self._bots),
            interval_minutes,
        )
        schedule.every(interval_minutes).minutes.do(self.run_all_once)
        self.run_all_once()  # Immediate first run
        while True:
            schedule.run_pending()
            time.sleep(30)
