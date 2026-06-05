"""
Daily paper-trading report — emails a multi-arm comparison to krishhiv@gmail.com.

Triggered by systemd timer at 15:50 IST (10:20 UTC) Mon–Fri. Reads each arm's
trade log under paper_trader/logs/arms/<arm>/paper_trades.csv, computes today's
stats plus the running cumulative, and emails a ranked comparison so we can see
which version wins and why (per-instrument + exit breakdown).

Gmail credentials read from .env (REPORT_EMAIL_FROM, GMAIL_APP_PASSWORD).
Recipient is hardcoded — never read from .env.
"""

from __future__ import annotations

import os
import smtplib
import sys
from email.message import EmailMessage
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parents[1] / ".env")
except ModuleNotFoundError:
    pass

from paper_trader.config import REPORT_EMAIL_TO
from paper_trader.monitor.metrics import (
    today_ist, discover_arms, realized_for_arms, cumulative_for_arm,
)


def _m(n: float) -> str:
    return ("+" if n >= 0 else "−") + "₹" + format(abs(round(n)), ",")


def _build_body(date: str, realized: dict[str, dict], cum: dict[str, dict]) -> str:
    # Rank arms by today's net P&L (descending).
    order = sorted(realized.keys(), key=lambda a: realized[a]["net_pnl"], reverse=True)

    lines = [f"Argus Paper — Multi-Arm — {date}", "=" * 60, "", "LEADERBOARD (today)", "-" * 60,
             f"  {'arm':<11}{'today':>11}{'trades':>8}{'WR':>6}{'cumulative':>14}"]
    for a in order:
        r, c = realized[a], cum.get(a, {})
        wr = f"{r['win_rate']*100:.0f}%" if r["n_trades"] else "—"
        cumtxt = f"{_m(c.get('total_net', 0))} ({c.get('n_days', 0)}d)"
        lines.append(f"  {a:<11}{_m(r['net_pnl']):>11}{r['n_trades']:>8}{wr:>6}{cumtxt:>14}")

    lines += ["", "PER-ARM DETAIL", "=" * 60]
    for a in order:
        r = realized[a]
        lines += ["", f"── {a} ──"]
        if r["n_trades"] == 0:
            lines.append("  no trades today")
            continue
        lines.append(f"  net {_m(r['net_pnl'])} · {r['n_trades']} trades · "
                     f"WR {r['win_rate']*100:.0f}% · payoff {r['payoff']:.2f} · "
                     f"W {_m(r['avg_win'])}/L {_m(r['avg_loss'])}")
        inst = " | ".join(
            f"{s} {_m(d['net'])} ({d['n']},{d['win_rate']*100:.0f}%)"
            for s, d in sorted(r["per_instrument"].items(), key=lambda kv: -kv[1]["net"])
        )
        lines.append(f"  instruments: {inst}")
        exits = " | ".join(
            f"{m} {d['n']}@{_m(d['net'])} ({d['win_rate']*100:.0f}%)"
            for m, d in sorted(r["exit_breakdown"].items(), key=lambda kv: -kv[1]["net"])
        )
        lines.append(f"  exits: {exits}")

    lines += ["", "─" * 60,
              "Arms run in parallel on the same live feed (risk-free paper).",
              "Decide on the 10-day cumulative + consistency, not one day.", "─" * 60]
    return "\n".join(lines)


def _send(subject: str, body: str) -> None:
    from_addr    = os.environ["REPORT_EMAIL_FROM"]
    app_password = os.environ["GMAIL_APP_PASSWORD"]
    msg = EmailMessage()
    msg["From"]    = from_addr
    msg["To"]      = REPORT_EMAIL_TO        # hardcoded recipient — never from env
    msg["Subject"] = subject
    msg.set_content(body)
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
        smtp.login(from_addr, app_password)
        smtp.send_message(msg)
    print(f"Report sent to {REPORT_EMAIL_TO} — {subject!r}")


def main() -> int:
    date = today_ist()
    arms = discover_arms()
    if not arms:
        print(f"No arms found for {date} — skipping report.")
        return 0

    realized = realized_for_arms(date)
    cum      = {a: cumulative_for_arm(a) for a in arms}

    if all(r["n_trades"] == 0 for r in realized.values()):
        print(f"No paper trades for {date} across any arm — skipping report.")
        return 0

    body = _build_body(date, realized, cum)

    # Subject: best arm today + the control baseline.
    best = max(realized, key=lambda a: realized[a]["net_pnl"])
    ctrl = realized.get("control", realized[best])
    subject = (f"Argus Paper — {date} — best {best} {_m(realized[best]['net_pnl'])} "
               f"| control {_m(ctrl['net_pnl'])}")
    _send(subject, body)
    return 0


if __name__ == "__main__":
    sys.exit(main())
