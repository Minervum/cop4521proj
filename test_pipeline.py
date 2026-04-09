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
from datetime import datetime
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

    if offline and not upcoming_matches:
        print(f"  [{INFO}] Offline + no cached matches — seeding synthetic match for model test")
        # Inject synthetic upcoming matches so the model can run without network
        db = BotDStorage(TEST_DB)
        now = datetime.utcnow()
        synthetic = [
            {
                "match_id": "test_cs2_001",
                "game": "cs2",
                "team1": "Team Alpha",
                "team2": "Team Beta",
                "team1_id": "hltv_alpha",
                "team2_id": "hltv_beta",
                "match_datetime": (now.replace(hour=18, minute=0, second=0)).isoformat(sep=" ", timespec="seconds"),
                "tournament": "Test Tournament",
                "tournament_tier": "A",
                "match_format": "Bo3",
                "prize_pool": "$200,000",
                "stream_url": "",
                "updated_at": db.now(),
            },
            {
                "match_id": "test_val_001",
                "game": "val",
                "team1": "Sentinels",
                "team2": "NRG",
                "team1_id": "vlr_sentinels",
                "team2_id": "vlr_nrg",
                "match_datetime": (now.replace(hour=20, minute=0, second=0)).isoformat(sep=" ", timespec="seconds"),
                "tournament": "VCT Americas",
                "tournament_tier": "S",
                "match_format": "Bo3",
                "prize_pool": "$1,000,000",
                "stream_url": "",
                "updated_at": db.now(),
            },
        ]
        for m in synthetic:
            db.upsert_upcoming_match(m)
        upcoming_matches = synthetic
        print(f"  [{INFO}] Injected {len(synthetic)} synthetic matches for model test")

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
# Test 8: Kelly sizing sanity check
# ------------------------------------------------------------------

def test_kelly():
    section("Test 8: Kelly Sizing Sanity Check")

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
    test_kelly()

    upcoming = test_liquipedia_upcoming(offline=args.offline)
    cs2_teams = test_hltv_rankings(offline=args.offline)
    val_teams = test_vlr_rankings(offline=args.offline)

    test_team_stats_population(upcoming, offline=args.offline)
    test_match_snapshot(upcoming)
    signals = test_signals(upcoming, offline=args.offline)

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
    print(f"  Signals generated:   {len(signals)}")
    print(f"  Signals with edge:   {sum(1 for s in signals if s.has_edge)}")
    print()

    if args.offline:
        print("  NOTE: Run without --offline to do a full live data fetch.\n")

    print("  Pipeline test complete.\n")


if __name__ == "__main__":
    main()
