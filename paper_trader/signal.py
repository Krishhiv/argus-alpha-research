"""
Real-time micro_deviation signal computation.

Mirrors the logic in research/features/depth_features.py:add_microprice()
but operates on a single packet (scalar values) rather than a DataFrame,
so it runs with zero pandas overhead on every depth websocket message.
"""

from __future__ import annotations


def compute_micro_deviation(
    bid_price: float,
    bid_qty:   float,
    ask_price: float,
    ask_qty:   float,
) -> float:
    """
    Returns microprice − midprice for one depth packet.

    Positive  → book leans bullish (more size on ask, expect upward pressure).
    Negative  → book leans bearish.
    Near zero → balanced book, no strong lean.
    """
    total = bid_qty + ask_qty
    if total < 1e-9:
        return 0.0
    mid      = (bid_price + ask_price) / 2.0
    microprice = (bid_price * ask_qty + ask_price * bid_qty) / total
    return microprice - mid
