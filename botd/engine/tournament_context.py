"""
Tournament context engine for Bot D.

Classifies every upcoming esports match into one or more bracket-context
states and computes the corresponding ELO / probability adjustments.

Four context factors
--------------------
1. Must-win    One or both teams are eliminated if they lose.
               Historical data shows elevated performance → +ELO_ADJ_MUST_WIN.

2. Clinched    A team has already secured its bracket position; this result
               has no impact on their tournament outcome.  Teams routinely
               rest starters, run experimental drafts, or play at reduced
               intensity.  This is the most reliable edge in esports markets
               because Kalshi participants rarely track bracket state.
               Effect: ELO_ADJ_CLINCHED (negative).

3. Playoff     Playoff bracket matches have lower upset rates than group
               stage.  Stronger preparation, longer series, players under
               maximum pressure → compress the format multiplier boost by
               PLAYOFF_FORMAT_MULT_BOOST.

4. Rematch     Two teams met in the same tournament within 7 days.  The
               losing team has had time to review VODs and develop counter-
               strategies.  Results become less predictable → reduce the
               ELO gap by REMATCH_ELO_GAP_REDUCTION.

Bracket state sources (priority order)
---------------------------------------
1. tournament_state table  — refreshed every TOURNAMENT_STATE_REFRESH_MINUTES
2. liq_brackets table      — populated by LiquipediaScraper.sync_bracket_state()
3. Keyword inference        — from match.tournament / block name strings
4. Default                 — 'playing' (no adjustment)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Optional

from botd.config import (
    DB_PATH,
    ELO_ADJ_MUST_WIN,
    ELO_ADJ_CLINCHED,
    PLAYOFF_FORMAT_MULT_BOOST,
    REMATCH_ELO_GAP_REDUCTION,
    TOURNAMENT_STATE_REFRESH_MINUTES,
)
from botd.storage.db import BotDStorage

if TYPE_CHECKING:
    # Avoid circular import at runtime; AdjustmentFactor is imported locally
    # inside get_elo_adjustments() where signals.py is already in sys.modules.
    pass

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Stage-keyword classification tables
# ---------------------------------------------------------------------------

# These keywords in a stage/tournament name indicate an elimination round
# where losing = out of the tournament.
_MUST_WIN_KEYWORDS: list[str] = [
    "lower bracket", "losers bracket", "losers'", "elimination round",
    "decider", "must win", "last chance", "survival match",
    "relegation", "knockdown", "do-or-die", "consolidation",
    "seeding match (loser out)",
]

# Keywords that indicate a team has clinched advancement already
_CLINCHED_KEYWORDS: list[str] = [
    "clinched", "already qualified", "guaranteed playoff",
    "sealed bracket position", "locked in",
]

# Keywords that identify a match as a playoff-stage contest
_PLAYOFF_KEYWORDS: list[str] = [
    "playoff", "quarterfinal", "quarter-final", "quarter final",
    "semifinal", "semi-final", "semi final",
    "grand final", "grand-final", "upper final",
    "bracket stage", "knockout stage", "knockout",
    "single elimination", "double elimination",
    "upper bracket final", "lower bracket final",
]

# Keywords that definitively identify a group/regular-season stage
_GROUP_KEYWORDS: list[str] = [
    "group stage", "group a", "group b", "group c", "group d",
    "group e", "group f", "swiss stage", "round robin",
    "regular season", "league stage", "play-in",
]

# Routing: game → match history table
_MATCH_TABLES: dict[str, str] = {
    "cs2":   "cs2_matches",
    "val":   "val_matches",
    "dota2": "dota2_matches",
    "lol":   "lol_matches",
}


# ---------------------------------------------------------------------------
# MatchContext dataclass
# ---------------------------------------------------------------------------

@dataclass
class MatchContext:
    """
    Full tournament/bracket context for one upcoming match.
    Produced by TournamentContextEngine.get_match_context().
    """
    game: str
    tournament: str
    stage: str              # 'group' | 'playoffs' | 'grand_final' | 'qualifier' | 'unknown'
    is_playoff: bool
    t1_status: str          # 'must_win' | 'clinched' | 'eliminated' | 'playing'
    t2_status: str
    is_rematch: bool
    prior_match_winner_id: Optional[str]
    playoff_mult_boost: float   # delta added to FORMAT_PROB_MULTIPLIERS value
    source: str             # 'tournament_state' | 'liq_brackets' | 'inferred' | 'default'


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------

class TournamentContextEngine:
    """
    Computes tournament bracket context and ELO adjustments for upcoming matches.

    Instantiated once inside EloSignalEngine and called per match during a
    signal-generation cycle.  All persistence is via BotDStorage (SQLite).
    """

    def __init__(self, db_path: str = DB_PATH):
        self.db = BotDStorage(db_path)
        self._last_source: str = "default"

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def get_match_context(
        self,
        match: dict,
        t1_id: str,
        t2_id: str,
        t1_name: str,
        t2_name: str,
        game: str,
    ) -> MatchContext:
        """
        Return the full bracket context for this match.
        Checks tournament_state → liq_brackets → keyword inference.
        """
        tournament = (match.get("tournament") or "").strip()
        # block_name is sometimes stored separately in match dicts from
        # Liquipedia / LoL scraper (e.g. "Quarterfinals")
        block_name = (match.get("block_name") or "").strip()
        stage_hint = block_name or tournament

        # 1. Classify stage from tournament / block name
        stage = _classify_stage(tournament, stage_hint)
        is_playoff = stage in ("playoffs", "grand_final")
        playoff_mult_boost = PLAYOFF_FORMAT_MULT_BOOST if is_playoff else 0.0

        # 2. Determine per-team bracket status
        t1_status = self._team_status(t1_name, t1_id, tournament, game, stage_hint)
        t2_status = self._team_status(t2_name, t2_id, tournament, game, stage_hint)
        source = self._last_source

        # 3. Detect same-tournament rematch within 7 days
        is_rematch, prior_winner_id = self._detect_rematch(
            t1_id, t2_id, tournament, game
        )

        return MatchContext(
            game=game,
            tournament=tournament,
            stage=stage,
            is_playoff=is_playoff,
            t1_status=t1_status,
            t2_status=t2_status,
            is_rematch=is_rematch,
            prior_match_winner_id=prior_winner_id,
            playoff_mult_boost=playoff_mult_boost,
            source=source,
        )

    def get_elo_adjustments(
        self,
        ctx: MatchContext,
        t1_name: str,
        t2_name: str,
        elo1_base: float,
        elo2_base: float,
    ) -> list:
        """
        Translate a MatchContext into additive ELO AdjustmentFactor objects.
        AdjustmentFactor is imported locally to avoid circular imports
        (signals.py → tournament_context.py → signals.py).
        """
        from botd.engine.signals import AdjustmentFactor  # local to avoid circ-import

        factors: list[AdjustmentFactor] = []

        # ── 1. Must-win / clinched bracket status ──────────────────────
        bracket_factor = self._bracket_status_adj(
            ctx, t1_name, t2_name, AdjustmentFactor
        )
        if bracket_factor.is_active:
            factors.append(bracket_factor)

        # ── 2. Rematch gap compression ─────────────────────────────────
        if ctx.is_rematch:
            rematch_factor = self._rematch_adj(
                t1_name, t2_name, elo1_base, elo2_base,
                ctx.prior_match_winner_id, AdjustmentFactor,
            )
            if rematch_factor.is_active:
                factors.append(rematch_factor)

        return factors

    # ------------------------------------------------------------------
    # Sync / seeding
    # ------------------------------------------------------------------

    def sync_all_active(self) -> dict:
        """
        Pull bracket state for all active tournaments from Liquipedia and
        populate tournament_state table.  Skips games/tournaments refreshed
        within TOURNAMENT_STATE_REFRESH_MINUTES.
        """
        from botd.data.liquipedia import LiquipediaScraper
        scraper = LiquipediaScraper(self.db.db_path)

        results: dict[str, int] = {}
        for game in ("cs2", "val", "dota2", "lol"):
            tournaments = self.db.execute(
                "SELECT * FROM liq_tournaments "
                "WHERE game=? AND (end_date >= date('now') OR end_date = '') "
                "ORDER BY tier ASC LIMIT 20",
                (game,),
            )
            synced = 0
            for t in tournaments:
                tourn_name = t.get("name") or ""
                if not tourn_name:
                    continue
                # Check freshness
                if self.db.is_fresh(
                    "tournament_state",
                    where=f"game='{game}' AND tournament_name LIKE '%{tourn_name[:20]}%'",
                    max_age_hours=TOURNAMENT_STATE_REFRESH_MINUTES / 60.0,
                ):
                    continue
                # Convert name to Liquipedia wiki-page style
                page = tourn_name.replace(" ", "_")
                try:
                    bracket_rows = scraper.sync_bracket_state(game, page)
                    self._ingest_bracket_rows(bracket_rows, t, game)
                    synced += len(bracket_rows)
                except Exception as exc:
                    logger.debug("bracket sync %s/%s: %s", game, page, exc)
            results[game] = synced

        logger.info("Tournament state sync complete: %s", results)
        return results

    def ingest_from_liq_brackets(self, game: str) -> int:
        """
        Seed tournament_state from existing liq_brackets rows — no network
        call.  Used at bot startup and in the tournament-state CLI command.
        """
        rows = self.db.execute(
            "SELECT b.*, t.name AS t_name, t.tier AS t_tier "
            "FROM liq_brackets b "
            "LEFT JOIN liq_tournaments t USING (tournament_id, game) "
            "WHERE b.game=?",
            (game,),
        )
        count = 0
        for r in rows:
            stage_raw = r.get("stage") or ""
            tourn_name = r.get("t_name") or r.get("tournament_id") or ""
            liq_status = r.get("status") or "playing"
            stage = _classify_stage(tourn_name, stage_raw)
            bracket_status = _liq_status_to_context(liq_status, stage_raw)

            state = {
                "tournament_name": tourn_name,
                "game": game,
                "stage": stage,
                "team_name": r.get("team_name") or "",
                "team_id": "",
                "bracket_status": bracket_status,
                "wins": 0,
                "losses": 0,
                "position": None,
                "updated_at": r.get("updated_at") or self.db.now(),
            }
            self.db.upsert_tournament_state(state)
            count += 1
        return count

    # ------------------------------------------------------------------
    # Dashboard
    # ------------------------------------------------------------------

    def tournament_dashboard(self) -> str:
        """
        Human-readable summary of all tracked tournament states.
        Shows active tournaments, team bracket statuses, and upcoming
        matches that have exploitable context (clinched or must-win).
        """
        W = 72
        lines: list[str] = [
            "═" * W,
            "  BOT D  —  TOURNAMENT CONTEXT DASHBOARD",
            f"  {datetime.utcnow().strftime('%Y-%m-%d %H:%M')} UTC",
            "═" * W,
        ]

        rows = self.db.get_active_tournament_states()

        if not rows:
            lines += [
                "  No tournament state data.",
                "  Run 'python3 main.py botd sync' then retry.",
                "═" * W,
            ]
            return "\n".join(lines)

        # Group by game → tournament
        current_game = None
        current_tourn = None
        for r in rows:
            game = r["game"]
            tourn = r["tournament_name"]

            if game != current_game:
                lines.append("")
                lines.append(f"  {game.upper()}")
                lines.append("  " + "─" * (W - 4))
                current_game = game
                current_tourn = None

            if tourn != current_tourn:
                stage = r.get("stage") or "unknown"
                lines.append(f"\n  [{stage.upper()}]  {tourn}")
                lines.append(f"  {'Team':<32} {'Status':<14} {'W':>3} {'L':>3}")
                lines.append("  " + "·" * 56)
                current_tourn = tourn

            status = r.get("bracket_status") or "playing"
            indicator = {
                "must_win":   "⚡ MUST-WIN",
                "clinched":   "✓ CLINCHED",
                "eliminated": "✗ ELIMINATED",
                "playing":    "  playing",
            }.get(status, status)
            lines.append(
                f"  {r['team_name']:<32} {indicator:<14} "
                f"{r.get('wins', 0):>3} {r.get('losses', 0):>3}"
            )

        # Exploitable matches section
        lines += [
            "",
            "═" * W,
            "  EXPLOITABLE CONTEXT",
            "  Upcoming matches with clinched or must-win teams",
            "─" * W,
        ]
        exploitable = self._find_exploitable_matches()
        if exploitable:
            for item in exploitable:
                dt_short = (item.get("match_datetime") or "")[:16]
                lines.append(
                    f"  [{item['game'].upper():<5}] {item['team1']:<22} vs "
                    f"{item['team2']:<22} {dt_short}"
                )
                lines.append(f"    Context: {item['context']}")
                lines.append(f"    {item['tournament']}")
        else:
            lines.append("  None detected in upcoming matches.")

        lines.append("═" * W)
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _team_status(
        self,
        team_name: str,
        team_id: str,
        tournament: str,
        game: str,
        stage_raw: str,
    ) -> str:
        """
        Determine bracket status for one team.  Priority order:
          1. tournament_state table
          2. liq_brackets table
          3. Keyword inference from stage_raw / tournament name
          4. Default: 'playing'
        """
        # 1. tournament_state (freshest computed status)
        ts = self.db.get_team_tournament_state(team_name, game)
        if ts:
            self._last_source = "tournament_state"
            return ts.get("bracket_status") or "playing"

        # 2. liq_brackets (raw Liquipedia data)
        lb = self.db.execute(
            "SELECT status, stage FROM liq_brackets "
            "WHERE game=? AND LOWER(team_name) LIKE ? "
            "ORDER BY updated_at DESC LIMIT 1",
            (game, f"%{team_name[:20].lower()}%"),
        )
        if lb:
            self._last_source = "liq_brackets"
            return _liq_status_to_context(
                lb[0].get("status") or "playing",
                lb[0].get("stage") or "",
            )

        # 3. Keyword inference from match/stage strings
        combined = f"{tournament} {stage_raw}".lower()
        if any(kw in combined for kw in _MUST_WIN_KEYWORDS):
            self._last_source = "inferred"
            return "must_win"
        if any(kw in combined for kw in _CLINCHED_KEYWORDS):
            self._last_source = "inferred"
            return "clinched"

        self._last_source = "default"
        return "playing"

    def _detect_rematch(
        self,
        t1_id: str,
        t2_id: str,
        tournament: str,
        game: str,
    ) -> tuple[bool, Optional[str]]:
        """
        Return (is_rematch, prior_winner_id) if the two teams met in the
        same tournament within the past 7 days.
        """
        match_table = _MATCH_TABLES.get(game, f"{game}_matches")
        cutoff = (datetime.utcnow() - timedelta(days=7)).strftime("%Y-%m-%d")

        if tournament:
            tourn_prefix = tournament[:25]
            rows = self.db.execute(
                f"SELECT winner_id FROM {match_table} "
                f"WHERE ((team1_id=? AND team2_id=?) OR (team1_id=? AND team2_id=?)) "
                f"  AND match_date >= ? AND tournament LIKE ? "
                f"ORDER BY match_date DESC LIMIT 1",
                (t1_id, t2_id, t2_id, t1_id, cutoff, f"%{tourn_prefix}%"),
            )
        else:
            rows = self.db.execute(
                f"SELECT winner_id FROM {match_table} "
                f"WHERE ((team1_id=? AND team2_id=?) OR (team1_id=? AND team2_id=?)) "
                f"  AND match_date >= ? "
                f"ORDER BY match_date DESC LIMIT 1",
                (t1_id, t2_id, t2_id, t1_id, cutoff),
            )

        if rows:
            return True, rows[0].get("winner_id")
        return False, None

    def _bracket_status_adj(
        self,
        ctx: MatchContext,
        t1_name: str,
        t2_name: str,
        AdjFactor,
    ):
        """
        Build AdjustmentFactor for must-win (+) and clinched (−) context.

        Must-win teams demonstrate measurably elevated performance.
        Clinched teams frequently rest players or experiment — the most
        reliable esports-specific edge Kalshi markets miss.
        Eliminated teams are already handled by tournament_pressure_adj.
        """
        d1, d2 = 0.0, 0.0
        parts: list[str] = []

        for status, name, slot in (
            (ctx.t1_status, t1_name, 1),
            (ctx.t2_status, t2_name, 2),
        ):
            if status == "must_win":
                delta = ELO_ADJ_MUST_WIN
                parts.append(
                    f"{name} must-win ({delta:+.0f} ELO — elevated stakes)"
                )
                if slot == 1:
                    d1 = delta
                else:
                    d2 = delta
            elif status == "clinched":
                delta = ELO_ADJ_CLINCHED
                parts.append(
                    f"{name} clinched ({delta:+.0f} ELO — reduced intensity expected)"
                )
                if slot == 1:
                    d1 = delta
                else:
                    d2 = delta

        return AdjFactor(
            name="tournament_context",
            team1_delta=d1,
            team2_delta=d2,
            reason="; ".join(parts) if parts else "No bracket context (both teams playing normally)",
        )

    def _rematch_adj(
        self,
        t1_name: str,
        t2_name: str,
        elo1: float,
        elo2: float,
        prior_winner_id: Optional[str],
        AdjFactor,
    ):
        """
        Compress the ELO gap by REMATCH_ELO_GAP_REDUCTION for same-tournament
        rematches within 7 days.

        The losing team has had time to review VODs and adapt counter-strats.
        Results are genuinely more unpredictable — compressing the gap reflects
        this reduced predictability, not that the teams are equally skilled.
        """
        gap = elo1 - elo2
        if abs(gap) < 1.0:
            return AdjFactor(
                name="rematch",
                team1_delta=0.0,
                team2_delta=0.0,
                reason="Rematch: teams ELO-even — gap compression irrelevant",
            )

        half_red = round(abs(gap) * REMATCH_ELO_GAP_REDUCTION / 2.0, 2)
        if gap > 0:    # T1 is ELO-stronger; reduce its advantage
            d1, d2 = -half_red, +half_red
        else:          # T2 is ELO-stronger
            d1, d2 = +half_red, -half_red

        new_gap = gap + d1 - d2
        return AdjFactor(
            name="rematch",
            team1_delta=d1,
            team2_delta=d2,
            reason=(
                f"Same-tournament rematch within 7 days: "
                f"ELO gap compressed {REMATCH_ELO_GAP_REDUCTION:.0%} "
                f"({gap:+.1f} → {new_gap:+.1f}) — VOD review expected"
            ),
        )

    def _ingest_bracket_rows(
        self, rows: list[dict], tournament: dict, game: str
    ) -> None:
        """Store Liquipedia bracket rows into tournament_state."""
        tourn_name = tournament.get("name") or ""
        for r in rows:
            stage_raw = r.get("stage") or ""
            liq_status = r.get("status") or "playing"
            stage = _classify_stage(tourn_name, stage_raw)
            bracket_status = _liq_status_to_context(liq_status, stage_raw)
            self.db.upsert_tournament_state({
                "tournament_name": tourn_name,
                "game": game,
                "stage": stage,
                "team_name": r.get("team_name") or "",
                "team_id": r.get("team_id") or "",
                "bracket_status": bracket_status,
                "wins": r.get("wins") or 0,
                "losses": r.get("losses") or 0,
                "position": None,
            })

    def _find_exploitable_matches(self) -> list[dict]:
        """
        Query upcoming matches where at least one team has a contextually
        exploitable status (clinched or must-win).
        """
        rows = self.db.execute(
            "SELECT m.game, m.team1, m.team2, m.match_datetime, m.tournament, "
            "  ts1.bracket_status AS t1_ctx, ts2.bracket_status AS t2_ctx "
            "FROM liq_upcoming_matches m "
            "LEFT JOIN tournament_state ts1 "
            "  ON ts1.game = m.game "
            "  AND LOWER(ts1.team_name) LIKE '%' || LOWER(SUBSTR(m.team1,1,15)) || '%' "
            "LEFT JOIN tournament_state ts2 "
            "  ON ts2.game = m.game "
            "  AND LOWER(ts2.team_name) LIKE '%' || LOWER(SUBSTR(m.team2,1,15)) || '%' "
            "WHERE m.match_datetime >= datetime('now') "
            "  AND (ts1.bracket_status IN ('must_win','clinched') "
            "       OR ts2.bracket_status IN ('must_win','clinched')) "
            "ORDER BY m.match_datetime ASC LIMIT 25"
        )
        result = []
        for row in rows:
            t1_ctx = row.get("t1_ctx") or "playing"
            t2_ctx = row.get("t2_ctx") or "playing"
            ctx_parts = []
            if t1_ctx in ("must_win", "clinched"):
                ctx_parts.append(f"{row['team1']}: {t1_ctx.replace('_', '-').upper()}")
            if t2_ctx in ("must_win", "clinched"):
                ctx_parts.append(f"{row['team2']}: {t2_ctx.replace('_', '-').upper()}")
            result.append({
                "game":           row.get("game") or "",
                "team1":          row.get("team1") or "",
                "team2":          row.get("team2") or "",
                "context":        "  |  ".join(ctx_parts),
                "match_datetime": row.get("match_datetime") or "",
                "tournament":     row.get("tournament") or "",
            })
        return result


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------

def _classify_stage(tournament_name: str, stage_raw: str) -> str:
    """
    Classify a match into one of:
      'group' | 'playoffs' | 'grand_final' | 'qualifier' | 'unknown'

    Uses keyword matching on the combined tournament name and stage string.
    """
    combined = f"{tournament_name} {stage_raw}".lower()

    if "grand final" in combined or "grand-final" in combined:
        return "grand_final"

    if any(kw in combined for kw in ("qualifier", "open bracket", "open qualifier")):
        return "qualifier"

    if any(kw in combined for kw in _GROUP_KEYWORDS):
        return "group"

    if any(kw in combined for kw in _PLAYOFF_KEYWORDS):
        return "playoffs"

    return "unknown"


def _liq_status_to_context(liq_status: str, stage_raw: str) -> str:
    """
    Map Liquipedia's three bracket statuses to the richer context vocabulary.

    Liquipedia: eliminated | qualified | playing
    Context:    eliminated | clinched  | playing | must_win
    """
    s = (liq_status or "").lower()
    if s == "eliminated":
        return "eliminated"
    if s == "qualified":
        return "clinched"
    # 'playing' — check if the stage name implies an elimination round
    if any(kw in (stage_raw or "").lower() for kw in _MUST_WIN_KEYWORDS):
        return "must_win"
    return "playing"
