"""
Bot D database layer.

Owns all cs2_*, val_*, and liq_* tables.
Also initialises the shared trades_* tables via the paper trader.
"""

from __future__ import annotations

import json

from shared.storage import BaseStorage
from botd.config import DB_PATH


_SCHEMA = """
-- ============================================================
-- CS2 / HLTV tables
-- ============================================================

CREATE TABLE IF NOT EXISTS cs2_teams (
    team_id          TEXT PRIMARY KEY,
    name             TEXT NOT NULL,
    ranking          INTEGER,
    ranking_points   REAL,
    country          TEXT,
    updated_at       TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS cs2_matches (
    match_id         TEXT PRIMARY KEY,
    team1_id         TEXT,
    team2_id         TEXT,
    team1_name       TEXT,
    team2_name       TEXT,
    team1_score      INTEGER DEFAULT 0,
    team2_score      INTEGER DEFAULT 0,
    winner_id        TEXT,
    tournament       TEXT,
    tournament_tier  TEXT,
    match_format     TEXT,
    match_date       TEXT,
    created_at       TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS cs2_map_stats (
    team_id          TEXT NOT NULL,
    map_name         TEXT NOT NULL,
    wins             INTEGER DEFAULT 0,
    losses           INTEGER DEFAULT 0,
    win_rate         REAL DEFAULT 0.0,
    ct_win_rate      REAL DEFAULT 0.0,
    t_win_rate       REAL DEFAULT 0.0,
    updated_at       TEXT DEFAULT (datetime('now')),
    PRIMARY KEY (team_id, map_name)
);

CREATE TABLE IF NOT EXISTS cs2_h2h (
    team1_id         TEXT NOT NULL,
    team2_id         TEXT NOT NULL,
    team1_wins       INTEGER DEFAULT 0,
    team2_wins       INTEGER DEFAULT 0,
    total_matches    INTEGER DEFAULT 0,
    last_updated     TEXT DEFAULT (datetime('now')),
    PRIMARY KEY (team1_id, team2_id)
);

CREATE TABLE IF NOT EXISTS cs2_player_stats (
    player_id        TEXT PRIMARY KEY,
    player_name      TEXT,
    team_id          TEXT,
    rating           REAL,   -- HLTV Rating 2.0
    kd_ratio         REAL,
    impact           REAL,
    adr              REAL,
    kast             REAL,
    maps_played      INTEGER DEFAULT 0,
    updated_at       TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS cs2_roster_changes (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    player_id        TEXT,
    player_name      TEXT,
    from_team_id     TEXT,
    from_team_name   TEXT,
    to_team_id       TEXT,
    to_team_name     TEXT,
    change_type      TEXT,   -- 'join', 'leave', 'loan', 'inactive'
    change_date      TEXT,
    created_at       TEXT DEFAULT (datetime('now'))
);

-- ============================================================
-- Valorant / VLR tables
-- ============================================================

CREATE TABLE IF NOT EXISTS val_teams (
    team_id          TEXT PRIMARY KEY,
    name             TEXT NOT NULL,
    ranking          INTEGER,
    region           TEXT,
    updated_at       TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS val_matches (
    match_id         TEXT PRIMARY KEY,
    team1_id         TEXT,
    team2_id         TEXT,
    team1_name       TEXT,
    team2_name       TEXT,
    team1_score      INTEGER DEFAULT 0,
    team2_score      INTEGER DEFAULT 0,
    winner_id        TEXT,
    tournament       TEXT,
    tournament_tier  TEXT,
    match_format     TEXT,
    match_date       TEXT,
    created_at       TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS val_map_stats (
    team_id          TEXT NOT NULL,
    map_name         TEXT NOT NULL,
    wins             INTEGER DEFAULT 0,
    losses           INTEGER DEFAULT 0,
    win_rate         REAL DEFAULT 0.0,
    atk_win_rate     REAL DEFAULT 0.0,
    def_win_rate     REAL DEFAULT 0.0,
    updated_at       TEXT DEFAULT (datetime('now')),
    PRIMARY KEY (team_id, map_name)
);

CREATE TABLE IF NOT EXISTS val_agent_stats (
    team_id          TEXT NOT NULL,
    agent_name       TEXT NOT NULL,
    times_played     INTEGER DEFAULT 0,
    win_rate         REAL DEFAULT 0.0,
    avg_acs          REAL DEFAULT 0.0,
    updated_at       TEXT DEFAULT (datetime('now')),
    PRIMARY KEY (team_id, agent_name)
);

CREATE TABLE IF NOT EXISTS val_h2h (
    team1_id         TEXT NOT NULL,
    team2_id         TEXT NOT NULL,
    team1_wins       INTEGER DEFAULT 0,
    team2_wins       INTEGER DEFAULT 0,
    total_matches    INTEGER DEFAULT 0,
    last_updated     TEXT DEFAULT (datetime('now')),
    PRIMARY KEY (team1_id, team2_id)
);

CREATE TABLE IF NOT EXISTS val_player_stats (
    player_id        TEXT PRIMARY KEY,
    player_name      TEXT,
    team_id          TEXT,
    acs              REAL,   -- Average Combat Score
    kd_ratio         REAL,
    adr              REAL,
    kast             REAL,
    hs_pct           REAL,   -- headshot percentage
    maps_played      INTEGER DEFAULT 0,
    updated_at       TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS val_roster_changes (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    player_id        TEXT,
    player_name      TEXT,
    from_team_id     TEXT,
    from_team_name   TEXT,
    to_team_id       TEXT,
    to_team_name     TEXT,
    change_type      TEXT,
    change_date      TEXT,
    created_at       TEXT DEFAULT (datetime('now'))
);

-- ============================================================
-- Liquipedia shared match/tournament tables
-- ============================================================

CREATE TABLE IF NOT EXISTS liq_upcoming_matches (
    match_id         TEXT PRIMARY KEY,
    game             TEXT NOT NULL,   -- 'cs2' or 'val'
    team1            TEXT,
    team2            TEXT,
    team1_id         TEXT,
    team2_id         TEXT,
    match_datetime   TEXT,
    tournament       TEXT,
    tournament_tier  TEXT,
    match_format     TEXT,
    prize_pool       TEXT,
    stream_url       TEXT,
    veto_maps        TEXT,            -- JSON list of map names when veto is known
    updated_at       TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS liq_tournaments (
    tournament_id    TEXT PRIMARY KEY,
    game             TEXT NOT NULL,
    name             TEXT,
    tier             TEXT,
    prize_pool       TEXT,
    start_date       TEXT,
    end_date         TEXT,
    location         TEXT,
    updated_at       TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS liq_brackets (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    tournament_id    TEXT,
    game             TEXT,
    team_name        TEXT,
    status           TEXT,   -- 'eliminated', 'qualified', 'playing'
    stage            TEXT,
    updated_at       TEXT DEFAULT (datetime('now'))
);

-- ============================================================
-- Dota 2 / OpenDota tables
-- ============================================================

CREATE TABLE IF NOT EXISTS dota2_teams (
    team_id          TEXT PRIMARY KEY,
    name             TEXT NOT NULL,
    ranking          INTEGER,
    country          TEXT,
    region           TEXT,
    wins             INTEGER DEFAULT 0,
    losses           INTEGER DEFAULT 0,
    rating           REAL DEFAULT 0.0,
    updated_at       TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS dota2_matches (
    match_id         TEXT PRIMARY KEY,
    team1_id         TEXT,
    team2_id         TEXT,
    team1_name       TEXT,
    team2_name       TEXT,
    team1_score      INTEGER DEFAULT 0,
    team2_score      INTEGER DEFAULT 0,
    winner_id        TEXT,
    tournament       TEXT,
    tournament_tier  TEXT,
    match_format     TEXT,
    match_date       TEXT,
    league_id        TEXT,
    created_at       TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS dota2_hero_stats (
    team_id          TEXT NOT NULL,
    hero_name        TEXT NOT NULL,
    times_picked     INTEGER DEFAULT 0,
    times_banned     INTEGER DEFAULT 0,
    wins             INTEGER DEFAULT 0,
    losses           INTEGER DEFAULT 0,
    win_rate         REAL DEFAULT 0.0,
    updated_at       TEXT DEFAULT (datetime('now')),
    PRIMARY KEY (team_id, hero_name)
);

CREATE TABLE IF NOT EXISTS dota2_player_stats (
    player_id        TEXT PRIMARY KEY,
    player_name      TEXT,
    team_id          TEXT,
    role             TEXT,   -- 'carry','mid','offlane','soft_support','hard_support'
    avg_gpm          REAL DEFAULT 0.0,
    avg_xpm          REAL DEFAULT 0.0,
    avg_kda          REAL DEFAULT 0.0,
    avg_kills        REAL DEFAULT 0.0,
    avg_deaths       REAL DEFAULT 0.0,
    avg_assists      REAL DEFAULT 0.0,
    matches_played   INTEGER DEFAULT 0,
    updated_at       TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS dota2_roster_changes (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    player_id        TEXT,
    player_name      TEXT,
    from_team_id     TEXT,
    from_team_name   TEXT,
    to_team_id       TEXT,
    to_team_name     TEXT,
    change_type      TEXT,
    change_date      TEXT,
    created_at       TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS dota2_h2h (
    team1_id         TEXT NOT NULL,
    team2_id         TEXT NOT NULL,
    team1_wins       INTEGER DEFAULT 0,
    team2_wins       INTEGER DEFAULT 0,
    total_matches    INTEGER DEFAULT 0,
    last_updated     TEXT DEFAULT (datetime('now')),
    PRIMARY KEY (team1_id, team2_id)
);

-- ============================================================
-- League of Legends / Riot Esports tables
-- ============================================================

CREATE TABLE IF NOT EXISTS lol_teams (
    team_id          TEXT PRIMARY KEY,
    name             TEXT NOT NULL,
    code             TEXT,
    ranking          INTEGER,
    region           TEXT,   -- 'KR','CN','EU','NA','BR','LAS','PCS', etc.
    league           TEXT,
    wins             INTEGER DEFAULT 0,
    losses           INTEGER DEFAULT 0,
    updated_at       TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS lol_matches (
    match_id         TEXT PRIMARY KEY,
    team1_id         TEXT,
    team2_id         TEXT,
    team1_name       TEXT,
    team2_name       TEXT,
    team1_score      INTEGER DEFAULT 0,
    team2_score      INTEGER DEFAULT 0,
    winner_id        TEXT,
    tournament       TEXT,
    tournament_tier  TEXT,
    match_format     TEXT,
    match_date       TEXT,
    league_id        TEXT,
    created_at       TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS lol_position_stats (
    team_id          TEXT NOT NULL,
    position         TEXT NOT NULL,  -- top, jungle, mid, bot, support
    wins             INTEGER DEFAULT 0,
    losses           INTEGER DEFAULT 0,
    win_rate         REAL DEFAULT 0.0,
    updated_at       TEXT DEFAULT (datetime('now')),
    PRIMARY KEY (team_id, position)
);

CREATE TABLE IF NOT EXISTS lol_player_stats (
    player_id        TEXT PRIMARY KEY,
    player_name      TEXT,
    team_id          TEXT,
    position         TEXT,
    avg_kda          REAL DEFAULT 0.0,
    avg_cs_per_min   REAL DEFAULT 0.0,
    avg_gold_per_min REAL DEFAULT 0.0,
    avg_damage_share REAL DEFAULT 0.0,
    matches_played   INTEGER DEFAULT 0,
    updated_at       TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS lol_champion_stats (
    player_id        TEXT NOT NULL,
    champion_name    TEXT NOT NULL,
    times_played     INTEGER DEFAULT 0,
    wins             INTEGER DEFAULT 0,
    losses           INTEGER DEFAULT 0,
    win_rate         REAL DEFAULT 0.0,
    avg_kda          REAL DEFAULT 0.0,
    updated_at       TEXT DEFAULT (datetime('now')),
    PRIMARY KEY (player_id, champion_name)
);

CREATE TABLE IF NOT EXISTS lol_roster_changes (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    player_id        TEXT,
    player_name      TEXT,
    from_team_id     TEXT,
    from_team_name   TEXT,
    to_team_id       TEXT,
    to_team_name     TEXT,
    change_type      TEXT,
    change_date      TEXT,
    created_at       TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS lol_h2h (
    team1_id         TEXT NOT NULL,
    team2_id         TEXT NOT NULL,
    team1_wins       INTEGER DEFAULT 0,
    team2_wins       INTEGER DEFAULT 0,
    total_matches    INTEGER DEFAULT 0,
    last_updated     TEXT DEFAULT (datetime('now')),
    PRIMARY KEY (team1_id, team2_id)
);

-- ============================================================
-- Indices
-- ============================================================

CREATE INDEX IF NOT EXISTS idx_cs2_matches_date    ON cs2_matches(match_date);
CREATE INDEX IF NOT EXISTS idx_cs2_matches_t1      ON cs2_matches(team1_id);
CREATE INDEX IF NOT EXISTS idx_cs2_matches_t2      ON cs2_matches(team2_id);
CREATE INDEX IF NOT EXISTS idx_val_matches_date    ON val_matches(match_date);
CREATE INDEX IF NOT EXISTS idx_val_matches_t1      ON val_matches(team1_id);
CREATE INDEX IF NOT EXISTS idx_dota2_matches_date  ON dota2_matches(match_date);
CREATE INDEX IF NOT EXISTS idx_dota2_matches_t1    ON dota2_matches(team1_id);
CREATE INDEX IF NOT EXISTS idx_dota2_matches_t2    ON dota2_matches(team2_id);
CREATE INDEX IF NOT EXISTS idx_lol_matches_date    ON lol_matches(match_date);
CREATE INDEX IF NOT EXISTS idx_lol_matches_t1      ON lol_matches(team1_id);
CREATE INDEX IF NOT EXISTS idx_lol_matches_t2      ON lol_matches(team2_id);
CREATE INDEX IF NOT EXISTS idx_liq_upcoming_dt     ON liq_upcoming_matches(match_datetime);
CREATE INDEX IF NOT EXISTS idx_liq_upcoming_game   ON liq_upcoming_matches(game);

-- ============================================================
-- Tournament context state
-- ============================================================

CREATE TABLE IF NOT EXISTS tournament_state (
    tournament_name  TEXT NOT NULL,
    game             TEXT NOT NULL,
    stage            TEXT NOT NULL DEFAULT 'unknown',
    -- 'group' | 'playoffs' | 'grand_final' | 'qualifier' | 'unknown'
    team_name        TEXT NOT NULL,
    team_id          TEXT NOT NULL DEFAULT '',
    bracket_status   TEXT NOT NULL DEFAULT 'playing',
    -- 'must_win' | 'clinched' | 'eliminated' | 'playing'
    wins             INTEGER DEFAULT 0,
    losses           INTEGER DEFAULT 0,
    position         INTEGER,
    updated_at       TEXT NOT NULL,
    PRIMARY KEY (game, tournament_name, team_name)
);

CREATE INDEX IF NOT EXISTS idx_tournament_state_game   ON tournament_state(game);
CREATE INDEX IF NOT EXISTS idx_tournament_state_status ON tournament_state(bracket_status);
"""


class BotDStorage(BaseStorage):
    def __init__(self, db_path: str = DB_PATH):
        super().__init__(db_path)
        self._init_schema()

    def _init_schema(self):
        with self.conn() as c:
            c.executescript(_SCHEMA)

    # ------------------------------------------------------------------
    # CS2 helpers
    # ------------------------------------------------------------------

    def upsert_cs2_team(self, team: dict):
        team.setdefault("updated_at", self.now())
        self.upsert("cs2_teams", team, ["team_id"])

    def upsert_cs2_match(self, match: dict):
        match.setdefault("created_at", self.now())
        self.upsert("cs2_matches", match, ["match_id"])

    def upsert_cs2_map_stats(self, stats: dict):
        stats.setdefault("updated_at", self.now())
        self.upsert("cs2_map_stats", stats, ["team_id", "map_name"])

    def upsert_cs2_h2h(self, h2h: dict):
        h2h.setdefault("last_updated", self.now())
        self.upsert("cs2_h2h", h2h, ["team1_id", "team2_id"])

    def upsert_cs2_player(self, player: dict):
        player.setdefault("updated_at", self.now())
        self.upsert("cs2_player_stats", player, ["player_id"])

    def insert_cs2_roster_change(self, change: dict):
        change.setdefault("created_at", self.now())
        with self.conn() as c:
            cols = list(change.keys())
            c.execute(
                f"INSERT OR IGNORE INTO cs2_roster_changes "
                f"({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                list(change.values()),
            )

    def get_cs2_team(self, team_id: str) -> dict | None:
        rows = self.execute(
            "SELECT * FROM cs2_teams WHERE team_id=?", (team_id,)
        )
        return rows[0] if rows else None

    def get_cs2_matches(
        self, team_id: str, days: int = 180
    ) -> list[dict]:
        return self.execute(
            "SELECT * FROM cs2_matches "
            "WHERE (team1_id=? OR team2_id=?) "
            "  AND match_date >= date('now', ? || ' days') "
            "ORDER BY match_date DESC",
            (team_id, team_id, f"-{days}"),
        )

    def get_cs2_h2h(self, team1_id: str, team2_id: str) -> dict | None:
        rows = self.execute(
            "SELECT * FROM cs2_h2h WHERE "
            "(team1_id=? AND team2_id=?) OR (team1_id=? AND team2_id=?)",
            (team1_id, team2_id, team2_id, team1_id),
        )
        return rows[0] if rows else None

    def get_cs2_map_stats(self, team_id: str) -> list[dict]:
        return self.execute(
            "SELECT * FROM cs2_map_stats WHERE team_id=? ORDER BY win_rate DESC",
            (team_id,),
        )

    def get_cs2_players(self, team_id: str) -> list[dict]:
        return self.execute(
            "SELECT * FROM cs2_player_stats WHERE team_id=? ORDER BY rating DESC",
            (team_id,),
        )

    def get_recent_roster_changes(self, team_id: str, days: int = 30) -> list[dict]:
        return self.execute(
            "SELECT * FROM cs2_roster_changes "
            "WHERE (from_team_id=? OR to_team_id=?) "
            "  AND change_date >= date('now', ? || ' days') "
            "ORDER BY change_date DESC",
            (team_id, team_id, f"-{days}"),
        )

    # ------------------------------------------------------------------
    # Valorant helpers
    # ------------------------------------------------------------------

    def upsert_val_team(self, team: dict):
        team.setdefault("updated_at", self.now())
        self.upsert("val_teams", team, ["team_id"])

    def upsert_val_match(self, match: dict):
        match.setdefault("created_at", self.now())
        self.upsert("val_matches", match, ["match_id"])

    def upsert_val_map_stats(self, stats: dict):
        stats.setdefault("updated_at", self.now())
        self.upsert("val_map_stats", stats, ["team_id", "map_name"])

    def upsert_val_agent_stats(self, stats: dict):
        stats.setdefault("updated_at", self.now())
        self.upsert("val_agent_stats", stats, ["team_id", "agent_name"])

    def upsert_val_player(self, player: dict):
        player.setdefault("updated_at", self.now())
        self.upsert("val_player_stats", player, ["player_id"])

    def get_val_matches(self, team_id: str, days: int = 180) -> list[dict]:
        return self.execute(
            "SELECT * FROM val_matches "
            "WHERE (team1_id=? OR team2_id=?) "
            "  AND match_date >= date('now', ? || ' days') "
            "ORDER BY match_date DESC",
            (team_id, team_id, f"-{days}"),
        )

    def get_val_h2h(self, team1_id: str, team2_id: str) -> dict | None:
        rows = self.execute(
            "SELECT * FROM val_h2h WHERE "
            "(team1_id=? AND team2_id=?) OR (team1_id=? AND team2_id=?)",
            (team1_id, team2_id, team2_id, team1_id),
        )
        return rows[0] if rows else None

    def get_val_players(self, team_id: str) -> list[dict]:
        return self.execute(
            "SELECT * FROM val_player_stats WHERE team_id=? ORDER BY acs DESC",
            (team_id,),
        )

    # ------------------------------------------------------------------
    # Dota 2 helpers
    # ------------------------------------------------------------------

    def upsert_dota2_team(self, team: dict):
        team.setdefault("updated_at", self.now())
        self.upsert("dota2_teams", team, ["team_id"])

    def upsert_dota2_match(self, match: dict):
        match.setdefault("created_at", self.now())
        self.upsert("dota2_matches", match, ["match_id"])

    def upsert_dota2_hero_stats(self, stats: dict):
        stats.setdefault("updated_at", self.now())
        self.upsert("dota2_hero_stats", stats, ["team_id", "hero_name"])

    def upsert_dota2_player(self, player: dict):
        player.setdefault("updated_at", self.now())
        self.upsert("dota2_player_stats", player, ["player_id"])

    def upsert_dota2_h2h(self, h2h: dict):
        h2h.setdefault("last_updated", self.now())
        self.upsert("dota2_h2h", h2h, ["team1_id", "team2_id"])

    def insert_dota2_roster_change(self, change: dict):
        change.setdefault("created_at", self.now())
        with self.conn() as c:
            cols = list(change.keys())
            c.execute(
                f"INSERT OR IGNORE INTO dota2_roster_changes "
                f"({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                list(change.values()),
            )

    def get_dota2_matches(self, team_id: str, days: int = 180) -> list[dict]:
        return self.execute(
            "SELECT * FROM dota2_matches "
            "WHERE (team1_id=? OR team2_id=?) "
            "  AND match_date >= date('now', ? || ' days') "
            "ORDER BY match_date DESC",
            (team_id, team_id, f"-{days}"),
        )

    def get_dota2_hero_stats(self, team_id: str) -> list[dict]:
        return self.execute(
            "SELECT * FROM dota2_hero_stats WHERE team_id=? ORDER BY win_rate DESC",
            (team_id,),
        )

    def get_dota2_h2h(self, team1_id: str, team2_id: str) -> dict | None:
        rows = self.execute(
            "SELECT * FROM dota2_h2h WHERE "
            "(team1_id=? AND team2_id=?) OR (team1_id=? AND team2_id=?)",
            (team1_id, team2_id, team2_id, team1_id),
        )
        return rows[0] if rows else None

    # ------------------------------------------------------------------
    # League of Legends helpers
    # ------------------------------------------------------------------

    def upsert_lol_team(self, team: dict):
        team.setdefault("updated_at", self.now())
        self.upsert("lol_teams", team, ["team_id"])

    def upsert_lol_match(self, match: dict):
        match.setdefault("created_at", self.now())
        self.upsert("lol_matches", match, ["match_id"])

    def upsert_lol_position_stats(self, stats: dict):
        stats.setdefault("updated_at", self.now())
        self.upsert("lol_position_stats", stats, ["team_id", "position"])

    def upsert_lol_player(self, player: dict):
        player.setdefault("updated_at", self.now())
        self.upsert("lol_player_stats", player, ["player_id"])

    def upsert_lol_champion_stats(self, stats: dict):
        stats.setdefault("updated_at", self.now())
        self.upsert("lol_champion_stats", stats, ["player_id", "champion_name"])

    def upsert_lol_h2h(self, h2h: dict):
        h2h.setdefault("last_updated", self.now())
        self.upsert("lol_h2h", h2h, ["team1_id", "team2_id"])

    def insert_lol_roster_change(self, change: dict):
        change.setdefault("created_at", self.now())
        with self.conn() as c:
            cols = list(change.keys())
            c.execute(
                f"INSERT OR IGNORE INTO lol_roster_changes "
                f"({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                list(change.values()),
            )

    def get_lol_team(self, team_id: str) -> dict | None:
        rows = self.execute("SELECT * FROM lol_teams WHERE team_id=?", (team_id,))
        return rows[0] if rows else None

    def get_lol_matches(self, team_id: str, days: int = 180) -> list[dict]:
        return self.execute(
            "SELECT * FROM lol_matches "
            "WHERE (team1_id=? OR team2_id=?) "
            "  AND match_date >= date('now', ? || ' days') "
            "ORDER BY match_date DESC",
            (team_id, team_id, f"-{days}"),
        )

    def get_lol_h2h(self, team1_id: str, team2_id: str) -> dict | None:
        rows = self.execute(
            "SELECT * FROM lol_h2h WHERE "
            "(team1_id=? AND team2_id=?) OR (team1_id=? AND team2_id=?)",
            (team1_id, team2_id, team2_id, team1_id),
        )
        return rows[0] if rows else None

    def get_lol_position_stats(self, team_id: str) -> list[dict]:
        return self.execute(
            "SELECT * FROM lol_position_stats WHERE team_id=? ORDER BY win_rate DESC",
            (team_id,),
        )

    # ------------------------------------------------------------------
    # Liquipedia helpers
    # ------------------------------------------------------------------

    def upsert_upcoming_match(self, match: dict):
        match = dict(match)  # don't mutate caller's dict
        match.setdefault("updated_at", self.now())
        # Serialize list fields to JSON for TEXT storage
        if isinstance(match.get("veto_maps"), list):
            match["veto_maps"] = json.dumps(match["veto_maps"])
        self.upsert("liq_upcoming_matches", match, ["match_id"])

    def upsert_tournament(self, tourn: dict):
        tourn.setdefault("updated_at", self.now())
        self.upsert("liq_tournaments", tourn, ["tournament_id"])

    def get_upcoming_matches(self, game: str = None, days: int = 7) -> list[dict]:
        if game:
            rows = self.execute(
                "SELECT * FROM liq_upcoming_matches "
                "WHERE game=? AND match_datetime >= datetime('now') "
                "  AND match_datetime <= datetime('now', ? || ' days') "
                "ORDER BY match_datetime ASC",
                (game, str(days)),
            )
        else:
            rows = self.execute(
                "SELECT * FROM liq_upcoming_matches "
                "WHERE match_datetime >= datetime('now') "
                "  AND match_datetime <= datetime('now', ? || ' days') "
                "ORDER BY match_datetime ASC",
                (str(days),),
            )
        # Deserialize JSON fields
        for row in rows:
            if isinstance(row.get("veto_maps"), str):
                try:
                    row["veto_maps"] = json.loads(row["veto_maps"])
                except (json.JSONDecodeError, TypeError):
                    row["veto_maps"] = []
        return rows

    # ------------------------------------------------------------------
    # Tournament context state helpers
    # ------------------------------------------------------------------

    def upsert_tournament_state(self, state: dict):
        state = dict(state)
        state.setdefault("updated_at", self.now())
        self.upsert("tournament_state", state, ["game", "tournament_name", "team_name"])

    def get_team_tournament_state(self, team_name: str, game: str) -> dict | None:
        """Lookup a team's bracket status in the most-recently-updated tournament."""
        rows = self.execute(
            "SELECT * FROM tournament_state "
            "WHERE game=? AND LOWER(team_name) LIKE ? "
            "ORDER BY updated_at DESC LIMIT 1",
            (game, f"%{team_name[:20].lower()}%"),
        )
        return rows[0] if rows else None

    def get_active_tournament_states(self, game: str | None = None) -> list[dict]:
        """Return all tournament_state rows, optionally filtered by game."""
        if game:
            return self.execute(
                "SELECT * FROM tournament_state WHERE game=? "
                "ORDER BY game, tournament_name, bracket_status, team_name",
                (game,),
            )
        return self.execute(
            "SELECT * FROM tournament_state "
            "ORDER BY game, tournament_name, bracket_status, team_name"
        )
