"""
Daily paper trading report — emails a summary to krishhiv@gmail.com.

Triggered by systemd timer at 15:50 IST (10:20 UTC) Mon–Fri.
Reads today's trades from paper_trader/logs/paper_trades.csv,
computes key metrics, and sends a plain-text email with a PnL summary.

Gmail credentials read from .env (REPORT_EMAIL_FROM, GMAIL_APP_PASSWORD).
Recipient is hardcoded — not read from .env.
"""

from __future__ import annotations

import csv
import os
import smtplib
import sys
from datetime import date, datetime, timezone, timedelta
from email.message import EmailMessage
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parents[1] / ".env")
except ModuleNotFoundError:
    pass

from paper_trader.config import REPORT_EMAIL_TO, TRADES_LOG

IST = timezone(timedelta(hours=5, minutes=30))


def _today_ist() -> str:
    return datetime.now(IST).strftime("%Y-%m-%d")


def _load_today_trades(trading_date: str) -> list[dict]:
    path = Path(TRADES_LOG)
    if not path.exists():
        return []
    with open(path) as f:
        return [r for r in csv.DictReader(f) if r.get("date") == trading_date]


def _compute_metrics(trades: list[dict]) -> dict:
    if not trades:
        return {}
    net_pnls   = [float(t["net_pnl"]) for t in trades]
    gross_pnls = [float(t["gross_pnl"]) for t in trades]
    winners    = [p for p in net_pnls if p > 0]
    losers     = [p for p in net_pnls if p <= 0]
    exit_methods = {}
    for t in trades:
        em = t.get("exit_method", "?")
        exit_methods[em] = exit_methods.get(em, 0) + 1

    return {
        "n_trades":        len(trades),
        "net_pnl":         round(sum(net_pnls), 2),
        "gross_pnl":       round(sum(gross_pnls), 2),
        "total_fees":      round(sum(gross_pnls) - sum(net_pnls), 2),
        "win_rate":        round(len(winners) / len(trades), 3),
        "avg_net":         round(sum(net_pnls) / len(trades), 2),
        "avg_winner":      round(sum(winners) / len(winners), 2) if winners else 0,
        "avg_loser":       round(sum(losers) / len(losers), 2) if losers else 0,
        "exit_methods":    exit_methods,
        "best_trade":      round(max(net_pnls), 2),
        "worst_trade":     round(min(net_pnls), 2),
    }


def _per_instrument(trades: list[dict]) -> dict[str, dict]:
    by_inst: dict[str, list] = {}
    for t in trades:
        by_inst.setdefault(t["underlying"], []).append(t)
    return {inst: _compute_metrics(ts) for inst, ts in by_inst.items()}


def _build_body(trading_date: str, m: dict, by_inst: dict[str, dict],
                n_posts: int | None = None) -> str:
    sign = "+" if m.get("net_pnl", 0) >= 0 else ""
    lines = [
        f"Argus Paper Trading — {trading_date}",
        "=" * 45,
        "",
        f"  Net PnL      ₹{sign}{m['net_pnl']:,.0f}",
        f"  Gross PnL    ₹{sign}{m['gross_pnl']:,.0f}",
        f"  Total fees   ₹{m['total_fees']:,.0f}",
        f"  Trades       {m['n_trades']}",
        f"  Win rate     {m['win_rate']:.1%}",
        f"  Avg net/trade ₹{m['avg_net']:+.0f}",
        f"  Best trade   ₹{m['best_trade']:+.0f}",
        f"  Worst trade  ₹{m['worst_trade']:+.0f}",
        "",
        "Exit breakdown:",
    ]
    for method, count in m.get("exit_methods", {}).items():
        lines.append(f"  {method:<20} {count}")

    lines += ["", "Per instrument:", "-" * 35]
    for inst, im in by_inst.items():
        if not im:
            continue
        sign_i = "+" if im["net_pnl"] >= 0 else ""
        lines.append(
            f"  {inst:<12} ₹{sign_i}{im['net_pnl']:>8,.0f}  "
            f"{im['n_trades']} trades  WR {im['win_rate']:.0%}"
        )

    lines += [
        "",
        "─" * 45,
        "Backtest baseline (OOS test): net ₹+1.05L / 5 days",
        "─" * 45,
    ]
    return "\n".join(lines)


def _send(subject: str, body: str) -> None:
    from_addr    = os.environ["REPORT_EMAIL_FROM"]
    app_password = os.environ["GMAIL_APP_PASSWORD"]

    msg = EmailMessage()
    msg["From"]    = from_addr
    msg["To"]      = REPORT_EMAIL_TO
    msg["Subject"] = subject
    msg.set_content(body)

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
        smtp.login(from_addr, app_password)
        smtp.send_message(msg)
    print(f"Report sent to {REPORT_EMAIL_TO} — {subject!r}")


def main() -> int:
    trading_date = _today_ist()
    trades = _load_today_trades(trading_date)

    if not trades:
        print(f"No paper trades found for {trading_date} — skipping report.")
        return 0

    m       = _compute_metrics(trades)
    by_inst = _per_instrument(trades)
    body    = _build_body(trading_date, m, by_inst)

    sign    = "+" if m["net_pnl"] >= 0 else ""
    subject = (
        f"Argus Paper — {trading_date} — "
        f"₹{sign}{m['net_pnl']:,.0f} net | "
        f"{m['n_trades']} trades | "
        f"WR {m['win_rate']:.0%}"
    )

    _send(subject, body)
    return 0


if __name__ == "__main__":
    sys.exit(main())
