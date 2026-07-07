"""
Arm registry - the declarative list of strategy variants to run in parallel.

Each Arm = {name, universe, params}. All arms are fed by ONE shared depth feed
(the runner subscribes to the union of their instruments) and run as independent
risk-free simulations, so we can compare them head-to-head on identical live data.

This weekend's arms test our biggest open questions:
  control   - today's live config (the baseline every other arm is measured against)
  expanded  - same params + 4 new names (does the edge generalize / diversify?)
  no_stop   - does the −15..22k/day stop bleed actually help, or cut recoverable trades?
  wide_stop - if a stop helps but 12 ticks is too tight
  no_icici  - is ICICIBANK a drag, or just one bad day?
  selective - fewer, higher-conviction trades (edge margin 1.5)
  reversal  - signal-reversal exit (exit when the microprice flips against us)

A6-maker and composite-maker arms are deferred to week 2 (need real-time signal
builds - not rushed into the start of a 10-day experiment).
"""

from __future__ import annotations

from dataclasses import dataclass, replace

from paper_trader.broker import StrategyParams
from paper_trader.config import INSTRUMENTS

# Proven universe + the expansion candidates under evaluation (mid-priced, liquid,
# cross-sector; chosen for favourable per-share fee economics - see README).
BASE_UNIVERSE: list[str]    = list(INSTRUMENTS)            # HDFCBANK, ICICIBANK, RELIANCE
# Cross-sector for real diversification (banks correlate): SBIN/AXISBANK (banks),
# ITC (FMCG), BHARTIARTL (telecom). All mid-priced + liquid → favourable fee
# economics. (TATAMOTORS has no active future in the master - demerged.)
NEW_CANDIDATES: list[str]   = ["SBIN", "AXISBANK", "ITC", "BHARTIARTL"]
EXPANDED_UNIVERSE: list[str] = BASE_UNIVERSE + NEW_CANDIDATES

CHAMPION = StrategyParams()   # current live config: stop=12, hold=250, margin=1.0


@dataclass(frozen=True)
class Arm:
    name: str
    universe: list[str]
    params: StrategyParams
    note: str = ""


ARMS: list[Arm] = [
    Arm("control",   BASE_UNIVERSE,             CHAMPION,                                "today's live config - baseline"),
    Arm("expanded",  EXPANDED_UNIVERSE,         CHAMPION,                                "champion params + 4 new names"),
    Arm("no_stop",   BASE_UNIVERSE,             replace(CHAMPION, stop_loss_ticks=0),    "does the stop help?"),
    Arm("wide_stop", BASE_UNIVERSE,             replace(CHAMPION, stop_loss_ticks=24),   "looser stop"),
    Arm("no_icici",  ["HDFCBANK", "RELIANCE"],  CHAMPION,                                "is ICICIBANK a drag?"),
    Arm("selective", BASE_UNIVERSE,             replace(CHAMPION, edge_margin=1.5),      "fewer, pickier trades"),
    Arm("reversal",  BASE_UNIVERSE,             replace(CHAMPION, exit_mode="reversal"), "signal-reversal exit"),
]


# ── Expenture I arms ──────────────────────────────────────────────────────────
#
# Recon I verdicts, turned into the go-forward race (see RECON_I_FINDINGS.md):
#   • kill the reversal exit        - worse by −₹6.5k/day, P(>0)=0   [A6]
#   • widen the stop 12 → 24 ticks  - wide_stop beat control P=0.99  [A2]
#   • drop HDFCBANK                  - the lone core-name laggard     [A3/C]
#   • measure the live fill haircut  - queue-aware exit fills         [Tier E]
#
# control + expanded run UNCHANGED for continuity: they keep banking days
# comparable to Basecamp's 12 to close expanded's DSR power gap (0.93 → 0.95).
# The v2 arms layer the config wins; the _q arms add realistic queue-aware exit
# fills to quantify how much of expanded_v2's edge survives non-optimistic fills
# (the binding constraint: breakeven p* ≈ 67% vs the touch-fill sim's 76%).

CHAMPION_V2: StrategyParams = replace(CHAMPION, stop_loss_ticks=24)
V2_UNIVERSE: list[str] = [s for s in EXPANDED_UNIVERSE if s != "HDFCBANK"]

EXPENTURE_ARMS: list[Arm] = [
    Arm("control",        BASE_UNIVERSE,     CHAMPION,    "Basecamp baseline (continuity)"),
    Arm("expanded",       EXPANDED_UNIVERSE, CHAMPION,    "Basecamp champion (continuity for DSR gap)"),
    Arm("expanded_v2",    V2_UNIVERSE,       CHAMPION_V2, "v2: stop 24, drop HDFC, no reversal"),
    Arm("expanded_v2_q",  V2_UNIVERSE,
        replace(CHAMPION_V2, queue_exit_fill=True, queue_exit_min_frac=1.0),
        "v2 + queue-aware exit fills (full queue must clear)"),
    Arm("expanded_v2_q50", V2_UNIVERSE,
        replace(CHAMPION_V2, queue_exit_fill=True, queue_exit_min_frac=0.5),
        "v2 + queue-aware exit fills (half queue)"),
]


def all_universe_symbols(arms: list[Arm] = ARMS) -> list[str]:
    """Union of every arm's instruments - what the single shared feed subscribes to."""
    seen: list[str] = []
    for arm in arms:
        for sym in arm.universe:
            if sym not in seen:
                seen.append(sym)
    return seen
