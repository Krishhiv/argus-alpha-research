# Recon I — Findings (Tier A / C / D)

*Analysis of the Basecamp multi-arm race. 12 clean data-days (2026-06-08 … 06-25;
excludes 06-19 & 06-26 holidays and the 06-22 Dhan depth-feed outage). All P&L is
`net_pnl` — net of every fee incl. STT. Reproduce: `python -m basecamp_recon.recon_report`.*

---

## 0. Headline

**`expanded` is decisively the best arm, but it does not yet clear the strict
promotion gate, and its whole edge is a thin residual exposed to the live
fill-rate.** It is the lead candidate to carry into Expenture for queue-aware
re-validation — *not* a green light for live capital.

---

## 1. Tier A — ranked arms

| arm | total net | mean/day | Sharpe | trades | n_eff | WR% | DSR | verdict |
|---|--:|--:|--:|--:|--:|--:|--:|---|
| **expanded** | **₹336,160** | 28,013 | **1.13** | 5849 | 5056 | 67.5 | **0.93** | lead candidate |
| wide_stop | 40,327 | 3,361 | 0.41 | 1614 | 1319 | 64.3 | 0.10 | stop tweak (see A2) |
| no_stop | 40,162 | 3,347 | 0.44 | 1568 | 1372 | 64.9 | 0.13 | — |
| control | 27,913 | 2,326 | 0.29 | 1665 | 792 | 62.9 | 0.04 | baseline |
| selective | 21,303 | 1,775 | 0.62 | 167 | 167 | 66.5 | 0.25 | low-risk variant |
| no_icici | 10,037 | 836 | 0.21 | 761 | 679 | 62.5 | 0.01 | rejected (A3) |
| reversal | −50,240 | −4,187 | −0.75 | 1835 | 976 | 46.0 | 0.00 | **kill (A6)** |

- **Promotion gates: DSR ≥ 0.95, PBO ≤ 0.20.**
- **PBO = 0.00** (CSCV, 20 splits) — the *selection* procedure generalizes
  strongly: the in-sample-best arm is reliably the out-of-sample-best. The race
  is **not** overfit.
- **No arm clears DSR ≥ 0.95.** `expanded` is closest at **0.93** — a *power*
  miss, not a quality miss: with only 12 daily observations the multiple-testing
  benchmark (SR\* = 0.78 across 7 arms) is hard to beat. The continuing
  collection + Expenture re-validation are exactly what closes this gap.

**Verdict:** PBO says "the winner is real, not luck." DSR says "12 days isn't
quite enough to *prove* it at 95%." Both point the same way: `expanded` is the
genuine leader; give it more data, don't bet the house yet.

---

## 2. Tier A — paired arm-vs-arm (A2–A6)

Same market days → paired test. Mean daily diff [95% bootstrap CI]:

| Q | comparison | diff/day | 95% CI | verdict |
|---|---|--:|---|---|
| A4 | expanded − control | **+25,687** | [+16.3k, +35.6k] | **✓ new names add real edge** |
| A2 | wide_stop − control | +1,034 | [+231, +1,791] | **✓ wider stop (24t) beats tight (12t)** |
| A2 | no_stop − control | +1,021 | [−501, +2,525] | ~ inconclusive |
| A5 | selective − control | −551 | [−4.1k, +2.7k] | ~ inconclusive |
| A3 | no_icici − control | −1,490 | [−5.4k, +1.9k] | ~ inconclusive (leans *worse*) |
| A6 | reversal − control | **−6,513** | [−8.7k, −4.3k] | **✗ reversal exit is worse** |

---

## 3. Tier C — attribution (where the money is)

### 3a. Exit method — *the structural finding*
The entire edge is the **maker exit**; both taker buckets bleed heavily.

| exit (expanded) | net | trades | mean/trade | WR |
|---|--:|--:|--:|--:|
| maker_exit | +958,785 | 4440 | +216 | 86% |
| taker_max_hold | −130,922 | 698 | −188 | 17% |
| taker_stop | −491,704 | 711 | −692 | 0% |

→ **Net ₹336k is only 35% of the ₹959k maker-exit gross; the taker buckets eat
the other 65%.** This is why the live fill-rate is everything (§5).

### 3b. Instrument — the edge is *new-name*, and *concentrated*
- **The 4 new names = 92% of expanded's total** (AXISBANK +₹182k = **54% alone**,
  BHARTIARTL +₹63k, SBIN +₹62k). The 3 core names contribute only ~₹28k.
- **A3 flip:** ICICIBANK is control's **best** core name (+₹17.9k), not a drag.
  **HDFCBANK is the laggard** (−₹0.5k, flat). Revisit HDFCBANK, keep ICICI.
- **Tier D verify:** ITC correctly almost never trades (1 trade) — the economic
  gate suppresses it (low price → spread can't cover fees). Working as designed.

### 3c. Time of day
The **9:00 open hour is the weakest** for both arms (control *loses* −₹13.8k
there; expanded earns its lowest ₹/trade). Candidate improvement: size down or
suppress entries in the first 15 min (adverse selection at the open).

---

## 4. Tier D — correlation & risk

- **expanded never had a down day** (worst = +₹1,731) and its best day is only 20%
  of total — *day*-diversified by breadth (control's best day = 59% of total). But
  it is **name-concentrated** (AXISBANK 54%). Diversified across days, fragile to
  one name.
- Skew positive for the winners (right-tailed, good); reversal negative (bad).
  no_icici has fat tails (kurt 8.5) — fewer names = lumpier.
- Arms are highly correlated (0.89–0.98 among core-universe arms; shared signal).
- **Caveat:** these 12 days contained **no hostile (whipsaw) regime** — the
  "never a down day" record almost certainly *understates* tail risk. The B2
  whipsaw-loss test still needs a hostile day to bite.

---

## 5. Tier E — the binding constraint (fill realism)

The sim fills maker-exits optimistically. Computed from expanded's own economics:

- maker_exit +₹216/trade, taker −₹442/trade.
- **Breakeven maker-exit fill rate p\* = 67.2%.**
- **The sim runs at p = 75.9%.** → cushion is only **~8.7 percentage points.**

So if the live maker-exit fill rate comes in below ~67% (queue position +
adverse selection, both worse live than in the touch-fill sim), `expanded` goes
negative. **This 8.7pp cushion — not the arm ranking — is the real risk.** It can
only be pinned by the queue-aware fill sim (Expenture I) or tiny-live. *(Updates
the earlier hand-waved "~80%" breakeven to a data-derived 67%.)*

---

## 6. Decisions & build list

**Act on now (config, well-supported):**
1. **Kill the reversal exit** — decisively worse (−₹6.5k/day, P(>0)=0). [A6]
2. **Widen the stop 12 → 24 ticks** — wide_stop beats control (P=0.99) with a
   better tail than no_stop. The tight stop fires on whipsaws. [A2]
3. **Keep ICICIBANK; flag HDFCBANK** — ICICI is a winner, HDFC is the laggard. [A3]

**Carry into Expenture (gated on queue-sim):**
4. **`expanded` is the lead candidate** — but re-validate under realistic fills
   before any capital; the 8.7pp p-cushion is the gate. [Tier E]
5. **Per-name robustness on AXISBANK** — 54% concentration; is the edge
   persistent or a 12-day fluke? Needs the extra collection days + per-name DSR.
6. **Open-hour suppression** — test sizing down/skipping the first 15 min. [3c]
7. **`selective` as a risk-sizing variant** — far fewer trades, lower tail,
   comparable Sharpe; useful when sizing real capital. [A5]

**Still open (need more/other data):**
- DSR ≥ 0.95 for expanded — a power problem; the continuing run banks more days.
- B2 whipsaw-loss test — needs a hostile regime day (none in these 12).

---

*Built: `basecamp_recon/arm_stats.py` (+ tests), `recon_report.py`. Full raw
output: `recon_data/recon_i_report.txt`.*
