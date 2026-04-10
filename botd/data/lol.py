"""
League of Legends esports data scraper — Riot Esports API.

Primary data source: https://esports-api.lolesports.com/persisted/gw
  Free, no registration required.  A public API key is used (see config.py).

Data collected:
  - Team standings per split (getStandings)
  - Upcoming/completed match schedule (getSchedule)
  - Team and player metadata

Regional ELO bias (applied at international events):
  KR +8, CN +6, EU +2, NA −4  (tunable in botd/config.py)

All match results stored in lol_matches.
Upcoming matches stored in liq_upcoming_matches with game='lol'
so the signal engine can query them uniformly.
"""

from __future__ import annotations

import hashlib
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

import requests

from botd.config import (
    LOL_REQUEST_DELAY,
    LOL_ESPORTS_API_KEY,
    DB_PATH,
    TOP_N_LOL_TEAMS,
)
from botd.storage.db import BotDStorage

logger = logging.getLogger(__name__)

_API_BASE = "https://esports-api.lolesports.com/persisted/gw"
_FEED_BASE = "https://feed.lolesports.com/livestats/v1"

# Leagues to track (Riot league IDs for major regions)
_LEAGUE_IDS: dict[str, str] = {
    "LCK":  "98767991310872058",   # KR
    "LPL":  "98767991314006698",   # CN
    "LEC":  "98767991302996019",   # EU
    "LCS":  "98767991299243165",   # NA
    "CBLOL":"98767991332355509",   # BR
    "PCS":  "104366947889790212",  # PCS
    "VCS":  "107407335299756657",  # VN
    "LJL":  "98767991349978712",   # JP
}

# Map league code → region
_LEAGUE_REGION: dict[str, str] = {
    "LCK": "KR", "LPL": "CN", "LEC": "EU", "LCS": "NA",
    "CBLOL": "BR", "PCS": "PCS", "VCS": "VN", "LJL": "JP",
    "LCO": "OCE", "TCL": "TR", "LLA": "LAS",
}

_TIER_MAP: dict[str, str] = {
    "worlds": "S", "msi": "S",
    "lck": "A", "lpl": "A", "lec": "A", "lcs": "A",
    "cblol": "B", "pcs": "B", "vcs": "B", "ljl": "B",
    "qualifier": "C",
}


def _infer_tier(tournament_name: str) -> str:
    name_lower = (tournament_name or "").lower()
    for kw, tier in _TIER_MAP.items():
        if kw in name_lower:
            return tier
    return "B"


def _normalize_format(best_of: int) -> str:
    if best_of == 1:
        return "Bo1"
    if best_of == 5:
        return "Bo5"
    return "Bo3"


def _match_id(game_id: str) -> str:
    return f"lol_{game_id}"


def _team_id(slug: str) -> str:
    return f"lol_{slug}"


class LoLScraper:
    """
    Scraper for League of Legends esports data via Riot Esports API.

    Usage:
        scraper = LoLScraper()
        scraper.sync_teams()
        scraper.sync_schedule(league_id="98767991310872058")
        scraper.sync_standings(tournament_id="...")
        scraper.sync_all()
    """

    def __init__(self, db_path: str = DB_PATH):
        self.db = BotDStorage(db_path)
        self._session = requests.Session()
        self._session.headers.update({
            "x-api-key": LOL_ESPORTS_API_KEY,
            "User-Agent": "BotD/1.0 esports-trading-research",
        })
        self._last_request = 0.0

    # ------------------------------------------------------------------
    # Rate-limited HTTP
    # ------------------------------------------------------------------

    def _get(self, path: str, params: dict | None = None) -> Optional[dict]:
        elapsed = time.time() - self._last_request
        if elapsed < LOL_REQUEST_DELAY:
            time.sleep(LOL_REQUEST_DELAY - elapsed)
        self._last_request = time.time()

        url = f"{_API_BASE}{path}"
        try:
            resp = self._session.get(url, params={"hl": "en-US", **(params or {})}, timeout=15)
            if resp.status_code == 429:
                logger.warning("LoL Esports API rate-limited, sleeping 30s")
                time.sleep(30)
                resp = self._session.get(url, params={"hl": "en-US", **(params or {})}, timeout=15)
            if resp.status_code == 403:
                logger.error("LoL Esports API: 403 Forbidden — check LOL_ESPORTS_API_KEY")
                return None
            resp.raise_for_status()
            return resp.json()
        except Exception as exc:
            logger.error("LoL API GET %s failed: %s", path, exc)
            return None

    # ------------------------------------------------------------------
    # Teams (from standings)
    # ------------------------------------------------------------------

    def sync_teams(self) -> list[dict]:
        """
        Pull team metadata from all major leagues via getLeagues + getStandings.
        Stores in lol_teams.  Returns list of stored team dicts.
        """
        if self.db.is_fresh("lol_teams", max_age_hours=24):
            rows = self.db.execute("SELECT * FROM lol_teams")
            if rows:
                logger.info("lol_teams fresh (%d teams)", len(rows))
                return rows

        logger.info("Fetching LoL teams from Riot Esports API")
        data = self._get("/getLeagues")
        if not data:
            return []

        leagues = data.get("data", {}).get("leagues", [])
        teams_seen: set[str] = set()
        teams: list[dict] = []

        for league in leagues:
            l_name = league.get("name", "")
            l_slug = league.get("slug", "").upper()
            region = _LEAGUE_REGION.get(l_slug, "OTHER")

            tournaments = league.get("tournaments") or []
            for tourn in tournaments[-1:]:  # most recent tournament
                t_id = tourn.get("id", "")
                if not t_id:
                    continue
                standings_data = self._get("/getStandings", {"tournamentId": t_id})
                if not standings_data:
                    continue

                stages = standings_data.get("data", {}).get("standings", [])
                for stage in stages:
                    for section in stage.get("sections", []):
                        for ranking in section.get("rankings", []):
                            for team_entry in ranking.get("teams", []):
                                t = team_entry
                                slug = t.get("slug") or t.get("code") or ""
                                if not slug or slug in teams_seen:
                                    continue
                                teams_seen.add(slug)

                                record = {
                                    "team_id":  _team_id(slug),
                                    "name":     t.get("name") or slug,
                                    "code":     t.get("code") or slug.upper(),
                                    "ranking":  ranking.get("ordinal", 99),
                                    "region":   region,
                                    "league":   l_name,
                                    "wins":     t.get("record", {}).get("wins", 0),
                                    "losses":   t.get("record", {}).get("losses", 0),
                                    "updated_at": self.db.now(),
                                }
                                self.db.upsert_lol_team(record)
                                teams.append(record)

        logger.info("Stored %d LoL teams", len(teams))
        return teams

    # ------------------------------------------------------------------
    # Schedule (upcoming + completed matches)
    # ------------------------------------------------------------------

    def sync_schedule(
        self, league_id: str, days_ahead: int = 7
    ) -> list[dict]:
        """
        Fetch schedule for a league.  Stores upcoming matches in
        liq_upcoming_matches and completed matches in lol_matches.
        """
        data = self._get("/getSchedule", {"leagueId": league_id})
        if not data:
            return []

        events = data.get("data", {}).get("schedule", {}).get("events", [])
        upcoming: list[dict] = []
        now = datetime.now(tz=timezone.utc)
        cutoff = now + timedelta(days=days_ahead)

        for ev in events:
            if ev.get("type") != "match":
                continue

            match = ev.get("match") or {}
            teams_raw = match.get("teams") or []
            if len(teams_raw) < 2:
                continue

            t1 = teams_raw[0]
            t2 = teams_raw[1]
            t1_slug = t1.get("slug") or t1.get("code") or ""
            t2_slug = t2.get("slug") or t2.get("code") or ""
            t1_id = _team_id(t1_slug)
            t2_id = _team_id(t2_slug)

            # Parse datetime
            start_time = ev.get("startTime") or ""
            try:
                if start_time.endswith("Z"):
                    start_time = start_time[:-1] + "+00:00"
                match_dt = datetime.fromisoformat(start_time)
                match_dt_str = match_dt.strftime("%Y-%m-%d %H:%M:%S")
                match_date = match_dt.strftime("%Y-%m-%d")
            except (ValueError, AttributeError):
                match_dt_str = ""
                match_date = now.strftime("%Y-%m-%d")
                match_dt = now

            best_of = match.get("strategy", {}).get("count") or 3
            match_format = _normalize_format(best_of)

            block_name = ev.get("blockName") or ""
            tournament_name = ev.get("league", {}).get("name") or ""
            tier = _infer_tier(tournament_name)

            # Game/match identifier
            match_id_raw = match.get("id") or ""
            mid = _match_id(match_id_raw or f"{t1_slug}_{t2_slug}_{match_date}")

            state = ev.get("state", "unstarted")

            if state in ("unstarted", "inProgress") and match_dt <= cutoff:
                # Upcoming match
                record = {
                    "match_id":       mid,
                    "game":           "lol",
                    "team1":          t1.get("name") or t1_slug,
                    "team2":          t2.get("name") or t2_slug,
                    "team1_id":       t1_id,
                    "team2_id":       t2_id,
                    "match_datetime": match_dt_str,
                    "tournament":     f"{tournament_name} — {block_name}",
                    "tournament_tier": tier,
                    "match_format":   match_format,
                    "prize_pool":     "",
                    "stream_url":     "",
                    "updated_at":     self.db.now(),
                }
                self.db.upsert_upcoming_match(record)
                upcoming.append(record)

            elif state == "completed":
                # Completed match — store result
                t1_result = t1.get("result") or {}
                t2_result = t2.get("result") or {}
                t1_wins = int(t1_result.get("gameWins") or 0)
                t2_wins = int(t2_result.get("gameWins") or 0)
                winner_id = t1_id if t1_wins > t2_wins else t2_id

                result_record = {
                    "match_id":       mid,
                    "team1_id":       t1_id,
                    "team2_id":       t2_id,
                    "team1_name":     t1.get("name") or t1_slug,
                    "team2_name":     t2.get("name") or t2_slug,
                    "team1_score":    t1_wins,
                    "team2_score":    t2_wins,
                    "winner_id":      winner_id,
                    "tournament":     tournament_name,
                    "tournament_tier": tier,
                    "match_format":   match_format,
                    "match_date":     match_date,
                    "league_id":      league_id,
                }
                self.db.upsert_lol_match(result_record)

        return upcoming

    # ------------------------------------------------------------------
    # Full upcoming matches sync (all major leagues)
    # ------------------------------------------------------------------

    def sync_upcoming_matches(self, days: int = 7) -> list[dict]:
        """
        Pull upcoming matches for all major leagues.
        Stores in liq_upcoming_matches with game='lol'.
        """
        all_upcoming: list[dict] = []
        for league_name, league_id in _LEAGUE_IDS.items():
            logger.info("Syncing LoL schedule for %s", league_name)
            upcoming = self.sync_schedule(league_id, days_ahead=days)
            all_upcoming.extend(upcoming)

        logger.info("Found %d upcoming LoL matches total", len(all_upcoming))
        return all_upcoming

    # ------------------------------------------------------------------
    # Standings (team win rates per split for position stats)
    # ------------------------------------------------------------------

    def sync_standings(self, tournament_id: str) -> dict:
        """
        Pull current standings and derive position win rates per team.
        The Riot Esports API doesn't expose per-position stats directly;
        we derive position strength from player performance where available.
        Returns raw standings data.
        """
        data = self._get("/getStandings", {"tournamentId": tournament_id})
        if not data:
            return {}
        return data.get("data", {})

    # ------------------------------------------------------------------
    # Full sync
    # ------------------------------------------------------------------

    def sync_all(self, days: int = 7) -> dict:
        """Sync teams and upcoming matches for all major leagues."""
        logger.info("Starting full LoL data sync")
        teams = self.sync_teams()
        upcoming = self.sync_upcoming_matches(days=days)

        # Sync match history for top teams via schedule
        matches_stored = self.db.scalar("SELECT COUNT(*) FROM lol_matches") or 0

        result = {
            "teams": len(teams),
            "upcoming_matches": len(upcoming),
            "historical_matches": matches_stored,
        }
        logger.info("LoL sync complete: %s", result)
        return result

    # ------------------------------------------------------------------
    # Match snapshot
    # ------------------------------------------------------------------

    def match_snapshot(self, t1_id: str, t2_id: str) -> dict:
        """Full match preview for two LoL teams."""
        def team_data(tid: str) -> dict:
            team = self.db.get_lol_team(tid)
            matches = self.db.get_lol_matches(tid, days=90)
            h2h = self.db.get_lol_h2h(tid, t2_id if tid == t1_id else t1_id)
            pos_stats = self.db.get_lol_position_stats(tid)
            recent_form = _compute_form(matches, tid)
            return {
                "team":        team or {},
                "recent_form": recent_form,
                "position_stats": pos_stats,
                "h2h":         h2h or {},
            }

        return {"team1": team_data(t1_id), "team2": team_data(t2_id)}


def _compute_form(matches: list[dict], team_id: str) -> dict:
    """Compute recent win rate from last 10 matches."""
    if not matches:
        return {"weighted_wr": 0.5, "n_matches": 0, "results": []}

    results = []
    for m in matches[:10]:
        won = m.get("winner_id") == team_id
        results.append("W" if won else "L")

    wins = results.count("W")
    return {
        "weighted_wr": round(wins / len(results), 3),
        "n_matches":   len(results),
        "results":     results,
    }
