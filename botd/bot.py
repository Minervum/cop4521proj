"""
Bot D — Esports trading bot.

The primary entry point for running Bot D standalone or via the orchestrator.
Combines the signal engine, paper trader, and data sync into a single run loop.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime
from typing import Optional

import schedule

from shared.paper_trader import PaperTrader
from botd.config import (
    DB_PATH,
    PAPER_BANKROLL,
    HLTV_REFRESH_INTERVAL,
    VLR_REFRESH_INTERVAL,
    LIQUIPEDIA_REFRESH_INTERVAL,
)
from botd.engine.signals import EloSignalEngine      # primary ELO engine
from botd.signals import EsportsSignalEngine          # legacy weight-blend (kept for comparison)
from botd.data.hltv import HLTVScraper
from botd.data.vlr import VLRScraper
from botd.data.dota import DotaScraper
from botd.data.lol import LoLScraper
from botd.data.liquipedia import LiquipediaScraper
from botd.storage.db import BotDStorage

logger = logging.getLogger(__name__)


class BotD:
    """
    Bot D: Kalshi esports trading bot (CS2 + Valorant + Dota 2 + LoL).

    Architecture mirrors Bot B (sports) and Bot C (economics):
      - SQLite storage via BotDStorage
      - Two-layer ELO signal engine with game-specific adjustments
      - Half-Kelly sizing → shared kelly.py
      - Paper trading via shared PaperTrader
      - Plugs into shared orchestrator via signal_engine property
    """

    BOT_ID = "botd"

    def __init__(self, db_path: str = DB_PATH):
        self.db = BotDStorage(db_path)
        self.signal_engine = EloSignalEngine(db_path)    # two-layer ELO engine
        self.trader = PaperTrader(self.BOT_ID, db_path, PAPER_BANKROLL)
        self.hltv = HLTVScraper(db_path)
        self.vlr = VLRScraper(db_path)
        self.dota = DotaScraper(db_path)
        self.lol = LoLScraper(db_path)
        self.liq = LiquipediaScraper(db_path)
        logger.info(
            "Bot D initialised | bankroll=$%.2f | db=%s",
            self.trader.bankroll(),
            db_path,
        )

    # ------------------------------------------------------------------
    # One-shot run cycle (called by orchestrator or scheduled loop)
    # ------------------------------------------------------------------

    def run_once(self) -> dict:
        """
        Single run cycle:
          1. Sync data if stale
          2. Generate signals
          3. Open paper positions for signals with edge
          4. Return summary
        """
        logger.info("=== Bot D run cycle starting ===")

        # Step 1: Data sync (respects freshness cache)
        self._sync_data()

        # Step 2: Generate signals
        signals = self.signal_engine.get_signals()
        logger.info("Generated %d signals", len(signals))

        # Step 3: Open paper positions
        new_positions = []
        for sig in signals:
            if not sig.has_edge:
                continue
            pos = self.trader.open_position(sig)
            if pos:
                logger.info(
                    "Opened %s position: %s vs %s | %s @ %.1f¢ | kelly=%.3f",
                    sig.game.upper(),
                    sig.team_a,
                    sig.team_b,
                    sig.recommended_side,
                    sig.market_yes_price if sig.recommended_side == "YES" else sig.market_no_price,
                    sig.recommended_kelly,
                )
                new_positions.append(pos)

        summary = self.trader.summary()
        summary["new_positions"] = len(new_positions)
        summary["signals_generated"] = len(signals)
        summary["signals_with_edge"] = sum(1 for s in signals if s.has_edge)
        logger.info("=== Bot D run cycle complete | %s ===", summary)
        return summary

    # ------------------------------------------------------------------
    # Data sync
    # ------------------------------------------------------------------

    def _sync_data(self):
        """Refresh stale data from all sources (freshness-gated)."""
        logger.info("Syncing Liquipedia upcoming matches...")
        self.liq.sync_all(days=7)

        if not self.db.is_fresh("cs2_teams", max_age_hours=24):
            logger.info("Syncing HLTV rankings...")
            self.hltv.sync_rankings()
            self.hltv.sync_roster_changes(days=30)

        if not self.db.is_fresh("val_teams", max_age_hours=24):
            logger.info("Syncing VLR rankings...")
            self.vlr.sync_rankings()
            self.vlr.sync_roster_changes(days=30)

        if not self.db.is_fresh("dota2_teams", max_age_hours=24):
            logger.info("Syncing Dota 2 data...")
            self.dota.sync_teams()

        if not self.db.is_fresh("lol_teams", max_age_hours=24):
            logger.info("Syncing LoL data...")
            self.lol.sync_teams()

    def full_sync(self):
        """Force a complete data refresh regardless of cache."""
        logger.info("Starting full Bot D data sync (all 4 games)...")
        self.hltv.sync_all()
        self.vlr.sync_all()
        self.dota.sync_all()
        self.lol.sync_all()
        self.liq.sync_all(days=7)
        logger.info("Full sync complete")

    # ------------------------------------------------------------------
    # Scheduled loop
    # ------------------------------------------------------------------

    def run_loop(self, cycle_interval_minutes: int = 15):
        """
        Run Bot D as a continuous scheduled loop.
        Designed for standalone operation; orchestrator calls run_once() directly.
        """
        logger.info("Starting Bot D scheduled loop (every %d min)", cycle_interval_minutes)
        schedule.every(cycle_interval_minutes).minutes.do(self.run_once)
        schedule.every().day.at("06:00").do(self.full_sync)

        self.run_once()  # Immediate first run
        while True:
            schedule.run_pending()
            time.sleep(30)

    # ------------------------------------------------------------------
    # Reporting
    # ------------------------------------------------------------------

    def status(self) -> dict:
        """Return current status for the unified dashboard."""
        summary = self.trader.summary()
        upcoming_count = self.db.scalar(
            "SELECT COUNT(*) FROM liq_upcoming_matches WHERE match_datetime >= datetime('now')"
        ) or 0
        calibration = self.signal_engine.calibrate()

        data = {}
        for game in ("cs2", "val", "dota2", "lol"):
            data[f"{game}_teams"]   = self.db.scalar(f"SELECT COUNT(*) FROM {game}_teams") or 0
            data[f"{game}_matches"] = self.db.scalar(f"SELECT COUNT(*) FROM {game}_matches") or 0
        data["upcoming_matches"] = upcoming_count

        return {
            "bot_id": self.BOT_ID,
            "games": ["cs2", "val", "dota2", "lol"],
            "bankroll": summary["bankroll"],
            "total_pnl": summary["total_pnl"],
            "total_trades": summary["total_trades"],
            "win_rate": (
                round(summary["wins"] / summary["total_trades"], 3)
                if summary["total_trades"] > 0 else None
            ),
            "open_positions": summary["open_positions"],
            "data": data,
            "calibration": calibration,
            "timestamp": datetime.utcnow().isoformat(sep=" ", timespec="seconds"),
        }


def main():
    """CLI entry point."""
    import argparse
    import json

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    parser = argparse.ArgumentParser(description="Bot D - Esports Kalshi Trading Bot")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("run", help="Run one signal cycle")
    sub.add_parser("loop", help="Run continuous scheduled loop")
    sub.add_parser("sync", help="Force full data sync")
    sub.add_parser("status", help="Print bot status")

    args = parser.parse_args()
    bot = BotD()

    if args.command == "loop":
        bot.run_loop()
    elif args.command == "sync":
        bot.full_sync()
    elif args.command == "status":
        print(json.dumps(bot.status(), indent=2))
    else:
        result = bot.run_once()
        print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
