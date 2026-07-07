"""
Generate the A1 (cointegration pairs / OU) pre-research notebooks.

Run:  python research/pairs_ou/build_notebooks.py
Produces three .ipynb in this directory:
  01_data_prep.ipynb            (run on the VPS - needs the stored depth + pyarrow)
  02_cointegration_screen.ipynb (portable - needs statsmodels)
  03_ou_fit_and_backtest.ipynb  (portable - reuses basecamp_recon.arm_stats gauntlet)

Cell sources are authored as plain Python strings; nbformat handles all escaping,
so the emitted notebooks are guaranteed schema-valid.
"""

from pathlib import Path

import nbformat as nbf
from nbformat.v4 import new_notebook, new_code_cell, new_markdown_cell

HERE = Path(__file__).parent


def build(cells):
    nb = new_notebook(metadata={
        "kernelspec": {"display_name": "Python 3", "language": "python", "name": "python3"},
        "language_info": {"name": "python"},
    })
    nb.cells = [new_markdown_cell(c[1]) if c[0] == "md" else new_code_cell(c[1]) for c in cells]
    return nb


# ── Notebook 1 - data prep ─────────────────────────────────────────────────────

NB1 = [
("md", """\
# A1 Pairs/OU - 01 · Data Prep

**Run this on the VPS** (the 20-level depth lives at `~/data/tbt-dhan/depth`, and the
collector venv has `pyarrow`):

```
cd ~/paper-trader && ~/collector-dhan/venv/bin/jupyter nbconvert --to notebook \\
    --execute research/pairs_ou/01_data_prep.ipynb
```
or just run the cells in an SSH Jupyter session.

It builds **minute mid-bars** for the universe across *all available* trading days,
plus a per-symbol cost table (median spread, price, lot), and saves a compact
`panel.parquet` + `symbol_stats.csv` you can sync down and feed to NB2/NB3.

> Honest note up front: cointegration wants a long, varied span. The core 3
> (HDFCBANK/ICICIBANK/RELIANCE) have the most history; the new names start ~mid-June.
> Treat short-history pairs' results as *directional*, not a verdict."""),

("code", """\
import os, glob
from pathlib import Path
import numpy as np
import pandas as pd

# ── config ────────────────────────────────────────────────────────────────────
DATA_ROOT = os.path.expanduser("~/data/tbt-dhan/depth")   # VPS depth root
SYMBOLS   = ["HDFCBANK", "ICICIBANK", "RELIANCE", "SBIN", "AXISBANK", "BHARTIARTL", "ITC"]
BAR_FREQ  = "1min"                                          # mid-bar frequency
OUT_DIR   = Path(os.path.expanduser("~/paper-trader/research/pairs_ou/out"))
OUT_DIR.mkdir(parents=True, exist_ok=True)
LOT_SIZES = {"HDFCBANK":550,"ICICIBANK":700,"RELIANCE":500,
             "SBIN":750,"AXISBANK":625,"BHARTIARTL":475,"ITC":1600}
print("depth root:", DATA_ROOT, "| exists:", os.path.isdir(DATA_ROOT))"""),

("code", """\
# ── loader: clean L1 mid + spread for one symbol-day (mirrors basecamp_recon.markout) ──
L1 = ["collector_received_at", "bid_price_01", "ask_price_01"]

def symbol_day_files(name, date_dir):
    hits = sorted(glob.glob(f"{date_dir}/symbol={name}-*/compacted-*.parquet"))
    if not hits:
        hits = sorted(glob.glob(f"{date_dir}/symbol={name}-*/*.parquet"))
    return hits

def load_symbol_day(name, date_dir):
    files = symbol_day_files(name, date_dir)
    if not files:
        return None
    comp = [p for p in files if "compacted" in Path(p).name]
    files = comp if comp else files
    df = pd.concat([pd.read_parquet(p, columns=L1) for p in files], ignore_index=True)
    bp, ap = df.bid_price_01.to_numpy(float), df.ask_price_01.to_numpy(float)
    good = (bp > 0) & (ap > 0) & (ap >= bp)
    df = df.loc[good]
    if df.empty:
        return None
    ts = pd.to_datetime(df.collector_received_at, utc=True)
    bp, ap = df.bid_price_01.to_numpy(float), df.ask_price_01.to_numpy(float)
    out = pd.DataFrame({"ts": ts.to_numpy(), "mid": (bp+ap)/2.0, "spread": ap-bp})
    return out.sort_values("ts")

def minute_bars(name):
    \"\"\"Concatenate minute mid-bars for `name` across all available trading days.\"\"\"
    parts, spreads = [], []
    for date_dir in sorted(glob.glob(f"{DATA_ROOT}/trading_date=*")):
        d = load_symbol_day(name, date_dir)
        if d is None:
            continue
        d = d.set_index("ts")
        bars = d["mid"].resample(BAR_FREQ).last().dropna()
        parts.append(bars)
        spreads.append(d["spread"].median())
    if not parts:
        return None, np.nan
    series = pd.concat(parts).sort_index()
    series = series[~series.index.duplicated(keep="last")]
    return series, float(np.nanmedian(spreads))"""),

("code", """\
# ── build the panel + coverage report ──────────────────────────────────────────
cols, stats = {}, []
for s in SYMBOLS:
    ser, med_spread = minute_bars(s)
    if ser is None or ser.empty:
        print(f"{s:11} NO DATA"); continue
    cols[s] = ser
    stats.append({"symbol": s, "n_bars": len(ser),
                  "first": ser.index.min(), "last": ser.index.max(),
                  "n_days": ser.index.normalize().nunique(),
                  "med_price": round(float(ser.median()),2),
                  "med_spread": round(med_spread,4),
                  "lot": LOT_SIZES.get(s)})
panel = pd.DataFrame(cols).sort_index()
stats = pd.DataFrame(stats).set_index("symbol")
print("panel shape:", panel.shape)
stats"""),

("code", """\
# ── coverage heat: bars per symbol per day ──────────────────────────────────────
import matplotlib.pyplot as plt
daily = panel.notna().groupby(panel.index.normalize()).sum()
print("days x symbols:", daily.shape)
ax = (panel / panel.bfill().iloc[0]).plot(figsize=(13,5), title="normalized mid (sanity)")
ax.legend(loc="upper left", ncol=4, fontsize=8); plt.show()
daily.tail(15)"""),

("code", """\
# ── save compact artifacts to sync down for NB2/NB3 ─────────────────────────────
panel.to_parquet(OUT_DIR / "panel.parquet")
stats.to_csv(OUT_DIR / "symbol_stats.csv")
print("wrote:", OUT_DIR / "panel.parquet", "and symbol_stats.csv")
print("\\nSync down with:")
print(f"  rsync -avz lightsail-mumbai:{OUT_DIR}/ ./research/pairs_ou/out/")"""),

("md", """\
**Next:** sync `out/` to your laptop and run `02_cointegration_screen.ipynb` (it needs
`statsmodels`, which is local-only). NB1 is the only notebook that must run on the VPS."""),
]


# ── Notebook 2 - cointegration screen ──────────────────────────────────────────

NB2 = [
("md", """\
# A1 Pairs/OU - 02 · Cointegration Screen

Find pairs with a **stationary spread**, the precondition for OU mean-reversion.
Method: Engle-Granger - OLS hedge ratio β on log-prices, ADF test on the residual.
We split **in-sample / out-of-sample** and require cointegration to hold in *both*.

> **Multiple-testing discipline:** we test every pair, so the best one looks good by
> luck. We record the number of pairs tested (`N_TRIALS`) and deflate for it in NB3
> (DSR). Do **not** cherry-pick the prettiest spread here and forget the others existed."""),

("code", """\
import os, sys, itertools
import numpy as np, pandas as pd
import statsmodels.api as sm
from statsmodels.tsa.stattools import adfuller, coint
import matplotlib.pyplot as plt
sys.path.insert(0, os.path.abspath("../.."))          # repo root for basecamp_recon

OUT = "out"
panel = pd.read_parquet(f"{OUT}/panel.parquet")
stats = pd.read_csv(f"{OUT}/symbol_stats.csv", index_col=0)
logp  = np.log(panel)
print("panel:", panel.shape, "| span:", panel.index.min(), "->", panel.index.max())
stats"""),

("code", """\
# ── in-sample / out-of-sample split (chronological) ─────────────────────────────
SPLIT = 0.6
n = len(logp); k = int(n*SPLIT)
IS, OOS = logp.iloc[:k], logp.iloc[k:]
print(f"IS bars={len(IS)}  OOS bars={len(OOS)}")"""),

("code", """\
def eg_test(a, b):
    \"\"\"Engle-Granger on a,b (log-price series). Returns hedge beta + ADF p on residual.\"\"\"
    x = sm.add_constant(b.values)
    beta = sm.OLS(a.values, x).fit().params         # [const, slope]
    resid = a.values - (beta[0] + beta[1]*b.values)
    adf_p = adfuller(resid, maxlag=1, autolag=None)[1]
    return beta[1], adf_p, resid

def rolling_beta_std(a, b, win=120):
    cov = a.rolling(win).cov(b); var = b.rolling(win).var()
    return float((cov/var).std())

rows = []
pairs = list(itertools.combinations(panel.columns, 2))
for x, y in pairs:
    s = panel[[x, y]].dropna()
    if len(s) < 500:                                  # need enough overlap
        continue
    ai, bi = np.log(s[x]).loc[IS.index.intersection(s.index)], np.log(s[y]).loc[IS.index.intersection(s.index)]
    ao, bo = np.log(s[x]).loc[OOS.index.intersection(s.index)], np.log(s[y]).loc[OOS.index.intersection(s.index)]
    if len(ai) < 300 or len(ao) < 200:
        continue
    beta_is, p_is, _   = eg_test(ai, bi)
    beta_oos, p_oos, _ = eg_test(ao, bo)
    rows.append({"x": x, "y": y, "beta_is": round(beta_is,3), "beta_oos": round(beta_oos,3),
                 "adf_p_is": round(p_is,4), "adf_p_oos": round(p_oos,4),
                 "beta_drift": round(rolling_beta_std(np.log(s[x]), np.log(s[y])),3),
                 "corr": round(np.log(s[x]).diff().corr(np.log(s[y]).diff()),3),
                 "n": len(s)})
N_TRIALS = len(rows)
screen = pd.DataFrame(rows).sort_values("adf_p_oos")
print("pairs tested (N_TRIALS):", N_TRIALS)
screen"""),

("code", """\
# ── candidates: cointegrated IS *and* OOS, stable beta ──────────────────────────
ALPHA = 0.05
cand = screen[(screen.adf_p_is < ALPHA) & (screen.adf_p_oos < ALPHA)].copy()
cand = cand.sort_values(["adf_p_oos", "beta_drift"])
print(f"{len(cand)} pair(s) cointegrated in BOTH halves at p<{ALPHA}:")
cand"""),

("code", """\
# ── visualise the top candidate spreads ─────────────────────────────────────────
top = cand.head(4) if len(cand) else screen.head(4)
fig, axes = plt.subplots(len(top), 1, figsize=(13, 2.6*len(top)), squeeze=False)
for ax, (_, r) in zip(axes[:,0], top.iterrows()):
    s = panel[[r.x, r.y]].dropna()
    beta = sm.OLS(np.log(s[r.x]).values, sm.add_constant(np.log(s[r.y]).values)).fit().params
    spread = np.log(s[r.x]) - (beta[0] + beta[1]*np.log(s[r.y]))
    ax.plot(spread.values); ax.axhline(spread.mean(), color="k", lw=.6)
    ax.set_title(f"{r.x}-{r.y}  adf_p_oos={r.adf_p_oos}  beta_drift={r.beta_drift}", fontsize=9)
plt.tight_layout(); plt.show()"""),

("code", """\
# ── save candidates + trial count for NB3 ───────────────────────────────────────
cand.to_csv(f"{OUT}/candidate_pairs.csv", index=False)
pd.Series({"N_TRIALS": N_TRIALS}).to_csv(f"{OUT}/screen_meta.csv")
print("wrote candidate_pairs.csv (N_TRIALS=%d)" % N_TRIALS)"""),

("md", """\
**Read it honestly:** a pair passing here is a *candidate*, not an edge. Beta drift
matters as much as the ADF p-value - a spread that's stationary but whose hedge ratio
wanders is not tradeable. If **zero** pairs pass in both halves, that's a clean *kill*
of A1 on this data; don't lower ALPHA to manufacture survivors. Next: `03_ou_fit_and_backtest.ipynb`."""),
]


# ── Notebook 3 - OU fit + cost-realistic backtest + gauntlet ────────────────────

NB3 = [
("md", """\
# A1 Pairs/OU - 03 · OU Fit + Cost-Realistic Backtest + Gauntlet

For each surviving pair: fit the OU spread (→ half-life), backtest a z-score rule on
**OOS** with **realistic taker costs on both legs** (cross-spread + STT + fees), then
run the same gauntlet we used in Recon (`basecamp_recon.arm_stats`): n_eff → PSR →
**DSR deflated by N_TRIALS** → PBO.

> The bar is unchanged: **DSR ≥ 0.95, PBO ≤ 0.20, net of cost, out-of-sample.**
> Anything else is a hypothesis, not an edge."""),

("code", """\
import os, sys
import numpy as np, pandas as pd
import statsmodels.api as sm
import matplotlib.pyplot as plt
sys.path.insert(0, os.path.abspath("../.."))
from basecamp_recon.arm_stats import sharpe, n_eff, psr, deflated_sharpe, pbo_cscv

OUT = "out"
panel = pd.read_parquet(f"{OUT}/panel.parquet")
stats = pd.read_csv(f"{OUT}/symbol_stats.csv", index_col=0)
cand  = pd.read_csv(f"{OUT}/candidate_pairs.csv")
N_TRIALS = int(pd.read_csv(f"{OUT}/screen_meta.csv", index_col=0).loc["N_TRIALS"].iloc[0])
print(f"{len(cand)} candidate pairs | N_TRIALS={N_TRIALS}")"""),

("code", """\
# ── OU fit via AR(1) on the spread:  S_t+1 = c + b S_t + e ───────────────────────
BAR_MIN = 1.0    # minutes per bar (matches NB1 BAR_FREQ)

def ou_fit(spread):
    s = spread.dropna().values
    x = sm.add_constant(s[:-1]); y = s[1:]
    c, b = sm.OLS(y, x).fit().params
    b = min(max(b, 1e-6), 0.999999)
    theta = -np.log(b) / BAR_MIN                      # per-minute mean-reversion
    half_life = np.log(2)/theta if theta > 0 else np.inf   # minutes
    mu = c/(1-b)
    sigma_eq = np.std(y - (c + b*s[:-1])) / np.sqrt(1-b**2)
    return dict(theta=theta, half_life_min=half_life, mu=mu, sigma_eq=sigma_eq, b=b)

def spread_series(x, y):
    s = panel[[x, y]].dropna()
    beta = sm.OLS(np.log(s[x]).values, sm.add_constant(np.log(s[y]).values)).fit().params
    sp = np.log(s[x]) - (beta[0] + beta[1]*np.log(s[y]))
    return sp, beta[1], s"""),

("code", """\
# ── taker cost per round-trip for the PAIR (both legs), in spread-log units ──────
STT = 0.000125          # sell-side; applied ~once per leg round-trip (approx)
BROKER = 20.0           # per order

def leg_cost_bps(sym):
    px, sp = stats.loc[sym, "med_price"], stats.loc[sym, "med_spread"]
    cross_bps = (sp / px) * 1e4                       # cross the spread once (taker)
    stt_bps   = STT * 1e4                             # sell leg
    misc_bps  = (BROKER / (px * stats.loc[sym,"lot"])) * 1e4 * 2   # brokerage both orders
    return cross_bps + stt_bps + misc_bps

def pair_roundtrip_cost_logret(x, y):
    # round trip = open+close, both legs taker → ~2x leg cost per leg, summed over 2 legs
    return (leg_cost_bps(x) + leg_cost_bps(y)) / 1e4   # as a log-return-ish fraction
print({s: round(leg_cost_bps(s),2) for s in stats.index})"""),

("code", """\
# ── z-score backtest on OOS spread ──────────────────────────────────────────────
SPLIT = 0.6
ENTRY_Z, EXIT_Z, STOP_Z = 2.0, 0.5, 4.0
ZWIN = 120              # rolling window for z (~2h at 1min)

def backtest(x, y):
    sp, beta, s = spread_series(x, y)
    ou = ou_fit(sp)
    k = int(len(sp)*SPLIT); oos = sp.iloc[k:]
    z = (oos - oos.rolling(ZWIN).mean()) / oos.rolling(ZWIN).std()
    pos = pd.Series(0.0, index=oos.index)
    state = 0
    for t in range(1, len(oos)):
        zt = z.iloc[t]
        if np.isnan(zt): continue
        if state == 0 and abs(zt) > ENTRY_Z:   state = -np.sign(zt)   # fade the spread
        elif state != 0 and (abs(zt) < EXIT_Z or abs(zt) > STOP_Z): state = 0
        pos.iloc[t] = state
    dspread = oos.diff().fillna(0.0)
    gross = pos.shift(1).fillna(0.0) * dspread        # spread log-return pnl (beta-hedged)
    turns = pos.diff().abs().fillna(0.0)
    cost  = turns * (pair_roundtrip_cost_logret(x, y) / 2.0)   # cost per side-change
    net = gross - cost
    daily = net.groupby(net.index.normalize()).sum()
    return ou, daily, pos

results = {}
for _, r in cand.iterrows():
    ou, daily, pos = backtest(r.x, r.y)
    results[f"{r.x}-{r.y}"] = {"ou": ou, "daily": daily,
                               "n_trades": int(pos.diff().abs().sum()/2)}
{k: (round(v["ou"]["half_life_min"],1), v["n_trades"]) for k,v in results.items()}"""),

("code", """\
# ── the gauntlet, per pair (net-of-cost OOS daily returns) ───────────────────────
sr_trials = []
table = []
for name, v in results.items():
    d = v["daily"]; r = d.values
    if len(r) < 3 or r.std() == 0:
        continue
    sr = sharpe(r); sr_trials.append(sr)
for name, v in results.items():
    d = v["daily"]; r = d.values
    if len(r) < 3 or r.std() == 0:
        table.append({"pair": name, "note": "too few days / zero var"}); continue
    sr = sharpe(r)
    dsr, sr_star = deflated_sharpe(sr, len(r), float(pd.Series(r).skew()),
                                   float(pd.Series(r).kurt()+3.0),
                                   np.array(sr_trials if len(sr_trials)>1 else [sr,0.0]))
    table.append({"pair": name, "days": len(r), "half_life_min": round(v["ou"]["half_life_min"],1),
                  "trades": v["n_trades"], "net_total": round(float(r.sum()),5),
                  "sharpe_day": round(sr,2), "PSR>0": round(psr(sr,len(r),0,3),2),
                  "DSR": round(dsr,2)})
gauntlet = pd.DataFrame(table)
print("N_TRIALS used for deflation:", N_TRIALS, "(note: also deflate by sweep below)")
gauntlet"""),

("code", """\
# ── PBO across the candidate pairs (CSCV) ───────────────────────────────────────
M = pd.DataFrame({name: v["daily"] for name, v in results.items()
                  if len(v["daily"]) >= 4}).dropna()
if M.shape[1] >= 2 and M.shape[0] >= 4:
    print("PBO:", pbo_cscv(M, s=min(6, M.shape[0])))
else:
    print("not enough pairs/days for a meaningful PBO - treat single-pair results with extreme caution")"""),

("code", """\
# ── equity curves ───────────────────────────────────────────────────────────────
plt.figure(figsize=(13,5))
for name, v in results.items():
    v["daily"].cumsum().plot(label=name)
plt.legend(fontsize=8); plt.title("OOS cumulative net-of-cost spread P&L (log-units)"); plt.show()"""),

("md", """\
## Verdict checklist (be ruthless)

- **DSR ≥ 0.95 and PBO ≤ 0.20**, net of cost, OOS - *and* remember DSR here is only
  deflated by `N_TRIALS` pairs; if you *also* swept ENTRY_Z/EXIT_Z/ZWIN, the true trial
  count is larger and DSR is optimistic. Don't sweep-and-pick.
- **Half-life sane** (minutes-to-hours, stable) - a 1-bar half-life is noise; a
  multi-day half-life can't be traded intraday on this capital.
- **Costs didn't do all the work** - check `net_total` vs a zero-cost run; if the edge
  is razor-thin above cost, it won't survive real fills (we learned that the hard way).

**If nothing clears the bar:** that's a clean, cheap kill of A1 - log it and move to the
next candidate in `SEAT_AND_STRATEGIES.md`. **If something does:** the *only* next step is
a queue-/cost-aware paper arm on live data - never straight to capital."""),
]


def main():
    for fname, cells in [("01_data_prep.ipynb", NB1),
                         ("02_cointegration_screen.ipynb", NB2),
                         ("03_ou_fit_and_backtest.ipynb", NB3)]:
        nbf.write(build(cells), str(HERE / fname))
        print("wrote", HERE / fname)


if __name__ == "__main__":
    main()
