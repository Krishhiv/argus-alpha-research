# alpha_lab

*Isolated home for the post-maker quant alpha search.* Self-contained - it does **not**
import any maker-era code (`paper_trader`, `basecamp_recon`, `research/`). Companion to
[../ALPHA_CATALOG.md](../ALPHA_CATALOG.md) (the strategies) and
[../SEAT_AND_STRATEGIES.md](../SEAT_AND_STRATEGIES.md) (why this seat trades these things).

**What we trade:** equity *futures* (execution). **What we discover on:** equity *cash*
1m + daily history (clean, no roll). **Horizon:** minutes-to-hours, taker-side - edges
where our ~15 ms latency doesn't decide the outcome.

## Layout
```
alpha_lab/
  stats.py              # the gauntlet (n_eff / PSR / DSR / PBO) - isolated copy
  data/
    dhan_history.py     # historical OHLCV puller (daily + 1m)
    out/                # stored history (gitignored)
  notebooks/            # one screen per alpha (added as we go)
```

## Start here - the sequence

**Step 1 - build the data foundation (do this first).**
Run the puller on the VPS (the Dhan token lives there):
```
cd ~/paper-trader && ~/collector-dhan/venv/bin/python -m alpha_lab.data.dhan_history --years 3
rsync -avz lightsail-mumbai:'~/paper-trader/alpha_lab/data/out/' ./alpha_lab/data/out/
```
On the first pull, **verify the timestamp timezone** (first 1m bar should be ~09:15 IST)
and spot-check 2-3 resolved security IDs against Dhan before trusting the data.

**Step 2 - first screens** (notebooks, reusing `stats.py`):
1. **A2 - PCA residual reversion** (Avellaneda-Lee): the canonical market-neutral
   stat-arb. Top pick.
2. **A1/A3 - cointegration, re-tested properly** on *years* of daily data (our 2-month
   intraday test was underpowered → re-run before any final verdict; A3 = Kalman dynamic β).
3. **A4 - cross-sectional short-horizon reversal.**

**Step 3 - gauntlet, then (only then) paper.**
Every survivor: cost-realistic (futures STT + lot rounding) + **DSR ≥ 0.95, PBO ≤ 0.20**,
out-of-sample, deflated for *every* alpha/parameter tried → then a live paper arm → tiny-live.

## Non-negotiable discipline (we learned these the expensive way)
- **Candles discover; the live fill model decides belief.** A 1m-candle backtest assumes
  fills at the close with no spread/slippage - the exact optimism that cost us ₹1M of
  imaginary maker edge. Survivors re-run through real cost + execution realism.
- **Mid-bars vs trade-bars:** reversal-type signals can be bid-ask-bounce artifacts on
  trade-price bars - cross-check those on mid bars (from the depth feed) before believing.
- **n_eff, not trade count. Deflate hard.** With years of 1m data you can overfit at
  industrial scale; the gauntlet matters *more*, not less.
- **Most candidates die. That's the point** - a disciplined search, each with a cheap kill.
