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

# ── Strategy parameters (must match best OOS backtest run) ────────────────────

ENTRY_THRESHOLD      = 0.20   # minimum |micro_deviation| to post an order
MAX_HOLD_PACKETS     = 500    # taker fallback after this many packets (~200s)
ORDER_TIMEOUT_PKTS   = 10     # cancel unfilledentry after this many packets (~4s)
N_LOTS               = 1      # contracts per order
TICK_SIZE            = 0.05   # NSE equity futures minimum price increment

# ── Fill detection ────────────────────────────────────────────────────────────

MARKET_FEED_STALE_SECS = 5.0  # suppress Layer-2 confirmation if ltp older than this

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
