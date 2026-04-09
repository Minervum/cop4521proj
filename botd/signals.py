"""
Bot D signal engine — combines HLTV, VLR, and Liquipedia data into
Kalshi trading signals for CS2 and Valorant markets.

The engine:
  1. Pulls upcoming matches from Liquipedia
  2. Resolves team IDs to HLTV/VLR team records
  3. Assembles MatchFeatures for each match
  4. Runs the probability model
  5. Computes Kelly fractions and edge
  6. Returns Signal objects for any match with positive edge

Plugs into the orchestrator via BaseSignalEngine interface.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import Optional

from shared.signal_engine import BaseSignalEngine, Signal
from shared.kelly import best_side, edge

from botd.config import DB_PATH, MIN_EDGE, KELLY_FRACTION, MAX_POSITION_FRACTION
from botd.storage.db import BotDStorage
from botd.data.hltv import HLTVScraper
from botd.data.vlr import VLRScraper
from botd.data.liquipedia import LiquipediaScraper
from botd.models.probability import (
    EsportsProbabilityModel,
    build_cs2_features,
    build_val_features,
    compute_roster_stability,
)

logger = logging.getLogger(__name__)


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9-]", "", name.lower().replace(" ", "-"))


class EsportsSignalEngine(BaseSignalEngine):
    """
    Signal engine for Bot D: CS2 and Valorant esports markets on Kalshi.

    Edge thesis: information asymmetry. Most Kalshi traders in esports
    markets are casual fans going by name recognition. We synthesize
    HLTV rankings, form, H2H, map pools, player ratings, roster changes,
    and tournament context into a probability that is systematically
    better calibrated than the market.
    """

    def __init__(self, db_path: str = DB_PATH):
        self.db = BotDStorage(db_path)
        self.hltv = HLTVScraper(db_path)
        self.vlr = VLRScraper(db_path)
        self.liq = LiquipediaScraper(db_path)
        self.model = EsportsProbabilityModel()

    @property
    def bot_id(self) -> str:
        return "botd"

    @property
    def games(self) -> list[str]:
        return ["cs2", "val"]

    # ------------------------------------------------------------------
    # Main entry point
    # ------------------------------------------------------------------

    def get_signals(self, market_ids: Optional[list[str]] = None) -> list[Signal]:
        """
        Generate trading signals for upcoming esports matches.

        In live mode, market_ids would be Kalshi market IDs fetched from
        the Kalshi API. In research/paper mode we generate signals for
        all upcoming matches we can find, assigning synthetic market IDs.
        """
        signals = []
        signals.extend(self._signals_for_game("cs2"))
        signals.extend(self._signals_for_game("val"))

        # Filter by market_ids if provided
        if market_ids:
            signals = [s for s in signals if s.market_id in market_ids]

        logger.info(
            "Signal engine generated %d signals (%d with edge)",
            len(signals),
            sum(1 for s in signals if s.has_edge),
        )
        return signals

    def _signals_for_game(self, game: str) -> list[Signal]:
        """Generate signals for all upcoming matches in one game."""
        # Check cache first — if we have fresh data, skip the network fetch
        cached = self.db.get_upcoming_matches(game, days=7)
        if cached and self.db.is_fresh("liq_upcoming_matches", where=f"game='{game}'", max_age_hours=0.5):
            upcoming = cached
        else:
            upcoming = self.liq.sync_upcoming_matches(game, days=7)
        if not upcoming:
            logger.warning("No upcoming %s matches found", game)
            return []

        signals = []
        for match in upcoming:
            try:
                sig = self._evaluate_match(match, game)
                if sig:
                    signals.append(sig)
            except Exception as exc:
                logger.error(
                    "Error evaluating %s match %s vs %s: %s",
                    game,
                    match.get("team1"),
                    match.get("team2"),
                    exc,
                )
        return signals

    # ------------------------------------------------------------------
    # Per-match evaluation
    # ------------------------------------------------------------------

    def _evaluate_match(self, match: dict, game: str) -> Optional[Signal]:
        team1_name = match.get("team1", "")
        team2_name = match.get("team2", "")
        if not team1_name or not team2_name:
            return None

        # Resolve team records from our database
        team1_id = match.get("team1_id") or self._resolve_team_id(team1_name, game)
        team2_id = match.get("team2_id") or self._resolve_team_id(team2_name, game)

        if game == "cs2":
            return self._evaluate_cs2_match(match, team1_id, team2_id, team1_name, team2_name)
        else:
            return self._evaluate_val_match(match, team1_id, team2_id, team1_name, team2_name)

    def _evaluate_cs2_match(
        self, match: dict,
        team1_id: str, team2_id: str,
        team1_name: str, team2_name: str,
    ) -> Optional[Signal]:
        # Gather all feature data
        team1 = self.db.get_cs2_team(team1_id) or {"name": team1_name}
        team2 = self.db.get_cs2_team(team2_id) or {"name": team2_name}

        form1 = self.hltv.compute_recent_form(team1_id)
        form2 = self.hltv.compute_recent_form(team2_id)
        h2h = self.hltv.compute_h2h(team1_id, team2_id)
        map_stats1 = self.db.get_cs2_map_stats(team1_id)
        map_stats2 = self.db.get_cs2_map_stats(team2_id)
        players1 = self.db.get_cs2_players(team1_id)
        players2 = self.db.get_cs2_players(team2_id)
        roster1 = self.db.get_recent_roster_changes(team1_id)
        roster2 = self.db.get_recent_roster_changes(team2_id)

        features = build_cs2_features(
            match=match,
            h2h=h2h, form1=form1, form2=form2,
            map_stats1=map_stats1, map_stats2=map_stats2,
            players1=players1, players2=players2,
            roster_changes1=roster1, roster_changes2=roster2,
            team1=team1, team2=team2,
        )
        result = self.model.predict(features)
        return self._build_signal(match, team1_name, team2_name, result, "cs2")

    def _evaluate_val_match(
        self, match: dict,
        team1_id: str, team2_id: str,
        team1_name: str, team2_name: str,
    ) -> Optional[Signal]:
        t1_rows = self.db.execute("SELECT * FROM val_teams WHERE team_id=?", (team1_id,))
        t2_rows = self.db.execute("SELECT * FROM val_teams WHERE team_id=?", (team2_id,))
        team1 = t1_rows[0] if t1_rows else {"name": team1_name}
        team2 = t2_rows[0] if t2_rows else {"name": team2_name}

        form1 = self.vlr.compute_recent_form(team1_id)
        form2 = self.vlr.compute_recent_form(team2_id)
        h2h_rows = self.db.get_val_h2h(team1_id, team2_id)
        map_stats1 = self.db.execute(
            "SELECT * FROM val_map_stats WHERE team_id=?", (team1_id,)
        )
        map_stats2 = self.db.execute(
            "SELECT * FROM val_map_stats WHERE team_id=?", (team2_id,)
        )
        players1 = self.db.get_val_players(team1_id)
        players2 = self.db.get_val_players(team2_id)
        roster1 = self.db.execute(
            "SELECT * FROM val_roster_changes WHERE "
            "(from_team_id=? OR to_team_id=?) "
            "AND change_date >= date('now', '-30 days')",
            (team1_id, team1_id),
        )
        roster2 = self.db.execute(
            "SELECT * FROM val_roster_changes WHERE "
            "(from_team_id=? OR to_team_id=?) "
            "AND change_date >= date('now', '-30 days')",
            (team2_id, team2_id),
        )

        features = build_val_features(
            match=match,
            h2h=h2h_rows,
            form1=form1, form2=form2,
            map_stats1=map_stats1, map_stats2=map_stats2,
            players1=players1, players2=players2,
            roster_changes1=roster1, roster_changes2=roster2,
            team1=team1, team2=team2,
        )
        result = self.model.predict(features)
        return self._build_signal(match, team1_name, team2_name, result, "val")

    # ------------------------------------------------------------------
    # Build Signal from prediction result
    # ------------------------------------------------------------------

    def _build_signal(
        self,
        match: dict,
        team1_name: str,
        team2_name: str,
        result,
        game: str,
    ) -> Optional[Signal]:
        """
        Given a prediction result and match info, construct a Signal.

        In paper-trading mode we synthesize a Kalshi market price from the
        model's own probability estimate plus a small random spread to
        simulate the market not being perfectly calibrated. In live mode
        this would be the actual Kalshi market price.
        """
        # Synthetic YES price (cents) — in live mode, pull from Kalshi API
        # We simulate the market as being slightly mis-priced relative to our model
        # by assuming the market price tracks the model probability within ±8 cents
        p1 = result.p_team1

        # Simulate market: assume market is ~70% as accurate as our model
        # (i.e., it's mostly driven by casual traders with name-recognition bias)
        # For a real integration, replace market_yes_price with Kalshi API price
        import random
        random.seed(hash(match.get("match_id", "") + team1_name))
        noise = random.uniform(-0.08, 0.08)
        market_p = max(0.05, min(0.95, p1 + noise))
        yes_price = round(market_p * 100, 1)
        no_price = round(100.0 - yes_price, 1)

        edge_yes = edge(p1, yes_price)
        edge_no = edge(result.p_team2, no_price)

        # Only generate signal if edge exceeds minimum threshold
        if max(abs(edge_yes), abs(edge_no)) < MIN_EDGE:
            side = "PASS"
            k = 0.0
        else:
            side, k = best_side(
                p1, yes_price,
                fraction=KELLY_FRACTION,
                max_fraction=MAX_POSITION_FRACTION,
            )

        market_id = f"botd_{game}_{match.get('match_id', 'unknown')}"

        return Signal(
            market_id=market_id,
            game=game,
            team_a=team1_name,
            team_b=team2_name,
            p_a=p1,
            p_b=result.p_team2,
            market_yes_price=yes_price,
            market_no_price=no_price,
            edge_yes=round(edge_yes, 4),
            edge_no=round(edge_no, 4),
            kelly_yes=best_side(p1, yes_price, KELLY_FRACTION, MAX_POSITION_FRACTION)[1]
            if side == "YES"
            else 0.0,
            kelly_no=best_side(p1, yes_price, KELLY_FRACTION, MAX_POSITION_FRACTION)[1]
            if side == "NO"
            else 0.0,
            recommended_side=side,
            recommended_kelly=k,
            confidence=result.confidence,
            match_format=match.get("match_format", "Bo3"),
            tournament=match.get("tournament", ""),
            tournament_tier=match.get("tournament_tier", "C"),
            match_datetime=match.get("match_datetime", ""),
            reasoning=result.reasoning,
            signal_components=result.features.components if result.features else {},
        )

    # ------------------------------------------------------------------
    # Team ID resolution
    # ------------------------------------------------------------------

    def _resolve_team_id(self, team_name: str, game: str) -> str:
        """
        Try to find the team's internal ID from our database by name match.
        Returns a synthetic ID if not found.
        """
        table = "cs2_teams" if game == "cs2" else "val_teams"
        rows = self.db.execute(
            f"SELECT team_id FROM {table} WHERE name LIKE ? LIMIT 1",
            (f"%{team_name}%",),
        )
        if rows:
            return rows[0]["team_id"]

        # Return synthetic ID for teams not yet in DB
        import hashlib
        prefix = "hltv_" if game == "cs2" else "vlr_"
        return prefix + hashlib.md5(team_name.lower().encode()).hexdigest()[:8]

    # ------------------------------------------------------------------
    # Calibration
    # ------------------------------------------------------------------

    def calibrate(self, lookback_days: int = 90) -> dict:
        """
        Compare past predictions to outcomes and compute calibration metrics.
        Requires settled trade history in the paper trader.

        Returns: brier_score, accuracy, n_predictions
        """
        # Pull closed positions for Bot D
        rows = self.db.execute(
            "SELECT p_a, market_yes_price, side, pnl "
            "FROM trades_positions "
            "WHERE bot_id='botd' AND status='closed' "
            "AND game IN ('cs2', 'val') "
            "LIMIT 500"
        )

        if not rows:
            return {"brier_score": None, "accuracy": None, "n_predictions": 0}

        brier_sum = 0.0
        correct = 0
        for row in rows:
            p = row.get("p_a") or 0.5
            won = (row.get("pnl") or 0.0) > 0
            brier_sum += (p - int(won)) ** 2
            correct += int(won == (p > 0.5))

        n = len(rows)
        return {
            "brier_score": round(brier_sum / n, 4),
            "accuracy": round(correct / n, 4),
            "n_predictions": n,
        }
