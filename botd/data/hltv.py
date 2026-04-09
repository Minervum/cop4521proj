"""
HLTV data scraper for CS2.

HLTV is the authoritative source for CS2 statistics. It uses Cloudflare
protection; we bypass it with cloudscraper and enforce a minimum delay
between requests.

Data pulled and stored in cs2_* SQLite tables:
  - Team world rankings (weekly)
  - Team match history (last 6 months)
  - Map win rates per team per map
  - Head-to-head record between any two teams
  - Recent form (last 10 matches, recency-weighted)
  - Player HLTV Rating 2.0
  - Roster changes (last 30 days)
  - Tournament tier (S / A / B / C)
"""

from __future__ import annotations

import logging
import re
import time
import hashlib
from datetime import datetime, timedelta
from typing import Optional
from urllib.parse import urljoin

try:
    import cloudscraper
    _SCRAPER_AVAILABLE = True
except ImportError:
    import requests
    _SCRAPER_AVAILABLE = False

from bs4 import BeautifulSoup

from botd.storage.db import BotDStorage
from botd.config import HLTV_REQUEST_DELAY, TOP_N_CS2_TEAMS, DB_PATH

logger = logging.getLogger(__name__)

BASE_URL = "https://www.hltv.org"

# Known tournament tier keywords (HLTV naming conventions)
_TIER_PATTERNS: list[tuple[str, str]] = [
    (r"(major|blast\s+premier\s+final|esl\s+one|iem\s+katowice|iem\s+cologne|iem\s+rio)", "S"),
    (r"(blast\s+premier|esl\s+pro\s+league|navi\s+nation|pgl|esea\s+premier)", "A"),
    (r"(esl\s+challenger|republeague|iem\s+road|dreamhack|cct)", "B"),
]


def _classify_tier(tournament_name: str) -> str:
    name_lower = tournament_name.lower()
    for pattern, tier in _TIER_PATTERNS:
        if re.search(pattern, name_lower):
            return tier
    return "C"


def _make_team_id(name: str, hltv_id: str = "") -> str:
    if hltv_id:
        return f"hltv_{hltv_id}"
    return "hltv_" + hashlib.md5(name.lower().encode()).hexdigest()[:8]


def _make_match_id(team1: str, team2: str, date_str: str) -> str:
    key = f"{team1}|{team2}|{date_str}"
    return "hltv_" + hashlib.md5(key.encode()).hexdigest()[:12]


class HLTVScraper:
    """
    Scrapes HLTV and populates cs2_* tables in the shared SQLite database.

    Usage:
        scraper = HLTVScraper()
        scraper.sync_rankings()
        scraper.sync_team("4608", "natus-vincere")
    """

    def __init__(self, db_path: str = DB_PATH):
        self.db = BotDStorage(db_path)
        self._delay = HLTV_REQUEST_DELAY
        self._last_request = 0.0

        if _SCRAPER_AVAILABLE:
            self._session = cloudscraper.create_scraper(
                browser={"browser": "chrome", "platform": "windows", "mobile": False}
            )
        else:
            import requests
            self._session = requests.Session()

        self._session.headers.update({
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
            "Accept-Language": "en-US,en;q=0.9",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Referer": "https://www.hltv.org/",
        })

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _get(self, path: str, params: dict = None) -> Optional[BeautifulSoup]:
        """Rate-limited GET returning a BeautifulSoup or None on failure."""
        elapsed = time.time() - self._last_request
        if elapsed < self._delay:
            time.sleep(self._delay - elapsed)

        url = urljoin(BASE_URL, path)
        try:
            resp = self._session.get(url, params=params, timeout=20)
            self._last_request = time.time()
            if resp.status_code == 200:
                return BeautifulSoup(resp.text, "lxml")
            if resp.status_code == 429:
                logger.warning("HLTV rate-limited, sleeping 30s")
                time.sleep(30)
                return None
            logger.warning("HLTV %s -> HTTP %d", url, resp.status_code)
            return None
        except Exception as exc:
            logger.error("HLTV request error for %s: %s", url, exc)
            return None

    # ------------------------------------------------------------------
    # Rankings
    # ------------------------------------------------------------------

    def sync_rankings(self) -> list[dict]:
        """
        Pull the current HLTV world rankings and store in cs2_teams.
        Returns list of team dicts.
        """
        if self.db.is_fresh("cs2_teams", max_age_hours=24):
            logger.info("CS2 rankings are fresh, skipping fetch")
            return self.db.execute("SELECT * FROM cs2_teams ORDER BY ranking")

        logger.info("Fetching HLTV team rankings")
        soup = self._get("/ranking/teams/")
        if not soup:
            return []

        teams = []
        for item in soup.select(".ranked-team.standard-box"):
            try:
                position_el = item.select_one(".position")
                name_el = item.select_one(".name")
                points_el = item.select_one(".points")
                hltv_id_el = item.select_one("a[href*='/team/']")

                if not (position_el and name_el):
                    continue

                ranking = int(re.sub(r"[^0-9]", "", position_el.text.strip()))
                name = name_el.text.strip()

                points_text = points_el.text.strip() if points_el else "0"
                points_match = re.search(r"([\d,]+)", points_text)
                points = float(points_match.group(1).replace(",", "")) if points_match else 0.0

                hltv_id = ""
                if hltv_id_el:
                    href = hltv_id_el.get("href", "")
                    id_match = re.search(r"/team/(\d+)/", href)
                    if id_match:
                        hltv_id = id_match.group(1)

                country_el = item.select_one(".country img")
                country = country_el.get("title", "") if country_el else ""

                team_id = _make_team_id(name, hltv_id)
                team = {
                    "team_id": team_id,
                    "name": name,
                    "ranking": ranking,
                    "ranking_points": points,
                    "country": country,
                }
                self.db.upsert_cs2_team(team)
                teams.append(team)

                if len(teams) >= TOP_N_CS2_TEAMS:
                    break
            except Exception as exc:
                logger.debug("Error parsing ranking row: %s", exc)
                continue

        logger.info("Stored %d CS2 team rankings", len(teams))
        return teams

    # ------------------------------------------------------------------
    # Match history
    # ------------------------------------------------------------------

    def sync_match_history(self, team_id: str, team_slug: str, hltv_id: str, days: int = 180) -> list[dict]:
        """
        Pull match history for a team from HLTV stats page.
        Stores in cs2_matches, updates cs2_h2h.
        """
        if self.db.is_fresh(
            "cs2_matches",
            where=f"team1_id='{team_id}' OR team2_id='{team_id}'",
            max_age_hours=6,
        ):
            return self.db.get_cs2_matches(team_id, days)

        start_date = (datetime.utcnow() - timedelta(days=days)).strftime("%Y-%m-%d")
        logger.info("Fetching match history for %s (hltv_id=%s)", team_slug, hltv_id)

        soup = self._get(
            f"/stats/teams/matches/{hltv_id}/{team_slug}",
            params={"startDate": start_date, "rankingFilter": "Top50"},
        )
        if not soup:
            return []

        matches = []
        table = soup.select_one("table.stats-table")
        if not table:
            # Fallback: try results page
            return self._sync_results_page(team_id, team_slug, hltv_id, days)

        for row in table.select("tbody tr"):
            try:
                cells = row.select("td")
                if len(cells) < 7:
                    continue

                date_el = cells[0].select_one("a") or cells[0]
                date_text = date_el.text.strip()
                try:
                    match_date = datetime.strptime(date_text, "%Y-%m-%d").strftime("%Y-%m-%d")
                except ValueError:
                    continue

                opponent_el = cells[1].select_one("a")
                opp_name = opponent_el.text.strip() if opponent_el else cells[1].text.strip()

                opponent_href = opponent_el.get("href", "") if opponent_el else ""
                opp_id_match = re.search(r"/team/(\d+)/", opponent_href)
                opp_hltv_id = opp_id_match.group(1) if opp_id_match else ""
                opp_id = _make_team_id(opp_name, opp_hltv_id)

                score_text = cells[2].text.strip()
                score_match = re.match(r"(\d+)\s*[:-]\s*(\d+)", score_text)
                our_score = int(score_match.group(1)) if score_match else 0
                their_score = int(score_match.group(2)) if score_match else 0

                result_el = cells[2]
                won = (
                    "won" in (result_el.get("class") or [])
                    or our_score > their_score
                    or "win" in score_text.lower()
                )

                tournament_el = cells[4] if len(cells) > 4 else None
                tournament = tournament_el.text.strip() if tournament_el else ""
                tier = _classify_tier(tournament)

                fmt_el = cells[5] if len(cells) > 5 else None
                match_format = fmt_el.text.strip().replace("-", "") if fmt_el else "Bo3"
                if match_format not in ("Bo1", "Bo3", "Bo5"):
                    match_format = "Bo3"

                match_id = _make_match_id(
                    team_id, opp_id, match_date
                )

                winner_id = team_id if won else opp_id
                match = {
                    "match_id": match_id,
                    "team1_id": team_id,
                    "team2_id": opp_id,
                    "team1_name": team_slug.replace("-", " ").title(),
                    "team2_name": opp_name,
                    "team1_score": our_score,
                    "team2_score": their_score,
                    "winner_id": winner_id,
                    "tournament": tournament,
                    "tournament_tier": tier,
                    "match_format": match_format,
                    "match_date": match_date,
                }
                self.db.upsert_cs2_match(match)
                matches.append(match)
            except Exception as exc:
                logger.debug("Error parsing match row: %s", exc)
                continue

        logger.info("Stored %d matches for %s", len(matches), team_slug)
        self._update_h2h_from_matches(team_id, matches)
        return matches

    def _sync_results_page(self, team_id: str, team_slug: str, hltv_id: str, days: int) -> list[dict]:
        """Fallback: parse /results page filtered by team."""
        start_date = (datetime.utcnow() - timedelta(days=days)).strftime("%Y-%m-%d")
        soup = self._get(
            "/results",
            params={"startDate": start_date, "team": hltv_id, "content": "stats"},
        )
        if not soup:
            return []

        matches = []
        for result in soup.select(".result-con"):
            try:
                teams = result.select(".team-cell .team")
                if len(teams) < 2:
                    continue

                team1_el = teams[0]
                team2_el = teams[1]

                t1_name = team1_el.text.strip()
                t2_name = team2_el.text.strip()

                score_els = result.select(".result-score span")
                t1_score = int(score_els[0].text.strip()) if len(score_els) > 0 else 0
                t2_score = int(score_els[1].text.strip()) if len(score_els) > 1 else 0

                date_el = result.select_one(".date")
                date_text = date_el.text.strip() if date_el else ""
                try:
                    match_date = datetime.strptime(date_text, "%d/%m/%Y").strftime("%Y-%m-%d")
                except ValueError:
                    match_date = datetime.utcnow().strftime("%Y-%m-%d")

                event_el = result.select_one(".event-name")
                tournament = event_el.text.strip() if event_el else ""
                tier = _classify_tier(tournament)

                t1_id = _make_team_id(t1_name)
                t2_id = _make_team_id(t2_name)

                winner_id = t1_id if t1_score > t2_score else t2_id
                match_id = _make_match_id(t1_id, t2_id, match_date)

                match = {
                    "match_id": match_id,
                    "team1_id": t1_id,
                    "team2_id": t2_id,
                    "team1_name": t1_name,
                    "team2_name": t2_name,
                    "team1_score": t1_score,
                    "team2_score": t2_score,
                    "winner_id": winner_id,
                    "tournament": tournament,
                    "tournament_tier": tier,
                    "match_format": "Bo3",
                    "match_date": match_date,
                }
                self.db.upsert_cs2_match(match)
                matches.append(match)
            except Exception as exc:
                logger.debug("Results page parse error: %s", exc)
                continue

        self._update_h2h_from_matches(team_id, matches)
        return matches

    # ------------------------------------------------------------------
    # Map statistics
    # ------------------------------------------------------------------

    def sync_map_stats(self, team_id: str, team_slug: str, hltv_id: str, days: int = 180) -> list[dict]:
        """Pull per-map win rates from HLTV team stats page."""
        if self.db.is_fresh(
            "cs2_map_stats",
            where=f"team_id='{team_id}'",
            max_age_hours=24,
        ):
            return self.db.get_cs2_map_stats(team_id)

        start_date = (datetime.utcnow() - timedelta(days=days)).strftime("%Y-%m-%d")
        logger.info("Fetching map stats for %s", team_slug)

        soup = self._get(
            f"/stats/teams/maps/{hltv_id}/{team_slug}",
            params={"startDate": start_date},
        )
        if not soup:
            return []

        stats = []
        for row in soup.select("table.stats-table tbody tr"):
            try:
                cells = row.select("td")
                if len(cells) < 4:
                    continue

                map_name = cells[0].text.strip()
                wins_text = cells[1].text.strip()
                losses_text = cells[2].text.strip()

                wins = int(re.sub(r"[^0-9]", "", wins_text)) if re.search(r"\d", wins_text) else 0
                losses = int(re.sub(r"[^0-9]", "", losses_text)) if re.search(r"\d", losses_text) else 0

                total = wins + losses
                win_rate = wins / total if total > 0 else 0.0

                # CT/T side win rates if available
                ct_rate = 0.0
                t_rate = 0.0
                if len(cells) >= 6:
                    ct_text = cells[4].text.strip()
                    t_text = cells[5].text.strip()
                    ct_match = re.search(r"([\d.]+)%?", ct_text)
                    t_match = re.search(r"([\d.]+)%?", t_text)
                    if ct_match:
                        ct_rate = float(ct_match.group(1)) / 100 if float(ct_match.group(1)) > 1 else float(ct_match.group(1))
                    if t_match:
                        t_rate = float(t_match.group(1)) / 100 if float(t_match.group(1)) > 1 else float(t_match.group(1))

                stat = {
                    "team_id": team_id,
                    "map_name": map_name,
                    "wins": wins,
                    "losses": losses,
                    "win_rate": round(win_rate, 4),
                    "ct_win_rate": round(ct_rate, 4),
                    "t_win_rate": round(t_rate, 4),
                }
                self.db.upsert_cs2_map_stats(stat)
                stats.append(stat)
            except Exception as exc:
                logger.debug("Map stats parse error: %s", exc)
                continue

        logger.info("Stored %d map stats for %s", len(stats), team_slug)
        return stats

    # ------------------------------------------------------------------
    # Player ratings (HLTV Rating 2.0)
    # ------------------------------------------------------------------

    def sync_player_stats(self, team_id: str, team_slug: str, hltv_id: str, days: int = 90) -> list[dict]:
        """Pull HLTV Rating 2.0 and related stats for players on a team."""
        if self.db.is_fresh(
            "cs2_player_stats",
            where=f"team_id='{team_id}'",
            max_age_hours=24,
        ):
            return self.db.get_cs2_players(team_id)

        start_date = (datetime.utcnow() - timedelta(days=days)).strftime("%Y-%m-%d")
        logger.info("Fetching player stats for %s", team_slug)

        soup = self._get(
            "/stats/players",
            params={
                "startDate": start_date,
                "rankingFilter": "Top50",
                "team": hltv_id,
            },
        )
        if not soup:
            return []

        players = []
        for row in soup.select("table.stats-table tbody tr"):
            try:
                cells = row.select("td")
                if len(cells) < 8:
                    continue

                name_el = cells[0].select_one("a") or cells[0]
                player_name = name_el.text.strip()

                player_href = (cells[0].select_one("a") or {}).get("href", "")
                pid_match = re.search(r"/player/(\d+)/", player_href)
                player_id = f"hltv_{pid_match.group(1)}" if pid_match else _make_team_id(player_name)

                def safe_float(cell_text: str) -> float:
                    match = re.search(r"([\d.]+)", cell_text.strip())
                    return float(match.group(1)) if match else 0.0

                rating = safe_float(cells[2].text)
                kd = safe_float(cells[4].text)
                impact = safe_float(cells[5].text) if len(cells) > 5 else 0.0
                adr = safe_float(cells[6].text) if len(cells) > 6 else 0.0
                kast = safe_float(cells[7].text) if len(cells) > 7 else 0.0
                if kast > 1.0:
                    kast = kast / 100.0

                player = {
                    "player_id": player_id,
                    "player_name": player_name,
                    "team_id": team_id,
                    "rating": rating,
                    "kd_ratio": kd,
                    "impact": impact,
                    "adr": adr,
                    "kast": kast,
                }
                self.db.upsert_cs2_player(player)
                players.append(player)
            except Exception as exc:
                logger.debug("Player parse error: %s", exc)
                continue

        logger.info("Stored %d players for %s", len(players), team_slug)
        return players

    # ------------------------------------------------------------------
    # Roster changes
    # ------------------------------------------------------------------

    def sync_roster_changes(self, days: int = 30) -> list[dict]:
        """Pull recent roster changes from the HLTV roster changes page."""
        logger.info("Fetching HLTV roster changes (last %d days)", days)
        soup = self._get("/roster-changes")
        if not soup:
            return []

        cutoff = datetime.utcnow() - timedelta(days=days)
        changes = []

        for item in soup.select(".roster-change-box"):
            try:
                date_el = item.select_one(".date")
                date_text = date_el.text.strip() if date_el else ""
                try:
                    change_date = datetime.strptime(date_text, "%d %b %Y")
                except ValueError:
                    try:
                        change_date = datetime.strptime(date_text, "%Y-%m-%d")
                    except ValueError:
                        continue

                if change_date < cutoff:
                    continue

                player_el = item.select_one(".player-name") or item.select_one("a[href*='/player/']")
                player_name = player_el.text.strip() if player_el else "Unknown"

                player_href = (player_el or {}).get("href", "") if player_el else ""
                pid_match = re.search(r"/player/(\d+)/", player_href)
                player_id = f"hltv_{pid_match.group(1)}" if pid_match else _make_team_id(player_name)

                from_el = item.select_one(".from-team a")
                to_el = item.select_one(".to-team a")

                from_name = from_el.text.strip() if from_el else ""
                to_name = to_el.text.strip() if to_el else ""

                from_href = (from_el or {}).get("href", "") if from_el else ""
                to_href = (to_el or {}).get("href", "") if to_el else ""

                from_id_m = re.search(r"/team/(\d+)/", from_href)
                to_id_m = re.search(r"/team/(\d+)/", to_href)

                from_id = _make_team_id(from_name, from_id_m.group(1) if from_id_m else "")
                to_id = _make_team_id(to_name, to_id_m.group(1) if to_id_m else "")

                change_type = "join" if to_name else "leave"
                if "bench" in item.text.lower():
                    change_type = "inactive"

                change = {
                    "player_id": player_id,
                    "player_name": player_name,
                    "from_team_id": from_id,
                    "from_team_name": from_name,
                    "to_team_id": to_id,
                    "to_team_name": to_name,
                    "change_type": change_type,
                    "change_date": change_date.strftime("%Y-%m-%d"),
                }
                self.db.insert_cs2_roster_change(change)
                changes.append(change)
            except Exception as exc:
                logger.debug("Roster change parse error: %s", exc)
                continue

        logger.info("Stored %d CS2 roster changes", len(changes))
        return changes

    # ------------------------------------------------------------------
    # H2H computation
    # ------------------------------------------------------------------

    def _update_h2h_from_matches(self, team_id: str, matches: list[dict]):
        """Build H2H records from a batch of match dicts."""
        h2h_map: dict[str, dict] = {}
        for m in matches:
            opp_id = m["team2_id"] if m["team1_id"] == team_id else m["team1_id"]
            key = tuple(sorted([team_id, opp_id]))
            if key not in h2h_map:
                h2h_map[key] = {"team1_id": key[0], "team2_id": key[1], "team1_wins": 0, "team2_wins": 0, "total_matches": 0}
            h2h_map[key]["total_matches"] += 1
            if m["winner_id"] == key[0]:
                h2h_map[key]["team1_wins"] += 1
            elif m["winner_id"] == key[1]:
                h2h_map[key]["team2_wins"] += 1

        for h2h in h2h_map.values():
            self.db.upsert_cs2_h2h(h2h)

    def compute_h2h(self, team1_id: str, team2_id: str) -> dict:
        """Return H2H record between two teams from stored match history."""
        existing = self.db.get_cs2_h2h(team1_id, team2_id)
        if existing:
            return existing

        # Build from stored matches
        matches = self.db.execute(
            "SELECT * FROM cs2_matches WHERE "
            "(team1_id=? AND team2_id=?) OR (team1_id=? AND team2_id=?)",
            (team1_id, team2_id, team2_id, team1_id),
        )
        key = tuple(sorted([team1_id, team2_id]))
        h2h = {"team1_id": key[0], "team2_id": key[1], "team1_wins": 0, "team2_wins": 0, "total_matches": len(matches)}
        for m in matches:
            if m["winner_id"] == key[0]:
                h2h["team1_wins"] += 1
            elif m["winner_id"] == key[1]:
                h2h["team2_wins"] += 1
        self.db.upsert_cs2_h2h(h2h)
        return h2h

    def compute_recent_form(
        self, team_id: str, n: int = 10, decay: float = 0.85
    ) -> dict:
        """
        Compute a recency-weighted win rate over the last n matches.

        Returns dict with: weighted_wr, raw_wr, n_matches, results (list of W/L)
        """
        matches = self.db.get_cs2_matches(team_id, days=180)[:n]
        if not matches:
            return {"weighted_wr": 0.5, "raw_wr": 0.5, "n_matches": 0, "results": []}

        total_weight = 0.0
        weighted_wins = 0.0
        raw_wins = 0
        results = []

        for i, m in enumerate(matches):
            weight = (decay ** i)
            tier_mult = 1.0
            tier = m.get("tournament_tier", "C")
            tier_weights = {"S": 1.5, "A": 1.2, "B": 1.0, "C": 0.7}
            tier_mult = tier_weights.get(tier, 0.7)
            w = weight * tier_mult

            won = m["winner_id"] == team_id
            results.append("W" if won else "L")
            total_weight += w
            if won:
                weighted_wins += w
                raw_wins += 1

        weighted_wr = weighted_wins / total_weight if total_weight > 0 else 0.5
        raw_wr = raw_wins / len(matches) if matches else 0.5

        return {
            "weighted_wr": round(weighted_wr, 4),
            "raw_wr": round(raw_wr, 4),
            "n_matches": len(matches),
            "results": results,
        }

    # ------------------------------------------------------------------
    # Full sync for top N teams
    # ------------------------------------------------------------------

    def sync_all(self, top_n: int = TOP_N_CS2_TEAMS) -> dict:
        """
        Full data sync: rankings + match history + map stats + players + roster changes.
        Iterates over top N ranked teams.
        Returns summary dict.
        """
        logger.info("Starting full HLTV sync for top %d teams", top_n)
        teams = self.sync_rankings()[:top_n]
        self.sync_roster_changes(days=30)

        synced = 0
        for team in teams:
            name = team["name"]
            team_id = team["team_id"]
            # Derive HLTV ID from team_id (format: hltv_XXXXXX)
            hltv_id = team_id.replace("hltv_", "")
            # Derive slug from name
            slug = re.sub(r"[^a-z0-9-]", "", name.lower().replace(" ", "-"))

            try:
                self.sync_match_history(team_id, slug, hltv_id)
                self.sync_map_stats(team_id, slug, hltv_id)
                self.sync_player_stats(team_id, slug, hltv_id)
                synced += 1
            except Exception as exc:
                logger.error("Error syncing team %s: %s", name, exc)

        return {"teams_synced": synced, "rankings": len(teams)}

    # ------------------------------------------------------------------
    # Quick sample output for one match
    # ------------------------------------------------------------------

    def match_snapshot(self, team1_id: str, team2_id: str) -> dict:
        """Return all stats needed to evaluate one CS2 match."""
        t1 = self.db.get_cs2_team(team1_id) or {}
        t2 = self.db.get_cs2_team(team2_id) or {}
        h2h = self.compute_h2h(team1_id, team2_id)
        form1 = self.compute_recent_form(team1_id)
        form2 = self.compute_recent_form(team2_id)
        maps1 = self.db.get_cs2_map_stats(team1_id)
        maps2 = self.db.get_cs2_map_stats(team2_id)
        players1 = self.db.get_cs2_players(team1_id)
        players2 = self.db.get_cs2_players(team2_id)
        roster_changes1 = self.db.get_recent_roster_changes(team1_id)
        roster_changes2 = self.db.get_recent_roster_changes(team2_id)

        avg_rating1 = (
            sum(p["rating"] for p in players1 if p.get("rating")) / len(players1)
            if players1 else 0.0
        )
        avg_rating2 = (
            sum(p["rating"] for p in players2 if p.get("rating")) / len(players2)
            if players2 else 0.0
        )

        return {
            "team1": {
                **t1,
                "form": form1,
                "map_stats": maps1[:5],
                "avg_player_rating": round(avg_rating1, 3),
                "recent_roster_changes": roster_changes1,
            },
            "team2": {
                **t2,
                "form": form2,
                "map_stats": maps2[:5],
                "avg_player_rating": round(avg_rating2, 3),
                "recent_roster_changes": roster_changes2,
            },
            "h2h": h2h,
        }
