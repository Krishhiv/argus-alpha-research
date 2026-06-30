"""
Dhan historical data puller — the data foundation for the alpha search.

Pulls **daily** and **intraday 1-min** OHLCV for a universe and stores parquet.
Built from the v2 docs:
  daily    : POST https://api.dhan.co/v2/charts/historical   (fromDate/toDate = YYYY-MM-DD)
  intraday : POST https://api.dhan.co/v2/charts/intraday      (interval ∈ {1,5,15,25,60};
             fromDate/toDate = 'YYYY-MM-DD HH:MM:SS'; max 90 days per request → we paginate)
Response arrays: open[], high[], low[], close[], volume[], timestamp[] (epoch), open_interest[].

Run on the VPS (the Dhan token lives there):
    cd ~/paper-trader
    ~/collector-dhan/venv/bin/python -m alpha_lab.data.dhan_history --years 3

Discovery vehicle = **cash equity** (NSE_EQ / EQUITY) — clean, no roll (per ALPHA_CATALOG
Part 1). Signals map to futures for execution. Output → alpha_lab/data/out/{daily,1m}/SYMBOL.parquet.
"""

from __future__ import annotations

import argparse
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import requests
from dotenv import load_dotenv

DAILY_URL    = "https://api.dhan.co/v2/charts/historical"
INTRADAY_URL = "https://api.dhan.co/v2/charts/intraday"
OUT = Path(__file__).parent / "out"

# Universe to discover on (cash equity). Fill security IDs from the scrip master via
# resolve_equities(), or hardcode {symbol: securityId} here once resolved.
UNIVERSE = ["HDFCBANK", "ICICIBANK", "RELIANCE", "SBIN", "AXISBANK", "BHARTIARTL",
            "ITC", "KOTAKBANK", "INFY", "TCS", "LT", "BAJFINANCE"]


def load_token() -> str:
    load_dotenv()
    p = Path(os.environ["DHAN_ACCESS_TOKEN_PATH"]).expanduser()
    tok = p.read_text().strip()
    if not tok:
        raise RuntimeError(f"empty token file: {p}")
    return tok


def resolve_equities(symbols: list[str], master_path: str | None = None) -> dict[str, str]:
    """
    Best-effort symbol → NSE_EQ securityId from the Dhan scrip master CSV.
    Tolerant to column-name variants; verify the first few against Dhan before trusting.
    """
    master_path = master_path or os.environ.get("INSTRUMENT_MASTER_PATH", "")
    if not master_path or not Path(master_path).expanduser().exists():
        raise FileNotFoundError("set INSTRUMENT_MASTER_PATH to the Dhan scrip master CSV")
    df = pd.read_csv(Path(master_path).expanduser(), low_memory=False)
    U = {c.upper(): c for c in df.columns}
    def col(*opts, required=True):
        for o in opts:
            if o in U:
                return U[o]
        if required:
            raise KeyError(f"none of {opts} in master columns {list(df.columns)[:8]}…")
        return None
    # Dhan detailed scrip master: EXCH_ID / SEGMENT / INSTRUMENT / UNDERLYING_SYMBOL / SECURITY_ID.
    # NSE cash equity = EXCH_ID 'NSE', SEGMENT 'E', INSTRUMENT 'EQUITY'; symbol in UNDERLYING_SYMBOL.
    exch_c  = col("EXCH_ID", "SEM_EXM_EXCH_ID", "EXCHANGE", required=False)
    seg_c   = col("SEGMENT", "SEM_SEGMENT")
    instr_c = col("INSTRUMENT", "SEM_INSTRUMENT_NAME", required=False)
    sym_c   = col("UNDERLYING_SYMBOL", "SEM_TRADING_SYMBOL", "SYMBOL_NAME", "TRADINGSYMBOL")
    id_c    = col("SECURITY_ID", "SEM_SMST_SECURITY_ID", "SECURITYID")
    eq = df
    if exch_c:
        eq = eq[eq[exch_c].astype(str).str.upper() == "NSE"]
    eq = eq[eq[seg_c].astype(str).str.upper() == "E"]
    if instr_c:
        eq = eq[eq[instr_c].astype(str).str.upper() == "EQUITY"]
    out = {}
    for s in symbols:
        hit = eq[eq[sym_c].astype(str).str.upper() == s.upper()]
        if len(hit):
            out[s] = str(int(float(hit.iloc[0][id_c])))
    missing = set(symbols) - set(out)
    if missing:
        print(f"  ! unresolved equities (set manually): {sorted(missing)}")
    return out


def _post(url: str, body: dict, token: str, retries: int = 4) -> dict:
    headers = {"Content-Type": "application/json", "access-token": token}
    for attempt in range(retries):
        r = requests.post(url, json=body, headers=headers, timeout=30)
        if r.status_code == 200:
            return r.json()
        if r.status_code in (429, 500, 502, 503):          # rate-limited / transient
            time.sleep(1.5 * (attempt + 1))
            continue
        raise RuntimeError(f"{url} -> {r.status_code}: {r.text[:200]}")
    raise RuntimeError(f"{url} failed after {retries} retries")


def _to_df(j: dict) -> pd.DataFrame:
    if not j or not j.get("timestamp"):
        return pd.DataFrame()
    df = pd.DataFrame({
        "ts": pd.to_datetime(j["timestamp"], unit="s", utc=True),   # VERIFY tz on first pull
        "open": j["open"], "high": j["high"], "low": j["low"],
        "close": j["close"], "volume": j["volume"],
    })
    return df.sort_values("ts").reset_index(drop=True)


def _chunks(start: datetime, end: datetime, days: int):
    cur = start
    while cur < end:
        nxt = min(cur + timedelta(days=days), end)
        yield cur, nxt
        cur = nxt


def fetch(security_id: str, *, intraday: bool, start: datetime, end: datetime,
          token: str, segment="NSE_EQ", instrument="EQUITY", interval=1,
          pause=0.6) -> pd.DataFrame:
    url = INTRADAY_URL if intraday else DAILY_URL
    chunk_days = 85 if intraday else 365          # intraday hard-capped at 90d/request
    fmt = "%Y-%m-%d %H:%M:%S" if intraday else "%Y-%m-%d"
    parts = []
    for a, b in _chunks(start, end, chunk_days):
        body = {"securityId": security_id, "exchangeSegment": segment,
                "instrument": instrument,
                "fromDate": a.strftime(fmt), "toDate": b.strftime(fmt)}
        if intraday:
            body["interval"] = str(interval)
        df = _to_df(_post(url, body, token))
        if len(df):
            parts.append(df)
        time.sleep(pause)                          # be polite to the rate limiter
    if not parts:
        return pd.DataFrame()
    out = pd.concat(parts, ignore_index=True).drop_duplicates("ts").sort_values("ts")
    return out.reset_index(drop=True)


def main() -> int:
    ap = argparse.ArgumentParser(description="Backfill Dhan daily + 1m history.")
    ap.add_argument("--years", type=float, default=3.0)
    ap.add_argument("--no-intraday", action="store_true", help="daily only")
    ap.add_argument("--intraday-only", action="store_true", help="skip daily (1m only)")
    ap.add_argument("--master", default=None, help="scrip-master CSV path (else env)")
    a = ap.parse_args()

    token = load_token()
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=int(a.years * 365))
    ids = resolve_equities(UNIVERSE, a.master)
    print(f"resolved {len(ids)}/{len(UNIVERSE)} equities; window {start.date()} → {end.date()}")

    stages = ([] if a.intraday_only else [("daily", False)]) + ([] if a.no_intraday else [("1m", True)])
    for kind, intraday in stages:
        d = OUT / kind
        d.mkdir(parents=True, exist_ok=True)
        for sym, sid in ids.items():
            try:
                df = fetch(sid, intraday=intraday, start=start, end=end, token=token)
            except Exception as exc:                 # noqa: BLE001 — one bad name shouldn't kill the run
                print(f"  {sym:11} {kind}: ERROR {exc}")
                continue
            if df.empty:
                print(f"  {sym:11} {kind}: no data"); continue
            df.to_parquet(d / f"{sym}.parquet")
            print(f"  {sym:11} {kind}: {len(df):>6} bars  {df.ts.min()} → {df.ts.max()}")
    print(f"\nwrote {OUT}/  — sync down and run the screens in alpha_lab/notebooks/")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
