"""
Dota 2 data scraper — OpenDota API.

Primary data source: https://api.opendota.com/api (free, no auth required)
Secondary: Liquipedia Dota 2 for upcoming matches (via LiquipediaScraper).

Data collected:
  - Pro team rankings and metadata (OpenDota /teams)
  - Match history with series results (OpenDota /teams/{id}/matches)
  - Hero stats per team: pick rates, ban rates, win rates (/teams/{id}/heroes)
  - Player roster and performance (/teams/{id}/players)
  - Roster changes (inferred from player transfers)
  - Upcoming matches from Liquipedia Dota 2 wiki

All data stored in dota2_* tables.  Upcoming matches stored in
liq_upcoming_matches with game='dota2' so the signal engine can
query them uniformly.
"""

from __future__ import annotations

import hashlib
import logging
import time
from datetime import datetime, timedelta
from typing import Optional

import requests

from botd.config import OPENDOTA_REQUEST_DELAY, DB_PATH, TOP_N_DOTA2_TEAMS
from botd.storage.db import BotDStorage

logger = logging.getLogger(__name__)

_OPENDOTA_BASE = "https://api.opendota.com/api"
_LIQUIPEDIA_BASE = "https://liquipedia.net/dota2/api.php"

_TIER_KEYWORDS: dict[str, list[str]] = {
    "S": ["the international", "ti ", "major", "dreamleague s", "esl one"],
    "A": ["dreamleague", "esl one", "blast", "bali", "lima"],
    "B": ["open qualifier", "regional qualifier", "summit"],
    "C": ["showmatch", "qualifier"],
}


def _infer_tier(tournament_name: str) -> str:
    name_lower = (tournament_name or "").lower()
    for tier, keywords in _TIER_KEYWORDS.items():
        if any(kw in name_lower for kw in keywords):
            return tier
    return "B"


def _team_id(opendota_id) -> str:
    return f"dota2_{opendota_id}"


def _player_id(account_id) -> str:
    return f"dota2_p_{account_id}"


class DotaScraper:
    """
    Scraper for Dota 2 pro match data via OpenDota API.

    Usage:
        scraper = DotaScraper()
        scraper.sync_teams()
        scraper.sync_team_matches(team_id)
        scraper.sync_hero_stats(team_id)
        scraper.sync_all()
    """

    def __init__(self, db_path: str = DB_PATH):
        self.db = BotDStorage(db_path)
        self._session = requests.Session()
        self._session.headers.update({
            "User-Agent": "BotD/1.0 esports-trading-research",
            "Accept": "application/json",
        })
        self._last_request = 0.0

    # ------------------------------------------------------------------
    # Rate-limited HTTP
    # ------------------------------------------------------------------

    def _get(self, path: str, params: dict | None = None) -> Optional[dict | list]:
        elapsed = time.time() - self._last_request
        if elapsed < OPENDOTA_REQUEST_DELAY:
            time.sleep(OPENDOTA_REQUEST_DELAY - elapsed)
        self._last_request = time.time()

        url = f"{_OPENDOTA_BASE}{path}"
        try:
            resp = self._session.get(url, params=params or {}, timeout=15)
            if resp.status_code == 429:
                logger.warning("OpenDota rate-limited, sleeping 60s")
                time.sleep(60)
                resp = self._session.get(url, params=params or {}, timeout=15)
            resp.raise_for_status()
            return resp.json()
        except Exception as exc:
            logger.error("OpenDota GET %s failed: %s", path, exc)
            return None

    # ------------------------------------------------------------------
    # Team sync
    # ------------------------------------------------------------------

    def sync_teams(self, limit: int = TOP_N_DOTA2_TEAMS * 2) -> list[dict]:
        """
        Fetch top pro teams from OpenDota and store in dota2_teams.
        Returns list of stored team dicts.
        """
        if self.db.is_fresh("dota2_teams", max_age_hours=24):
            rows = self.db.execute("SELECT * FROM dota2_teams ORDER BY rating DESC")
            if rows:
                logger.info("dota2_teams fresh (%d teams)", len(rows))
                return rows

        logger.info("Fetching Dota 2 pro teams from OpenDota")
        data = self._get("/teams", {"limit": limit, "page": 0})
        if not data or not isinstance(data, list):
            return []

        teams = []
        for i, t in enumerate(data[:TOP_N_DOTA2_TEAMS]):
            tid = _team_id(t.get("team_id", ""))
            if not tid:
                continue
            record = {
                "team_id":   tid,
                "name":      (t.get("name") or t.get("tag") or str(t.get("team_id"))).strip(),
                "ranking":   i + 1,
                "country":   t.get("country") or "",
                "region":    _infer_region(t.get("country") or ""),
                "wins":      t.get("wins") or 0,
                "losses":    t.get("losses") or 0,
                "rating":    float(t.get("rating") or 0.0),
                "updated_at": self.db.now(),
            }
            self.db.upsert_dota2_team(record)
            teams.append(record)

        logger.info("Stored %d Dota 2 teams", len(teams))
        return teams

    # ------------------------------------------------------------------
    # Match history
    # ------------------------------------------------------------------

    def sync_team_matches(self, team_id: str, limit: int = 100) -> list[dict]:
        """
        Fetch match history for a team from OpenDota.
        Stores series-level results (win/loss per series, inferred from
        individual game results) in dota2_matches.
        """
        opendota_id = team_id.replace("dota2_", "")
        data = self._get(f"/teams/{opendota_id}/matches", {"limit": limit})
        if not data or not isinstance(data, list):
            return []

        matches = []
        for m in data:
            match_id = f"dota2_{m.get('match_id', '')}"
            league_id = str(m.get("leagueid") or "")
            radiant_win = m.get("radiant_win", False)
            team1_radiant = m.get("radiant", False)

            # team1 is our team, team2 is the opponent
            opp_name = m.get("opposing_team_name") or "Unknown"
            opp_id_raw = m.get("opposing_team_id")
            opp_id = _team_id(opp_id_raw) if opp_id_raw else f"dota2_opp_{opp_name[:8]}"

            our_win = (radiant_win and team1_radiant) or (not radiant_win and not team1_radiant)

            try:
                match_dt = datetime.utcfromtimestamp(m.get("start_time", 0))
                match_date = match_dt.strftime("%Y-%m-%d")
            except (OSError, ValueError, TypeError):
                match_date = datetime.utcnow().strftime("%Y-%m-%d")

            league_name = m.get("league_name") or ""
            tier = _infer_tier(league_name)

            record = {
                "match_id":       match_id,
                "team1_id":       team_id,
                "team2_id":       opp_id,
                "team1_name":     self._team_name(team_id),
                "team2_name":     opp_name,
                "team1_score":    1 if our_win else 0,
                "team2_score":    0 if our_win else 1,
                "winner_id":      team_id if our_win else opp_id,
                "tournament":     league_name,
                "tournament_tier": tier,
                "match_format":   "Bo3",   # OpenDota stores individual games; default series format
                "match_date":     match_date,
                "league_id":      league_id,
            }
            self.db.upsert_dota2_match(record)
            matches.append(record)

        logger.info("Stored %d matches for %s", len(matches), team_id)
        return matches

    # ------------------------------------------------------------------
    # Hero stats
    # ------------------------------------------------------------------

    def sync_hero_stats(self, team_id: str) -> list[dict]:
        """
        Fetch hero win/pick/ban stats for a team and store in dota2_hero_stats.
        Returns list of hero stat dicts.
        """
        opendota_id = team_id.replace("dota2_", "")
        data = self._get(f"/teams/{opendota_id}/heroes")
        if not data or not isinstance(data, list):
            return []

        stats = []
        for h in data:
            hero = h.get("localized_name") or h.get("hero_id") or "Unknown"
            games_played = int(h.get("games_played") or 0)
            wins = int(h.get("wins") or 0)
            losses = games_played - wins
            record = {
                "team_id":      team_id,
                "hero_name":    str(hero),
                "times_picked": games_played,
                "times_banned": int(h.get("with_games_played") or 0) - games_played,
                "wins":         wins,
                "losses":       max(0, losses),
                "win_rate":     round(wins / games_played if games_played > 0 else 0.5, 4),
                "updated_at":   self.db.now(),
            }
            self.db.upsert_dota2_hero_stats(record)
            stats.append(record)

        logger.info("Stored %d hero stats for %s", len(stats), team_id)
        return stats

    # ------------------------------------------------------------------
    # Player stats
    # ------------------------------------------------------------------

    def sync_players(self, team_id: str) -> list[dict]:
        """
        Fetch current roster from OpenDota /teams/{id}/players.
        Stores in dota2_player_stats.
        """
        opendota_id = team_id.replace("dota2_", "")
        data = self._get(f"/teams/{opendota_id}/players")
        if not data or not isinstance(data, list):
            return []

        players = []
        for p in data:
            if not p.get("is_current_team_member"):
                continue
            account_id = p.get("account_id")
            if not account_id:
                continue

            player_id = _player_id(account_id)
            record = {
                "player_id":    player_id,
                "player_name":  p.get("name") or p.get("personaname") or str(account_id),
                "team_id":      team_id,
                "role":         "",      # OpenDota /players endpoint has role
                "avg_gpm":      0.0,
                "avg_xpm":      0.0,
                "avg_kda":      0.0,
                "avg_kills":    0.0,
                "avg_deaths":   0.0,
                "avg_assists":  0.0,
                "matches_played": int(p.get("games_played") or 0),
                "updated_at":   self.db.now(),
            }
            self.db.upsert_dota2_player(record)
            players.append(record)

        return players

    # ------------------------------------------------------------------
    # H2H computation
    # ------------------------------------------------------------------

    def compute_h2h(self) -> None:
        """Compute head-to-head win records from dota2_matches."""
        matches = self.db.execute("SELECT * FROM dota2_matches")
        h2h: dict[tuple, dict] = {}

        for m in matches:
            t1 = m.get("team1_id") or ""
            t2 = m.get("team2_id") or ""
            winner = m.get("winner_id") or ""
            if not t1 or not t2:
                continue

            key = tuple(sorted([t1, t2]))
            if key not in h2h:
                h2h[key] = {
                    "team1_id": key[0], "team2_id": key[1],
                    "team1_wins": 0, "team2_wins": 0, "total_matches": 0,
                }
            h2h[key]["total_matches"] += 1
            if winner == key[0]:
                h2h[key]["team1_wins"] += 1
            elif winner == key[1]:
                h2h[key]["team2_wins"] += 1

        for record in h2h.values():
            self.db.upsert_dota2_h2h(record)

    # ------------------------------------------------------------------
    # Upcoming matches (via Liquipedia Dota2 wiki)
    # ------------------------------------------------------------------

    def sync_upcoming_matches(self, days: int = 7) -> list[dict]:
        """
        Fetch upcoming Dota 2 matches from Liquipedia.
        Stores in liq_upcoming_matches with game='dota2'.
        Returns list of match dicts.
        """
        from botd.data.liquipedia import LiquipediaScraper
        liq = LiquipediaScraper(self.db.db_path)
        return liq.sync_upcoming_matches("dota2", days=days)

    # ------------------------------------------------------------------
    # Full sync
    # ------------------------------------------------------------------

    def sync_all(self, days: int = 7) -> dict:
        """Sync teams, matches, hero stats, and upcoming matches."""
        logger.info("Starting full Dota 2 data sync")
        teams = self.sync_teams()

        matches_total = 0
        hero_stats_total = 0
        for team in teams[:TOP_N_DOTA2_TEAMS]:
            tid = team["team_id"]
            matches_total += len(self.sync_team_matches(tid))
            hero_stats_total += len(self.sync_hero_stats(tid))
            self.sync_players(tid)

        self.compute_h2h()
        upcoming = self.sync_upcoming_matches(days=days)

        result = {
            "teams": len(teams),
            "matches": matches_total,
            "hero_stats": hero_stats_total,
            "upcoming": len(upcoming),
        }
        logger.info("Dota 2 sync complete: %s", result)
        return result

    # ------------------------------------------------------------------
    # Match snapshot (for Test 6 equivalent)
    # ------------------------------------------------------------------

    def match_snapshot(self, t1_id: str, t2_id: str) -> dict:
        """Full match preview dict for two teams."""
        def team_data(tid: str) -> dict:
            team = self.db.execute(
                "SELECT * FROM dota2_teams WHERE team_id=?", (tid,)
            )
            heroes = self.db.get_dota2_hero_stats(tid)
            h2h = self.db.get_dota2_h2h(tid, t2_id if tid == t1_id else t1_id)
            return {
                "team":        team[0] if team else {},
                "hero_pool":   heroes[:10],
                "h2h":         h2h or {},
            }

        return {"team1": team_data(t1_id), "team2": team_data(t2_id)}

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _team_name(self, team_id: str) -> str:
        rows = self.db.execute("SELECT name FROM dota2_teams WHERE team_id=?", (team_id,))
        return rows[0]["name"] if rows else team_id


def _infer_region(country: str) -> str:
    """Map country code to broad esports region."""
    country = (country or "").upper().strip()
    eu_countries = {
        "DE", "FR", "SE", "FI", "DK", "NO", "NL", "PL", "UA", "RU", "CZ",
        "HU", "SK", "RO", "BG", "AT", "CH", "BE", "UK", "GB", "ES", "IT",
        "HR", "SI", "RS", "MK", "BA", "ME", "AL", "GR", "TR",
    }
    cn_countries = {"CN"}
    kr_countries = {"KR"}
    na_countries = {"US", "CA"}
    sea_countries = {"PH", "SG", "MY", "TH", "ID", "VN"}
    sa_countries = {"BR", "AR", "CL", "PE", "CO", "MX"}

    if country in cn_countries:
        return "CN"
    if country in kr_countries:
        return "KR"
    if country in eu_countries:
        return "EU"
    if country in na_countries:
        return "NA"
    if country in sea_countries:
        return "SEA"
    if country in sa_countries:
        return "SA"
    return "OTHER"
