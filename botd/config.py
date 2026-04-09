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

# Match format upset probability adjustments
# Bo1 is much higher variance than Bo5
FORMAT_UPSET_SCALE: dict[str, float] = {
    "Bo1": 1.40,  # 40% more variance
    "Bo3": 1.00,  # baseline
    "Bo5": 0.75,  # skill dominates
}
