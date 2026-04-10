"""
Bot D configuration. Reads from environment / .env file.
"""

import os
from dotenv import load_dotenv

load_dotenv()

DB_PATH: str = os.environ.get("BOT_DB_PATH", "data/botd.db")

PAPER_TRADING: bool = os.environ.get("PAPER_TRADING", "true").lower() == "true"
PAPER_BANKROLL: float = float(os.environ.get("PAPER_BANKROLL", "10000.0"))

KELLY_FRACTION: float = float(os.environ.get("KELLY_FRACTION", "0.5"))
MAX_POSITION_FRACTION: float = float(os.environ.get("MAX_POSITION_FRACTION", "0.10"))
MIN_EDGE: float = float(os.environ.get("MIN_EDGE", "0.05"))

HLTV_REQUEST_DELAY: float = float(os.environ.get("HLTV_REQUEST_DELAY", "3.0"))
VLR_REQUEST_DELAY: float = float(os.environ.get("VLR_REQUEST_DELAY", "2.0"))
LIQUIPEDIA_REQUEST_DELAY: float = float(os.environ.get("LIQUIPEDIA_REQUEST_DELAY", "2.0"))
OPENDOTA_REQUEST_DELAY: float = float(os.environ.get("OPENDOTA_REQUEST_DELAY", "1.5"))
LOL_REQUEST_DELAY: float = float(os.environ.get("LOL_REQUEST_DELAY", "1.0"))

# Stale-data thresholds (hours before a re-fetch is triggered)
RANKINGS_MAX_AGE_HOURS: float = 24.0
MATCH_HISTORY_MAX_AGE_HOURS: float = 6.0
UPCOMING_MAX_AGE_HOURS: float = 0.5

# How many top teams to maintain full stats for
TOP_N_CS2_TEAMS: int = 30
TOP_N_VAL_TEAMS: int = 30
TOP_N_DOTA2_TEAMS: int = 30
TOP_N_LOL_TEAMS: int = 30

# Form weight decay: recent matches count more
RECENCY_DECAY: float = 0.85  # each match back multiplied by this factor

# Tournament tier weights for form calculation
TIER_WEIGHTS: dict[str, float] = {
    "S": 1.5,
    "A": 1.2,
    "B": 1.0,
    "C": 0.7,
    "D": 0.5,
}

# Match format upset probability adjustments (old weight-blend model)
FORMAT_UPSET_SCALE: dict[str, float] = {
    "Bo1": 1.40,
    "Bo3": 1.00,
    "Bo5": 0.75,
}

# ── ELO engine ─────────────────────────────────────────────────────────────

# Map / hero / position pools per game
CS2_MAP_POOL: list[str] = [
    "Mirage", "Inferno", "Nuke", "Ancient", "Anubis", "Dust2", "Vertigo"
]
VAL_MAP_POOL: list[str] = [
    "Abyss", "Ascent", "Bind", "Haven", "Lotus", "Pearl", "Split"
]
DOTA2_ROLE_POOL: list[str] = [
    "carry", "mid", "offlane", "soft_support", "hard_support"
]
LOL_POSITION_POOL: list[str] = [
    "top", "jungle", "mid", "bot", "support"
]

# Maps each game string to its "secondary ELO" pool list
GAME_POOL: dict[str, list[str]] = {
    "cs2":   CS2_MAP_POOL,
    "val":   VAL_MAP_POOL,
    "dota2": DOTA2_ROLE_POOL,   # hero pool uses dynamic hero names, roles here
    "lol":   LOL_POSITION_POOL,
}

ELO_INITIAL_RATING: float = 1500.0

# K-factors per match format
ELO_K_FACTORS: dict[str, float] = {
    "Bo1": 32.0,   # High volatility — upsets happen more
    "Bo2": 28.0,   # Dota2 sometimes uses Bo2 in group stages
    "Bo3": 24.0,   # Standard
    "Bo5": 16.0,   # Finals/playoffs, skill stabilises outcome
}

# 15% recency decay per month: K_eff = K_base × (0.85)^months_ago
ELO_RECENCY_DECAY_PER_MONTH: float = 0.15

# Map ELO scale: ELO delta per unit of win-rate deviation from 0.50
# Calibrated so 0.60 map win rate → +70 ELO (consistent with ELO math)
ELO_MAP_WIN_RATE_SCALE: float = 700.0

# Minimum results before secondary-layer ELO is considered reliable
ELO_MIN_MATCHES_FOR_MAP: int = 5

# Edge threshold for the ELO signal engine (8% — tighter than old 5%)
ELO_MIN_EDGE: float = float(os.environ.get("ELO_MIN_EDGE", "0.08"))

# Situational ELO adjustments (in ELO points)
ELO_ADJ_STANDIN: float = -5.0          # Stand-in / loan player
ELO_ADJ_RECENT_ADDITION: float = -3.0  # New player, <14 days on roster
ELO_ADJ_TOURNAMENT_PRESSURE: float = 8.0   # Team with something to play for
ELO_ADJ_TRAVEL_FATIGUE: float = -3.0   # International travel within 48 h
ELO_VENUE_ADJ_MAX: float = 50.0        # Cap on online/LAN venue adjustment

# Dota 2: draft advantage from hero pool diversity (±10 ELO max)
DOTA2_DRAFT_ADJ_SCALE: float = 20.0    # pool_score ∈ [0,1] → ±10 ELO
DOTA2_HERO_POOL_TARGET: int = 30       # diverse pool = 30+ heroes with ≥5 games

# League of Legends: regional ELO bias at international events
# Applied when two teams from different regions meet at Worlds / MSI / etc.
LOL_REGION_ELO_ADJ: dict[str, float] = {
    "KR": 8.0,    # LCK historically strongest internationally
    "CN": 6.0,    # LPL strong internationally
    "EU": 2.0,    # LEC solid but below KR/CN
    "NA": -4.0,   # LCS historically underperforms vs East
    "BR": -2.0,
    "LAS": -3.0,
    "LAN": -3.0,
    "VN": 0.0,
    "PCS": -1.0,
    "JP": -2.0,
    "OCE": -3.0,
}
# Keywords that identify international LoL events
LOL_INTERNATIONAL_KEYWORDS: list[str] = [
    "worlds", "msi", "all-star", "international", "global", "mid-season"
]

# LoL Esports API (publicly documented key, override via env var)
LOL_ESPORTS_API_KEY: str = os.environ.get(
    "LOL_ESPORTS_API_KEY", "0TvQnueqKa5mxJntVWt0w4LpLfEkrV1Ta8rQBb9Z"
)

# Format probability multipliers for ELO engine
# Applied to the deviation from 0.50 AFTER ELO adjustment
FORMAT_PROB_MULTIPLIERS: dict[str, float] = {
    "Bo1": 0.85,   # Compress toward 50% by 15% (Bo1 is high variance)
    "Bo2": 0.90,   # Slight compression (used in Dota2 group stages)
    "Bo3": 1.00,   # No adjustment (baseline)
    "Bo5": 1.10,   # Expand away from 50% by 10% (skill dominates)
}

# Kalshi market ticker prefixes per game
KALSHI_TICKERS: dict[str, str] = {
    "cs2":   "KXCS2",
    "val":   "KXVAL",
    "dota2": "KXDOTA",
    "lol":   "KXLOL",
}

# Refresh intervals from env
HLTV_REFRESH_INTERVAL: int = int(os.environ.get("HLTV_REFRESH_INTERVAL", "360"))
VLR_REFRESH_INTERVAL: int = int(os.environ.get("VLR_REFRESH_INTERVAL", "360"))
LIQUIPEDIA_REFRESH_INTERVAL: int = int(os.environ.get("LIQUIPEDIA_REFRESH_INTERVAL", "30"))
