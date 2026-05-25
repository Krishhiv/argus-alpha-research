# Paper Trader — Deploy Guide

One-time setup on the VPS, then `git pull` to update forever.

---

## First-time setup

```bash
ssh lightsail-mumbai
cd /home/ubuntu

# Clone the research repo (paper_trader/ lives inside it)
git clone https://github.com/<your-username>/argus-alpha-research.git paper-trader
cd paper-trader

# Create venv and install paper trader dependencies only
python3 -m venv venv
venv/bin/pip install --upgrade pip
venv/bin/pip install -r paper_trader/requirements.txt

# Symlink .env from the collector (reuses Gmail + Dhan credentials)
ln -s /home/ubuntu/collector-dhan/.env .env
```

---

## Install systemd units

```bash
sudo cp paper_trader/systemd/*.service /etc/systemd/system/
sudo cp paper_trader/systemd/*.timer   /etc/systemd/system/

sudo systemctl daemon-reload

# Enable timers (auto-start/stop + daily report)
sudo systemctl enable --now argus-paper-trader-start.timer
sudo systemctl enable --now argus-paper-trader-stop.timer
sudo systemctl enable --now argus-paper-trader-report.timer
```

---

## Daily workflow — update after pushing changes

```bash
ssh lightsail-mumbai
cd /home/ubuntu/paper-trader
git pull
sudo systemctl restart argus-paper-trader   # if running during session
```

That's it. The timers handle start/stop automatically every trading day.

---

## Useful commands

```bash
# Check if paper trader is running
sudo systemctl status argus-paper-trader

# Watch live logs
sudo journalctl -u argus-paper-trader -f

# Check timer schedule
sudo systemctl list-timers | grep argus-paper

# Manually trigger report (e.g. for testing)
sudo systemctl start argus-paper-trader-report.service

# View today's trades
cat /home/ubuntu/paper-trader/paper_trader/logs/paper_trades.csv

# Running PnL
cat /home/ubuntu/paper-trader/paper_trader/logs/paper_pnl.csv | tail -20
```

---

## Schedule summary

| Timer | UTC | IST | Purpose |
|---|---|---|---|
| argus-paper-trader-start | 03:40 | 09:10 | Start process before open |
| argus-paper-trader-stop | 10:05 | 15:35 | Graceful stop after close |
| argus-paper-trader-report | 10:20 | 15:50 | Email daily report |
