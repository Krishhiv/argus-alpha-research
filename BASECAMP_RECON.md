# Phase Basecamp Recon — Analysis, Research, Test & Verify Plan

*The investigative phase after the 15-day Basecamp multi-arm run. Goal: interpret
the data rigorously, settle the open questions, and decide what to build — without
fooling ourselves. Every item below is a **hypothesis to test**, not a belief to
confirm.*

---

## 0. Guiding discipline (read first)

> A backtest/experiment is a *search*. The more arms and variants we raced, the
> better the best one looks **by luck alone**. Our job is to subtract that luck
> before believing any winner.

**Hard gates before any arm is promoted to Expenture:**
- **Deflated Sharpe Ratio (DSR) ≥ 0.95** — winner's edge survives multiple-testing deflation.
- **Probability of Backtest Overfitting (PBO) ≤ 0.2** — the selection procedure generalizes.
- Rank on **STT-adjusted net** returns, never gross.
- Use **effective sample size (n_eff)**, never raw trade count (trades are autocorrelated).

Anything that fails the gates is a *hypothesis*, not an edge.

---

## 1. Prerequisites (do these before the analysis)

- [ ] **Freeze the dataset** — Basecamp ends; no collector/config changes during analysis.
- [ ] **Add signal-context logging** to the trade record: `micro_deviation` at entry, `spread`, `edge_margin`, and (if cheap) a realized-vol snapshot. *Pure observability, zero strategy change.* Without this, we can't ask "what conditions produce the best trades." (Applies going forward — Basecamp trades won't have it, but the depth join below recovers it for the 3 core names.)
- [ ] **Stand up the depth↔trade join** for HDFCBANK / ICICIBANK / RELIANCE (the 3 names with both paper trades *and* months of collector depth). This is the analytical workhorse — it reconstructs the full microstructure context at each trade.
- [ ] Confirm the **4 new names** (SBIN, AXISBANK, ITC, BHARTIARTL) are now archiving depth in the collector (added 2026-06-16) for *future* per-name microstructure work.

---

## 2. Tier A — Rigorous arm evaluation (the core deliverable)

Settle, with the §0 discipline, which arm(s) genuinely have edge.

| # | Question | Method | Arms compared |
|---|---|---|---|
| A1 | Which arm is genuinely best (not luckiest)? | n_eff → PSR → DSR → PBO on STT-net returns | all 7 |
| A2 | **Does the stop help?** | head-to-head net + tail (worst-trade) + exit mix | `no_stop` vs `control` vs `wide_stop` |
| A3 | **Is ICICIBANK a drag, or a high-variance engine?** | per-instrument net/WR + arm-level | `no_icici` vs `control` |
| A4 | **Do the 4 new instruments earn their place?** | per-instrument DSR; is `expanded`'s edge real or BHARTIARTL/SBIN-driven? | `expanded` vs `control` |
| A5 | **Does selectivity (margin 1.5) help?** | net/trade, WR, fill rate, total | `selective` vs `control` |
| A6 | **Does the reversal-exit work?** | (provisional: looks *weak* — 50% WR, underperforms on trend days) | `reversal` vs `control` |

**Methodology to build for this tier:**
- **Paired comparison** of arms (arm A − arm B on the *same* day/instrument) — far more powerful than independent comparison, since the arms share market moves.
- **Block bootstrap** for confidence intervals on Sharpe and on arm-vs-arm *differences* (robust to non-normal, autocorrelated returns).
- **Honest trial counting** — arms × instruments × every threshold/lookback we ever swept; estimate the *effective* number of independent trials from the arm-correlation matrix.

---

## 3. Tier B — Regime & microstructure research (the exciting part)

This is where the data teaches us things we'd have guessed wrong.

### B1 — ⭐ THE headline hypothesis: does the maker excel in *orderly trends*, not chop?
- **Origin:** On 2026-06-18, expanded made +₹67.8k, and the **biggest winners were the most *trending* instruments** (SBIN +₹27.6k @ 83% WR, trend≈89%; ICICIBANK +₹14.6k, trend≈88%). This is the *opposite* of our original "makers love chop" theory.
- **Proposed mechanism:** our microprice signal is *directional* (follows order-flow imbalance), so in a trend it keeps us positioned *with* the move — we *ride* orderly trends rather than fading them.
- **Reframed regime axis to test:** `orderly trend` (best) › `gentle chop` (ok) › `sharp reversals / whipsaws` (worst). The enemy is the **speed of direction *change***, not direction or volatility.
- **Test:** classify each session-instrument by trend/chop/whipsaw (variance-ratio, Hurst, signed-drift/range) and regress arm P&L on regime. Confirm or kill the "orderly-trend = sweet spot" claim with real features (not the rough `drift/range` proxy used live).

### B2 — The reversal/whipsaw loss hypothesis
- **Claim (unverified):** our losses (`taker_stop` + `taker_max_hold`) cluster around **sharp reversals** — we get filled right as price flips, then run over (adverse selection); the signal lags the flip.
- **Test:** take every `taker_stop` loss, join to depth around entry, check whether price was moving *with* us just before entry then reversed — a *whipsaw kill* vs. just "we were wrong." If losses concentrate there → confirmed.
- **Connection:** this would explain why **tight stops were catastrophic** earlier (whipsaw stops you out right before the revert). Consistency check.

### B3 — Regime-conditional performance per arm
- For each arm: P&L conditioned on regime. *Caveat:* n_eff collapses when you slice by regime (maybe ~10 trending episodes total) — "arm X wins in trends" may be a 10-sample claim. Quantify the conditional n_eff and don't over-read.

### B4 — Build the regime/trend estimator (the shared lens)
- **Kalman velocity** (constant-velocity model on log-price) + **OFI/microprice** fusion (multi-sensor) + **variance-ratio / Hurst** regime classifier.
- Build & validate **offline on the 3 core names' stored depth**. Normalized/dimensionless features so it **generalizes** to all instruments (apply live via L1; calibrate on the 3).
- This estimator powers everything downstream (regime filter, trend-aligned entries, momentum).

---

## 4. Tier C — Performance attribution / decomposition

Answer *"where and why"* each arm makes/loses money.

- **By instrument** — which names carry real alpha vs. ride variance (ties to A3/A4).
- **By exit method** — economics of `maker_exit` vs `taker_stop` vs `taker_max_hold` vs `taker_reversal`. (Recurring pattern: maker_exit bucket profitable, taker buckets bleed.)
- **By time-of-day** — open / mid-day / close behavior (intraday regime seasonality).
- **By regime** — the B1/B3 conditioning.

---

## 5. Tier D — Specific items to verify

- [ ] **ITC isn't trading** — confirm the economic gate is correctly suppressing it (low price → spread can't cover fees), not a bug.
- [ ] **The big expanded days (+₹61k, +₹67.8k)** — regime-attribute them. Skill × favorable regime × breadth, or something else?
- [ ] **Expanded's correlation risk** — quantify how correlated the 7 instruments' daily P&L is (expanded amplifies *both* good and bad regime days; measure the downside amplification).
- [ ] **Day-to-day variance** — characterize the return distribution per arm (skew, kurtosis, worst day) — feeds PSR and the sizing decision later.

---

## 6. Tier E — The paper-to-live fidelity gap

*The thing that has bitten us and isn't an overfitting problem.*

- **Known issue:** the compacted-data replay missed live by ₹11k on one day — our simulation fill model is unreliable.
- **Research:** how would realistic **queue position, partial fills, and slippage** change the maker's P&L? The current model assumes optimistic fills. This gates how much we trust *any* paper number when sizing real capital (post-Expenture).
- Deliverable: a more realistic fill model, or at least an estimated haircut on paper P&L.

---

## 7. What we build *after* the analysis (gated on findings)

Only build these if the analysis *warrants* them. They are the "improve/optimise" half of Recon → validated in Expenture.

| Build | Gated on | Cycle |
|---|---|---|
| **Regime/trend estimator** (Kalman+OFI+VR) | B4 (it's the prerequisite for all below) | this cycle |
| **Trend-aligned entries** | B1 confirming the maker rides orderly trends | this cycle → Expenture |
| **Regime / whipsaw filter** | B1/B2 confirming a hostile regime exists & is detectable | this cycle → Expenture |
| **Momentum arm** | the trend opportunity being large enough | later cycle |
| **Regime-based algo switching** (momentum ↔ MM) | *both* a refined maker AND a momentum arm validated first | later cycle |

> Note: B1 may *flip* the original plan — if the maker excels in *orderly trends*, we **lean into** them (trend-aligned entries) rather than filtering trends out. The filter targets **whipsaws/reversals**, not trends.

---

## 8. Recon's required outputs (definition of done)

1. A **ranked arm table** with DSR/PBO/n_eff — and a defensible **promotion decision** (which arm(s), if any, clear the gates).
2. A **regime map**: which regimes the maker wins/loses in, confirmed with real features — and the verdict on B1 (orderly-trend hypothesis) and B2 (whipsaw-loss hypothesis).
3. A working **regime/trend estimator** (offline-validated on the 3 core names).
4. A **build list** for Expenture (trend-aligned entries / filter), each justified by a specific finding.
5. An honest **fill-realism haircut** to apply to all paper P&L before any real-capital thinking.

---

*Created 2026-06-18, mid-Basecamp. The discipline that matters most: rank with n_eff
and deflate with DSR — subtract luck before believing a winner. Everything here is a
question, not an answer.*
