"""
Abstract base class for all bot signal engines.

Every bot exposes the same interface so the orchestrator can poll them
uniformly. The signal dataclass carries everything needed for the paper
trader to size and log a position.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class Signal:
    # Market identification
    market_id: str
    game: str                    # 'cs2', 'val', 'sports', 'econ', ...
    team_a: str
    team_b: str

    # Probability estimates (must sum to ~1.0)
    p_a: float                   # estimated prob team A wins
    p_b: float                   # = 1 - p_a

    # Market prices (Kalshi cents, 0-100)
    market_yes_price: float      # YES price for team_a winning
    market_no_price: float       # NO price (100 - yes_price)

    # Edge and sizing
    edge_yes: float              # p_a - market_yes_price/100
    edge_no: float               # p_b - market_no_price/100
    kelly_yes: float             # bankroll fraction for YES
    kelly_no: float              # bankroll fraction for NO
    recommended_side: str        # 'YES', 'NO', or 'PASS'
    recommended_kelly: float     # kelly fraction for recommended side

    # Signal metadata
    confidence: float            # 0.0-1.0 composite confidence
    match_format: str            # 'Bo1', 'Bo3', 'Bo5'
    tournament: str
    tournament_tier: str         # 'S', 'A', 'B', 'C'
    match_datetime: str          # ISO datetime UTC

    # Human-readable breakdown
    reasoning: str
    signal_components: dict = field(default_factory=dict)  # sub-scores

    # Timestamp
    generated_at: str = ""

    def __post_init__(self):
        if not self.generated_at:
            from datetime import datetime
            self.generated_at = datetime.utcnow().isoformat(sep=" ", timespec="seconds")

    @property
    def has_edge(self) -> bool:
        return self.recommended_side != "PASS"

    def to_dict(self) -> dict:
        from dataclasses import asdict
        return asdict(self)


class BaseSignalEngine(ABC):
    """
    Every bot subclasses this and implements get_signals() and calibrate().
    The orchestrator calls get_signals() on each registered bot, collects
    Signals, sizes them via Kelly, and routes them to the paper trader.
    """

    @abstractmethod
    def get_signals(self, market_ids: Optional[list[str]] = None) -> list[Signal]:
        """
        Generate trading signals.

        Args:
            market_ids: Optional list of specific Kalshi market IDs to evaluate.
                        If None, evaluate all known upcoming markets.

        Returns:
            List of Signal objects. May be empty if no edge found.
        """
        ...

    @abstractmethod
    def calibrate(self, lookback_days: int = 90) -> dict:
        """
        Calibrate probability estimates against historical outcomes.

        Returns a dict with metrics: brier_score, log_loss, accuracy, etc.
        """
        ...

    @property
    @abstractmethod
    def bot_id(self) -> str:
        """Short identifier, e.g. 'botd'."""
        ...

    @property
    @abstractmethod
    def games(self) -> list[str]:
        """List of games/markets this engine covers, e.g. ['cs2', 'val']."""
        ...
