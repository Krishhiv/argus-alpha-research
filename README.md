# Argus Alpha Research

A self-directed quantitative trading research project on NSE equity futures, built and run end to end by one person: live market-data engineering, a live head-to-head experimentation harness, microstructure and statistical analysis, and (the part most retail projects skip) the discipline to prove a strategy does not work and stop.

> **Status: Halted (June 2026).** This project reached a clear, well-documented research verdict, it was not abandoned mid-guess. No real capital was ever deployed; every trade in this repository is simulated. See [Why it was halted](#why-it-was-halted).

---

## The one result worth reading first

The system paper-traded a microstructure market-making ("maker") strategy that, on the simulator's optimistic fills, looked strongly profitable. A single execution-realism analysis showed the entire "edge" was an artifact of the fill model, not a real signal.

The method: take every simulated fill and re-price it at the mid (removing the free assumption that you always buy at the bid and sell at the ask), then decompose the P&L.

| Measure | Value | Meaning |
|---|---|---|
| Simulated net P&L | **+Rs 238k** | what the optimistic fill model reported |
| Marked to mid (mid-to-mid) | **-Rs 1.01M** | the actual price moves the strategy traded into |
| Spread-capture component | **+Rs 1.25M** | the bid/ask edge the sim *assumed* it captured on both legs |
| Win rate: simulated vs at-mid | **77% -> 12.6%** | the "wins" were the spread assumption, not direction |

The strategy's directional edge was **negative**. All of its apparent profit, and then some, came from an assumed spread capture that a back-of-queue, non-colocated retail account cannot realistically achieve: break-even required retaining **>= 81% of the theoretical spread**, and the microstructure evidence (queue-position simulation plus per-fill markout) showed real retention lands far below that.

The valuable part is not the strategy. It is that the research caught this **on paper, with zero capital at risk**, using methods most self-directed projects never apply.

---

## What this project demonstrates

- **Market-microstructure fluency:** microprice / order-flow-imbalance signals, adverse selection, queue position, effective vs realized spread, price impact, and why they decide retail viability.
- **Statistical rigor against self-deception:** effective sample size for autocorrelated returns (n_eff), Probabilistic and Deflated Sharpe Ratios (PSR / DSR), and Probability of Backtest Overfitting via combinatorially-symmetric cross-validation (PBO / CSCV). Backtests were deflated for the number of variants tried, not read at face value.
- **Full-stack build:** a live 20-level depth-feed collector, a real-time multi-arm paper-trading harness, an always-on monitoring dashboard, and a reproducible research pipeline, all deployed and running on a cloud VPS.
- **Research judgment:** killing a favored hypothesis when the data contradicted it, then re-deriving the *correct* next direction from first principles rather than tweaking a dead idea.

---

## What was built

**Live data collection.** A collector on a Mumbai VPS archives the full NSE 20-level tick-by-tick order book (plus the trade/quote feed) for a liquid futures universe, stored as partitioned Parquet. This is the raw material the whole project runs on.

**Multi-arm experimentation harness.** Because a compacted-data backtest proved unreliable (it missed a live day by Rs 11k), strategy variants were instead raced **live, head-to-head, on one shared feed**: seven "arms" (control vs challengers on stop width, universe, selectivity, exit logic) each with isolated brokers, risk governor, and logs. Champion-vs-challenger on identical live data is the only trustworthy way to compare variants, and this harness is that platform.

**Overfitting-aware evaluation.** A reusable statistics module ranks arms with n_eff -> PSR -> DSR -> PBO on cost-realistic, out-of-sample returns, with a hard promotion gate (DSR >= 0.95, PBO <= 0.2). The best arm reached DSR 0.93, short of the bar, and the analysis said so plainly rather than promoting it.

**Execution-realism analysis.** A per-fill markout study and a queue-aware fill simulator quantified the gap between simulated and achievable P&L. This is what produced the headline finding above.

---

## Key findings

- **The maker edge was a fill-model artifact.** Proven by the mid-to-mid decomposition and per-fill markout: positive apparent profit, negative directional edge, break-even needing >= 81% spread retention.
- **STT dominates the cost structure** (~two-thirds of round-trip cost, sell-side), which structurally penalizes a high-turnover, one-tick-spread strategy for a retail participant.
- **The signal is real but latency-gated.** Microprice / order-flow imbalance predicts the next price tick, but at retail latency (~15 ms, no colocation) you act after the colocated participants who set that price, so the directional component does not survive to your fills.
- **A backtest can lie an order of magnitude.** The same code path that "made" money on optimistic fills lost heavily once fills were priced honestly. Every simulated number here is treated as a hypothesis until execution realism is applied.

---

## The pivot (documented research judgment)

Rather than tweak a structurally dead strategy, the project re-derived the problem from the **trading seat outward**: given a retail account (no colocation, no exchange membership, no maker rebates, ~15 ms latency, small capital), which edges are even *available*? See [`SEAT_AND_STRATEGIES.md`](SEAT_AND_STRATEGIES.md) and [`ALPHA_CATALOG.md`](ALPHA_CATALOG.md).

The conclusion: abandon the latency-race (market-making) game, which that seat cannot win, and move to **taker-side, model-based alphas at a horizon where latency does not decide the outcome** (statistical arbitrage / OU mean-reversion, PCA residual reversion, cross-sectional reversal, calendar spreads), explicitly excluding tuned chart indicators in favor of edges derived from a testable statistical property. A clean, isolated research workspace ([`alpha_lab/`](alpha_lab/)) and a three-year daily + one-minute historical dataset were built as the foundation for that program.

That program was scoped but not completed, which is where the project was halted.

---

## Why it was halted

The research produced its verdict: the market-making strategy has no viable edge for a self-funded retail seat, and its only path to profitability requires institutional execution infrastructure (colocation, FPGA order handling, exchange membership) that is outside a bootstrapped budget. The identified pivot is a longer research program that would need either risk capital or more runway than a self-funded project affords.

Given that, the disciplined choice was to **halt at a clean, documented stopping point rather than deploy capital into an unvalidated strategy.** The deliverable is the research itself: the methodology, the findings, and the decision.

---

## Repository guide

| Path | What it is |
|---|---|
| [`ALPHA_CATALOG.md`](ALPHA_CATALOG.md) | Quant-grade alpha catalog and the equity-futures asset decision (the pivot's design) |
| [`SEAT_AND_STRATEGIES.md`](SEAT_AND_STRATEGIES.md) | Seat-first strategy design: what edges a retail seat can and cannot access |
| [`BASECAMP_RECON.md`](BASECAMP_RECON.md) | The overfitting-disciplined analysis plan (DSR / PBO / n_eff, regime attribution, fill realism) |
| [`basecamp_recon/`](basecamp_recon/) | Analysis code: the statistics gauntlet, markout/adverse-selection study, regime (Kalman + variance-ratio/Hurst) lens |
| [`alpha_lab/`](alpha_lab/) | The isolated next-phase workspace: history puller, statistics gauntlet, research notebooks |
| [`paper_trader/`](paper_trader/) | The live multi-arm paper-trading system: broker simulator, harness, monitor, deployment |
| [`research/`](research/) | Feature library, backtester, and IC/ICIR notebooks from the initial signal research |
| [`tests/`](tests/) | Test suite for the trading engine, harness, and monitor |

---

## Tech

Python (pandas, numpy, statsmodels, scipy), asyncio websockets for live feeds, Parquet/PyArrow storage, systemd services and timers on a cloud VPS, a dependency-free stdlib monitoring dashboard, and Jupyter for research. Statistical toolkit: cointegration (Engle-Granger / Johansen), Ornstein-Uhlenbeck processes, Kalman filtering, PSR/DSR/PBO, block bootstrap.

*All trading in this repository is simulated. No orders were ever sent to a live exchange and no real capital was deployed.*
