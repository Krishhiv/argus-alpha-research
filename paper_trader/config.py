"""
Paper trader configuration — single source of truth for all parameters.
Mirror any changes here back to the backtest if strategy params change.
"""

# ── Instruments ───────────────────────────────────────────────────────────────

LOT_SIZES: dict[str, int] = {
    "HDFCBANK":  550,
    "ICICIBANK": 700,
    "RELIANCE":  500,
    "TCS":       175,
}

# Live trading universe. TCS is SUSPENDED: across May 29 + June 1-2 (live) and a
# replay of June 1-2 depth, TCS lost at a ~49% win-rate under every edge-gate
# setting. Root cause is structural — it has the highest per-share break-even
# (₹0.72, STT-dominated by its ~₹2460 price) and the weakest signal (flagged in
# the IC analysis). The banks (ICICIBANK, RELIANCE) carry the alpha. Re-add TCS
# here to resume it once the signal is improved. LOT_SIZES keeps all 4 for the
# fee model and contract resolution.
INSTRUMENTS = ["HDFCBANK", "ICICIBANK", "RELIANCE"]

# ── Strategy parameters ───────────────────────────────────────────────────────

# ENTRY_THRESHOLD is now only a SIGNAL FLOOR (sub-noise guard), not the primary
# gate. The primary entry gate is the economic edge gate below — a flat rupee
# threshold is meaningless across instruments whose price, spread, and per-share
# fees differ 3×. See EDGE_MARGIN.
ENTRY_THRESHOLD      = 0.15   # minimum |micro_deviation| (rupees) — signal floor only
MAX_HOLD_PACKETS     = 150    # taker fallback after this many packets (~60s)
ORDER_TIMEOUT_PKTS   = 10     # cancel unfilled entry after this many packets (~4s)
MIN_HOLD_PKTS        = 10     # packets in position before posting passive exit (~4s)
N_LOTS               = 1      # contracts per order
TICK_SIZE            = 0.05   # NSE equity futures minimum price increment

# ── Economic edge gate (primary entry filter) ─────────────────────────────────

# A maker only earns money if the half-spread it captures exceeds its per-share
# round-trip fee. We therefore require, at entry:
#
#     spread / 2  >=  EDGE_MARGIN × (round_trip_fee / qty)
#
# round_trip_fee is computed live from the current mid via the same fee model
# used for PnL, so the gate is per-instrument, price-aware and self-calibrating.
# This is what stops TCS (break-even ≈ ₹0.72/share, win-rate ≈ 50%) from
# over-trading while leaving the cheap, high-win-rate banks active.
#   EDGE_MARGIN = 1.0  → half-spread must at least cover fees (minimum viable).
#   Raise toward 1.5+  → fewer, higher-conviction trades.
EDGE_MARGIN          = 1.0

# ── Fill quality filter ───────────────────────────────────────────────────────

# Fill is accepted only if the cumulative L1 qty drop since post is at least
# this fraction of queue_ahead. Guards against noise bounces that never consumed
# real queue depth. Set to 0.0 to disable.
QUEUE_FILL_MIN_FRAC  = 0.10

# ── Session hours (IST) ───────────────────────────────────────────────────────

SESSION_START = "09:15"
SESSION_END   = "15:30"
# No NEW entries after this IST time. The depth feed goes silent ~15:30 and
# Dhan emits zero-price packets at close; entering late risks an un-exitable
# position force-closed on a stale/garbage mid. 15:25 matches the research
# session filter (09:20–15:25).
NO_NEW_ENTRY_IST = "15:25"

# ── Reporting ─────────────────────────────────────────────────────────────────

REPORT_EMAIL_TO = "krishhiv@gmail.com"   # hardcoded — never read from .env

# ── Logging paths (relative to repo root on VPS) ─────────────────────────────

LOGS_DIR        = "paper_trader/logs"
TRADES_LOG      = f"{LOGS_DIR}/paper_trades.csv"
ORDERS_LOG      = f"{LOGS_DIR}/paper_orders.csv"
PNL_LOG         = f"{LOGS_DIR}/paper_pnl.csv"
