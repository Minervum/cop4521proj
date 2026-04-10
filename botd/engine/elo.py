"""
Two-layer ELO rating system for esports teams.

Layer 1 — Overall ELO
=====================
Standard Elo updated after every series result by replaying the full match
history in chronological order.  K-factor is format-dependent:

    Bo1 → K = 32   (single map, coin-flip territory)
    Bo3 → K = 24   (standard professional match)
    Bo5 → K = 16   (finals/playoffs, skill stabilises outcome)

Recency decay: K_effective = K_base × (1 − 0.15)^months_ago
A result from 3 months ago carries (0.85³ ≈ 0.61×) its original weight.
A result from 6 months ago carries (0.85⁶ ≈ 0.38×) its original weight.

Layer 2 — Map ELO
==================
Each team has a separate implicit ELO per map, derived from their aggregate
map win rate relative to their overall ELO:

    map_elo = overall_elo + (map_win_rate − 0.50) × 700

Scale chosen so 0.60 map win rate → +70 ELO, consistent with the standard
ELO formula (expected_score = 0.60 when rating diff = 70.4).

Only applied when both teams have ≥ ELO_MIN_MATCHES_FOR_MAP map results.

Venue stats (online vs LAN)
============================
Tracked separately per team to support the venue-type signal adjustment
in engine/signals.py.  A tournament is 'online' if its Liquipedia location
field contains the word "online" or is blank.

Storage
=======
All data stored in three tables (initialised by EloEngine.__init__):
  elo_team_ratings   — current cached ELO per (team, game, map)
  elo_match_log      — per-match rating deltas (audit trail)
  elo_venue_stats    — per-team LAN/online win rates
"""

from __future__ import annotations

import logging
import math
from datetime import datetime, timedelta
from typing import Optional

from botd.storage.db import BotDStorage
from botd.config import (
    DB_PATH,
    CS2_MAP_POOL,
    VAL_MAP_POOL,
    DOTA2_ROLE_POOL,
    LOL_POSITION_POOL,
    ELO_INITIAL_RATING,
    ELO_K_FACTORS,
    ELO_RECENCY_DECAY_PER_MONTH,
    ELO_MAP_WIN_RATE_SCALE,
    ELO_MIN_MATCHES_FOR_MAP,
)

# Routing tables: game → table names
_MATCH_TABLES: dict[str, str] = {
    "cs2":   "cs2_matches",
    "val":   "val_matches",
    "dota2": "dota2_matches",
    "lol":   "lol_matches",
}
_TEAM_TABLES: dict[str, str] = {
    "cs2":   "cs2_teams",
    "val":   "val_teams",
    "dota2": "dota2_teams",
    "lol":   "lol_teams",
}
_LAYER2_TABLES: dict[str, str] = {
    "cs2":   "cs2_map_stats",
    "val":   "val_map_stats",
    "dota2": "dota2_hero_stats",
    "lol":   "lol_position_stats",
}
_LAYER2_NAME_COL: dict[str, str] = {
    "cs2":   "map_name",
    "val":   "map_name",
    "dota2": "hero_name",
    "lol":   "position",
}

logger = logging.getLogger(__name__)

_ELO_SCHEMA = """
CREATE TABLE IF NOT EXISTS elo_team_ratings (
    team_id     TEXT    NOT NULL,
    game        TEXT    NOT NULL,
    map_name    TEXT    NOT NULL DEFAULT 'overall',
    rating      REAL    NOT NULL DEFAULT 1500.0,
    matches     INTEGER NOT NULL DEFAULT 0,
    wins        INTEGER NOT NULL DEFAULT 0,
    computed_at TEXT,
    PRIMARY KEY (team_id, game, map_name)
);

CREATE TABLE IF NOT EXISTS elo_match_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    match_id        TEXT,
    game            TEXT    NOT NULL,
    map_name        TEXT    NOT NULL DEFAULT 'overall',
    winner_id       TEXT,
    loser_id        TEXT,
    winner_elo_pre  REAL,
    loser_elo_pre   REAL,
    winner_elo_post REAL,
    loser_elo_post  REAL,
    k_applied       REAL,
    expected_winner REAL,
    match_date      TEXT,
    match_format    TEXT,
    logged_at       TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS elo_venue_stats (
    team_id     TEXT    NOT NULL,
    game        TEXT    NOT NULL,
    venue_type  TEXT    NOT NULL,
    matches     INTEGER NOT NULL DEFAULT 0,
    wins        INTEGER NOT NULL DEFAULT 0,
    win_rate    REAL    NOT NULL DEFAULT 0.5,
    updated_at  TEXT,
    PRIMARY KEY (team_id, game, venue_type)
);

CREATE INDEX IF NOT EXISTS idx_elo_ratings_game ON elo_team_ratings(game, map_name);
CREATE INDEX IF NOT EXISTS idx_elo_log_game     ON elo_match_log(game);
CREATE INDEX IF NOT EXISTS idx_elo_venue_team   ON elo_venue_stats(team_id, game);
"""


class EloEngine:
    """
    Computes and caches two-layer ELO ratings for CS2, Valorant, Dota 2,
    and League of Legends teams.

    The secondary ELO layer is game-specific:
      CS2:   per-map win rates  → map ELO
      Val:   per-map win rates  → map ELO
      Dota2: per-hero win rates → hero ELO (proxy for draft strength)
      LoL:   per-position stats → position ELO

    Typical usage:
        elo = EloEngine()
        elo.sync_all()
        p = elo.predict_match(t1, t2, "dota2")["win_prob_team1"]
    """

    def __init__(self, db_path: str = DB_PATH):
        self.db = BotDStorage(db_path)
        self._init_schema()

    def _init_schema(self):
        with self.db.conn() as c:
            c.executescript(_ELO_SCHEMA)

    # ------------------------------------------------------------------
    # Core ELO mathematics
    # ------------------------------------------------------------------

    @staticmethod
    def expected_score(r_a: float, r_b: float) -> float:
        """Probability that A beats B under standard ELO (400-point scale)."""
        return 1.0 / (1.0 + 10.0 ** ((r_b - r_a) / 400.0))

    @staticmethod
    def effective_k(match_format: str, months_ago: float) -> float:
        """
        K-factor with 15% recency decay per month.

        Example decay:
          0 months  →  K × 1.00  (full weight)
          1 month   →  K × 0.85
          3 months  →  K × 0.61
          6 months  →  K × 0.38
        """
        base = ELO_K_FACTORS.get(match_format, ELO_K_FACTORS["Bo3"])
        decay = (1.0 - ELO_RECENCY_DECAY_PER_MONTH) ** max(0.0, months_ago)
        return base * decay

    @staticmethod
    def update_ratings(
        r_a: float, r_b: float, a_won: bool, k: float
    ) -> tuple[float, float]:
        """
        Apply one ELO update.  Returns (new_r_a, new_r_b).
        """
        e_a = EloEngine.expected_score(r_a, r_b)
        e_b = 1.0 - e_a
        s_a = 1.0 if a_won else 0.0
        s_b = 0.0 if a_won else 1.0
        return (r_a + k * (s_a - e_a), r_b + k * (s_b - e_b))

    # ------------------------------------------------------------------
    # Layer 1: Overall ELO
    # ------------------------------------------------------------------

    def compute_overall_elos(
        self,
        game: str,
        force: bool = False,
        log_history: bool = False,
    ) -> dict[str, float]:
        """
        Replay all stored match results (oldest → newest) with recency-decayed
        K-factors to produce current ELO ratings for every team.

        Results are cached in elo_team_ratings (map_name='overall') and
        refreshed automatically after 6 hours or when force=True.

        Returns: {team_id: rating}
        """
        if not force and self.db.is_fresh(
            "elo_team_ratings",
            where=f"game='{game}' AND map_name='overall'",
            max_age_hours=6.0,
        ):
            rows = self.db.execute(
                "SELECT team_id, rating FROM elo_team_ratings "
                "WHERE game=? AND map_name='overall'",
                (game,),
            )
            if rows:
                logger.debug("Using cached %s ELO (%d teams)", game, len(rows))
                return {r["team_id"]: r["rating"] for r in rows}

        logger.info("Computing %s overall ELO from full match history", game)
        table = _MATCH_TABLES.get(game, f"{game}_matches")

        matches = self.db.execute(
            f"SELECT * FROM {table} ORDER BY match_date ASC, created_at ASC"
        )
        if not matches:
            logger.warning("No %s matches found — ELO ratings will be blank", game)
            return {}


        now = datetime.utcnow()
        ratings: dict[str, float] = {}
        win_counts: dict[str, int] = {}
        match_counts: dict[str, int] = {}

        for match in matches:
            t1 = match.get("team1_id", "")
            t2 = match.get("team2_id", "")
            winner = match.get("winner_id", "")
            if not t1 or not t2 or not winner:
                continue

            r1 = ratings.get(t1, ELO_INITIAL_RATING)
            r2 = ratings.get(t2, ELO_INITIAL_RATING)

            # Months since this match (drives recency decay)
            try:
                match_dt = datetime.fromisoformat(str(match["match_date"]))
            except (ValueError, TypeError):
                match_dt = now
            months_ago = max(0.0, (now - match_dt).days / 30.0)

            k = self.effective_k(match.get("match_format") or "Bo3", months_ago)
            a_won = (winner == t1)
            r1_new, r2_new = self.update_ratings(r1, r2, a_won, k)

            if log_history:
                e1 = self.expected_score(r1, r2)
                self._log_match(
                    match_id=match.get("match_id", ""),
                    game=game,
                    map_name="overall",
                    winner_id=t1 if a_won else t2,
                    loser_id=t2 if a_won else t1,
                    winner_elo_pre=r1 if a_won else r2,
                    loser_elo_pre=r2 if a_won else r1,
                    winner_elo_post=r1_new if a_won else r2_new,
                    loser_elo_post=r2_new if a_won else r1_new,
                    k_applied=round(k, 4),
                    expected_winner=round(e1 if a_won else 1.0 - e1, 4),
                    match_date=match.get("match_date", ""),
                    match_format=match.get("match_format", "Bo3"),
                )

            ratings[t1] = r1_new
            ratings[t2] = r2_new
            match_counts[t1] = match_counts.get(t1, 0) + 1
            match_counts[t2] = match_counts.get(t2, 0) + 1
            if a_won:
                win_counts[t1] = win_counts.get(t1, 0) + 1
            else:
                win_counts[t2] = win_counts.get(t2, 0) + 1

        computed_at = self.db.now()
        for team_id, rating in ratings.items():
            self.db.upsert("elo_team_ratings", {
                "team_id": team_id,
                "game": game,
                "map_name": "overall",
                "rating": round(rating, 2),
                "matches": match_counts.get(team_id, 0),
                "wins": win_counts.get(team_id, 0),
                "computed_at": computed_at,
            }, ["team_id", "game", "map_name"])

        logger.info(
            "Overall ELO computed for %d %s teams from %d matches",
            len(ratings), game, len(matches),
        )
        return ratings

    def _log_match(self, **kwargs):
        with self.db.conn() as c:
            cols = list(kwargs.keys())
            c.execute(
                f"INSERT INTO elo_match_log ({', '.join(cols)}) "
                f"VALUES ({', '.join('?' * len(cols))})",
                list(kwargs.values()),
            )

    # ------------------------------------------------------------------
    # Layer 2: Map ELO
    # ------------------------------------------------------------------

    def compute_map_elos(self, game: str) -> None:
        """
        Derive per-map ELO for every team from their aggregate map win rates,
        anchored to their overall ELO.

        Formula:
            map_elo = overall_elo + (map_win_rate − 0.50) × 700

        This is an approximation valid when opponent quality is uniform.
        With a scale of 700:  +0.10 win rate ≈ +70 ELO
        (Exact ELO formula gives +70.4 for 60% expected score.)

        Teams with fewer than ELO_MIN_MATCHES_FOR_MAP map games are skipped;
        their map lookups fall back to overall ELO automatically.
        """
        overall = self.compute_overall_elos(game)
        if not overall:
            return

        layer2_table = _LAYER2_TABLES.get(game)
        name_col     = _LAYER2_NAME_COL.get(game, "map_name")
        if not layer2_table:
            return

        # For cs2/val we filter by the fixed map pool; for dota2/lol accept any entry
        pool: list[str] | None = CS2_MAP_POOL if game == "cs2" else (
            VAL_MAP_POOL if game == "val" else None
        )

        map_stats = self.db.execute(f"SELECT * FROM {layer2_table}")
        computed_at = self.db.now()
        stored = 0

        for stat in map_stats:
            team_id  = stat["team_id"]
            map_name = stat.get(name_col) or ""
            if pool is not None and map_name not in pool:
                continue
            total = (stat.get("wins") or 0) + (stat.get("losses") or 0)
            if total < ELO_MIN_MATCHES_FOR_MAP:
                continue

            overall_elo = overall.get(team_id, ELO_INITIAL_RATING)
            win_rate    = stat.get("win_rate") or 0.5
            map_elo     = overall_elo + (win_rate - 0.5) * ELO_MAP_WIN_RATE_SCALE

            self.db.upsert("elo_team_ratings", {
                "team_id":    team_id,
                "game":       game,
                "map_name":   map_name,
                "rating":     round(map_elo, 2),
                "matches":    total,
                "wins":       stat.get("wins") or 0,
                "computed_at": computed_at,
            }, ["team_id", "game", "map_name"])
            stored += 1

        logger.info("Layer-2 ELO (%s): stored %d entries for %s", name_col, stored, game)

    # ------------------------------------------------------------------
    # Venue stats (online vs LAN)
    # ------------------------------------------------------------------

    def compute_venue_stats(self, game: str) -> None:
        """
        Compute per-team win rates split by venue type (LAN vs online).

        A tournament is classed as 'online' if its Liquipedia location
        contains "online" or is empty.  Otherwise it is 'lan'.

        The JOIN is fuzzy (LIKE) because tournament names don't always
        match exactly between the match table and liq_tournaments.
        """
        match_table = _MATCH_TABLES.get(game, f"{game}_matches")

        # Bring in location from liq_tournaments via a fuzzy name match
        matches = self.db.execute(
            f"""
            SELECT m.team1_id, m.team2_id, m.winner_id,
                   COALESCE(t.location, '') AS t_location
            FROM {match_table} m
            LEFT JOIN liq_tournaments t
                   ON t.game = ?
                  AND LOWER(m.tournament) LIKE '%' || LOWER(SUBSTR(t.name,1,20)) || '%'
            """,
            (game,),
        )

        # {team_id: {venue: {matches, wins}}}
        stats: dict[str, dict[str, dict[str, int]]] = {}

        for m in matches:
            loc = (m.get("t_location") or "").lower().strip()
            venue = "online" if (not loc or "online" in loc) else "lan"

            for team_id in (m.get("team1_id"), m.get("team2_id")):
                if not team_id:
                    continue
                if team_id not in stats:
                    stats[team_id] = {
                        "lan":    {"matches": 0, "wins": 0},
                        "online": {"matches": 0, "wins": 0},
                    }
                stats[team_id][venue]["matches"] += 1
                if m.get("winner_id") == team_id:
                    stats[team_id][venue]["wins"] += 1

        updated_at = self.db.now()
        for team_id, vd in stats.items():
            for venue_type, counts in vd.items():
                n = counts["matches"]
                w = counts["wins"]
                self.db.upsert("elo_venue_stats", {
                    "team_id":    team_id,
                    "game":       game,
                    "venue_type": venue_type,
                    "matches":    n,
                    "wins":       w,
                    "win_rate":   round(w / n if n > 0 else 0.5, 4),
                    "updated_at": updated_at,
                }, ["team_id", "game", "venue_type"])

        logger.info("Venue stats computed for %d %s teams", len(stats), game)

    # ------------------------------------------------------------------
    # Getters
    # ------------------------------------------------------------------

    def get_team_elo(
        self, team_id: str, game: str, map_name: str = "overall"
    ) -> float:
        """
        Return a team's ELO for a given map (or overall).
        Falls back to overall ELO if no map-specific rating exists.
        Returns ELO_INITIAL_RATING for unknown teams.
        """
        rows = self.db.execute(
            "SELECT rating FROM elo_team_ratings "
            "WHERE team_id=? AND game=? AND map_name=?",
            (team_id, game, map_name),
        )
        if rows:
            return float(rows[0]["rating"])

        if map_name != "overall":
            return self.get_team_elo(team_id, game, "overall")

        return ELO_INITIAL_RATING

    def get_venue_win_rate(
        self, team_id: str, game: str, venue_type: str, min_matches: int = 5
    ) -> Optional[float]:
        """
        Return team's win rate for 'lan' or 'online' venue.
        Returns None if fewer than min_matches results available.
        """
        rows = self.db.execute(
            "SELECT win_rate, matches FROM elo_venue_stats "
            "WHERE team_id=? AND game=? AND venue_type=?",
            (team_id, game, venue_type),
        )
        if rows and (rows[0].get("matches") or 0) >= min_matches:
            return float(rows[0]["win_rate"])
        return None

    def has_map_elo(self, team_id: str, game: str, map_name: str) -> bool:
        """True if a reliable (≥ min matches) map-specific ELO exists."""
        rows = self.db.execute(
            "SELECT matches FROM elo_team_ratings "
            "WHERE team_id=? AND game=? AND map_name=? AND matches >= ?",
            (team_id, game, map_name, ELO_MIN_MATCHES_FOR_MAP),
        )
        return bool(rows)

    # ------------------------------------------------------------------
    # Match prediction (base ELO only, no situational adjustments)
    # ------------------------------------------------------------------

    def predict_match(
        self,
        team1_id: str,
        team2_id: str,
        game: str,
        match_format: str = "Bo3",
        veto_maps: Optional[list[str]] = None,
    ) -> dict:
        """
        Compute raw ELO win probability for team1.

        If veto_maps is provided, averages the map-specific ELOs for the
        listed maps (falling back to overall ELO per map if needed).
        Otherwise uses overall ELO for both teams.

        Returns:
            team1_elo       float
            team2_elo       float
            elo_diff        float  (team1 − team2)
            win_prob_team1  float  (0–1)
            elo_source      'overall' | 'map_weighted'
            map_breakdown   list[{map, elo1, elo2, prob_team1}]
        """
        if veto_maps:
            map_elos1, map_elos2, breakdown = [], [], []
            for m in veto_maps:
                e1 = self.get_team_elo(team1_id, game, m)
                e2 = self.get_team_elo(team2_id, game, m)
                p = self.expected_score(e1, e2)
                map_elos1.append(e1)
                map_elos2.append(e2)
                breakdown.append({
                    "map": m,
                    "elo1": round(e1, 1),
                    "elo2": round(e2, 1),
                    "prob_team1": round(p, 4),
                })
            elo1 = sum(map_elos1) / len(map_elos1)
            elo2 = sum(map_elos2) / len(map_elos2)
            elo_source = "map_weighted"
        else:
            elo1 = self.get_team_elo(team1_id, game, "overall")
            elo2 = self.get_team_elo(team2_id, game, "overall")
            elo_source = "overall"
            breakdown = []

        return {
            "team1_elo":      round(elo1, 2),
            "team2_elo":      round(elo2, 2),
            "elo_diff":       round(elo1 - elo2, 2),
            "win_prob_team1": round(self.expected_score(elo1, elo2), 4),
            "elo_source":     elo_source,
            "map_breakdown":  breakdown,
        }

    # ------------------------------------------------------------------
    # Full sync
    # ------------------------------------------------------------------

    def sync(self, game: str, force: bool = False) -> dict:
        """Compute (or refresh) all ELO layers for one game."""
        overall = self.compute_overall_elos(game, force=force)
        self.compute_map_elos(game)
        self.compute_venue_stats(game)
        return {"game": game, "teams_with_elo": len(overall)}

    def sync_all(self, force: bool = False) -> dict:
        """Sync all four supported games."""
        return {g: self.sync(g, force=force) for g in ("cs2", "val", "dota2", "lol")}

    # ------------------------------------------------------------------
    # Reporting helpers
    # ------------------------------------------------------------------

    def elo_rankings(self, game: str, top_n: int = 20) -> list[dict]:
        """Top N teams by overall ELO, joined with team name."""
        team_table = _TEAM_TABLES.get(game, f"{game}_teams")
        return self.db.execute(
            f"SELECT e.team_id, e.rating, e.matches, e.wins, "
            f"       COALESCE(t.name, e.team_id) AS name "
            f"FROM elo_team_ratings e "
            f"LEFT JOIN {team_table} t ON e.team_id = t.team_id "
            f"WHERE e.game=? AND e.map_name='overall' "
            f"ORDER BY e.rating DESC LIMIT ?",
            (game, top_n),
        )

    def team_map_strengths(self, team_id: str, game: str) -> list[dict]:
        """
        Per-slot ELO breakdown for a team, sorted strongest → weakest.

        For CS2/Val: slot = map name.
        For Dota2: slot = hero name (all heroes with ≥ ELO_MIN_MATCHES_FOR_MAP picks).
        For LoL: slot = position.
        """
        # For fixed pools use the pool list; for dota2 use all stored heroes
        if game in ("cs2", "val"):
            pool: list[str] = CS2_MAP_POOL if game == "cs2" else VAL_MAP_POOL
        elif game == "lol":
            pool = LOL_POSITION_POOL
        else:
            # Dota2: pull all heroes with ELO stored for this team
            rows = self.db.execute(
                "SELECT map_name FROM elo_team_ratings "
                "WHERE team_id=? AND game=? AND map_name != 'overall' "
                "ORDER BY rating DESC LIMIT 30",
                (team_id, game),
            )
            pool = [r["map_name"] for r in rows]

        overall = self.get_team_elo(team_id, game, "overall")
        result = []
        for slot in pool:
            rows = self.db.execute(
                "SELECT rating, matches FROM elo_team_ratings "
                "WHERE team_id=? AND game=? AND map_name=?",
                (team_id, game, slot),
            )
            if rows:
                elo = float(rows[0]["rating"])
                n = rows[0]["matches"]
            else:
                elo = overall
                n = 0
            result.append({
                "map":              slot,
                "elo":              round(elo, 1),
                "delta_vs_overall": round(elo - overall, 1),
                "matches":          n,
                "reliable":         n >= ELO_MIN_MATCHES_FOR_MAP,
            })
        return sorted(result, key=lambda x: x["elo"], reverse=True)

    def head_to_head_elo(
        self, team1_id: str, team2_id: str, game: str,
        veto_maps: Optional[list[str]] = None,
    ) -> dict:
        """
        Compact summary of an ELO matchup — useful for signal reasoning text.
        """
        pred = self.predict_match(team1_id, team2_id, game, veto_maps=veto_maps)
        t1_name = self._team_name(team1_id, game)
        t2_name = self._team_name(team2_id, game)
        return {
            "team1": t1_name,
            "team2": t2_name,
            **pred,
        }

    def _team_name(self, team_id: str, game: str) -> str:
        table = _TEAM_TABLES.get(game, f"{game}_teams")
        rows = self.db.execute(
            f"SELECT name FROM {table} WHERE team_id=?", (team_id,)
        )
        return rows[0]["name"] if rows else team_id
