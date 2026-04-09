"""
Half-Kelly position sizing for Kalshi prediction markets.

Kalshi prices are cents (0-100). A YES contract at price P pays $1 if the
event occurs; you risk P cents to win (100-P) cents.

Kelly formula adapted for binary prediction markets:
  b  = (100 - P) / P   (net profit per dollar risked on YES)
  f* = (b*p - q) / b   (full Kelly)
  f  = f* * fraction    (fractional Kelly)

where p = estimated probability, q = 1-p, P = market price in cents.
"""

from __future__ import annotations


def half_kelly(
    p_win: float,
    market_price_cents: float,
    fraction: float = 0.5,
    max_fraction: float = 0.10,
) -> float:
    """
    Calculate fractional-Kelly bet size for a YES position on Kalshi.

    Args:
        p_win: Estimated probability the event occurs (0.0 – 1.0).
        market_price_cents: Current Kalshi YES price in cents (0 – 100).
        fraction: Kelly multiplier; 0.5 = half-Kelly (default).
        max_fraction: Hard cap on fraction of bankroll per trade.

    Returns:
        Fraction of bankroll to deploy (0.0 – max_fraction).
        Returns 0.0 when there is no positive edge.
    """
    if not (0 < market_price_cents < 100):
        return 0.0
    if not (0 < p_win < 1):
        return 0.0

    P = market_price_cents / 100.0
    b = (1.0 - P) / P   # net profit per dollar risked
    p = p_win
    q = 1.0 - p

    kelly_full = (b * p - q) / b
    kelly_frac = kelly_full * fraction

    return float(max(0.0, min(kelly_frac, max_fraction)))


def kelly_no(
    p_win: float,
    market_price_cents: float,
    fraction: float = 0.5,
    max_fraction: float = 0.10,
) -> float:
    """
    Half-Kelly for a NO position (i.e., betting the event does NOT occur).

    p_win here is still the probability the event DOES occur, so the
    probability relevant for the NO side is (1 - p_win).
    """
    p_no = 1.0 - p_win
    no_price = 100.0 - market_price_cents
    return half_kelly(p_no, no_price, fraction, max_fraction)


def edge(p_win: float, market_price_cents: float) -> float:
    """Return raw edge: estimated probability minus implied probability."""
    return p_win - (market_price_cents / 100.0)


def best_side(
    p_win: float,
    yes_price: float,
    fraction: float = 0.5,
    max_fraction: float = 0.10,
) -> tuple[str, float]:
    """
    Return the better side ('YES' or 'NO') and the Kelly fraction for it.
    Returns ('PASS', 0.0) if neither side has positive Kelly.
    """
    k_yes = half_kelly(p_win, yes_price, fraction, max_fraction)
    k_no = kelly_no(p_win, yes_price, fraction, max_fraction)

    if k_yes == 0.0 and k_no == 0.0:
        return ("PASS", 0.0)
    if k_yes >= k_no:
        return ("YES", k_yes)
    return ("NO", k_no)
