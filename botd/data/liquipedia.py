"""
Liquipedia data scraper for CS2 and Valorant.

Liquipedia provides structured tournament data via its MediaWiki/Cargo API.
No authentication is required for read-only access; we respect their
rate limits (1 request per 2 seconds, User-Agent with contact info).

Data pulled and stored in liq_* SQLite tables:
  - Upcoming match schedule (next 7+ days), confirmed teams
  - Tournament brackets: who is eliminated / already qualified
  - Prize pool and tier (S/A/B/C)
  - Match format (Bo1 / Bo3 / Bo5)

Liquipedia API base URLs:
  CS2:      https://liquipedia.net/counterstrike/api.php
  Valorant: https://liquipedia.net/valorant/api.php

Cargo tables used:
  - MatchSchedule   upcoming and recent matches
  - Tournaments     tournament metadata
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from datetime import datetime, timedelta
from typing import Optional

import requests

from botd.storage.db import BotDStorage
from botd.config import LIQUIPEDIA_REQUEST_DELAY, DB_PATH

logger = logging.getLogger(__name__)

_GAME_APIS: dict[str, str] = {
    "cs2":   "https://liquipedia.net/counterstrike/api.php",
    "val":   "https://liquipedia.net/valorant/api.php",
    "dota2": "https://liquipedia.net/dota2/api.php",
    "lol":   "https://liquipedia.net/leagueoflegends/api.php",
}

_PRIZE_TIER_BREAKS: list[tuple[int, str]] = [
    (1_000_000, "S"),
    (200_000, "A"),
    (50_000, "B"),
    (10_000, "C"),
]


def _prize_to_tier(prize_str: str) -> str:
    """Estimate tier from prize pool string, e.g. '$1,000,000'."""
    if not prize_str:
        return "C"
    amount_match = re.search(r"([\d,]+)", prize_str.replace("$", ""))
    if not amount_match:
        return "C"
    amount = int(amount_match.group(1).replace(",", ""))
    for threshold, tier in _PRIZE_TIER_BREAKS:
        if amount >= threshold:
            return tier
    return "D"


def _normalize_format(raw: str) -> str:
    """Normalize match format string to Bo1/Bo3/Bo5."""
    raw = str(raw).lower().strip()
    if raw in ("1", "bo1", "best of 1"):
        return "Bo1"
    if raw in ("5", "bo5", "best of 5"):
        return "Bo5"
    return "Bo3"


def _make_match_id(game: str, team1: str, team2: str, dt_str: str) -> str:
    key = f"{game}|{team1}|{team2}|{dt_str}"
    return "liq_" + hashlib.md5(key.encode()).hexdigest()[:12]


def _make_tournament_id(game: str, name: str) -> str:
    key = f"{game}|{name}"
    return "liq_" + hashlib.md5(key.encode()).hexdigest()[:10]


class LiquipediaScraper:
    """
    Queries the Liquipedia MediaWiki Cargo API and populates liq_* tables.

    The Cargo API returns JSON and is far more reliable than HTML scraping.
    Rate limit: 1 request per 2 seconds maximum.
    """

    def __init__(self, db_path: str = DB_PATH):
        self.db = BotDStorage(db_path)
        self._delay = max(LIQUIPEDIA_REQUEST_DELAY, 2.0)
        self._last_request: dict[str, float] = {"cs2": 0.0, "val": 0.0}
        self._session = requests.Session()
        self._session.headers.update({
            # Liquipedia requires a descriptive User-Agent
            "User-Agent": (
                "BotD-EsportsTrader/1.0 (Kalshi trading research; "
                "contact: research@example.com)"
            ),
            "Accept": "application/json",
        })

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _api_get(self, game: str, params: dict) -> Optional[dict]:
        """Rate-limited GET to Liquipedia API. Returns JSON dict or None."""
        api_url = _GAME_APIS.get(game)
        if not api_url:
            logger.error("Unknown game: %s", game)
            return None

        elapsed = time.time() - self._last_request.get(game, 0.0)
        if elapsed < self._delay:
            time.sleep(self._delay - elapsed)

        params.setdefault("format", "json")
        try:
            resp = self._session.get(api_url, params=params, timeout=20)
            self._last_request[game] = time.time()
            if resp.status_code == 200:
                return resp.json()
            if resp.status_code == 429:
                logger.warning("Liquipedia %s rate-limited, sleeping 60s", game)
                time.sleep(60)
                return None
            logger.warning("Liquipedia %s -> HTTP %d", api_url, resp.status_code)
            return None
        except Exception as exc:
            logger.error("Liquipedia request error for %s: %s", game, exc)
            return None

    def _cargo_query(
        self, game: str, tables: str, fields: str, where: str = "",
        order_by: str = "", limit: int = 100, offset: int = 0,
    ) -> list[dict]:
        """Execute a Liquipedia Cargo query and return list of field dicts."""
        params = {
            "action": "cargoquery",
            "tables": tables,
            "fields": fields,
            "limit": str(limit),
            "offset": str(offset),
        }
        if where:
            params["where"] = where
        if order_by:
            params["order_by"] = order_by

        data = self._api_get(game, params)
        if not data:
            return []

        try:
            return [item["title"] for item in data.get("cargoquery", [])]
        except (KeyError, TypeError) as exc:
            logger.debug("Cargo parse error: %s", exc)
            return []

    # ------------------------------------------------------------------
    # Upcoming matches
    # ------------------------------------------------------------------

    def sync_upcoming_matches(self, game: str, days: int = 7) -> list[dict]:
        """
        Pull upcoming matches for the next `days` days from Liquipedia.
        Stores in liq_upcoming_matches.

        Args:
            game: 'cs2' or 'val'
            days: horizon in days

        Returns list of upcoming match dicts.
        """
        cache_where = f"game='{game}'"
        if self.db.is_fresh("liq_upcoming_matches", where=cache_where, max_age_hours=0.5):
            logger.info("Liquipedia %s upcoming matches are fresh", game)
            return self.db.get_upcoming_matches(game, days)

        logger.info("Fetching Liquipedia upcoming matches for %s (next %d days)", game, days)

        now_utc = datetime.utcnow()
        end_utc = now_utc + timedelta(days=days)
        now_str = now_utc.strftime("%Y-%m-%d %H:%M:%S")
        end_str = end_utc.strftime("%Y-%m-%d %H:%M:%S")

        rows = self._cargo_query(
            game=game,
            tables="MatchSchedule",
            fields=(
                "MatchId, Team1, Team2, DateTime_UTC, BestOf, "
                "Tournament, Stream, HasTime"
            ),
            where=f"DateTime_UTC >= '{now_str}' AND DateTime_UTC <= '{end_str}'",
            order_by="DateTime_UTC ASC",
            limit=200,
        )

        if not rows:
            # Fallback: try the wikitext parse approach
            return self._sync_upcoming_via_parse(game, days)

        matches = []
        for row in rows:
            try:
                team1 = (row.get("Team1") or "").strip()
                team2 = (row.get("Team2") or "").strip()
                if not team1 or not team2:
                    continue

                dt_raw = row.get("DateTime UTC") or row.get("DateTime_UTC") or ""
                dt_raw = dt_raw.strip()
                try:
                    match_dt = datetime.strptime(dt_raw, "%Y-%m-%d %H:%M:%S")
                    match_datetime = match_dt.isoformat(sep=" ", timespec="seconds")
                except ValueError:
                    match_datetime = dt_raw

                best_of_raw = row.get("BestOf") or row.get("Best of") or "3"
                match_format = _normalize_format(str(best_of_raw))

                tournament = (row.get("Tournament") or "").strip()
                stream = (row.get("Stream") or "").strip()

                match_id = _make_match_id(game, team1, team2, match_datetime)

                # Pull tournament info for prize/tier
                tourn_info = self._get_tournament_info(game, tournament)
                tier = tourn_info.get("tier", "C")
                prize_pool = tourn_info.get("prize_pool", "")

                match = {
                    "match_id": match_id,
                    "game": game,
                    "team1": team1,
                    "team2": team2,
                    "team1_id": "",
                    "team2_id": "",
                    "match_datetime": match_datetime,
                    "tournament": tournament,
                    "tournament_tier": tier,
                    "match_format": match_format,
                    "prize_pool": prize_pool,
                    "stream_url": stream,
                }
                self.db.upsert_upcoming_match(match)
                matches.append(match)
            except Exception as exc:
                logger.debug("Match row parse error: %s", exc)
                continue

        logger.info("Stored %d upcoming %s matches from Liquipedia Cargo", len(matches), game)
        return matches

    def _sync_upcoming_via_parse(self, game: str, days: int) -> list[dict]:
        """
        Fallback: parse the Liquipedia upcoming matches template via wikitext.
        Used when Cargo tables are unavailable.
        """
        logger.info("Using wikitext fallback for %s upcoming matches", game)
        data = self._api_get(game, {
            "action": "parse",
            "page": "Liquipedia:Upcoming_and_ongoing_matches",
            "prop": "wikitext",
        })
        if not data:
            return []

        wikitext = ""
        try:
            wikitext = data["parse"]["wikitext"]["*"]
        except (KeyError, TypeError):
            return []

        matches = []
        now = datetime.utcnow()
        cutoff = now + timedelta(days=days)

        # Parse {{MatchList|...}} templates
        pattern = re.compile(
            r"\|team1\s*=\s*([^\|\n]+).*?"
            r"\|team2\s*=\s*([^\|\n]+).*?"
            r"\|date\s*=\s*([^\|\n]+).*?"
            r"\|bestof\s*=\s*([^\|\n]+)",
            re.DOTALL,
        )

        for m in pattern.finditer(wikitext):
            try:
                team1 = m.group(1).strip()
                team2 = m.group(2).strip()
                date_str = m.group(3).strip()
                best_of = m.group(4).strip()

                match_format = _normalize_format(best_of)

                # Try to parse date
                for fmt in ("%B %d, %Y", "%Y-%m-%d", "%d %B %Y"):
                    try:
                        match_dt = datetime.strptime(date_str[:20], fmt)
                        break
                    except ValueError:
                        match_dt = now

                if not (now <= match_dt <= cutoff):
                    continue

                match_datetime = match_dt.isoformat(sep=" ", timespec="seconds")
                match_id = _make_match_id(game, team1, team2, match_datetime)

                match = {
                    "match_id": match_id,
                    "game": game,
                    "team1": team1,
                    "team2": team2,
                    "team1_id": "",
                    "team2_id": "",
                    "match_datetime": match_datetime,
                    "tournament": "",
                    "tournament_tier": "C",
                    "match_format": match_format,
                    "prize_pool": "",
                    "stream_url": "",
                }
                self.db.upsert_upcoming_match(match)
                matches.append(match)
            except Exception as exc:
                logger.debug("Wikitext parse error: %s", exc)
                continue

        logger.info("Wikitext fallback: stored %d upcoming %s matches", len(matches), game)
        return matches

    # ------------------------------------------------------------------
    # Tournament info
    # ------------------------------------------------------------------

    def _get_tournament_info(self, game: str, tournament_name: str) -> dict:
        """Fetch or retrieve cached tournament metadata (tier, prize pool)."""
        if not tournament_name:
            return {"tier": "C", "prize_pool": ""}

        tourn_id = _make_tournament_id(game, tournament_name)
        cached = self.db.execute(
            "SELECT * FROM liq_tournaments WHERE tournament_id=?", (tourn_id,)
        )
        if cached:
            return cached[0]

        return self._fetch_tournament_info(game, tournament_name, tourn_id)

    def _fetch_tournament_info(self, game: str, tournament_name: str, tourn_id: str) -> dict:
        """Pull tournament metadata from Liquipedia Cargo."""
        rows = self._cargo_query(
            game=game,
            tables="Tournaments",
            fields="Name, Prize, Tier, Startdate, Enddate, Location, Type",
            where=f"Name LIKE '%{tournament_name[:30]}%'",
            limit=1,
        )

        if rows:
            row = rows[0]
            prize_raw = row.get("Prize") or ""
            tier_raw = row.get("Tier") or ""
            tier = self._map_liquipedia_tier(tier_raw, prize_raw)

            tourn = {
                "tournament_id": tourn_id,
                "game": game,
                "name": tournament_name,
                "tier": tier,
                "prize_pool": prize_raw,
                "start_date": row.get("Startdate") or "",
                "end_date": row.get("Enddate") or "",
                "location": row.get("Location") or "",
            }
        else:
            tourn = {
                "tournament_id": tourn_id,
                "game": game,
                "name": tournament_name,
                "tier": "C",
                "prize_pool": "",
                "start_date": "",
                "end_date": "",
                "location": "",
            }

        self.db.upsert_tournament(tourn)
        return tourn

    def sync_tournaments(self, game: str, limit: int = 50) -> list[dict]:
        """Pull upcoming and ongoing tournaments from Liquipedia."""
        logger.info("Fetching Liquipedia tournament list for %s", game)
        now_str = datetime.utcnow().strftime("%Y-%m-%d")

        rows = self._cargo_query(
            game=game,
            tables="Tournaments",
            fields="Name, Prize, Tier, Startdate, Enddate, Location, Type",
            where=f"Enddate >= '{now_str}'",
            order_by="Startdate ASC",
            limit=limit,
        )

        tournaments = []
        for row in rows:
            try:
                name = (row.get("Name") or "").strip()
                prize_raw = (row.get("Prize") or "").strip()
                tier_raw = (row.get("Tier") or "").strip()
                tier = self._map_liquipedia_tier(tier_raw, prize_raw)

                tourn_id = _make_tournament_id(game, name)
                tourn = {
                    "tournament_id": tourn_id,
                    "game": game,
                    "name": name,
                    "tier": tier,
                    "prize_pool": prize_raw,
                    "start_date": (row.get("Startdate") or "").strip(),
                    "end_date": (row.get("Enddate") or "").strip(),
                    "location": (row.get("Location") or "").strip(),
                }
                self.db.upsert_tournament(tourn)
                tournaments.append(tourn)
            except Exception as exc:
                logger.debug("Tournament row parse error: %s", exc)
                continue

        logger.info("Stored %d %s tournaments", len(tournaments), game)
        return tournaments

    def _map_liquipedia_tier(self, tier_raw: str, prize_raw: str) -> str:
        """Map Liquipedia tier string to S/A/B/C."""
        t = tier_raw.lower().strip()
        if t in ("s", "s-tier", "premier", "major"):
            return "S"
        if t in ("a", "a-tier", "a1"):
            return "A"
        if t in ("b", "b-tier", "a2"):
            return "B"
        if t in ("c", "c-tier", "qualifier"):
            return "C"
        # Fallback to prize-based tier
        return _prize_to_tier(prize_raw)

    # ------------------------------------------------------------------
    # Bracket state
    # ------------------------------------------------------------------

    def sync_bracket_state(self, game: str, tournament_page: str) -> list[dict]:
        """
        Pull bracket/placement state for a tournament: who is eliminated,
        who has qualified, who is still playing.

        Args:
            game: 'cs2' or 'val'
            tournament_page: Liquipedia page name, e.g. 'ESL_Pro_League/Season_20'
        """
        logger.info("Fetching bracket for %s / %s", game, tournament_page)
        tourn_id = _make_tournament_id(game, tournament_page)

        rows = self._cargo_query(
            game=game,
            tables="Placements",
            fields="Team, Place, Qualified, Status",
            where=f"Tournament='{tournament_page}'",
            limit=100,
        )

        brackets = []
        for row in rows:
            try:
                team_name = (row.get("Team") or "").strip()
                place_raw = (row.get("Place") or "").strip()
                qualified = (row.get("Qualified") or "").lower() in ("1", "true", "yes")
                status_raw = (row.get("Status") or "").strip()

                if not team_name:
                    continue

                if "elim" in status_raw.lower() or place_raw.startswith("E"):
                    status = "eliminated"
                elif qualified or "qual" in status_raw.lower():
                    status = "qualified"
                else:
                    status = "playing"

                bracket = {
                    "tournament_id": tourn_id,
                    "game": game,
                    "team_name": team_name,
                    "status": status,
                    "stage": place_raw,
                    "updated_at": self.db.now(),
                }
                with self.db.conn() as c:
                    c.execute(
                        "INSERT OR REPLACE INTO liq_brackets "
                        "(tournament_id, game, team_name, status, stage, updated_at) "
                        "VALUES (?,?,?,?,?,?)",
                        list(bracket.values()),
                    )
                brackets.append(bracket)
            except Exception as exc:
                logger.debug("Bracket row parse error: %s", exc)
                continue

        logger.info("Stored %d bracket entries for %s", len(brackets), tournament_page)
        return brackets

    # ------------------------------------------------------------------
    # Full sync
    # ------------------------------------------------------------------

    def sync_all(self, days: int = 7) -> dict:
        """Sync upcoming matches and tournaments for all four supported games."""
        logger.info("Starting full Liquipedia sync (next %d days)", days)
        results = {}
        for game in ("cs2", "val", "dota2", "lol"):
            upcoming = self.sync_upcoming_matches(game, days)
            tournaments = self.sync_tournaments(game)
            results[game] = {
                "upcoming_matches": len(upcoming),
                "tournaments": len(tournaments),
            }
        return results

    # ------------------------------------------------------------------
    # Query helpers
    # ------------------------------------------------------------------

    def get_match_context(self, match_id: str) -> dict:
        """Return full context for a specific upcoming match."""
        rows = self.db.execute(
            "SELECT m.*, t.tier as t_tier, t.prize_pool as t_prize "
            "FROM liq_upcoming_matches m "
            "LEFT JOIN liq_tournaments t ON m.tournament = t.name "
            "WHERE m.match_id=?",
            (match_id,),
        )
        return rows[0] if rows else {}

    def get_team_bracket_status(self, team_name: str) -> list[dict]:
        """Return all bracket entries for a team across all tracked tournaments."""
        return self.db.execute(
            "SELECT b.*, t.name as tournament_name, t.prize_pool "
            "FROM liq_brackets b "
            "LEFT JOIN liq_tournaments t ON b.tournament_id = t.tournament_id "
            "WHERE b.team_name LIKE ?",
            (f"%{team_name}%",),
        )
