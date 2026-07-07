"""
One-shot terminal status - prints the multi-arm leaderboard + per-arm breakdown
and exits. Run via a single short SSH command (status.sh), which is reliable
even when the live dashboard's sustained tunnel drops on a lossy route.

    venv/bin/python -m paper_trader.monitor.status
"""

from __future__ import annotations

import datetime as dt

from paper_trader.config import TELEMETRY_PATH
from paper_trader.monitor.metrics import today_ist, realized_for_arms
from paper_trader.telemetry import load_snapshot

_LIVE_STALE_SEC = 15.0


def _m(n) -> str:
    if n is None:
        return "-"
    return ("+" if n >= 0 else "−") + "₹" + format(abs(round(n)), ",")


def main() -> int:
    date = today_ist()
    realized = realized_for_arms(date)

    live_arms, online, age = {}, False, None
    try:
        snap = load_snapshot(TELEMETRY_PATH)
        gen = snap.get("generated_at")
        if gen:
            age = (dt.datetime.now(dt.timezone.utc) - dt.datetime.fromisoformat(gen)).total_seconds()
            online = age < _LIVE_STALE_SEC
        live_arms = snap.get("arms", {})
    except Exception:
        pass

    names = sorted(set(realized) | set(live_arms))
    if not names:
        print(f"No arm data yet for {date}.")
        return 0

    def total(a):
        r = realized.get(a, {}).get("net_pnl", 0) or 0
        u = (live_arms.get(a, {}).get("totals", {}) or {}).get("unrealized_pnl", 0) if online else 0
        return r, u, r + u

    status = "LIVE" if online else "OFFLINE"
    aget = f"{age:.0f}s" if age is not None else "-"
    print(f"\nARGUS PAPER - MULTI-ARM - {date}   [{status}, telemetry {aget}]")
    print("=" * 70)
    print(f"  {'arm':<11}{'realized':>11}{'unreal':>9}{'TOTAL':>11}{'trades':>8}{'WR':>6}")
    for a in sorted(names, key=lambda a: -total(a)[2]):
        r = realized.get(a, {})
        rp, up, tot = total(a)
        n = r.get("n_trades", 0)
        wr = f"{r.get('win_rate', 0) * 100:.0f}%" if n else "-"
        flag = "  HALT" if (live_arms.get(a, {}).get("risk", {}) or {}).get("halted") else ""
        print(f"  {a:<11}{_m(rp):>11}{_m(up):>9}{_m(tot):>11}{n:>8}{wr:>6}{flag}")

    print("\nPER-ARM (realized today)")
    print("-" * 70)
    for a in sorted(names, key=lambda a: -total(a)[2]):
        r = realized.get(a, {})
        if not r.get("n_trades"):
            print(f"  {a}: no trades"); continue
        inst = " | ".join(
            f"{s} {_m(d['net'])}({d['n']},{d['win_rate']*100:.0f}%)"
            for s, d in sorted(r["per_instrument"].items(), key=lambda kv: -kv[1]["net"]))
        exits = " | ".join(
            f"{m} {d['n']}@{_m(d['net'])}({d['win_rate']*100:.0f}%)"
            for m, d in sorted(r["exit_breakdown"].items(), key=lambda kv: -kv[1]["net"]))
        print(f"  {a}: net {_m(r['net_pnl'])} · WR {r['win_rate']*100:.0f}% · payoff {r['payoff']:.2f}")
        print(f"     inst: {inst}")
        print(f"     exit: {exits}")
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
