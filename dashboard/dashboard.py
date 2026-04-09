"""
Unified Flask dashboard for the 12-bot Kalshi trading system.

Displays:
  - Per-bot status: bankroll, P&L, open positions, win rate
  - Recent trades across all bots
  - Upcoming esports matches (Bot D specific)
  - Signal stream

Run:
    python -m dashboard.dashboard
"""

from __future__ import annotations

import json
import os
import logging
from datetime import datetime

from flask import Flask, jsonify, render_template_string
from flask_cors import CORS

from botd.bot import BotD
from botd.storage.db import BotDStorage
from botd.config import DB_PATH

logger = logging.getLogger(__name__)

app = Flask(__name__)
CORS(app)

_bot_d: BotD | None = None
_db: BotDStorage | None = None


def _get_bot() -> BotD:
    global _bot_d
    if _bot_d is None:
        _bot_d = BotD()
    return _bot_d


def _get_db() -> BotDStorage:
    global _db
    if _db is None:
        _db = BotDStorage(DB_PATH)
    return _db


# ------------------------------------------------------------------
# HTML dashboard template
# ------------------------------------------------------------------

_DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Kalshi Bot System Dashboard</title>
  <style>
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body { font-family: 'Courier New', monospace; background: #0d1117; color: #c9d1d9; }
    .header { background: #161b22; border-bottom: 1px solid #30363d;
              padding: 16px 24px; display: flex; justify-content: space-between; }
    .header h1 { color: #58a6ff; font-size: 1.2em; }
    .header .ts { color: #8b949e; font-size: 0.8em; }
    .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(280px,1fr));
            gap: 16px; padding: 24px; }
    .card { background: #161b22; border: 1px solid #30363d; border-radius: 6px;
            padding: 16px; }
    .card h2 { color: #58a6ff; font-size: 0.95em; margin-bottom: 12px;
               border-bottom: 1px solid #30363d; padding-bottom: 8px; }
    .metric { display: flex; justify-content: space-between; padding: 4px 0;
              font-size: 0.85em; border-bottom: 1px solid #21262d; }
    .metric:last-child { border-bottom: none; }
    .pos { color: #3fb950; }
    .neg { color: #f85149; }
    .neutral { color: #8b949e; }
    .badge { padding: 2px 8px; border-radius: 12px; font-size: 0.75em; font-weight: bold; }
    .badge-s { background: #1f6feb; color: #fff; }
    .badge-a { background: #388bfd; color: #fff; }
    .badge-b { background: #2ea043; color: #fff; }
    .badge-c { background: #6e7681; color: #fff; }
    .match-row { padding: 8px 0; border-bottom: 1px solid #21262d; font-size: 0.8em; }
    .match-row:last-child { border-bottom: none; }
    .match-teams { font-weight: bold; color: #e6edf3; }
    .match-meta { color: #8b949e; font-size: 0.75em; margin-top: 2px; }
    .signal-yes { color: #3fb950; font-weight: bold; }
    .signal-no  { color: #f85149; font-weight: bold; }
    .signal-pass { color: #6e7681; }
    table { width: 100%; border-collapse: collapse; font-size: 0.8em; }
    th { color: #8b949e; font-weight: normal; text-align: left;
         border-bottom: 1px solid #30363d; padding: 6px 4px; }
    td { padding: 5px 4px; border-bottom: 1px solid #21262d; }
    tr:last-child td { border-bottom: none; }
    .refresh-btn { background: #21262d; color: #58a6ff; border: 1px solid #30363d;
                   padding: 6px 14px; border-radius: 4px; cursor: pointer;
                   font-family: monospace; font-size: 0.85em; }
    .refresh-btn:hover { background: #30363d; }
  </style>
</head>
<body>
  <div class="header">
    <h1>&#127918; Kalshi Esports Bot — Bot D Dashboard</h1>
    <div>
      <span class="ts" id="ts">Loading...</span>
      &nbsp;
      <button class="refresh-btn" onclick="loadAll()">&#8635; Refresh</button>
    </div>
  </div>

  <div class="grid" id="main-grid">
    <!-- Populated by JS -->
  </div>

  <script>
  async function fetchJSON(url) {
    const r = await fetch(url);
    return r.json();
  }

  function pnlClass(v) { return v > 0 ? 'pos' : v < 0 ? 'neg' : 'neutral'; }
  function tierBadge(t) { return `<span class="badge badge-${(t||'c').toLowerCase()}">${t||'?'}</span>`; }

  async function loadAll() {
    document.getElementById('ts').textContent = new Date().toISOString().replace('T',' ').substring(0,19) + ' UTC';
    const grid = document.getElementById('main-grid');
    grid.innerHTML = '<div style="color:#8b949e;padding:24px">Loading...</div>';

    const [status, upcoming, signals] = await Promise.all([
      fetchJSON('/api/status'),
      fetchJSON('/api/upcoming'),
      fetchJSON('/api/signals'),
    ]);

    let html = '';

    // --- Bot D Status Card ---
    const s = status;
    const wr = s.win_rate != null ? (s.win_rate * 100).toFixed(1) + '%' : 'N/A';
    html += `<div class="card">
      <h2>Bot D — Status</h2>
      <div class="metric"><span>Bankroll</span><span>$${(s.bankroll||0).toFixed(2)}</span></div>
      <div class="metric"><span>Total P&L</span>
        <span class="${pnlClass(s.total_pnl)}">$${(s.total_pnl||0).toFixed(2)}</span></div>
      <div class="metric"><span>Win Rate</span><span>${wr}</span></div>
      <div class="metric"><span>Trades (all time)</span><span>${s.total_trades||0}</span></div>
      <div class="metric"><span>Open Positions</span><span>${s.open_positions||0}</span></div>
    </div>`;

    // --- Data Coverage Card ---
    const d = s.data || {};
    html += `<div class="card">
      <h2>Data Coverage</h2>
      <div class="metric"><span>CS2 teams</span><span>${d.cs2_teams||0}</span></div>
      <div class="metric"><span>CS2 matches</span><span>${d.cs2_matches||0}</span></div>
      <div class="metric"><span>Val teams</span><span>${d.val_teams||0}</span></div>
      <div class="metric"><span>Val matches</span><span>${d.val_matches||0}</span></div>
      <div class="metric"><span>Upcoming matches</span><span>${d.upcoming_matches||0}</span></div>
    </div>`;

    // --- Calibration Card ---
    const cal = s.calibration || {};
    html += `<div class="card">
      <h2>Model Calibration</h2>
      <div class="metric"><span>Predictions</span><span>${cal.n_predictions||0}</span></div>
      <div class="metric"><span>Accuracy</span>
        <span>${cal.accuracy != null ? (cal.accuracy*100).toFixed(1)+'%' : 'N/A'}</span></div>
      <div class="metric"><span>Brier Score</span>
        <span>${cal.brier_score != null ? cal.brier_score.toFixed(4) : 'N/A'}</span></div>
      <div class="metric"><span>Status</span>
        <span style="color:#3fb950">PAPER MODE</span></div>
    </div>`;

    // --- Upcoming Matches Card ---
    let mRows = '';
    for (const m of (upcoming||[]).slice(0, 8)) {
      const dt = (m.match_datetime||'').substring(0,16);
      mRows += `<div class="match-row">
        <div class="match-teams">${m.team1} vs ${m.team2}</div>
        <div class="match-meta">
          ${dt} UTC &nbsp;|&nbsp; ${m.match_format||'Bo3'} &nbsp;|&nbsp;
          ${tierBadge(m.tournament_tier)} &nbsp;|&nbsp;
          <span style="color:#8b949e">${(m.tournament||'').substring(0,30)}</span>
        </div>
      </div>`;
    }
    html += `<div class="card" style="grid-column: span 2;">
      <h2>Upcoming Matches (next 7 days)</h2>
      ${mRows || '<div class="neutral" style="padding:8px">No upcoming matches found</div>'}
    </div>`;

    // --- Signals Card ---
    let sigRows = '';
    for (const sig of (signals||[]).slice(0, 10)) {
      const sideClass = sig.recommended_side === 'YES' ? 'signal-yes'
                      : sig.recommended_side === 'NO' ? 'signal-no' : 'signal-pass';
      sigRows += `<tr>
        <td>${sig.game.toUpperCase()}</td>
        <td>${sig.team_a} vs ${sig.team_b}</td>
        <td>${(sig.p_a*100).toFixed(1)}%</td>
        <td>${sig.market_yes_price}¢</td>
        <td class="${sideClass}">${sig.recommended_side}</td>
        <td>${sig.confidence != null ? (sig.confidence*100).toFixed(0)+'%' : '-'}</td>
        <td>${(sig.recommended_kelly*100).toFixed(1)}%</td>
      </tr>`;
    }
    html += `<div class="card" style="grid-column: span 2;">
      <h2>Live Signals</h2>
      <table>
        <thead><tr>
          <th>Game</th><th>Match</th><th>P(team1)</th>
          <th>Mkt Price</th><th>Side</th><th>Conf</th><th>Kelly</th>
        </tr></thead>
        <tbody>${sigRows || '<tr><td colspan="7" class="neutral">No signals — run a cycle first</td></tr>'}</tbody>
      </table>
    </div>`;

    grid.innerHTML = html;
  }

  loadAll();
  setInterval(loadAll, 60000);
  </script>
</body>
</html>"""


# ------------------------------------------------------------------
# Routes
# ------------------------------------------------------------------

@app.route("/")
def index():
    return render_template_string(_DASHBOARD_HTML)


@app.route("/api/status")
def api_status():
    try:
        return jsonify(_get_bot().status())
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.route("/api/upcoming")
def api_upcoming():
    try:
        db = _get_db()
        matches = db.get_upcoming_matches(days=7)
        return jsonify(matches)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.route("/api/signals")
def api_signals():
    try:
        signals = _get_bot().signal_engine.get_signals()
        return jsonify([s.to_dict() for s in signals])
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.route("/api/positions")
def api_positions():
    try:
        open_pos = _get_bot().trader.open_positions()
        closed = _get_bot().trader.closed_positions(limit=50)
        return jsonify({"open": open_pos, "closed": closed})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.route("/api/run", methods=["POST"])
def api_run():
    """Trigger a manual run cycle."""
    try:
        result = _get_bot().run_once()
        return jsonify(result)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    host = os.environ.get("DASHBOARD_HOST", "127.0.0.1")
    port = int(os.environ.get("DASHBOARD_PORT", "5001"))
    logger.info("Starting Bot D dashboard on http://%s:%d", host, port)
    app.run(host=host, port=port, debug=False)
