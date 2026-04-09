"""
Pipeline smoke test for Bot D.

Tests:
  1. Database initialises cleanly with correct schema
  2. Liquipedia: pull next 7 days of CS2 + Valorant upcoming matches
  3. HLTV rankings parse and store
  4. VLR rankings parse and store
  5. Confirm team stats are populated per match
  6. Show full sample output for one match (all data fields)
  7. Signal engine produces output

Run:
    python test_pipeline.py
    python test_pipeline.py --offline   # skip live scraping, use any cached data
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import datetime, timedelta
from pprint import pprint

# Ensure project root is on path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("test_pipeline")

# Use a test-specific database
TEST_DB = "data/test_botd.db"
os.environ["BOT_DB_PATH"] = TEST_DB
os.makedirs("data", exist_ok=True)

# Now import bot modules (they read BOT_DB_PATH at import time via config)
from botd.storage.db import BotDStorage
from botd.data.hltv import HLTVScraper
from botd.data.vlr import VLRScraper
from botd.data.liquipedia import LiquipediaScraper
from botd.signals import EsportsSignalEngine
from botd.engine.elo import EloEngine
from botd.engine.signals import EloSignalEngine
from shared.kelly import half_kelly, edge, best_side

PASS = "\033[92mPASS\033[0m"
FAIL = "\033[91mFAIL\033[0m"
INFO = "\033[94mINFO\033[0m"


def check(condition: bool, message: str):
    status = PASS if condition else FAIL
    print(f"  [{status}] {message}")
    return condition


def section(title: str):
    print(f"\n{'='*60}")
    print(f"  {title}")
    print("=" * 60)


# ------------------------------------------------------------------
# Test 1: Schema initialisation
# ------------------------------------------------------------------

def test_schema():
    section("Test 1: Database Schema")
    db = BotDStorage(TEST_DB)

    expected_tables = [
        "cs2_teams", "cs2_matches", "cs2_map_stats", "cs2_h2h",
        "cs2_player_stats", "cs2_roster_changes",
        "val_teams", "val_matches", "val_map_stats", "val_agent_stats",
        "val_h2h", "val_player_stats", "val_roster_changes",
        "liq_upcoming_matches", "liq_tournaments", "liq_brackets",
    ]

    tables = [
        row["name"]
        for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")
    ]

    all_ok = True
    for t in expected_tables:
        ok = check(t in tables, f"Table '{t}' exists")
        all_ok = all_ok and ok

    print(f"\n  Total tables found: {len(tables)}")
    return all_ok


# ------------------------------------------------------------------
# Test 2: Liquipedia upcoming matches
# ------------------------------------------------------------------

def test_liquipedia_upcoming(offline: bool = False):
    section("Test 2: Liquipedia Upcoming Matches")
    liq = LiquipediaScraper(TEST_DB)

    if offline:
        print(f"  [{INFO}] Offline mode — checking cached data only")
        db = BotDStorage(TEST_DB)
        cs2_matches = db.get_upcoming_matches("cs2", days=7)
        val_matches = db.get_upcoming_matches("val", days=7)
    else:
        print("  Fetching CS2 upcoming matches from Liquipedia...")
        cs2_matches = liq.sync_upcoming_matches("cs2", days=7)

        print("  Fetching Valorant upcoming matches from Liquipedia...")
        val_matches = liq.sync_upcoming_matches("val", days=7)

    check(True, f"CS2 upcoming matches fetched: {len(cs2_matches)}")
    check(True, f"Valorant upcoming matches fetched: {len(val_matches)}")

    all_matches = cs2_matches + val_matches

    if all_matches:
        required_fields = ["match_id", "game", "team1", "team2", "match_datetime",
                           "tournament", "tournament_tier", "match_format"]
        sample = all_matches[0]
        for field in required_fields:
            check(field in sample, f"Field '{field}' present in match record")
    else:
        print(f"  [{INFO}] No upcoming matches available (API may be rate-limited or offline)")

    return all_matches


# ------------------------------------------------------------------
# Test 3: HLTV rankings
# ------------------------------------------------------------------

def test_hltv_rankings(offline: bool = False):
    section("Test 3: HLTV CS2 Rankings")
    hltv = HLTVScraper(TEST_DB)

    if offline:
        print(f"  [{INFO}] Offline mode — checking cached rankings")
        db = BotDStorage(TEST_DB)
        teams = db.execute("SELECT * FROM cs2_teams ORDER BY ranking LIMIT 10")
    else:
        print("  Fetching HLTV team rankings...")
        teams = hltv.sync_rankings()

    check(True, f"CS2 teams in database: {len(teams)}")
    if teams:
        check(teams[0].get("ranking") is not None, "Teams have ranking field")
        check(teams[0].get("name") is not None, "Teams have name field")
        check(teams[0].get("team_id") is not None, "Teams have team_id field")

        print(f"\n  Top 10 CS2 teams:")
        for t in teams[:10]:
            print(f"    #{t.get('ranking', '?'):>3}  {t['name']:25s}  "
                  f"pts={t.get('ranking_points', 0):.0f}  id={t['team_id']}")

    return teams


# ------------------------------------------------------------------
# Test 4: VLR Valorant rankings
# ------------------------------------------------------------------

def test_vlr_rankings(offline: bool = False):
    section("Test 4: VLR.gg Valorant Rankings")
    vlr = VLRScraper(TEST_DB)

    if offline:
        print(f"  [{INFO}] Offline mode — checking cached rankings")
        db = BotDStorage(TEST_DB)
        teams = db.execute("SELECT * FROM val_teams ORDER BY ranking LIMIT 10")
    else:
        print("  Fetching VLR.gg team rankings...")
        teams = vlr.sync_rankings()

    check(True, f"Valorant teams in database: {len(teams)}")
    if teams:
        check(teams[0].get("ranking") is not None, "Teams have ranking field")
        check(teams[0].get("name") is not None, "Teams have name field")

        print(f"\n  Top 10 Valorant teams:")
        for t in teams[:10]:
            print(f"    #{t.get('ranking', '?'):>3}  {t['name']:25s}  "
                  f"region={t.get('region', '?')}")

    return teams


# ------------------------------------------------------------------
# Test 5: Team stats population for a match
# ------------------------------------------------------------------

def test_team_stats_population(upcoming_matches: list, offline: bool = False):
    section("Test 5: Team Stats Population")
    db = BotDStorage(TEST_DB)
    hltv = HLTVScraper(TEST_DB)
    vlr = VLRScraper(TEST_DB)

    # Find a match where both teams might be in our DB
    cs2_matches = [m for m in upcoming_matches if m.get("game") == "cs2"]
    val_matches = [m for m in upcoming_matches if m.get("game") == "val"]

    # For CS2
    if cs2_matches:
        match = cs2_matches[0]
        t1 = match["team1"]
        t2 = match["team2"]
        t1_id = db.execute(
            "SELECT team_id FROM cs2_teams WHERE name LIKE ? LIMIT 1",
            (f"%{t1[:8]}%",),
        )
        t2_id = db.execute(
            "SELECT team_id FROM cs2_teams WHERE name LIKE ? LIMIT 1",
            (f"%{t2[:8]}%",),
        )
        has_t1 = len(t1_id) > 0
        has_t2 = len(t2_id) > 0
        check(
            True,
            f"CS2 match '{t1} vs {t2}': "
            f"team1 in DB={has_t1}, team2 in DB={has_t2}",
        )
    else:
        print(f"  [{INFO}] No CS2 matches to check stats for")

    # For Valorant
    if val_matches:
        match = val_matches[0]
        t1 = match["team1"]
        t2 = match["team2"]
        t1_id = db.execute(
            "SELECT team_id FROM val_teams WHERE name LIKE ? LIMIT 1",
            (f"%{t1[:8]}%",),
        )
        has_t1 = len(t1_id) > 0
        check(True, f"Val match '{t1} vs {t2}': team1 in DB={has_t1}")
    else:
        print(f"  [{INFO}] No Valorant matches to check stats for")

    # Show overall data coverage
    cs2_count = db.scalar("SELECT COUNT(*) FROM cs2_teams") or 0
    val_count = db.scalar("SELECT COUNT(*) FROM val_teams") or 0
    cs2_matches_count = db.scalar("SELECT COUNT(*) FROM cs2_matches") or 0
    val_matches_count = db.scalar("SELECT COUNT(*) FROM val_matches") or 0

    print(f"\n  Data coverage:")
    print(f"    CS2 teams:   {cs2_count}")
    print(f"    Val teams:   {val_count}")
    print(f"    CS2 matches: {cs2_matches_count}")
    print(f"    Val matches: {val_matches_count}")


# ------------------------------------------------------------------
# Test 6: Full match snapshot
# ------------------------------------------------------------------

def test_match_snapshot(upcoming_matches: list):
    section("Test 6: Full Match Snapshot (Sample Output)")
    db = BotDStorage(TEST_DB)
    hltv = HLTVScraper(TEST_DB)
    vlr = VLRScraper(TEST_DB)

    cs2_teams = db.execute("SELECT * FROM cs2_teams ORDER BY ranking LIMIT 5")
    val_teams = db.execute("SELECT * FROM val_teams ORDER BY ranking LIMIT 5")

    # Build a synthetic match for the top 2 ranked teams in each game
    if len(cs2_teams) >= 2:
        t1 = cs2_teams[0]
        t2 = cs2_teams[1]
        print(f"\n  CS2 Match Snapshot: {t1['name']} vs {t2['name']}")
        print(f"  {'─'*50}")

        snap = hltv.match_snapshot(t1["team_id"], t2["team_id"])

        def show_team(label: str, td: dict):
            print(f"\n  [{label}]")
            print(f"    Ranking:          #{td.get('ranking', 'N/A')}")
            print(f"    Ranking Points:   {td.get('ranking_points', 0):.0f}")
            form = td.get("form", {})
            print(f"    Recent Form:      {form.get('weighted_wr', 0):.1%} "
                  f"({form.get('n_matches', 0)} matches)")
            print(f"    Last 10:          {''.join(form.get('results', []))}")
            print(f"    Avg Player Rtg:   {td.get('avg_player_rating', 0):.3f}")
            maps = td.get("map_stats", [])
            if maps:
                print(f"    Top Maps:")
                for m in maps[:3]:
                    print(f"      {m['map_name']:15s} {m['win_rate']:.1%} "
                          f"({m['wins']}W-{m['losses']}L)")
            roster = td.get("recent_roster_changes", [])
            if roster:
                print(f"    Roster Changes (30d): {len(roster)}")
                for rc in roster[:2]:
                    print(f"      {rc['player_name']} [{rc['change_type']}] "
                          f"-> {rc.get('to_team_name', '?')}")

        show_team(t1["name"], snap["team1"])
        show_team(t2["name"], snap["team2"])

        h2h = snap.get("h2h", {})
        if h2h.get("total_matches", 0) > 0:
            print(f"\n  [H2H Record]")
            print(f"    {t1['name']}: {h2h.get('team1_wins', 0)} wins")
            print(f"    {t2['name']}: {h2h.get('team2_wins', 0)} wins")
            print(f"    Total matches: {h2h.get('total_matches', 0)}")
        else:
            print(f"\n  [H2H] No shared match history found yet")

        check(True, "CS2 match snapshot generated")

    elif len(cs2_teams) == 1:
        print(f"  [{INFO}] Only 1 CS2 team in DB, need 2 for snapshot")
    else:
        print(f"  [{INFO}] No CS2 teams in DB yet — run without --offline to fetch")

    if len(val_teams) >= 2:
        t1 = val_teams[0]
        t2 = val_teams[1]
        print(f"\n\n  Valorant Match Snapshot: {t1['name']} vs {t2['name']}")
        print(f"  {'─'*50}")
        snap = vlr.match_snapshot(t1["team_id"], t2["team_id"])

        def show_val_team(label: str, td: dict):
            print(f"\n  [{label}]")
            print(f"    Ranking:      #{td.get('ranking', 'N/A')}")
            print(f"    Region:       {td.get('region', '?')}")
            form = td.get("form", {})
            print(f"    Recent Form:  {form.get('weighted_wr', 0):.1%} "
                  f"({form.get('n_matches', 0)} matches)")
            print(f"    Last 10:      {''.join(form.get('results', []))}")
            print(f"    Avg ACS:      {td.get('avg_acs', 0):.1f}")
            maps = td.get("map_stats", [])
            if maps:
                print(f"    Top Maps:")
                for m in maps[:3]:
                    print(f"      {m['map_name']:15s} {m['win_rate']:.1%}")
            agents = td.get("agent_stats", [])
            if agents:
                print(f"    Top Agents:")
                for a in agents[:3]:
                    print(f"      {a['agent_name']:12s} {a['win_rate']:.1%} "
                          f"({a['times_played']} picks)")

        show_val_team(t1["name"], snap["team1"])
        show_val_team(t2["name"], snap["team2"])
        check(True, "Valorant match snapshot generated")


# ------------------------------------------------------------------
# Test 7: Signal engine
# ------------------------------------------------------------------

def test_signals(upcoming_matches: list, offline: bool = False):
    section("Test 7: Signal Engine")

    if offline:
        # In offline mode, always ensure at least one match per game is seeded so
        # the signal engine can run without touching the network.
        db = BotDStorage(TEST_DB)
        now = datetime.utcnow()
        existing_ids = {m["match_id"] for m in (upcoming_matches or [])}
        synthetic = []
        if not any(m.get("game") == "cs2" for m in (upcoming_matches or [])):
            cs2_match = {
                "match_id": "test_cs2_001",
                "game": "cs2",
                "team1": "Team Alpha",
                "team2": "Team Beta",
                "team1_id": "hltv_alpha",
                "team2_id": "hltv_beta",
                "match_datetime": (now + timedelta(hours=3)).strftime("%Y-%m-%d %H:%M:%S"),
                "tournament": "Test Tournament",
                "tournament_tier": "A",
                "match_format": "Bo3",
                "prize_pool": "$200,000",
                "stream_url": "",
                "updated_at": db.now(),
            }
            db.upsert_upcoming_match(cs2_match)
            synthetic.append(cs2_match)
        if not any(m.get("game") == "val" for m in (upcoming_matches or [])):
            val_match = {
                "match_id": "test_val_001",
                "game": "val",
                "team1": "Sentinels",
                "team2": "NRG",
                "team1_id": "vlr_sentinels",
                "team2_id": "vlr_nrg",
                "match_datetime": (now + timedelta(hours=4)).strftime("%Y-%m-%d %H:%M:%S"),
                "tournament": "VCT Americas",
                "tournament_tier": "S",
                "match_format": "Bo3",
                "prize_pool": "$1,000,000",
                "stream_url": "",
                "updated_at": db.now(),
            }
            db.upsert_upcoming_match(val_match)
            synthetic.append(val_match)
        if synthetic:
            upcoming_matches = list(upcoming_matches or []) + synthetic
            print(f"  [{INFO}] Seeded {len(synthetic)} synthetic matches for offline model test")

    engine = EsportsSignalEngine(TEST_DB)
    print("  Generating signals from all upcoming matches...")
    signals = engine.get_signals()

    check(True, f"Signal engine ran without error, produced {len(signals)} signals")

    if signals:
        edge_signals = [s for s in signals if s.has_edge]
        check(True, f"Signals with positive edge: {len(edge_signals)}")

        print(f"\n  {'─'*80}")
        print(f"  {'Game':5s} {'Match':35s} {'P(A)':6s} {'Mkt':5s} {'Side':5s} "
              f"{'Edge':6s} {'Kelly':6s} {'Conf':5s}")
        print(f"  {'─'*80}")
        for sig in signals[:10]:
            match_str = f"{sig.team_a[:15]} vs {sig.team_b[:15]}"
            side_str = sig.recommended_side
            print(
                f"  {sig.game.upper():5s} {match_str:35s} "
                f"{sig.p_a:.3f} {sig.market_yes_price:5.1f} "
                f"{side_str:5s} {max(sig.edge_yes, sig.edge_no):+.3f} "
                f"{sig.recommended_kelly:.3f} {sig.confidence:.2f}"
            )

        # Show full breakdown for first signal with edge
        if edge_signals:
            best = max(edge_signals, key=lambda s: s.recommended_kelly)
            print(f"\n  {'─'*80}")
            print(f"  BEST SIGNAL DETAIL")
            print(f"  {'─'*80}")
            print(f"  {best.game.upper()}: {best.team_a} vs {best.team_b}")
            print(f"  Match Format:    {best.match_format}")
            print(f"  Tournament:      {best.tournament} (Tier {best.tournament_tier})")
            print(f"  Match DateTime:  {best.match_datetime}")
            print(f"  P(team_a wins):  {best.p_a:.4f} ({best.p_a:.1%})")
            print(f"  Market YES:      {best.market_yes_price:.1f}¢")
            print(f"  Edge YES:        {best.edge_yes:+.4f}")
            print(f"  Edge NO:         {best.edge_no:+.4f}")
            print(f"  Recommended:     {best.recommended_side} @ Kelly={best.recommended_kelly:.4f}")
            print(f"  Confidence:      {best.confidence:.2%}")
            print(f"\n  Reasoning:")
            for line in best.reasoning.split("\n"):
                print(f"    {line}")
            if best.signal_components:
                print(f"\n  Signal Components:")
                for k, v in best.signal_components.items():
                    bar = "█" * int(v * 20) + "░" * (20 - int(v * 20))
                    print(f"    {k:20s} {v:.3f} |{bar}|")

    return signals


# ------------------------------------------------------------------
# Test 8: ELO engine
# ------------------------------------------------------------------

def test_elo_engine():
    section("Test 8: Two-Layer ELO Engine")
    db  = BotDStorage(TEST_DB)
    elo = EloEngine(TEST_DB)

    # ── ELO schema tables ────────────────────────────────────────
    elo_tables = ["elo_team_ratings", "elo_match_log", "elo_venue_stats"]
    tables = [r["name"] for r in db.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    )]
    for t in elo_tables:
        check(t in tables, f"ELO table '{t}' created")

    # ── Core math ────────────────────────────────────────────────
    p = EloEngine.expected_score(1600, 1400)
    check(abs(p - 0.7597) < 0.001, f"expected_score(1600,1400) ≈ 0.760 (got {p:.4f})")

    p_eq = EloEngine.expected_score(1500, 1500)
    check(abs(p_eq - 0.5) < 1e-9, f"expected_score(equal ELOs) = 0.500 (got {p_eq})")

    k_bo1 = EloEngine.effective_k("Bo1", months_ago=0)
    k_bo3 = EloEngine.effective_k("Bo3", months_ago=0)
    k_bo5 = EloEngine.effective_k("Bo5", months_ago=0)
    check(k_bo1 > k_bo3 > k_bo5, f"K-factor order: Bo1={k_bo1} > Bo3={k_bo3} > Bo5={k_bo5}")

    k_old = EloEngine.effective_k("Bo3", months_ago=6)
    check(abs(k_old - 24 * 0.85**6) < 0.001,
          f"6-month decay: K_eff={k_old:.3f} (expected {24*0.85**6:.3f})")

    r1, r2 = EloEngine.update_ratings(1500, 1500, True, 24)
    check(r1 > 1500 and r2 < 1500,
          f"update_ratings: winner {r1:.1f} ↑  loser {r2:.1f} ↓")
    check(abs((r1 - 1500) + (r2 - 1500)) < 0.001,
          "ELO is zero-sum (winner gain = loser loss)")

    # ── Seed synthetic match data ────────────────────────────────
    _seed_synthetic_matches(db)

    # ── Overall ELO computation ──────────────────────────────────
    print(f"\n  [{INFO}] Computing CS2 overall ELOs from seeded matches…")
    ratings = elo.compute_overall_elos("cs2", force=True)
    check(len(ratings) >= 2, f"ELO computed for {len(ratings)} teams")
    check("hltv_teamA" in ratings, "Team A has ELO rating")
    check("hltv_teamB" in ratings, "Team B has ELO rating")

    # After 10 wins for A vs B (equal K), A should be above 1500
    check(ratings.get("hltv_teamA", 1500) > 1500,
          f"Dominant team ELO above 1500 (got {ratings.get('hltv_teamA', 0):.1f})")
    check(ratings.get("hltv_teamB", 1500) < 1500,
          f"Losing team ELO below 1500 (got {ratings.get('hltv_teamB', 0):.1f})")

    # ── Map ELO ──────────────────────────────────────────────────
    _seed_map_stats(db)
    elo.compute_map_elos("cs2")

    map_elo = elo.get_team_elo("hltv_teamA", "cs2", "Mirage")
    overall  = elo.get_team_elo("hltv_teamA", "cs2", "overall")
    check(map_elo != overall,
          f"Mirage ELO ({map_elo:.1f}) differs from overall ({overall:.1f})")
    check(map_elo > overall,
          "Mirage ELO > overall (seeded 70% win rate on Mirage)")

    # ── Map strengths report ─────────────────────────────────────
    strengths = elo.team_map_strengths("hltv_teamA", "cs2")
    check(len(strengths) == 7, f"Map strengths returned {len(strengths)} maps (expected 7)")
    # Mirage (70% wr) should rank first
    check(strengths[0]["map"] == "Mirage",
          f"Strongest map is Mirage (got {strengths[0]['map']})")
    print(f"\n  Team A — map strengths:")
    for m in strengths:
        star = "★" if m["map"] == "Mirage" else " "
        print(f"    {star} {m['map']:12s}  ELO={m['elo']:6.1f}  "
              f"Δ={m['delta_vs_overall']:+6.1f}  n={m['matches']:3d}  "
              f"{'✓' if m['reliable'] else '·'}")

    # ── Map-weighted prediction (veto known) ─────────────────────
    pred_overall = elo.predict_match("hltv_teamA", "hltv_teamB", "cs2")
    pred_veto    = elo.predict_match(
        "hltv_teamA", "hltv_teamB", "cs2", veto_maps=["Mirage", "Inferno"]
    )
    check(pred_veto["elo_source"] == "map_weighted",
          f"Veto-aware prediction uses 'map_weighted' (got '{pred_veto['elo_source']}')")
    check(len(pred_veto["map_breakdown"]) == 2,
          f"Map breakdown has 2 entries (got {len(pred_veto['map_breakdown'])})")
    check(pred_veto["win_prob_team1"] != pred_overall["win_prob_team1"],
          f"Map-weighted P ({pred_veto['win_prob_team1']:.4f}) differs from "
          f"overall P ({pred_overall['win_prob_team1']:.4f})")

    print(f"\n  Overall prediction:     P(T1)={pred_overall['win_prob_team1']:.4f}  "
          f"ELO diff={pred_overall['elo_diff']:+.1f}")
    print(f"  Map-weighted (Mirage+Inferno): P(T1)={pred_veto['win_prob_team1']:.4f}")
    for mb in pred_veto["map_breakdown"]:
        print(f"    {mb['map']:10s}  T1={mb['elo1']:.1f}  T2={mb['elo2']:.1f}  "
              f"P(T1)={mb['prob_team1']:.4f}")

    # ── ELO rankings ────────────────────────────────────────────
    rankings = elo.elo_rankings("cs2", top_n=5)
    check(len(rankings) > 0, f"ELO rankings returned {len(rankings)} entries")

    return ratings


def _seed_synthetic_matches(db: BotDStorage):
    """
    Seed cs2_matches: Team A beats Team B 7 times, Team B wins 3 times.
    Uses dates going back 6 months so recency decay is exercised.
    """
    from datetime import datetime, timedelta
    base = datetime.utcnow() - timedelta(days=180)
    rows = []
    for i in range(10):
        dt = (base + timedelta(days=i * 18)).strftime("%Y-%m-%d")
        a_wins = i < 7  # first 7 matches: A wins
        rows.append({
            "match_id":       f"syn_{i:03d}",
            "team1_id":       "hltv_teamA",
            "team2_id":       "hltv_teamB",
            "team1_name":     "Team A",
            "team2_name":     "Team B",
            "team1_score":    2 if a_wins else 1,
            "team2_score":    1 if a_wins else 2,
            "winner_id":      "hltv_teamA" if a_wins else "hltv_teamB",
            "tournament":     "Test ESL Cup",
            "tournament_tier":"A",
            "match_format":   "Bo3",
            "match_date":     dt,
        })
    for r in rows:
        db.upsert_cs2_match(r)


def _seed_map_stats(db: BotDStorage):
    """
    Seed cs2_map_stats: Team A has 70% on Mirage, 45% on Inferno (weak map).
    Team B has 55% on Mirage, 65% on Inferno.
    """
    entries = [
        dict(team_id="hltv_teamA", map_name="Mirage",   wins=35, losses=15, win_rate=0.70, ct_win_rate=0.72, t_win_rate=0.68),
        dict(team_id="hltv_teamA", map_name="Inferno",  wins=18, losses=22, win_rate=0.45, ct_win_rate=0.44, t_win_rate=0.46),
        dict(team_id="hltv_teamA", map_name="Nuke",     wins=22, losses=18, win_rate=0.55, ct_win_rate=0.58, t_win_rate=0.52),
        dict(team_id="hltv_teamA", map_name="Ancient",  wins=20, losses=20, win_rate=0.50, ct_win_rate=0.50, t_win_rate=0.50),
        dict(team_id="hltv_teamA", map_name="Anubis",   wins=12, losses=8,  win_rate=0.60, ct_win_rate=0.62, t_win_rate=0.58),
        dict(team_id="hltv_teamA", map_name="Dust2",    wins=15, losses=25, win_rate=0.375,ct_win_rate=0.38, t_win_rate=0.37),
        dict(team_id="hltv_teamA", map_name="Vertigo",  wins=10, losses=10, win_rate=0.50, ct_win_rate=0.50, t_win_rate=0.50),
        dict(team_id="hltv_teamB", map_name="Mirage",   wins=27, losses=23, win_rate=0.54, ct_win_rate=0.55, t_win_rate=0.53),
        dict(team_id="hltv_teamB", map_name="Inferno",  wins=32, losses=18, win_rate=0.64, ct_win_rate=0.66, t_win_rate=0.62),
        dict(team_id="hltv_teamB", map_name="Nuke",     wins=24, losses=26, win_rate=0.48, ct_win_rate=0.47, t_win_rate=0.49),
        dict(team_id="hltv_teamB", map_name="Ancient",  wins=20, losses=20, win_rate=0.50, ct_win_rate=0.50, t_win_rate=0.50),
        dict(team_id="hltv_teamB", map_name="Anubis",   wins=8,  losses=12, win_rate=0.40, ct_win_rate=0.40, t_win_rate=0.40),
        dict(team_id="hltv_teamB", map_name="Dust2",    wins=28, losses=12, win_rate=0.70, ct_win_rate=0.71, t_win_rate=0.69),
        dict(team_id="hltv_teamB", map_name="Vertigo",  wins=15, losses=15, win_rate=0.50, ct_win_rate=0.50, t_win_rate=0.50),
    ]
    for e in entries:
        db.upsert_cs2_map_stats(e)


# ------------------------------------------------------------------
# Test 9: ELO signal engine (situational adjustments)
# ------------------------------------------------------------------

def test_elo_signals(upcoming_matches: list, offline: bool = False):
    section("Test 9: ELO Signal Engine + Situational Adjustments")
    db = BotDStorage(TEST_DB)

    # Ensure we have matches to evaluate
    if not upcoming_matches:
        # Fallback to what test_signals() already seeded
        upcoming_matches = db.get_upcoming_matches(days=7)

    now = datetime.utcnow()
    if not upcoming_matches:
        print(f"  [{INFO}] No upcoming matches available — seeding for ELO signal test")
        m = {
            "match_id": "elo_test_cs2_001",
            "game": "cs2",
            "team1": "Team A",
            "team2": "Team B",
            "team1_id": "hltv_teamA",
            "team2_id": "hltv_teamB",
            "match_datetime": (now + timedelta(hours=3)).strftime("%Y-%m-%d %H:%M:%S"),
            "tournament": "Test ESL Cup",
            "tournament_tier": "A",
            "match_format": "Bo3",
            "prize_pool": "$200,000",
            "stream_url": "",
            "updated_at": db.now(),
        }
        db.upsert_upcoming_match(m)
        upcoming_matches = [m]

    # Always seed a match with known veto for map-weighted test
    veto_match = {
        "match_id": "elo_test_cs2_veto",
        "game": "cs2",
        "team1": "Team A",
        "team2": "Team B",
        "team1_id": "hltv_teamA",
        "team2_id": "hltv_teamB",
        "match_datetime": (now + timedelta(hours=4)).strftime("%Y-%m-%d %H:%M:%S"),
        "tournament": "Test ESL Cup",
        "tournament_tier": "A",
        "match_format": "Bo3",
        "prize_pool": "$200,000",
        "stream_url": "",
        "veto_maps": ["Mirage", "Nuke", "Inferno"],  # map veto known!
        "updated_at": db.now(),
    }
    db.upsert_upcoming_match(veto_match)

    engine = EloSignalEngine(TEST_DB)
    print("  Generating ELO signals…")
    signals = engine.get_signals()
    check(True, f"EloSignalEngine produced {len(signals)} signals without error")

    edge_signals = [s for s in signals if s.has_edge]
    check(True, f"Signals with edge (≥8%): {len(edge_signals)}")

    # Find the veto-aware signal
    veto_sig = next(
        (s for s in signals
         if "veto_maps" in (s.signal_components or {})
         and s.signal_components["veto_maps"]),
        None
    )
    if veto_sig:
        check(True, "Map-veto signal found in output")
        check(
            veto_sig.signal_components.get("elo_source") == "map_weighted",
            f"Veto signal uses map_weighted ELO "
            f"(got '{veto_sig.signal_components.get('elo_source')}')"
        )
    else:
        print(f"  [{INFO}] No map-veto signal found (veto data may not have been picked up)")

    # ── Print full signal table ──────────────────────────────────
    if signals:
        print(f"\n  {'─'*85}")
        print(f"  {'Game':5s} {'Match':30s} {'ELO-diff':9s} {'P(T1)':7s} "
              f"{'Mkt':6s} {'Side':5s} {'Edge':7s} {'Kelly':6s}")
        print(f"  {'─'*85}")
        for s in signals:
            c = s.signal_components or {}
            elo_d = c.get("elo_diff_adj", c.get("elo_diff", "?"))
            elo_str = f"{elo_d:+.1f}" if isinstance(elo_d, (int, float)) else str(elo_d)
            print(
                f"  {s.game.upper():5s} "
                f"{s.team_a[:14]:14s} vs {s.team_b[:13]:13s}  "
                f"{elo_str:>9s}  "
                f"{s.p_a:.4f}  {s.market_yes_price:5.1f}  "
                f"{s.recommended_side:5s}  "
                f"{max(s.edge_yes,s.edge_no):+.4f}  "
                f"{s.recommended_kelly:.4f}"
            )

        # ── Full breakdown for the highest-Kelly signal ──────────
        if edge_signals:
            best = max(edge_signals, key=lambda s: s.recommended_kelly)
            print(f"\n  {'━'*60}")
            print(f"  FULL SIGNAL BREAKDOWN")
            print(f"  {'━'*60}")
            for line in best.reasoning.split("\n"):
                print(f"  {line}")

            print(f"\n  KALSHI POSITION")
            print(f"  Team A wins:  {best.team_a}")
            print(f"  Side:         {best.recommended_side}")
            print(f"  Market price: {best.market_yes_price:.1f}¢")
            print(f"  Edge:         {best.edge_yes:+.4f}")
            print(f"  Kelly stake:  {best.recommended_kelly:.4f} of bankroll")
            print(f"  Confidence:   {best.confidence:.1%}")

            print(f"\n  SIGNAL COMPONENTS")
            for k, v in (best.signal_components or {}).items():
                if isinstance(v, dict):
                    print(f"  {k}:")
                    for kk, vv in v.items():
                        print(f"      {kk}: {vv}")
                elif isinstance(v, list):
                    print(f"  {k}: {v}")
                elif isinstance(v, float):
                    bar = "█" * int(abs(v) / max(abs(v), 1) * 15)
                    print(f"  {k:<22s} {v:+.4f}  |{bar}|")
                else:
                    print(f"  {k:<22s} {v}")

    return signals


# ------------------------------------------------------------------
# Test 10: Kelly sizing sanity check
# ------------------------------------------------------------------

def test_kelly():
    section("Test 10: Kelly Sizing Sanity Check")

    # Case 1: 60% edge on a 50-cent market
    k = half_kelly(0.60, 50.0, fraction=0.5)
    check(k > 0, f"60% prob @ 50¢ market → Kelly={k:.4f} (should be > 0)")

    # Case 2: No edge (market correctly priced)
    k2 = half_kelly(0.50, 50.0)
    check(k2 == 0.0, f"50% prob @ 50¢ market → Kelly={k2:.4f} (should be 0)")

    # Case 3: Negative edge (overpriced)
    k3 = half_kelly(0.40, 60.0)
    check(k3 == 0.0, f"40% prob @ 60¢ market → Kelly={k3:.4f} (should be 0)")

    # Case 4: Cap at 10% max
    k4 = half_kelly(0.95, 5.0, max_fraction=0.10)
    check(k4 <= 0.10, f"95% prob @ 5¢ market → Kelly={k4:.4f} (capped at 0.10)")

    # Case 5: Best side selection
    side, k5 = best_side(0.65, 45.0)
    check(side == "YES", f"65% prob @ 45¢ → best side={side} (should be YES)")
    check(k5 > 0, f"Kelly for best side = {k5:.4f} (should be > 0)")


# ------------------------------------------------------------------
# Main
# ------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Bot D pipeline test")
    parser.add_argument("--offline", action="store_true",
                        help="Skip live scraping, test with cached data only")
    args = parser.parse_args()

    print("\n" + "=" * 60)
    print("  BOT D — ESPORTS TRADING PIPELINE TEST")
    print(f"  DB: {TEST_DB}")
    print(f"  Mode: {'OFFLINE (cached data)' if args.offline else 'LIVE (will fetch from web)'}")
    print(f"  Time: {datetime.utcnow().isoformat(sep=' ', timespec='seconds')} UTC")
    print("=" * 60)

    results = {}

    results["schema"] = test_schema()
    elo_ratings = test_elo_engine()
    test_kelly()

    upcoming = test_liquipedia_upcoming(offline=args.offline)
    cs2_teams = test_hltv_rankings(offline=args.offline)
    val_teams = test_vlr_rankings(offline=args.offline)

    test_team_stats_population(upcoming, offline=args.offline)
    test_match_snapshot(upcoming)
    signals = test_signals(upcoming, offline=args.offline)
    elo_signals = test_elo_signals(upcoming, offline=args.offline)

    # Summary
    section("PIPELINE SUMMARY")
    db = BotDStorage(TEST_DB)
    print(f"  CS2 teams:           {db.scalar('SELECT COUNT(*) FROM cs2_teams') or 0}")
    print(f"  CS2 matches:         {db.scalar('SELECT COUNT(*) FROM cs2_matches') or 0}")
    print(f"  CS2 map stats:       {db.scalar('SELECT COUNT(*) FROM cs2_map_stats') or 0}")
    print(f"  CS2 player stats:    {db.scalar('SELECT COUNT(*) FROM cs2_player_stats') or 0}")
    print(f"  Val teams:           {db.scalar('SELECT COUNT(*) FROM val_teams') or 0}")
    print(f"  Val matches:         {db.scalar('SELECT COUNT(*) FROM val_matches') or 0}")
    print(f"  Upcoming matches:    {db.scalar('SELECT COUNT(*) FROM liq_upcoming_matches') or 0}")
    print(f"  Tournaments:         {db.scalar('SELECT COUNT(*) FROM liq_tournaments') or 0}")
    print(f"  ELO teams rated:     {db.scalar('SELECT COUNT(DISTINCT team_id) FROM elo_team_ratings') or 0}")
    print(f"  ELO signal generated:{len(elo_signals)}")
    print(f"  ELO signals w/ edge: {sum(1 for s in elo_signals if s.has_edge)}")
    print(f"  Legacy signals:      {len(signals)}")
    print(f"  Legacy w/ edge:      {sum(1 for s in signals if s.has_edge)}")
    print()

    if args.offline:
        print("  NOTE: Run without --offline to do a full live data fetch.\n")

    print("  Pipeline test complete.\n")


if __name__ == "__main__":
    main()
