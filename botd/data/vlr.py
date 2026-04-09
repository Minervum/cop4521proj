"""
VLR.gg data scraper for Valorant.

VLR.gg is the authoritative statistical source for professional Valorant.
Data is scraped from HTML pages with BeautifulSoup.

Data pulled and stored in val_* SQLite tables:
  - Team rankings (global and regional)
  - Team match history (last 6 months)
  - Map win rates per team per map
  - Agent composition win rates
  - Head-to-head records
  - Recent form (last 10 matches, recency-weighted)
  - Player ACS (Average Combat Score) and related ratings
  - Roster changes (last 30 days)
"""

from __future__ import annotations

import hashlib
import logging
import re
import time
from datetime import datetime, timedelta
from typing import Optional
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from botd.storage.db import BotDStorage
from botd.config import VLR_REQUEST_DELAY, TOP_N_VAL_TEAMS, DB_PATH

logger = logging.getLogger(__name__)

BASE_URL = "https://www.vlr.gg"

_VLR_TIER_MAP: dict[str, str] = {
    "vct": "S",
    "champions": "S",
    "masters": "S",
    "lock//in": "S",
    "ascension": "A",
    "game changers": "A",
    "challengers": "B",
    "open": "C",
}


def _classify_tier(tournament_name: str) -> str:
    name_lower = tournament_name.lower()
    for keyword, tier in _VLR_TIER_MAP.items():
        if keyword in name_lower:
            return tier
    return "C"


def _make_team_id(name: str, vlr_id: str = "") -> str:
    if vlr_id:
        return f"vlr_{vlr_id}"
    return "vlr_" + hashlib.md5(name.lower().encode()).hexdigest()[:8]


def _make_match_id(team1_id: str, team2_id: str, date_str: str) -> str:
    key = f"{team1_id}|{team2_id}|{date_str}"
    return "vlr_" + hashlib.md5(key.encode()).hexdigest()[:12]


def _safe_float(text: str, default: float = 0.0) -> float:
    m = re.search(r"([\d.]+)", text.strip())
    return float(m.group(1)) if m else default


class VLRScraper:
    """
    Scrapes VLR.gg and populates val_* tables in the shared SQLite database.

    Usage:
        scraper = VLRScraper()
        scraper.sync_rankings()
        scraper.sync_team_matches("vlr_1234", "loud")
    """

    def __init__(self, db_path: str = DB_PATH):
        self.db = BotDStorage(db_path)
        self._delay = VLR_REQUEST_DELAY
        self._last_request = 0.0
        self._session = requests.Session()
        self._session.headers.update({
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": "https://www.vlr.gg/",
        })

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _get(self, path: str, params: dict = None) -> Optional[BeautifulSoup]:
        elapsed = time.time() - self._last_request
        if elapsed < self._delay:
            time.sleep(self._delay - elapsed)

        url = urljoin(BASE_URL, path) if not path.startswith("http") else path
        try:
            resp = self._session.get(url, params=params, timeout=20)
            self._last_request = time.time()
            if resp.status_code == 200:
                return BeautifulSoup(resp.text, "lxml")
            if resp.status_code == 429:
                logger.warning("VLR rate-limited, sleeping 30s")
                time.sleep(30)
                return None
            logger.warning("VLR %s -> HTTP %d", url, resp.status_code)
            return None
        except Exception as exc:
            logger.error("VLR request error for %s: %s", url, exc)
            return None

    # ------------------------------------------------------------------
    # Rankings
    # ------------------------------------------------------------------

    def sync_rankings(self, region: str = "world") -> list[dict]:
        """Pull VLR.gg global team rankings and store in val_teams."""
        if self.db.is_fresh("val_teams", max_age_hours=24):
            logger.info("Valorant rankings are fresh, skipping fetch")
            return self.db.execute("SELECT * FROM val_teams ORDER BY ranking")

        logger.info("Fetching VLR.gg %s rankings", region)
        path = f"/rankings/{region}" if region != "world" else "/rankings"
        soup = self._get(path)
        if not soup:
            return []

        teams = []
        rank = 1
        for item in soup.select(".ranked-item"):
            try:
                name_el = item.select_one(".team-name") or item.select_one(".text-of")
                if not name_el:
                    continue
                name = name_el.text.strip()

                link_el = item.select_one("a[href*='/team/']")
                vlr_id = ""
                if link_el:
                    href = link_el.get("href", "")
                    id_match = re.search(r"/team/(\d+)/", href)
                    if id_match:
                        vlr_id = id_match.group(1)

                region_el = item.select_one(".rank-item-tag") or item.select_one(".flag")
                team_region = region_el.text.strip() if region_el else region

                team_id = _make_team_id(name, vlr_id)
                team = {
                    "team_id": team_id,
                    "name": name,
                    "ranking": rank,
                    "region": team_region,
                }
                self.db.upsert_val_team(team)
                teams.append(team)
                rank += 1

                if rank > TOP_N_VAL_TEAMS:
                    break
            except Exception as exc:
                logger.debug("Rankings parse error: %s", exc)
                continue

        logger.info("Stored %d Valorant team rankings", len(teams))
        return teams

    # ------------------------------------------------------------------
    # Match history
    # ------------------------------------------------------------------

    def sync_team_matches(self, team_id: str, team_slug: str, vlr_id: str, days: int = 180) -> list[dict]:
        """Pull match history for a team from VLR results page."""
        if self.db.is_fresh(
            "val_matches",
            where=f"team1_id='{team_id}' OR team2_id='{team_id}'",
            max_age_hours=6,
        ):
            return self.db.get_val_matches(team_id, days)

        logger.info("Fetching match history for %s (vlr_id=%s)", team_slug, vlr_id)
        cutoff = datetime.utcnow() - timedelta(days=days)
        page = 1
        matches = []

        while True:
            soup = self._get(
                f"/team/{vlr_id}/{team_slug}",
                params={"tab": "results", "page": str(page)},
            )
            if not soup:
                break

            rows = soup.select(".m-item")
            if not rows:
                # Try alternate selector
                rows = soup.select("a.match-item")
            if not rows:
                break

            found_any = False
            for row in rows:
                try:
                    date_el = row.select_one(".m-item-date") or row.select_one(".match-item-time")
                    date_text = date_el.text.strip() if date_el else ""
                    try:
                        match_date = datetime.strptime(date_text, "%Y/%m/%d")
                    except ValueError:
                        try:
                            match_date = datetime.strptime(date_text, "%m/%d/%Y")
                        except ValueError:
                            match_date = datetime.utcnow()

                    if match_date < cutoff:
                        continue

                    team_els = row.select(".m-item-team") or row.select(".match-item-vs-team")
                    if len(team_els) < 2:
                        continue

                    t1_name = team_els[0].text.strip()
                    t2_name = team_els[1].text.strip()

                    score_els = row.select(".m-item-result span") or row.select(".match-item-score span")
                    t1_score = int(re.sub(r"[^0-9]", "", score_els[0].text)) if len(score_els) > 0 else 0
                    t2_score = int(re.sub(r"[^0-9]", "", score_els[1].text)) if len(score_els) > 1 else 0

                    event_el = row.select_one(".m-item-event") or row.select_one(".match-item-event")
                    tournament = event_el.text.strip() if event_el else ""
                    tier = _classify_tier(tournament)

                    fmt_el = row.select_one(".m-item-bo") or row.select_one(".match-item-meta")
                    fmt_text = fmt_el.text.strip() if fmt_el else "bo3"
                    if "1" in fmt_text:
                        match_format = "Bo1"
                    elif "5" in fmt_text:
                        match_format = "Bo5"
                    else:
                        match_format = "Bo3"

                    t1_id = _make_team_id(t1_name)
                    t2_id = _make_team_id(t2_name)
                    winner_id = t1_id if t1_score > t2_score else t2_id
                    match_id = _make_match_id(t1_id, t2_id, match_date.strftime("%Y-%m-%d"))

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
                        "match_format": match_format,
                        "match_date": match_date.strftime("%Y-%m-%d"),
                    }
                    self.db.upsert_val_match(match)
                    matches.append(match)
                    found_any = True
                except Exception as exc:
                    logger.debug("VLR match row parse error: %s", exc)
                    continue

            if not found_any or len(rows) < 10:
                break
            page += 1
            if page > 10:  # Limit pages per team
                break

        logger.info("Stored %d val matches for %s", len(matches), team_slug)
        self._update_h2h_from_matches(team_id, matches)
        return matches

    # ------------------------------------------------------------------
    # Map statistics
    # ------------------------------------------------------------------

    def sync_map_stats(self, team_id: str, vlr_id: str) -> list[dict]:
        """Pull per-map win rates from VLR team stats."""
        if self.db.is_fresh(
            "val_map_stats",
            where=f"team_id='{team_id}'",
            max_age_hours=24,
        ):
            return self.db.execute(
                "SELECT * FROM val_map_stats WHERE team_id=? ORDER BY win_rate DESC",
                (team_id,),
            )

        logger.info("Fetching VLR map stats for team_id=%s", vlr_id)
        soup = self._get(f"/team/{vlr_id}", params={"tab": "maps"})
        if not soup:
            return []

        stats = []
        for row in soup.select(".stats-table-row, .map-row"):
            try:
                cells = row.select("td, .stats-td")
                if len(cells) < 3:
                    continue

                map_name = cells[0].text.strip()
                if not map_name or map_name.lower() in ("map", "total"):
                    continue

                wins_text = cells[1].text.strip()
                losses_text = cells[2].text.strip()

                wins = int(re.sub(r"[^0-9]", "", wins_text)) if re.search(r"\d", wins_text) else 0
                losses = int(re.sub(r"[^0-9]", "", losses_text)) if re.search(r"\d", losses_text) else 0
                total = wins + losses
                win_rate = wins / total if total > 0 else 0.0

                atk_rate = 0.0
                def_rate = 0.0
                if len(cells) >= 5:
                    atk_rate = _safe_float(cells[3].text)
                    def_rate = _safe_float(cells[4].text)
                    if atk_rate > 1.0:
                        atk_rate /= 100.0
                    if def_rate > 1.0:
                        def_rate /= 100.0

                stat = {
                    "team_id": team_id,
                    "map_name": map_name,
                    "wins": wins,
                    "losses": losses,
                    "win_rate": round(win_rate, 4),
                    "atk_win_rate": round(atk_rate, 4),
                    "def_win_rate": round(def_rate, 4),
                }
                self.db.upsert_val_map_stats(stat)
                stats.append(stat)
            except Exception as exc:
                logger.debug("VLR map stat parse error: %s", exc)
                continue

        return stats

    # ------------------------------------------------------------------
    # Agent composition stats
    # ------------------------------------------------------------------

    def sync_agent_stats(self, team_id: str, vlr_id: str) -> list[dict]:
        """Pull agent pick/win rates for a team."""
        if self.db.is_fresh(
            "val_agent_stats",
            where=f"team_id='{team_id}'",
            max_age_hours=24,
        ):
            return self.db.execute(
                "SELECT * FROM val_agent_stats WHERE team_id=? ORDER BY win_rate DESC",
                (team_id,),
            )

        logger.info("Fetching agent stats for vlr_id=%s", vlr_id)
        soup = self._get(f"/team/{vlr_id}", params={"tab": "agents"})
        if not soup:
            return []

        stats = []
        for row in soup.select(".stats-table-row"):
            try:
                cells = row.select("td")
                if len(cells) < 3:
                    continue

                agent_el = cells[0].select_one("img")
                agent_name = agent_el.get("alt", "").strip() if agent_el else cells[0].text.strip()
                if not agent_name:
                    continue

                times_played = int(re.sub(r"[^0-9]", "", cells[1].text)) if re.search(r"\d", cells[1].text) else 0
                win_rate_text = cells[2].text.strip()
                win_rate = _safe_float(win_rate_text)
                if win_rate > 1.0:
                    win_rate /= 100.0

                avg_acs = _safe_float(cells[3].text) if len(cells) > 3 else 0.0

                stat = {
                    "team_id": team_id,
                    "agent_name": agent_name,
                    "times_played": times_played,
                    "win_rate": round(win_rate, 4),
                    "avg_acs": avg_acs,
                }
                self.db.upsert_val_agent_stats(stat)
                stats.append(stat)
            except Exception as exc:
                logger.debug("Agent stat parse error: %s", exc)
                continue

        return stats

    # ------------------------------------------------------------------
    # Player statistics (ACS)
    # ------------------------------------------------------------------

    def sync_player_stats(self, team_id: str, region: str = "all", days: int = 90) -> list[dict]:
        """Pull ACS and related stats from VLR player stats page."""
        if self.db.is_fresh(
            "val_player_stats",
            where=f"team_id='{team_id}'",
            max_age_hours=24,
        ):
            return self.db.get_val_players(team_id)

        logger.info("Fetching VLR player stats")
        start_date = (datetime.utcnow() - timedelta(days=days)).strftime("%Y-%m-%d")

        soup = self._get(
            "/stats",
            params={
                "region": region,
                "timespan": f"{days}d",
                "type": "agents",
            },
        )
        if not soup:
            return []

        players = []
        for row in soup.select("tbody tr"):
            try:
                cells = row.select("td")
                if len(cells) < 6:
                    continue

                name_el = cells[0].select_one("a") or cells[0]
                player_name = name_el.text.strip()
                player_href = (cells[0].select_one("a") or {}).get("href", "") if cells[0].select_one("a") else ""
                pid_match = re.search(r"/player/(\d+)/", player_href)
                player_id = f"vlr_{pid_match.group(1)}" if pid_match else _make_team_id(player_name)

                team_el = cells[1].select_one("a") if len(cells) > 1 else None
                player_team_href = (team_el or {}).get("href", "") if team_el else ""
                tid_match = re.search(r"/team/(\d+)/", player_team_href)
                player_tid = f"vlr_{tid_match.group(1)}" if tid_match else team_id

                acs = _safe_float(cells[2].text)
                kd = _safe_float(cells[3].text)
                adr = _safe_float(cells[4].text)
                kast_text = cells[5].text.strip()
                kast = _safe_float(kast_text)
                if kast > 1.0:
                    kast /= 100.0

                hs_pct = 0.0
                if len(cells) > 6:
                    hs_text = cells[6].text.strip()
                    hs_pct = _safe_float(hs_text)
                    if hs_pct > 1.0:
                        hs_pct /= 100.0

                maps_played = 0
                if len(cells) > 7:
                    maps_played = int(re.sub(r"[^0-9]", "", cells[7].text) or "0")

                player = {
                    "player_id": player_id,
                    "player_name": player_name,
                    "team_id": player_tid,
                    "acs": acs,
                    "kd_ratio": kd,
                    "adr": adr,
                    "kast": kast,
                    "hs_pct": hs_pct,
                    "maps_played": maps_played,
                }
                self.db.upsert_val_player(player)
                players.append(player)
            except Exception as exc:
                logger.debug("VLR player parse error: %s", exc)
                continue

        logger.info("Stored %d Valorant player stats", len(players))
        return players

    # ------------------------------------------------------------------
    # Roster changes
    # ------------------------------------------------------------------

    def sync_roster_changes(self, days: int = 30) -> list[dict]:
        """Pull recent Valorant roster changes from VLR transfers page."""
        logger.info("Fetching VLR roster changes (last %d days)", days)
        cutoff = datetime.utcnow() - timedelta(days=days)

        soup = self._get("/transfers")
        if not soup:
            return []

        changes = []
        for item in soup.select(".transfer-item, .roster-change"):
            try:
                date_el = item.select_one(".transfer-date") or item.select_one(".date")
                date_text = date_el.text.strip() if date_el else ""
                try:
                    change_date = datetime.strptime(date_text, "%Y/%m/%d")
                except ValueError:
                    try:
                        change_date = datetime.strptime(date_text, "%B %d, %Y")
                    except ValueError:
                        continue

                if change_date < cutoff:
                    break  # Transfers are listed newest-first

                player_el = item.select_one(".transfer-player") or item.select_one(".player")
                player_name = player_el.text.strip() if player_el else "Unknown"
                player_href = (player_el.select_one("a") or {}).get("href", "") if player_el else ""
                pid_match = re.search(r"/player/(\d+)/", player_href)
                player_id = f"vlr_{pid_match.group(1)}" if pid_match else _make_team_id(player_name)

                from_el = item.select_one(".transfer-from a") or item.select_one(".from-team a")
                to_el = item.select_one(".transfer-to a") or item.select_one(".to-team a")

                from_name = from_el.text.strip() if from_el else ""
                to_name = to_el.text.strip() if to_el else ""

                from_href = (from_el or {}).get("href", "") if from_el else ""
                to_href = (to_el or {}).get("href", "") if to_el else ""

                from_id_m = re.search(r"/team/(\d+)/", from_href)
                to_id_m = re.search(r"/team/(\d+)/", to_href)

                from_id = _make_team_id(from_name, from_id_m.group(1) if from_id_m else "")
                to_id = _make_team_id(to_name, to_id_m.group(1) if to_id_m else "")

                change_type_el = item.select_one(".transfer-type") or item.select_one(".type")
                change_type = change_type_el.text.strip().lower() if change_type_el else "join"
                if "retire" in change_type or "leave" in change_type:
                    change_type = "leave"
                elif "loan" in change_type:
                    change_type = "loan"
                else:
                    change_type = "join"

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
                with self.db.conn() as c:
                    cols = list(change.keys())
                    c.execute(
                        f"INSERT OR IGNORE INTO val_roster_changes "
                        f"({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                        list(change.values()),
                    )
                changes.append(change)
            except Exception as exc:
                logger.debug("VLR roster change parse error: %s", exc)
                continue

        logger.info("Stored %d Valorant roster changes", len(changes))
        return changes

    # ------------------------------------------------------------------
    # H2H and form
    # ------------------------------------------------------------------

    def _update_h2h_from_matches(self, team_id: str, matches: list[dict]):
        h2h_map: dict[tuple, dict] = {}
        for m in matches:
            opp_id = m["team2_id"] if m["team1_id"] == team_id else m["team1_id"]
            key = tuple(sorted([team_id, opp_id]))
            if key not in h2h_map:
                h2h_map[key] = {
                    "team1_id": key[0], "team2_id": key[1],
                    "team1_wins": 0, "team2_wins": 0, "total_matches": 0,
                }
            h2h_map[key]["total_matches"] += 1
            if m["winner_id"] == key[0]:
                h2h_map[key]["team1_wins"] += 1
            elif m["winner_id"] == key[1]:
                h2h_map[key]["team2_wins"] += 1

        for h2h in h2h_map.values():
            h2h["last_updated"] = self.db.now()
            self.db.upsert("val_h2h", h2h, ["team1_id", "team2_id"])

    def compute_recent_form(
        self, team_id: str, n: int = 10, decay: float = 0.85
    ) -> dict:
        matches = self.db.get_val_matches(team_id, days=180)[:n]
        if not matches:
            return {"weighted_wr": 0.5, "raw_wr": 0.5, "n_matches": 0, "results": []}

        tier_weights = {"S": 1.5, "A": 1.2, "B": 1.0, "C": 0.7}
        total_w = 0.0
        win_w = 0.0
        raw_wins = 0
        results = []

        for i, m in enumerate(matches):
            w = (decay ** i) * tier_weights.get(m.get("tournament_tier", "C"), 0.7)
            won = m["winner_id"] == team_id
            results.append("W" if won else "L")
            total_w += w
            if won:
                win_w += w
                raw_wins += 1

        return {
            "weighted_wr": round(win_w / total_w if total_w else 0.5, 4),
            "raw_wr": round(raw_wins / len(matches) if matches else 0.5, 4),
            "n_matches": len(matches),
            "results": results,
        }

    # ------------------------------------------------------------------
    # Full sync
    # ------------------------------------------------------------------

    def sync_all(self, top_n: int = TOP_N_VAL_TEAMS) -> dict:
        """Full data sync for top N Valorant teams."""
        logger.info("Starting full VLR sync for top %d teams", top_n)
        teams = self.sync_rankings()[:top_n]
        self.sync_roster_changes(days=30)

        synced = 0
        for team in teams:
            team_id = team["team_id"]
            vlr_id = team_id.replace("vlr_", "")
            slug = re.sub(r"[^a-z0-9-]", "", team["name"].lower().replace(" ", "-"))
            try:
                self.sync_team_matches(team_id, slug, vlr_id)
                self.sync_map_stats(team_id, vlr_id)
                self.sync_agent_stats(team_id, vlr_id)
                synced += 1
            except Exception as exc:
                logger.error("VLR sync error for %s: %s", team["name"], exc)

        self.sync_player_stats(team_id="", region="all")
        return {"teams_synced": synced, "rankings": len(teams)}

    # ------------------------------------------------------------------
    # Match snapshot
    # ------------------------------------------------------------------

    def match_snapshot(self, team1_id: str, team2_id: str) -> dict:
        """Return all stats needed to evaluate one Valorant match."""
        t1_rows = self.db.execute("SELECT * FROM val_teams WHERE team_id=?", (team1_id,))
        t2_rows = self.db.execute("SELECT * FROM val_teams WHERE team_id=?", (team2_id,))
        t1 = t1_rows[0] if t1_rows else {}
        t2 = t2_rows[0] if t2_rows else {}

        form1 = self.compute_recent_form(team1_id)
        form2 = self.compute_recent_form(team2_id)

        maps1 = self.db.execute(
            "SELECT * FROM val_map_stats WHERE team_id=? ORDER BY win_rate DESC LIMIT 5",
            (team1_id,),
        )
        maps2 = self.db.execute(
            "SELECT * FROM val_map_stats WHERE team_id=? ORDER BY win_rate DESC LIMIT 5",
            (team2_id,),
        )

        agents1 = self.db.execute(
            "SELECT * FROM val_agent_stats WHERE team_id=? ORDER BY win_rate DESC LIMIT 5",
            (team1_id,),
        )
        agents2 = self.db.execute(
            "SELECT * FROM val_agent_stats WHERE team_id=? ORDER BY win_rate DESC LIMIT 5",
            (team2_id,),
        )

        players1 = self.db.get_val_players(team1_id)
        players2 = self.db.get_val_players(team2_id)

        avg_acs1 = sum(p["acs"] for p in players1 if p.get("acs")) / len(players1) if players1 else 0.0
        avg_acs2 = sum(p["acs"] for p in players2 if p.get("acs")) / len(players2) if players2 else 0.0

        h2h_rows = self.db.execute(
            "SELECT * FROM val_h2h WHERE "
            "(team1_id=? AND team2_id=?) OR (team1_id=? AND team2_id=?)",
            (team1_id, team2_id, team2_id, team1_id),
        )

        return {
            "team1": {
                **t1,
                "form": form1,
                "map_stats": maps1,
                "agent_stats": agents1,
                "avg_acs": round(avg_acs1, 1),
                "players": players1[:5],
            },
            "team2": {
                **t2,
                "form": form2,
                "map_stats": maps2,
                "agent_stats": agents2,
                "avg_acs": round(avg_acs2, 1),
                "players": players2[:5],
            },
            "h2h": h2h_rows[0] if h2h_rows else {},
        }
