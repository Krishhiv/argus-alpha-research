"""
Out-of-sample validation for the maker strategy.
Train: Apr 24 – May 15 (15 days)
Test:  May 18 – May 22  (5 days)
"""

import subprocess
from research.backtester.maker_walkforward import run_maker_walkforward

TRAIN_DATES = [
    "2026-04-24", "2026-04-27", "2026-04-28", "2026-04-29", "2026-04-30",
    "2026-05-04", "2026-05-05", "2026-05-06", "2026-05-07", "2026-05-08",
    "2026-05-11", "2026-05-12", "2026-05-13", "2026-05-14", "2026-05-15",
]

TEST_DATES = [
    "2026-05-18", "2026-05-19", "2026-05-20", "2026-05-21", "2026-05-22",
]

PARAMS = dict(
    entry_threshold = 0.20,
    max_hold        = 500,
    order_timeout   = 10,
    exit_mode       = "maker",
    fresh_cross     = True,
    signal_col      = "micro_deviation",
)

for label, dates in [("TRAIN", TRAIN_DATES), ("TEST", TEST_DATES)]:
    print(f"\nRunning {label} ({len(dates)} days) ...\n")
    r = run_maker_walkforward(**PARAMS, dates=dates, verbose=True)
    s = r.summary()
    print(f"\n{'='*54}")
    print(f"  {label}  ({len(dates)} days)")
    print(f"  Net PnL:       ₹{s['net_pnl']:>+12,.0f}")
    print(f"  Gross PnL:     ₹{s['gross_pnl']:>+12,.0f}")
    print(f"  Total fees:    ₹{s['total_fees']:>12,.0f}")
    print(f"  Sharpe:        {s['sharpe']:>8.2f}")
    print(f"  Win rate:      {s['win_rate']:>8.1%}")
    print(f"  Fill rate:     {s['fill_rate']:>8.1%}")
    print(f"  Maker exit %:  {s['maker_exit_rate']:>8.1%}")
    print(f"  N posts:       {s['n_posts']:>8,}")
    print(f"  N fills:       {s['n_fills']:>8,}")
    print(f"  N trades:      {s['n_trades']:>8,}")
    print(f"  Max drawdown:  ₹{s['max_drawdown']:>+12,.0f}")
    print(f"  Avg hold (pkts): {s['avg_hold']:>6.1f}")
    print(f"{'='*54}\n")
    r.save_trade_log(f"trade_log_{label.lower()}.csv")

subprocess.run(["afplay", "/System/Library/Sounds/Glass.aiff"])
