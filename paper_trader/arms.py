"""
Arm registry — the declarative list of strategy variants to run in parallel.

Each Arm = {name, universe, params}. All arms are fed by ONE shared depth feed
(the runner subscribes to the union of their instruments) and run as independent
risk-free simulations, so we can compare them head-to-head on identical live data.

This weekend's arms test our biggest open questions:
  control   — today's live config (the baseline every other arm is measured against)
  expanded  — same params + 4 new names (does the edge generalize / diversify?)
  no_stop   — does the −15..22k/day stop bleed actually help, or cut recoverable trades?
  wide_stop — if a stop helps but 12 ticks is too tight
  no_icici  — is ICICIBANK a drag, or just one bad day?
  selective — fewer, higher-conviction trades (edge margin 1.5)
  reversal  — signal-reversal exit (exit when the microprice flips against us)

A6-maker and composite-maker arms are deferred to week 2 (need real-time signal
builds — not rushed into the start of a 10-day experiment).
"""

from __future__ import annotations

from dataclasses import dataclass, replace

from paper_trader.broker import StrategyParams
from paper_trader.config import INSTRUMENTS

# Proven universe + the expansion candidates under evaluation (mid-priced, liquid,
# cross-sector; chosen for favourable per-share fee economics — see README).
BASE_UNIVERSE: list[str]    = list(INSTRUMENTS)            # HDFCBANK, ICICIBANK, RELIANCE
NEW_CANDIDATES: list[str]   = ["SBIN", "AXISBANK", "ITC", "TATAMOTORS"]
EXPANDED_UNIVERSE: list[str] = BASE_UNIVERSE + NEW_CANDIDATES

CHAMPION = StrategyParams()   # current live config: stop=12, hold=250, margin=1.0


@dataclass(frozen=True)
class Arm:
    name: str
    universe: list[str]
    params: StrategyParams
    note: str = ""


ARMS: list[Arm] = [
    Arm("control",   BASE_UNIVERSE,             CHAMPION,                                "today's live config — baseline"),
    Arm("expanded",  EXPANDED_UNIVERSE,         CHAMPION,                                "champion params + 4 new names"),
    Arm("no_stop",   BASE_UNIVERSE,             replace(CHAMPION, stop_loss_ticks=0),    "does the stop help?"),
    Arm("wide_stop", BASE_UNIVERSE,             replace(CHAMPION, stop_loss_ticks=24),   "looser stop"),
    Arm("no_icici",  ["HDFCBANK", "RELIANCE"],  CHAMPION,                                "is ICICIBANK a drag?"),
    Arm("selective", BASE_UNIVERSE,             replace(CHAMPION, edge_margin=1.5),      "fewer, pickier trades"),
    Arm("reversal",  BASE_UNIVERSE,             replace(CHAMPION, exit_mode="reversal"), "signal-reversal exit"),
]


def all_universe_symbols(arms: list[Arm] = ARMS) -> list[str]:
    """Union of every arm's instruments — what the single shared feed subscribes to."""
    seen: list[str] = []
    for arm in arms:
        for sym in arm.universe:
            if sym not in seen:
                seen.append(sym)
    return seen
