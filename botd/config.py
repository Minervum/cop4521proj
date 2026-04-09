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

# Stale-data thresholds (hours before a re-fetch is triggered)
RANKINGS_MAX_AGE_HOURS: float = 24.0
MATCH_HISTORY_MAX_AGE_HOURS: float = 6.0
UPCOMING_MAX_AGE_HOURS: float = 0.5

# How many top teams to maintain full stats for
TOP_N_CS2_TEAMS: int = 30
TOP_N_VAL_TEAMS: int = 30

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

# Map pools (active as of 2025)
CS2_MAP_POOL: list[str] = [
    "Mirage", "Inferno", "Nuke", "Ancient", "Anubis", "Dust2", "Vertigo"
]
VAL_MAP_POOL: list[str] = [
    "Abyss", "Ascent", "Bind", "Haven", "Lotus", "Pearl", "Split"
]

ELO_INITIAL_RATING: float = 1500.0

# K-factors per match format
ELO_K_FACTORS: dict[str, float] = {
    "Bo1": 32.0,   # High volatility — upsets happen more
    "Bo3": 24.0,   # Standard
    "Bo5": 16.0,   # Skill dominates
}

# 15% recency decay per month: K_eff = K_base × (0.85)^months_ago
ELO_RECENCY_DECAY_PER_MONTH: float = 0.15

# Map ELO scale: ELO delta per unit of win-rate deviation from 0.50
# Calibrated so 0.60 map win rate → +70 ELO (consistent with ELO math)
ELO_MAP_WIN_RATE_SCALE: float = 700.0

# Minimum map results before map ELO is considered reliable
ELO_MIN_MATCHES_FOR_MAP: int = 5

# Edge threshold for the ELO signal engine (8% — tighter than old 5%)
ELO_MIN_EDGE: float = float(os.environ.get("ELO_MIN_EDGE", "0.08"))

# Situational ELO adjustments (in ELO points)
ELO_ADJ_STANDIN: float = -5.0          # Stand-in / loan player
ELO_ADJ_RECENT_ADDITION: float = -3.0  # New player, <14 days on roster
ELO_ADJ_TOURNAMENT_PRESSURE: float = 8.0   # Team with something to play for
ELO_ADJ_TRAVEL_FATIGUE: float = -3.0   # International travel within 48 h
ELO_VENUE_ADJ_MAX: float = 50.0        # Cap on online/LAN venue adjustment

# Format probability multipliers for ELO engine
# Applied to the deviation from 0.50 AFTER ELO adjustment
FORMAT_PROB_MULTIPLIERS: dict[str, float] = {
    "Bo1": 0.85,   # Compress toward 50% by 15% (Bo1 is high variance)
    "Bo3": 1.00,   # No adjustment (baseline)
    "Bo5": 1.10,   # Expand away from 50% by 10% (skill dominates)
}
