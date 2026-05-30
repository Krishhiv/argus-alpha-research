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
INSTRUMENTS = list(LOT_SIZES.keys())

# ── Strategy parameters ───────────────────────────────────────────────────────

ENTRY_THRESHOLD      = 0.35   # minimum |micro_deviation| to post an order
MAX_HOLD_PACKETS     = 500    # taker fallback after this many packets (~200s)
ORDER_TIMEOUT_PKTS   = 10     # cancel unfilled entry after this many packets (~4s)
MIN_HOLD_PKTS        = 10     # packets in position before posting passive exit (~4s)
N_LOTS               = 1      # contracts per order
TICK_SIZE            = 0.05   # NSE equity futures minimum price increment

# ── Fill quality filter ───────────────────────────────────────────────────────

# Fill is accepted only if the cumulative L1 qty drop since post is at least
# this fraction of queue_ahead. Guards against noise bounces that never consumed
# real queue depth. Set to 0.0 to disable.
QUEUE_FILL_MIN_FRAC  = 0.10

# ── Session hours (IST) ───────────────────────────────────────────────────────

SESSION_START = "09:15"
SESSION_END   = "15:30"

# ── Reporting ─────────────────────────────────────────────────────────────────

REPORT_EMAIL_TO = "krishhiv@gmail.com"   # hardcoded — never read from .env

# ── Logging paths (relative to repo root on VPS) ─────────────────────────────

LOGS_DIR        = "paper_trader/logs"
TRADES_LOG      = f"{LOGS_DIR}/paper_trades.csv"
ORDERS_LOG      = f"{LOGS_DIR}/paper_orders.csv"
PNL_LOG         = f"{LOGS_DIR}/paper_pnl.csv"
