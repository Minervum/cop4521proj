"""
Base SQLite storage class shared by all bots.

All bots use a single SQLite file with table-name prefixes to partition data:
  cs2_*       - CS2/HLTV data (Bot D)
  val_*       - Valorant/VLR data (Bot D)
  liq_*       - Liquipedia tournament data (Bot D)
  sports_*    - Sports data (Bot B)
  econ_*      - Economics data (Bot C)
  trades_*    - Shared paper-trader ledger
"""

import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from typing import Any, Optional


_DEFAULT_DB_PATH = os.environ.get("BOT_DB_PATH", "data/botd.db")


class BaseStorage:
    def __init__(self, db_path: str = _DEFAULT_DB_PATH):
        self.db_path = db_path
        os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)

    @contextmanager
    def conn(self):
        connection = sqlite3.connect(self.db_path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA foreign_keys=ON")
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def execute(self, sql: str, params: tuple = ()) -> list:
        with self.conn() as c:
            cur = c.execute(sql, params)
            return [dict(r) for r in cur.fetchall()]

    def executemany(self, sql: str, params_list: list) -> None:
        with self.conn() as c:
            c.executemany(sql, params_list)

    def scalar(self, sql: str, params: tuple = ()) -> Any:
        with self.conn() as c:
            cur = c.execute(sql, params)
            row = cur.fetchone()
            return row[0] if row else None

    def upsert(self, table: str, data: dict, conflict_cols: list) -> None:
        cols = list(data.keys())
        placeholders = ", ".join("?" for _ in cols)
        update_cols = [c for c in cols if c not in conflict_cols]
        conflict_str = ", ".join(conflict_cols)
        if update_cols:
            update_set = ", ".join(f"{c}=excluded.{c}" for c in update_cols)
            sql = (
                f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({placeholders}) "
                f"ON CONFLICT({conflict_str}) DO UPDATE SET {update_set}"
            )
        else:
            sql = (
                f"INSERT OR IGNORE INTO {table} ({', '.join(cols)}) "
                f"VALUES ({placeholders})"
            )
        with self.conn() as c:
            c.execute(sql, list(data.values()))

    def upsert_many(self, table: str, rows: list[dict], conflict_cols: list) -> None:
        if not rows:
            return
        for row in rows:
            self.upsert(table, row, conflict_cols)

    def is_fresh(self, table: str, where: str = "1=1", max_age_hours: float = 1.0) -> bool:
        """Return True if the newest row in table (filtered by where) is recent enough."""
        try:
            val = self.scalar(f"SELECT MAX(updated_at) FROM {table} WHERE {where}")
            if not val:
                return False
            updated = datetime.fromisoformat(str(val))
            age_hours = (datetime.utcnow() - updated).total_seconds() / 3600
            return age_hours < max_age_hours
        except Exception:
            return False

    def now(self) -> str:
        return datetime.utcnow().isoformat(sep=" ", timespec="seconds")
