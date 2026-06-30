# Quant Alpha Catalog & Asset Decision

*Companion to [SEAT_AND_STRATEGIES.md](SEAT_AND_STRATEGIES.md). That doc chose the
seat-appropriate **families**; this one (a) settles which **asset** we trade and
(b) catalogs **model-based, quant-grade alphas** — explicitly excluding tuned
chart-indicator strategies. Everything here is a hypothesis; nothing is believed
until it clears the gauntlet on cost-realistic, out-of-sample data.*

*Created 2026-06-30.*

---

## Part 0 — TL;DR

- **Trade equity *futures*** (single-stock futures; index futures for index-level
  ideas). **Cash equity is the *discovery* dataset** (clean, no roll) and a
  fine-sizing fallback — not the primary traded vehicle.
- **Discover price-based alphas on 1m + daily history; discover flow-based alphas on
  live depth.** Two data sources, two jobs.
- The catalog (Part 3) is grouped by mathematical family. The shortlist is in Part 6.

---

## Part 1 — Asset decision: equity futures vs. equity cash

| Dimension | Stock **futures** | Cash equity | Why it matters for us |
|---|---|---|---|
| **Shorting** | Short freely, hold overnight | Intraday short only (MIS); **no overnight short** for retail | Market-neutral / pairs / cross-sectional long-short *require* free shorting → **futures** |
| **STT (the tax that killed the maker)** | ~0.02% sell-side only | intraday ~0.025% sell; **delivery 0.1% both sides** | Futures are the cheapest active-trading vehicle; delivery equity is disqualified by cost |
| **Leverage / capital** | Built-in (~5–7×, ~15% margin) | Intraday MIS leverage; full capital for delivery | Small account (₹3–15L) → futures' leverage lets us hold meaningful notional |
| **Sizing granularity** | **Coarse** (1 lot ≈ ₹4–9L notional) | **Fine** (single shares) | The one place cash wins — precise hedge ratios for multi-leg baskets. A real constraint at our capital |
| **Universe** | ~180–220 F&O names + indices | ~5000 names, most illiquid | Our liquid universe *is* the F&O set anyway → no loss |
| **Roll / expiry** | Monthly roll, expiry distortion (we saw it) | None | Operational overhead for futures; handle with a roll calendar |
| **Overnight risk** | Yes (gaps) | Delivery only | Prefer intraday/low-overnight strategies given livelihood-sensitivity |

**Verdict: equity futures is the primary vehicle** — shortability, lower STT, and
leverage decide it, and the liquid universe is the F&O set regardless. The genuine
cost is **lot granularity**: one future lot is large for our capital, which (a) makes
precise multi-leg hedging hard and (b) pushes us toward *fewer, higher-conviction*
positions — which actually suits a small concentrated book.

**Two consequences we bake in:**
1. **Discover on cash-equity series** (no roll, longer clean history, fine for signal
   research); **implement and cost on futures** (real STT, lot sizing, roll). A signal
   found on spot maps to the future via `future ≈ spot + basis`.
2. **Lot granularity is a *selection filter*** — only promote multi-leg strategies
   whose hedge survives integer-lot rounding. If a basket needs 0.3 lots to be neutral,
   it's not tradeable on this seat; design around it or drop it.

*(Rates above are indicative — re-verify current STT/charges before sizing; they change in budgets.)*

---

## Part 2 — What "quant-grade" means here (and why indicators are out)

You're right to reject indicator strategies, but let me sharpen *why* — it's not
mainly "lag":

> A classic chart indicator (MA cross, RSI, MACD, Bollinger, stochastics) is a
> deterministic nonlinear transform of past prices with **no model of the
> return-generating process**, several **free parameters that invite overfitting**,
> and **decades of crowding** that has arbitraged away any edge. The problem isn't
> that it "lags" — momentum *also* uses past prices and can work. The problem is there's
> **no tested statistical property underneath it**, so a backtest is just curve-fitting.

**Quant-grade = the signal is *derived* from a statistical/economic property we can
test and that has an edge thesis:** cointegration (a stationary linear combination
exists), factor structure (idiosyncratic residuals mean-revert), order-flow impact
(signed flow moves price), autocorrelation structure (short-horizon reversal /
medium-horizon momentum are *measured* properties of returns), regime dynamics, or a
fitted stochastic process. The signal falls out of the model; we don't tune a chart
pattern until the backtest smiles.

**Litmus test for inclusion:** *"What measured statistical property of the data is this
exploiting, and what would make that property disappear?"* If there's no clean answer,
it's not in this catalog.

---

## Part 3 — The alpha catalog

Each entry: **model · edge thesis · data · horizon · seat-fit · killer test.**
Data tags: `1m`/`daily` = historical candles (have-able via Dhan pull) · `depth` =
live 20-level feed (have) · `events`/`fundamentals` = not in-house yet.

### A. Statistical mean-reversion / relative-value (market-neutral core)

**A1. Cointegration pairs, OU spread** — `daily`+`1m`
- *Model:* `S = log A − β log B`; test stationarity (Engle-Granger ADF / Johansen); model `S` as OU `dS=θ(μ−S)dt+σdW`; trade z-score.
- *Edge:* a genuine stationary linear combination reverts. *Killer test:* cointegration stable across rolling windows + years (our 2-month test was underpowered — re-run on daily history). Failed on 2mo intraday; **re-test properly before final verdict.**

**A2. PCA statistical arbitrage (Avellaneda–Lee residual reversion)** — `daily`+`1m` — ⭐
- *Model:* decompose returns into principal-component (or sector ETF) factors; the **idiosyncratic residual** for each name follows an OU process; trade its s-score (long when residual cheap, short when rich), netted factor-neutral.
- *Edge:* idiosyncratic over-/under-reaction reverts once common-factor moves are stripped. The canonical quant stat-arb; far more robust than single pairs (N-name diversification).
- *Killer test:* does the residual OU revert OOS net of cost; is the factor structure stable?

**A3. Kalman dynamic-hedge pairs/baskets** — `daily`+`1m`
- *Model:* hedge ratio β is a latent state estimated by a Kalman filter (time-varying), vs static OLS; spread = residual to the dynamic hedge.
- *Edge:* relationships drift; a dynamic β tracks them and reduces the β-instability that killed our static A1. *Killer test:* does dynamic-β spread beat static-β OOS net of cost?

**A4. Cross-sectional short-horizon reversal (Lo–MacKinlay / Lehmann)** — `daily`+`1m` — ⭐
- *Model:* rank universe by recent (intraday/overnight) return; **long losers, short winners**, market-neutral; exit on reversion.
- *Edge:* short-horizon returns are *negatively* autocorrelated cross-sectionally (liquidity provision / overreaction). A measured, durable property.
- *Killer test:* does the reversal spread survive cost + lot-granularity; is it just illiquidity (then we can't trade it)?

**A5. Calendar / basis spread** — `1m`+`depth`
- *Model:* front vs next-month future of the *same* name; spread = basis = cost-of-carry; cointegrated **by construction**.
- *Edge:* roll/expiry flows + carry convergence (we directly observed the expiry distortion). Highest-probability stationary spread on the list. *Killer test:* basis move > cost, roll pattern stable across months (need next-month depth + more cycles).

### B. Cross-sectional / factor (portfolio) alphas

**B1. Time-series momentum (Moskowitz–Ooi–Pedersen)** — `daily`+`1m`
- *Model:* sign/strength of own past return predicts future return; vol-scaled position.
- *Edge:* under-reaction / flow persistence. *Killer test:* positive autocorrelation at the chosen horizon OOS net of cost; not just a vol-risk-premium proxy.

**B2. Cross-sectional / residual momentum (Blitz)** — `daily`
- *Model:* rank by factor-residual (idiosyncratic) trailing return; long top / short bottom.
- *Edge:* residual momentum is cleaner than raw momentum (strips factor reversal). *Killer test:* survives transaction cost at our rebalance frequency + lot rounding.

**B3. Lead–lag / Granger network** — `1m`+`depth`
- *Model:* estimate which names/indices Granger-cause others at lag k; trade the laggard on the leader's move.
- *Edge:* information diffuses unevenly. *Killer test:* the predictive lag must exceed our latency (>~seconds) or it's a colo game; measure the cross-correlation lag first.

### C. Stochastic-process / time-series models

**C1. Hawkes self-exciting process on order flow** — `depth`
- *Model:* trade/quote arrivals as a self-exciting point process; intensity asymmetry predicts short-horizon direction.
- *Edge:* order flow clusters and is partially predictable. *Killer test:* predicted move (net of crossing+STT) > 0 at a latency-reachable horizon. (Flow → needs depth, not candles.)

**C2. Hidden Markov / Markov-switching regime model** — `daily`+`1m` (overlay)
- *Model:* latent regimes (e.g. trend/mean-revert/high-vol) inferred via HMM; condition other alphas / scale exposure.
- *Use:* a *gate/overlay*, not standalone — feeds A/B sizing. *Killer test:* regime conditioning improves a base alpha's OOS Sharpe (vs overfitting the regime labels).

**C3. State-space latent fair-value (Kalman) reversion** — `1m`+`depth`
- *Model:* Kalman constant-velocity fair value (we built this); trade deviation reversion when the series is locally stationary.
- *Edge:* transient dislocations from a smoothly-evolving fair value revert. *Killer test:* deviation predicts reversion net of cost, conditioned on regime (else whipsaw in trends).

**C4. GARCH vol forecast → vol-targeting / vol-risk overlay** — `daily`+`1m` (overlay)
- *Model:* forecast conditional variance (GARCH family); size positions inversely to forecast vol; or trade realized-vs-forecast.
- *Use:* risk overlay that raises Sharpe of any directional alpha; standalone vol-premium harvest needs options (D-tier, different seat).

### D. Microstructure / flow alphas (live depth only — *not* on candles)

**D1. Order-Flow-Imbalance regression (Cont–Kukanov–Stoikov)** — `depth`
- *Model:* signed L1 flow `OFI` over a window linearly predicts the next price change; taker entry on strong OFI.
- *Edge:* mechanical price impact of net flow. The honest taker successor to the dead maker. *Killer test:* realized-spread/impact study — predicted move net of crossing+STT > 0.

**D2. Queue-imbalance / book-pressure & VPIN toxicity** — `depth`
- *Model:* depth imbalance and order-flow toxicity (VPIN) predict short-horizon direction / adverse-selection risk.
- *Use:* directional signal *and* a "don't trade now" toxicity filter for other strategies. *Killer test:* separable edge from D1, net of cost.

### E. Statistical-learning / ML (high overfit risk — strict CV)

**E1. Regularized / GBM return prediction** — `daily`+`1m`(+`depth`)
- *Model:* predict next-bar/hour return (sign or magnitude) from engineered features (multi-lag returns, cross-sectional ranks, vol, OFI) via elastic-net or gradient-boosted trees.
- *Discipline (non-negotiable):* **purged + embargoed cross-validation** (López de Prado) to kill leakage; feature parsimony; and the *same* DSR/PBO gauntlet — ML makes overfitting trivial. *Killer test:* OOS IC > 0 under purged CV, edge survives cost.

**E2. Meta-labeling (López de Prado)** — overlay on any A/B/D signal
- *Model:* a primary model sets direction; a secondary ML model predicts P(primary is right) → sizes/filters trades (improves precision, not direction).
- *Use:* boosts a *validated* base alpha's hit-rate/sizing; never a standalone edge. *Killer test:* meta-layer improves OOS precision without shrinking n_eff to nothing.

### F. Event / structural (need data we don't have yet)

**F1. Index rebalance / inclusion flows** — `events` — predictable forced flows around index reconstitution.
**F2. Post-earnings-announcement drift (PEAD)** — `events`+`fundamentals` — drift after surprises.
> Parked until we have a clean corporate-events/fundamentals feed. Noted for completeness.

---

## Part 4 — Strategy baskets (ensembles)

Single alphas are fragile; **the edge is in combining low-correlation alphas into a
portfolio.** This is itself the quant-grade move (diversification of *signals*, not
just assets).

**Basket 1 — Market-neutral mean-reversion (the core book):**
`{ A2 PCA residual reversion + A4 cross-sectional reversal + A1/A3 cointegration pairs }`
→ each market/factor-neutral; combine by **risk parity** (inverse-vol or
inverse-covariance weights) on the *signal* level; gate exposure with **C2 (HMM
regime)** and **D2 (toxicity)**. Rationale: these share a reversion thesis but
different mechanisms, so their idiosyncratic errors diversify.

**Basket 2 — Directional / momentum (smaller allocation):**
`{ B1 time-series momentum + B2 residual momentum + D1 OFI }` with **C4 (GARCH)
vol-targeting**. Rationale: momentum diversifies the reversion book (they pay off in
different regimes), and the vol overlay caps drawdown.

**Combination layer:** allocate across Basket 1 / Basket 2 by realized correlation and
each basket's *deflated* (not raw) Sharpe; cap gross by margin/capital; enforce
integer-lot feasibility. The regime overlay tilts between reversion-heavy and
momentum-heavy as the HMM state shifts (this is the "regime-switching meta-strategy"
from the seat doc — but only after the sub-alphas individually clear the gauntlet).

---

## Part 5 — The shared gauntlet & discovery→execution discipline

*Same discipline as Recon, because we already learned what skipping it costs.*

1. **Discover on candles, *believe* only after execution realism.** A 1m-candle
   backtest assumes fills at the close with no spread/slippage — the *exact* optimism
   that cost us ₹1M of imaginary maker edge. Every survivor re-runs through **futures
   cost (real STT + lot rounding) + the live-depth fill sim** before we trust a rupee.
2. **Cost-realistic from the first backtest** — taker crossing + STT both/all legs.
3. **n_eff, not trade count.** **DSR ≥ 0.95, PBO ≤ 0.20**, deflated for *every* alpha and
   parameter we tried (ML especially — purged/embargoed CV).
4. **Lot-granularity feasibility** as a hard filter for multi-leg baskets.
5. **Paper on live data → tiny-live** before scaling. No exceptions.

> Honest expectation, unchanged: most of these die. The catalog's value is a *disciplined
> search space*, each item with a cheap predefined kill — not a promise any of them works.

---

## Part 6 — Prioritized shortlist & what to pull

**Build/test order** (best seat-fit + reuses our pipeline + cheapest kill first):

| # | Alpha | Why first | Data to pull |
|---|---|---|---|
| 1 | **A2 PCA residual reversion** | canonical market-neutral stat-arb; robust; reuses OU + gauntlet | cash-equity `daily`+`1m`, liquid universe |
| 2 | **A4 cross-sectional reversal** | measured durable property; cheap to test | same |
| 3 | **A1/A3 cointegration (re-test properly)** | close A1 rigorously on long daily history; A3 fixes β-drift | `daily` (years) |
| 4 | **A5 calendar/basis** | cointegrated by construction; reuses pipeline | futures `1m` front+next month |
| 5 | **D1 OFI taker** | honest successor to the maker; direct use of depth | live `depth` (have) |

**What to pull (informed by the asset decision):**
- **Cash-equity 1m + daily** for a liquid universe (F&O names + sector peers) → drives A2/A4/B/A1-retest. *Discovery dataset.*
- **Futures 1m (front + next month)** for the names we'd trade → calendar (A5), basis, and **execution/cost/roll modeling**.
- Keep the **live depth** feed for D-tier flow alphas + the fill-realism gate.

**Next concrete step:** build the Dhan history puller (paginate 90-day chunks, store
parquet, corporate-action adjust), backfill cash-equity daily+1m for the universe, and
re-run cointegration the rigorous way *plus* stand up the A2 PCA stat-arb screen.

---

*Same rule as always: the signal must come from a property we can test, and every
survivor must clear cost-realism before we believe it. Indicators tuned to a backtest
are not in this catalog by construction.*
