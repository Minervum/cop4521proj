"""
ELO-based signal engine for Bot D esports markets.

Replaces the weighted-blend model (botd/models/probability.py) with a
two-layer ELO system (botd/engine/elo.py) augmented by five situational
adjustment factors.

Probability pipeline
====================
1. Base ELO win probability
       If map veto is known  → average of map-specific ELOs for veto'd maps
       Otherwise             → overall team ELO

2. Situational ELO deltas (all additive before re-running ELO formula)
       a. Roster change penalty   stand-in → −5 ELO,  new addition → −3 ELO
       b. Tournament pressure     team with something to play for → +8 ELO
       c. Travel fatigue          international trip within 48 h → −3 ELO
       d. Online/LAN venue adj    based on per-team venue win-rate delta → ±≤50 ELO
       e. Format multiplier       applied to the final deviation from 50%:
                Bo1 → × 0.85  (compress 15% toward 50%)
                Bo3 → × 1.00  (no change)
                Bo5 → × 1.10  (expand 10% away from 50%)

3. Edge check: |p_model − p_market| ≥ 8%  →  generate Signal

Signal output
=============
Each Signal carries:
  team_a / team_b        names
  p_a / p_b              final model probabilities
  market_yes_price       Kalshi YES price in cents
  edge_yes / edge_no     raw edge for each side
  recommended_side       YES / NO / PASS
  recommended_kelly      half-Kelly fraction of bankroll
  confidence             composite data-quality score 0–1
  match_format           Bo1 / Bo3 / Bo5
  tournament             name + tier
  match_datetime         UTC ISO
  reasoning              full human-readable breakdown (all factors)
  signal_components      structured dict for dashboard rendering
"""

from __future__ import annotations

import hashlib
import logging
import random
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional

from shared.signal_engine import BaseSignalEngine, Signal
from shared.kelly import best_side, edge as kelly_edge

from botd.config import (
    DB_PATH,
    ELO_MIN_EDGE,
    ELO_ADJ_STANDIN,
    ELO_ADJ_RECENT_ADDITION,
    ELO_ADJ_TOURNAMENT_PRESSURE,
    ELO_ADJ_TRAVEL_FATIGUE,
    ELO_VENUE_ADJ_MAX,
    FORMAT_PROB_MULTIPLIERS,
    KELLY_FRACTION,
    MAX_POSITION_FRACTION,
)
from botd.engine.elo import EloEngine
from botd.storage.db import BotDStorage

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Internal data structures
# ---------------------------------------------------------------------------

@dataclass
class AdjustmentFactor:
    """One situational ELO delta for this matchup."""
    name: str
    team1_delta: float
    team2_delta: float
    reason: str

    @property
    def is_active(self) -> bool:
        return self.team1_delta != 0.0 or self.team2_delta != 0.0


@dataclass
class EloMatchPrediction:
    """Complete prediction breakdown — produced by _compute_adjusted_prediction."""
    team1_name: str
    team2_name: str

    # Base ELO (before situational adjustments)
    team1_elo_base: float
    team2_elo_base: float
    elo_source: str               # 'overall' | 'map_weighted'
    veto_maps: list[str]
    map_breakdown: list[dict]     # per-map elo/prob if veto known

    # Adjustments
    adjustments: list[AdjustmentFactor]
    total_adj1: float
    total_adj2: float

    # Adjusted ELO
    team1_elo_adj: float
    team2_elo_adj: float

    # Probability
    p_team1_raw: float            # from adjusted ELOs, before format mult
    p_team1_final: float          # after format multiplier
    format_multiplier: float

    # Metadata
    match_type: str               # 'lan' | 'online'
    confidence: float
    reasoning: str


# ---------------------------------------------------------------------------
# Signal engine
# ---------------------------------------------------------------------------

class EloSignalEngine(BaseSignalEngine):
    """
    ELO-based signal engine.  Implements BaseSignalEngine so it can be
    dropped into the orchestrator alongside Bot B and Bot C.

    Signal generation flow:
        get_signals()
          └─ _signals_for_game("cs2")
          └─ _signals_for_game("val")
               └─ _evaluate_match(match)
                    └─ _compute_adjusted_prediction(...)
                    └─ _build_signal(...)
    """

    def __init__(self, db_path: str = DB_PATH):
        self.db = BotDStorage(db_path)
        self.elo = EloEngine(db_path)

    @property
    def bot_id(self) -> str:
        return "botd"

    @property
    def games(self) -> list[str]:
        return ["cs2", "val"]

    # ------------------------------------------------------------------
    # BaseSignalEngine interface
    # ------------------------------------------------------------------

    def get_signals(self, market_ids: Optional[list[str]] = None) -> list[Signal]:
        """
        Sync ELO ratings, then generate signals for all upcoming matches.
        """
        # Refresh ELO ratings (cached — no-op if already fresh within 6 h)
        for game in self.games:
            self.elo.sync(game)

        signals: list[Signal] = []
        for game in self.games:
            signals.extend(self._signals_for_game(game))

        if market_ids:
            signals = [s for s in signals if s.market_id in market_ids]

        logger.info(
            "EloSignalEngine: %d signals, %d with edge (≥%.0f%%)",
            len(signals),
            sum(1 for s in signals if s.has_edge),
            ELO_MIN_EDGE * 100,
        )
        return signals

    def calibrate(self, lookback_days: int = 90) -> dict:
        """Brier score and accuracy against settled paper-trade history."""
        rows = self.db.execute(
            "SELECT p_a, pnl FROM trades_positions "
            "WHERE bot_id='botd' AND status='closed' LIMIT 500"
        )
        if not rows:
            return {"brier_score": None, "accuracy": None, "n_predictions": 0}

        brier, correct = 0.0, 0
        for r in rows:
            p = float(r.get("p_a") or 0.5)
            won = (float(r.get("pnl") or 0.0)) > 0
            brier += (p - int(won)) ** 2
            correct += int(won == (p > 0.5))

        n = len(rows)
        return {
            "brier_score":   round(brier / n, 4),
            "accuracy":      round(correct / n, 4),
            "n_predictions": n,
        }

    # ------------------------------------------------------------------
    # Per-game signal loop
    # ------------------------------------------------------------------

    def _signals_for_game(self, game: str) -> list[Signal]:
        # Use cached data if available; only fetch from network when cache is empty
        cached = self.db.get_upcoming_matches(game, days=7)
        if cached:
            upcoming = cached
        else:
            from botd.data.liquipedia import LiquipediaScraper
            liq = LiquipediaScraper(self.db.db_path)
            upcoming = liq.sync_upcoming_matches(game, days=7)

        if not upcoming:
            logger.debug("No upcoming %s matches found", game)
            return []

        signals = []
        for match in upcoming:
            try:
                sig = self._evaluate_match(match, game)
                if sig:
                    signals.append(sig)
            except Exception as exc:
                logger.error(
                    "%s match %s vs %s error: %s",
                    game, match.get("team1"), match.get("team2"), exc,
                )
        return signals

    def _evaluate_match(self, match: dict, game: str) -> Optional[Signal]:
        t1_name = (match.get("team1") or "").strip()
        t2_name = (match.get("team2") or "").strip()
        if not t1_name or not t2_name:
            return None

        t1_id = match.get("team1_id") or self._resolve_team_id(t1_name, game)
        t2_id = match.get("team2_id") or self._resolve_team_id(t2_name, game)

        pred = self._compute_adjusted_prediction(
            match, t1_id, t2_id, t1_name, t2_name, game
        )
        return self._build_signal(match, t1_name, t2_name, pred, game)

    # ------------------------------------------------------------------
    # Core prediction: adjusted ELO → final probability
    # ------------------------------------------------------------------

    def _compute_adjusted_prediction(
        self,
        match: dict,
        t1_id: str,
        t2_id: str,
        t1_name: str,
        t2_name: str,
        game: str,
    ) -> EloMatchPrediction:
        """
        Full five-step probability pipeline with complete audit trail.
        """
        match_format = match.get("match_format") or "Bo3"
        veto_maps: list[str] = match.get("veto_maps") or []

        # ── Step 1: Base ELO ─────────────────────────────────────────
        base = self.elo.predict_match(
            t1_id, t2_id, game, match_format, veto_maps or None
        )
        elo1 = base["team1_elo"]
        elo2 = base["team2_elo"]

        # ── Step 2: Situational adjustments ──────────────────────────
        match_type = self._detect_match_type(match, game)

        adjustments: list[AdjustmentFactor] = []
        adjustments.extend(self._roster_change_adj(t1_id, t2_id, game))
        adjustments.append(self._tournament_pressure_adj(t1_name, t2_name, game))
        adjustments.extend(
            self._travel_fatigue_adj(
                t1_id, t2_id,
                match.get("tournament", ""),
                match.get("match_datetime", ""),
                game,
            )
        )
        adjustments.append(self._venue_adj(t1_id, t2_id, game, match_type))

        # ── Step 3: Apply adjustments ────────────────────────────────
        total_adj1 = sum(a.team1_delta for a in adjustments)
        total_adj2 = sum(a.team2_delta for a in adjustments)
        elo1_adj = elo1 + total_adj1
        elo2_adj = elo2 + total_adj2

        # ── Step 4: Win probability from adjusted ELOs ───────────────
        p_raw = self.elo.expected_score(elo1_adj, elo2_adj)

        # ── Step 5: Format multiplier ─────────────────────────────────
        fmt_mult = FORMAT_PROB_MULTIPLIERS.get(match_format, 1.0)
        p_final = max(0.05, min(0.95, 0.5 + (p_raw - 0.5) * fmt_mult))

        confidence = self._compute_confidence(
            t1_id, t2_id, game, elo1, elo2,
            base.get("elo_source", "overall"), veto_maps,
        )

        reasoning = _build_reasoning(
            t1_name, t2_name, game, match_format,
            match.get("tournament", ""), match.get("tournament_tier", "C"),
            elo1, elo2, total_adj1, total_adj2, elo1_adj, elo2_adj,
            p_raw, p_final, fmt_mult, adjustments, base, veto_maps, match_type,
        )

        return EloMatchPrediction(
            team1_name=t1_name,
            team2_name=t2_name,
            team1_elo_base=elo1,
            team2_elo_base=elo2,
            elo_source=base.get("elo_source", "overall"),
            veto_maps=veto_maps,
            map_breakdown=base.get("map_breakdown", []),
            adjustments=adjustments,
            total_adj1=round(total_adj1, 2),
            total_adj2=round(total_adj2, 2),
            team1_elo_adj=round(elo1_adj, 2),
            team2_elo_adj=round(elo2_adj, 2),
            p_team1_raw=round(p_raw, 4),
            p_team1_final=round(p_final, 4),
            format_multiplier=fmt_mult,
            match_type=match_type,
            confidence=round(confidence, 3),
            reasoning=reasoning,
        )

    # ------------------------------------------------------------------
    # Adjustment factor implementations
    # ------------------------------------------------------------------

    def _roster_change_adj(
        self, t1_id: str, t2_id: str, game: str
    ) -> list[AdjustmentFactor]:
        """
        Penalise teams that have added a stand-in or very recently signed
        a player who is not yet integrated into the team's system.

        Lookback window: 14 days.
        Stand-in / loan player:   −5 ELO
        Recently signed player:   −3 ELO
        """
        factors: list[AdjustmentFactor] = []
        roster_table = "cs2_roster_changes" if game == "cs2" else "val_roster_changes"
        cutoff = (datetime.utcnow() - timedelta(days=14)).strftime("%Y-%m-%d")

        for team_id, slot in ((t1_id, 1), (t2_id, 2)):
            changes = self.db.execute(
                f"SELECT * FROM {roster_table} "
                f"WHERE (from_team_id=? OR to_team_id=?) AND change_date >= ?",
                (team_id, team_id, cutoff),
            )
            penalty = 0.0
            parts: list[str] = []

            for c in changes:
                ct = c.get("change_type", "")
                pname = c.get("player_name") or "?"
                if ct in ("loan", "inactive"):
                    penalty += ELO_ADJ_STANDIN
                    parts.append(f"{pname} stand-in ({ELO_ADJ_STANDIN:+.0f})")
                elif ct == "join":
                    penalty += ELO_ADJ_RECENT_ADDITION
                    parts.append(f"{pname} new arrival ({ELO_ADJ_RECENT_ADDITION:+.0f})")

            if penalty != 0.0:
                d1 = penalty if slot == 1 else 0.0
                d2 = penalty if slot == 2 else 0.0
                factors.append(AdjustmentFactor(
                    name="roster_change",
                    team1_delta=d1,
                    team2_delta=d2,
                    reason=f"Team{slot}: " + "; ".join(parts),
                ))

        return factors

    def _tournament_pressure_adj(
        self, t1_name: str, t2_name: str, game: str
    ) -> AdjustmentFactor:
        """
        If one team is already eliminated and the other is still alive,
        the surviving team gains +8 ELO (they are playing with stakes).

        Reads liq_brackets populated by LiquipediaScraper.sync_bracket_state().
        Falls back gracefully to 'playing' when no bracket data exists.
        """
        t1_status = self._bracket_status(t1_name, game)
        t2_status = self._bracket_status(t2_name, game)

        d1, d2 = 0.0, 0.0
        parts: list[str] = []

        if t1_status == "eliminated" and t2_status != "eliminated":
            d2 = ELO_ADJ_TOURNAMENT_PRESSURE
            parts.append(
                f"{t1_name} eliminated → {t2_name} has stakes "
                f"({ELO_ADJ_TOURNAMENT_PRESSURE:+.0f} ELO)"
            )
        elif t2_status == "eliminated" and t1_status != "eliminated":
            d1 = ELO_ADJ_TOURNAMENT_PRESSURE
            parts.append(
                f"{t2_name} eliminated → {t1_name} has stakes "
                f"({ELO_ADJ_TOURNAMENT_PRESSURE:+.0f} ELO)"
            )

        return AdjustmentFactor(
            name="tournament_pressure",
            team1_delta=d1,
            team2_delta=d2,
            reason="; ".join(parts) if parts else "No bracket pressure detected",
        )

    def _bracket_status(self, team_name: str, game: str) -> str:
        rows = self.db.execute(
            "SELECT status FROM liq_brackets "
            "WHERE game=? AND LOWER(team_name) LIKE ? "
            "ORDER BY updated_at DESC LIMIT 1",
            (game, f"%{team_name[:20].lower()}%"),
        )
        return rows[0]["status"] if rows else "playing"

    def _travel_fatigue_adj(
        self,
        t1_id: str,
        t2_id: str,
        tournament: str,
        match_datetime: str,
        game: str,
    ) -> list[AdjustmentFactor]:
        """
        Detect if a team played in a different physical location within the
        48 hours preceding this match.  Requires tournament location data
        from liq_tournaments.

        Penalty: −3 ELO per team that travelled internationally.

        Detection logic:
          1. Look up current match's tournament location.
          2. Query the team's most recent match within [match_dt − 48h, match_dt).
          3. If that match's tournament is in a different (non-empty, non-online)
             location → apply penalty.
        """
        factors: list[AdjustmentFactor] = []

        try:
            match_dt = datetime.fromisoformat(match_datetime.replace(" ", "T"))
        except (ValueError, AttributeError):
            return factors

        current_loc = self._tournament_location(tournament, game)
        if not current_loc or "online" in current_loc.lower():
            return factors  # no travel for online matches

        window_start = (match_dt - timedelta(hours=48)).strftime("%Y-%m-%d")
        window_end   = match_dt.strftime("%Y-%m-%d")
        match_table  = "cs2_matches" if game == "cs2" else "val_matches"

        for team_id, slot in ((t1_id, 1), (t2_id, 2)):
            prior = self.db.execute(
                f"SELECT tournament, match_date FROM {match_table} "
                f"WHERE (team1_id=? OR team2_id=?) "
                f"  AND match_date >= ? AND match_date < ? "
                f"ORDER BY match_date DESC LIMIT 3",
                (team_id, team_id, window_start, window_end),
            )
            for pm in prior:
                prior_loc = self._tournament_location(pm["tournament"], game)
                if (
                    prior_loc
                    and "online" not in prior_loc.lower()
                    and prior_loc.strip().lower() != current_loc.strip().lower()
                ):
                    d1 = ELO_ADJ_TRAVEL_FATIGUE if slot == 1 else 0.0
                    d2 = ELO_ADJ_TRAVEL_FATIGUE if slot == 2 else 0.0
                    factors.append(AdjustmentFactor(
                        name="travel_fatigue",
                        team1_delta=d1,
                        team2_delta=d2,
                        reason=(
                            f"Team{slot}: {prior_loc} → {current_loc} within 48 h "
                            f"({ELO_ADJ_TRAVEL_FATIGUE:+.0f} ELO)"
                        ),
                    ))
                    break  # one penalty per team

        return factors

    def _tournament_location(self, tournament_name: str, game: str) -> str:
        if not tournament_name:
            return ""
        rows = self.db.execute(
            "SELECT location FROM liq_tournaments "
            "WHERE game=? AND name LIKE ? LIMIT 1",
            (game, f"%{tournament_name[:25]}%"),
        )
        return (rows[0].get("location") or "") if rows else ""

    def _venue_adj(
        self, t1_id: str, t2_id: str, game: str, match_type: str
    ) -> AdjustmentFactor:
        """
        Reward (or penalise) teams based on their historical performance
        gap between online and LAN settings.

        For a LAN match:   delta = (LAN win rate − online win rate) × 700
        For an online match: delta = (online win rate − LAN win rate) × 700
        Capped at ±ELO_VENUE_ADJ_MAX (default ±50 ELO).

        Requires venue stats computed by EloEngine.compute_venue_stats().
        """
        parts: list[str] = []

        def team_delta(team_id: str) -> float:
            lan    = self.elo.get_venue_win_rate(team_id, game, "lan")
            online = self.elo.get_venue_win_rate(team_id, game, "online")
            if lan is None or online is None:
                return 0.0
            diff = (lan - online) if match_type == "lan" else (online - lan)
            return max(-ELO_VENUE_ADJ_MAX, min(ELO_VENUE_ADJ_MAX, diff * 700.0))

        d1 = round(team_delta(t1_id), 2)
        d2 = round(team_delta(t2_id), 2)
        if d1 != 0.0:
            parts.append(f"Team1 venue ({match_type}): {d1:+.1f} ELO")
        if d2 != 0.0:
            parts.append(f"Team2 venue ({match_type}): {d2:+.1f} ELO")

        return AdjustmentFactor(
            name="venue_type",
            team1_delta=d1,
            team2_delta=d2,
            reason="; ".join(parts) or f"No venue data ({match_type})",
        )

    def _detect_match_type(self, match: dict, game: str) -> str:
        loc = self._tournament_location(match.get("tournament", ""), game)
        return "online" if (not loc or "online" in loc.lower()) else "lan"

    # ------------------------------------------------------------------
    # Confidence scoring
    # ------------------------------------------------------------------

    def _compute_confidence(
        self,
        t1_id: str,
        t2_id: str,
        game: str,
        elo1: float,
        elo2: float,
        elo_source: str,
        veto_maps: list,
    ) -> float:
        """
        Composite confidence score 0–1 based on data completeness.

        Score components:
          Each team has ≥ 10 ELO matches      → +0.15 each  (max +0.30)
          Map-weighted ELO used (veto known)  → +0.20
          Each team has venue stats           → +0.075 each (max +0.15)
          |ELO diff| > 100 (clear favourite)  → +0.15
          Each team had a match in last 14 d  → +0.10 each  (max +0.20)
        """
        score = 0.0
        match_table = "cs2_matches" if game == "cs2" else "val_matches"
        recent_cutoff = (datetime.utcnow() - timedelta(days=14)).strftime("%Y-%m-%d")

        for tid in (t1_id, t2_id):
            # ELO match depth
            row = self.db.execute(
                "SELECT matches FROM elo_team_ratings "
                "WHERE team_id=? AND game=? AND map_name='overall'",
                (tid, game),
            )
            if row and (row[0].get("matches") or 0) >= 10:
                score += 0.15

            # Venue stats available
            if self.elo.get_venue_win_rate(tid, game, "lan") is not None:
                score += 0.075

            # Recently active
            cnt = self.db.scalar(
                f"SELECT COUNT(*) FROM {match_table} "
                f"WHERE (team1_id=? OR team2_id=?) AND match_date >= ?",
                (tid, tid, recent_cutoff),
            ) or 0
            if cnt > 0:
                score += 0.10

        if elo_source == "map_weighted" and veto_maps:
            score += 0.20

        if abs(elo1 - elo2) > 100:
            score += 0.15

        return min(1.0, score)

    # ------------------------------------------------------------------
    # Signal construction
    # ------------------------------------------------------------------

    def _build_signal(
        self,
        match: dict,
        t1_name: str,
        t2_name: str,
        pred: EloMatchPrediction,
        game: str,
    ) -> Optional[Signal]:
        p1 = pred.p_team1_final

        # In production: fetch from Kalshi API.
        # In paper mode: synthesise a plausible market price by adding
        # controlled noise (±8 cents) — simulating a market that is
        # roughly 70–80 % as well-calibrated as our model.
        rng = random.Random(hashlib.md5(
            f"{match.get('match_id','')}_{t1_name}".encode()
        ).digest()[:8])
        noise = rng.uniform(-0.08, 0.08)
        yes_price = round(max(5.0, min(95.0, (p1 + noise) * 100.0)), 1)
        no_price  = round(100.0 - yes_price, 1)

        edge_yes = kelly_edge(p1,       yes_price)
        edge_no  = kelly_edge(1.0 - p1, no_price)

        if max(abs(edge_yes), abs(edge_no)) < ELO_MIN_EDGE:
            side, k = "PASS", 0.0
        else:
            side, k = best_side(p1, yes_price, KELLY_FRACTION, MAX_POSITION_FRACTION)

        # Kelly fractions for each side explicitly
        k_yes = best_side(p1, yes_price, KELLY_FRACTION, MAX_POSITION_FRACTION)[1] \
                if side == "YES" else 0.0
        k_no  = best_side(p1, yes_price, KELLY_FRACTION, MAX_POSITION_FRACTION)[1] \
                if side == "NO"  else 0.0

        # Structured component dict for the dashboard
        components: dict = {
            "elo_base_t1":     pred.team1_elo_base,
            "elo_base_t2":     pred.team2_elo_base,
            "elo_adj_t1":      pred.team1_elo_adj,
            "elo_adj_t2":      pred.team2_elo_adj,
            "elo_diff_adj":    round(pred.team1_elo_adj - pred.team2_elo_adj, 2),
            "elo_source":      pred.elo_source,
            "match_type":      pred.match_type,
            "format_mult":     pred.format_multiplier,
            "p_raw":           pred.p_team1_raw,
            "p_final":         pred.p_team1_final,
            "total_adj_t1":    pred.total_adj1,
            "total_adj_t2":    pred.total_adj2,
            "confidence":      pred.confidence,
        }
        for adj in pred.adjustments:
            if adj.is_active:
                components[adj.name] = {
                    "t1_delta": adj.team1_delta,
                    "t2_delta": adj.team2_delta,
                    "reason":   adj.reason,
                }
        if pred.veto_maps:
            components["veto_maps"]     = pred.veto_maps
            components["map_breakdown"] = pred.map_breakdown

        return Signal(
            market_id=f"botd_{game}_{match.get('match_id','unknown')}",
            game=game,
            team_a=t1_name,
            team_b=t2_name,
            p_a=p1,
            p_b=round(1.0 - p1, 4),
            market_yes_price=yes_price,
            market_no_price=no_price,
            edge_yes=round(edge_yes, 4),
            edge_no=round(edge_no, 4),
            kelly_yes=k_yes,
            kelly_no=k_no,
            recommended_side=side,
            recommended_kelly=k,
            confidence=pred.confidence,
            match_format=match.get("match_format", "Bo3"),
            tournament=match.get("tournament", ""),
            tournament_tier=match.get("tournament_tier", "C"),
            match_datetime=match.get("match_datetime", ""),
            reasoning=pred.reasoning,
            signal_components=components,
        )

    # ------------------------------------------------------------------
    # Utilities
    # ------------------------------------------------------------------

    def _resolve_team_id(self, team_name: str, game: str) -> str:
        table = "cs2_teams" if game == "cs2" else "val_teams"
        rows = self.db.execute(
            f"SELECT team_id FROM {table} WHERE LOWER(name) LIKE ? LIMIT 1",
            (f"%{team_name[:15].lower()}%",),
        )
        if rows:
            return rows[0]["team_id"]
        prefix = "hltv_" if game == "cs2" else "vlr_"
        return prefix + hashlib.md5(team_name.lower().encode()).hexdigest()[:8]


# ---------------------------------------------------------------------------
# Reasoning builder (module-level for readability)
# ---------------------------------------------------------------------------

def _build_reasoning(
    t1_name: str, t2_name: str, game: str, match_format: str,
    tournament: str, tier: str,
    elo1: float, elo2: float,
    total_adj1: float, total_adj2: float,
    elo1_adj: float, elo2_adj: float,
    p_raw: float, p_final: float, fmt_mult: float,
    adjustments: list["AdjustmentFactor"],
    base_result: dict,
    veto_maps: list[str],
    match_type: str,
) -> str:
    W = 60
    sep  = "─" * W
    dsep = "═" * W

    lines = [
        dsep,
        f"  {game.upper()} | {t1_name} vs {t2_name}",
        f"  {match_format}  •  Tier {tier}  •  {match_type.upper()}",
        f"  {tournament}",
        sep,
        "",
        "  BASE ELO RATINGS",
        f"  {'Team':<30} {'ELO':>7}",
        f"  {t1_name:<30} {elo1:>7.1f}",
        f"  {t2_name:<30} {elo2:>7.1f}",
        f"  ELO gap (T1 − T2): {elo1 - elo2:+.1f}",
        f"  Source: {base_result.get('elo_source', 'overall')}",
    ]

    if veto_maps:
        lines.append(f"  Veto maps: {', '.join(veto_maps)}")
        for m in base_result.get("map_breakdown", []):
            lines.append(
                f"    {m['map']:<12}  T1: {m['elo1']:6.1f}  "
                f"T2: {m['elo2']:6.1f}  P(T1)={m['prob_team1']:.3f}"
            )

    lines += ["", "  SITUATIONAL ADJUSTMENTS"]
    active = [a for a in adjustments if a.is_active]
    if active:
        lines.append(
            f"  {'Factor':<20} {'T1 Δ':>7}  {'T2 Δ':>7}"
        )
        lines.append("  " + "·" * 38)
        for adj in active:
            lines.append(
                f"  {adj.name:<20} {adj.team1_delta:>+7.1f}  {adj.team2_delta:>+7.1f}"
            )
            lines.append(f"    ↳ {adj.reason}")
    else:
        lines.append("  (no active situational factors)")

    lines += [
        "",
        "  ADJUSTED ELO",
        f"  {t1_name:<30} {elo1_adj:>7.1f}  (base {elo1:.1f}  adj {total_adj1:+.1f})",
        f"  {t2_name:<30} {elo2_adj:>7.1f}  (base {elo2:.1f}  adj {total_adj2:+.1f})",
        "",
        "  PROBABILITY",
        f"  P(T1 wins) from adjusted ELO : {p_raw:.4f}  ({p_raw:.1%})",
        f"  Format multiplier [{match_format}]       : ×{fmt_mult:.2f}",
        f"  P(T1 wins) FINAL             : {p_final:.4f}  ({p_final:.1%})",
        dsep,
    ]
    return "\n".join(lines)
