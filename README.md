# Bot D — Esports Trading Bot

Part of a 12-bot Kalshi prediction market trading system.

Bot B (sports) and Bot C (economics) are already built. Bot D targets **CS2** and **Valorant** esports markets using pure information asymmetry — deeper scene knowledge than the casual fans who dominate Kalshi esports liquidity.

---

## Architecture

```
cop4521proj/
├── shared/               # Shared utilities (used by all bots)
│   ├── storage.py        # SQLite base class
│   ├── kelly.py          # Half-Kelly position sizing
│   ├── paper_trader.py   # Paper trading simulation
│   └── signal_engine.py  # Abstract signal engine interface
├── orchestrator/         # Multi-bot orchestrator
│   └── orchestrator.py
├── botb/                 # Bot B: sports (stub)
├── botc/                 # Bot C: economics (stub)
├── botd/                 # Bot D: esports (this bot)
│   ├── bot.py            # Main entry point
│   ├── config.py         # Configuration
│   ├── signals.py        # Signal engine (combines all data sources)
│   ├── data/
│   │   ├── hltv.py       # CS2 data from HLTV
│   │   ├── vlr.py        # Valorant data from VLR.gg
│   │   └── liquipedia.py # Tournament data from Liquipedia
│   ├── models/
│   │   └── probability.py # Win probability model
│   └── storage/
│       └── db.py         # All cs2_* / val_* / liq_* tables
├── dashboard/
│   └── dashboard.py      # Unified Flask dashboard
└── test_pipeline.py      # Pipeline smoke test
```

---

## Data Sources

| Source | Game | Data |
|--------|------|------|
| HLTV | CS2 | World rankings, match history, map win rates, H2H, player ratings (HLTV 2.0), roster changes, tournament tier |
| VLR.gg | Valorant | Rankings, match history, map win rates, agent composition, player ACS, roster changes |
| Liquipedia | Both | Upcoming match schedule, tournament brackets (eliminated/qualified), prize pool tier, match format (Bo1/Bo3/Bo5) |

---

## SQLite Schema

All data stored in a single `botd.db` file with prefixed tables:

- `cs2_*` — CS2/HLTV data
- `val_*` — Valorant/VLR data
- `liq_*` — Liquipedia tournament/schedule data
- `trades_*` — Shared paper trader ledger (all bots)

---

## Setup

```bash
pip install -r requirements.txt
cp .env.example .env
# Edit .env as needed
```

---

## Usage

```bash
# Run pipeline test (live fetch)
python test_pipeline.py

# Run pipeline test (offline, use cached data)
python test_pipeline.py --offline

# Run one signal cycle
python -m botd.bot run

# Run continuous loop (every 15 min)
python -m botd.bot loop

# Force full data sync
python -m botd.bot sync

# Status report
python -m botd.bot status

# Dashboard (http://127.0.0.1:5001)
python -m dashboard.dashboard
```

---

## Signal Model

Win probability is a weighted blend of 6 components:

| Component | Weight | Description |
|-----------|--------|-------------|
| World ranking | 20% | Elo-like sigmoid on ranking differential |
| Recent form | 25% | Recency-weighted win rate (decay=0.85/match), tier-weighted |
| Head-to-head | 15% | Laplace-smoothed H2H win rate |
| Map pool | 15% | Average win rate across maps |
| Player ratings | 15% | HLTV 2.0 / ACS differential |
| Roster stability | 10% | Penalises recent roster changes |

Format adjustment: Bo1 shrinks toward 50% (high variance), Bo5 stretches away (skill dominates).

Position sizing: **half-Kelly** (fraction=0.5), capped at 10% of bankroll per trade.

---

## Edge Thesis

Most Kalshi participants trading esports markets are casual fans betting on name recognition. They don't know:
- Current team form (teams go on 10-match losing streaks with no market reaction)
- Roster changes (a star player leaves → odds barely move on Kalshi)
- Map veto implications (teams strong on Mirage but weak on Inferno, and the opponent just banned Mirage)
- Tournament context (a team already qualified plays relaxed in group stage)
- Bo1 vs Bo3 variance (favorites are dramatically less reliable in Bo1)

We systematically know all of this. That is the edge.
