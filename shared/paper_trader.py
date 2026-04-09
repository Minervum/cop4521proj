"""
Paper trader - simulates Kalshi trade execution without real money.

Shared by all bots. Positions are stored in the shared SQLite database
under the trades_* table prefix so the unified dashboard can display
the combined P&L across all bots.
"""

from __future__ import annotations

import os
from datetime import datetime
from typing import Optional

from shared.storage import BaseStorage
from shared.signal_engine import Signal


_SCHEMA = """
CREATE TABLE IF NOT EXISTS trades_positions (
    position_id   TEXT PRIMARY KEY,
    bot_id        TEXT NOT NULL,
    market_id     TEXT NOT NULL,
    game          TEXT NOT NULL,
    team_a        TEXT,
    team_b        TEXT,
    side          TEXT NOT NULL,      -- YES or NO
    entry_price   REAL NOT NULL,      -- cents (0-100)
    contracts     INTEGER NOT NULL,
    cost_basis    REAL NOT NULL,      -- USD
    status        TEXT DEFAULT 'open',-- open, closed, expired
    exit_price    REAL,
    pnl           REAL,
    tournament    TEXT,
    match_datetime TEXT,
    signal_confidence REAL,
    kelly_fraction REAL,
    opened_at     TEXT NOT NULL,
    closed_at     TEXT,
    notes         TEXT
);

CREATE TABLE IF NOT EXISTS trades_bankroll (
    bot_id        TEXT PRIMARY KEY,
    bankroll      REAL NOT NULL,
    updated_at    TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_positions_bot   ON trades_positions(bot_id);
CREATE INDEX IF NOT EXISTS idx_positions_mkt   ON trades_positions(market_id);
CREATE INDEX IF NOT EXISTS idx_positions_status ON trades_positions(status);
"""


class PaperTrader:
    def __init__(
        self,
        bot_id: str,
        db_path: str = None,
        starting_bankroll: float = None,
    ):
        self.bot_id = bot_id
        db = db_path or os.environ.get("BOT_DB_PATH", "data/botd.db")
        self._store = BaseStorage(db)
        self._init_schema()
        starting = starting_bankroll or float(
            os.environ.get("PAPER_BANKROLL", "10000.0")
        )
        self._ensure_bankroll(starting)

    def _init_schema(self):
        with self._store.conn() as c:
            c.executescript(_SCHEMA)

    def _ensure_bankroll(self, starting: float):
        existing = self._store.scalar(
            "SELECT bankroll FROM trades_bankroll WHERE bot_id=?", (self.bot_id,)
        )
        if existing is None:
            with self._store.conn() as c:
                c.execute(
                    "INSERT INTO trades_bankroll(bot_id, bankroll, updated_at) "
                    "VALUES (?,?,?)",
                    (self.bot_id, starting, self._store.now()),
                )

    # ------------------------------------------------------------------
    # Bankroll
    # ------------------------------------------------------------------

    def bankroll(self) -> float:
        return float(
            self._store.scalar(
                "SELECT bankroll FROM trades_bankroll WHERE bot_id=?", (self.bot_id,)
            )
            or 0.0
        )

    def _set_bankroll(self, amount: float):
        with self._store.conn() as c:
            c.execute(
                "UPDATE trades_bankroll SET bankroll=?, updated_at=? WHERE bot_id=?",
                (amount, self._store.now(), self.bot_id),
            )

    # ------------------------------------------------------------------
    # Open a position
    # ------------------------------------------------------------------

    def open_position(
        self,
        signal: Signal,
        override_kelly: Optional[float] = None,
    ) -> Optional[dict]:
        """
        Size and open a paper position from a Signal.

        Returns the position dict if opened, None if skipped (no edge,
        insufficient bankroll, or PASS signal).
        """
        if signal.recommended_side == "PASS":
            return None

        kelly = override_kelly or signal.recommended_kelly
        if kelly <= 0:
            return None

        br = self.bankroll()
        cost_basis = round(br * kelly, 2)
        if cost_basis < 1.0:
            return None

        side = signal.recommended_side
        entry_price = (
            signal.market_yes_price if side == "YES" else signal.market_no_price
        )
        if entry_price <= 0:
            return None

        contracts = max(1, int(cost_basis / (entry_price / 100.0)))
        actual_cost = round(contracts * (entry_price / 100.0), 2)

        position_id = (
            f"{self.bot_id}_{signal.market_id}_{side}_{datetime.utcnow().strftime('%Y%m%d%H%M%S')}"
        )

        position = {
            "position_id": position_id,
            "bot_id": self.bot_id,
            "market_id": signal.market_id,
            "game": signal.game,
            "team_a": signal.team_a,
            "team_b": signal.team_b,
            "side": side,
            "entry_price": entry_price,
            "contracts": contracts,
            "cost_basis": actual_cost,
            "status": "open",
            "exit_price": None,
            "pnl": None,
            "tournament": signal.tournament,
            "match_datetime": signal.match_datetime,
            "signal_confidence": signal.confidence,
            "kelly_fraction": kelly,
            "opened_at": self._store.now(),
            "closed_at": None,
            "notes": signal.reasoning[:500] if signal.reasoning else "",
        }

        with self._store.conn() as c:
            c.execute(
                "INSERT INTO trades_positions VALUES "
                "(:position_id,:bot_id,:market_id,:game,:team_a,:team_b,"
                ":side,:entry_price,:contracts,:cost_basis,:status,"
                ":exit_price,:pnl,:tournament,:match_datetime,"
                ":signal_confidence,:kelly_fraction,:opened_at,:closed_at,:notes)",
                position,
            )
        self._set_bankroll(br - actual_cost)
        return position

    # ------------------------------------------------------------------
    # Settle a position
    # ------------------------------------------------------------------

    def settle_position(self, position_id: str, won: bool) -> Optional[dict]:
        """
        Mark a position as settled (closed).

        Args:
            position_id: The position to settle.
            won: True if our side was correct (we win the payout).

        Returns the updated position dict.
        """
        rows = self._store.execute(
            "SELECT * FROM trades_positions WHERE position_id=? AND status='open'",
            (position_id,),
        )
        if not rows:
            return None
        pos = rows[0]

        if won:
            payout = pos["contracts"] * 1.0  # $1 per contract on win
            pnl = payout - pos["cost_basis"]
        else:
            payout = 0.0
            pnl = -pos["cost_basis"]

        exit_price = 100.0 if won else 0.0

        with self._store.conn() as c:
            c.execute(
                "UPDATE trades_positions SET status='closed', exit_price=?, pnl=?, "
                "closed_at=? WHERE position_id=?",
                (exit_price, round(pnl, 2), self._store.now(), position_id),
            )

        new_br = self.bankroll() + payout
        self._set_bankroll(new_br)
        return {**pos, "status": "closed", "exit_price": exit_price, "pnl": round(pnl, 2)}

    # ------------------------------------------------------------------
    # Query helpers
    # ------------------------------------------------------------------

    def open_positions(self) -> list[dict]:
        return self._store.execute(
            "SELECT * FROM trades_positions WHERE bot_id=? AND status='open' "
            "ORDER BY opened_at DESC",
            (self.bot_id,),
        )

    def closed_positions(self, limit: int = 100) -> list[dict]:
        return self._store.execute(
            "SELECT * FROM trades_positions WHERE bot_id=? AND status='closed' "
            "ORDER BY closed_at DESC LIMIT ?",
            (self.bot_id, limit),
        )

    def summary(self) -> dict:
        closed = self._store.execute(
            "SELECT SUM(pnl) as total_pnl, COUNT(*) as total_trades, "
            "SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) as wins "
            "FROM trades_positions WHERE bot_id=? AND status='closed'",
            (self.bot_id,),
        )
        row = closed[0] if closed else {}
        open_count = self._store.scalar(
            "SELECT COUNT(*) FROM trades_positions WHERE bot_id=? AND status='open'",
            (self.bot_id,),
        )
        return {
            "bot_id": self.bot_id,
            "bankroll": self.bankroll(),
            "total_pnl": round(row.get("total_pnl") or 0.0, 2),
            "total_trades": row.get("total_trades") or 0,
            "wins": row.get("wins") or 0,
            "open_positions": open_count or 0,
        }
