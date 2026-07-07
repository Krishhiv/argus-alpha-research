"""Generate the vector figures for the research report (elegant, print-ready PDFs)."""
from pathlib import Path
import matplotlib as mpl
import matplotlib.pyplot as plt

FIG = Path(__file__).parent / "figures"; FIG.mkdir(exist_ok=True)

# dataviz palette: diverging blue<->red for the gain/loss polarity (CVD-safe),
# every value direct-labelled as secondary encoding. Light print surface.
BLUE, RED, NET = "#2a78d6", "#d03b3b", "#3a4a5a"
INK, SEC, MUTE, GRID, BASE, SURF = "#0b0b0b", "#52514e", "#898781", "#e1e0d9", "#c3c2b7", "#fcfcfb"

mpl.rcParams.update({
    "figure.dpi": 150, "savefig.dpi": 150, "savefig.bbox": "tight",
    "font.family": "serif", "font.serif": ["DejaVu Serif"], "mathtext.fontset": "dejavuserif",
    "font.size": 10, "text.color": INK,
    "axes.edgecolor": BASE, "axes.linewidth": 0.8, "axes.labelcolor": SEC,
    "axes.facecolor": SURF, "figure.facecolor": SURF, "savefig.facecolor": SURF,
    "xtick.color": MUTE, "ytick.color": MUTE, "xtick.labelcolor": SEC, "ytick.labelcolor": SEC,
    "axes.grid": True, "axes.axisbelow": True, "grid.color": GRID, "grid.linewidth": 0.6,
    "axes.spines.top": False, "axes.spines.right": False,
})

def _clean(ax):
    ax.grid(axis="x", visible=False)
    ax.tick_params(length=0)

# ── Fig 1: decomposition waterfall ────────────────────────────────────────────
def fig_waterfall():
    sc, dirn, net = 12.52, -10.14, 2.38   # Rs lakh
    fig, ax = plt.subplots(figsize=(6.4, 3.7))
    ax.bar(0, sc, width=0.62, color=BLUE)
    ax.bar(1, -dirn, bottom=net, width=0.62, color=RED)      # floats 2.38 -> 12.52
    ax.bar(2, net, width=0.62, color=NET)
    ax.plot([0.31, 0.69], [sc, sc], color=MUTE, lw=0.8, ls=(0, (4, 3)))
    ax.plot([1.31, 1.69], [net, net], color=MUTE, lw=0.8, ls=(0, (4, 3)))
    ax.axhline(0, color=BASE, lw=1.0)
    for x, v, lab, c in [(0, sc, "+12.52", "white"), (2, net, "+2.38", "white")]:
        ax.text(x, v/2, lab, ha="center", va="center", color=c, fontsize=11, fontweight="bold")
    ax.text(1, net + (-dirn)/2, "−" + "10.14", ha="center", va="center",
            color="white", fontsize=11, fontweight="bold")
    ax.set_xticks([0, 1, 2])
    ax.set_xticklabels(["Assumed\nspread capture", "Directional edge\n(marked to mid)",
                        "Reported net\n(simulator)"], color=SEC)
    ax.set_ylabel("P&L  (Rs lakh)")
    ax.set_ylim(-1, 14)
    _clean(ax)
    fig.savefig(FIG / "decomposition.pdf"); plt.close(fig)

# ── Fig 2: entry markout curve ────────────────────────────────────────────────
def fig_markout():
    h = [0, 1, 5, 30, 60]; y = [197.1, 164.4, 172.3, 171.0, 175.7]
    fig, ax = plt.subplots(figsize=(6.4, 3.4))
    ax.axhline(0, color=BASE, lw=1.0)
    ax.plot(range(len(h)), y, color=BLUE, lw=2.0, marker="o", ms=7,
            mfc=BLUE, mec="white", mew=1.0, zorder=3)
    ax.set_ylim(0, 230)
    ax.set_xticks(range(len(h))); ax.set_xticklabels([f"{t}s" for t in h], color=SEC)
    ax.set_xlabel("Horizon after fill"); ax.set_ylabel("Signed markout  (Rs / trade)")
    ax.annotate("Rs 197 captured at the fill", (0, 197.1), textcoords="offset points",
                xytext=(10, 6), color=SEC, fontsize=9)
    ax.annotate("Rs 176 at 60 s (barely decays)", (4, 175.7), textcoords="offset points",
                xytext=(-6, 10), ha="right", color=SEC, fontsize=9)
    _clean(ax)
    fig.savefig(FIG / "markout.pdf"); plt.close(fig)

# ── Fig 3: per-instrument sim vs mid ──────────────────────────────────────────
def fig_instruments():
    names = ["AXISBANK", "ICICIBANK", "BHARTIARTL", "SBIN", "RELIANCE", "HDFCBANK"]
    sim = [1.32, 0.18, 0.29, 0.48, 0.11, -0.01]
    mid = [-3.24, -2.50, -1.74, -1.21, -1.32, -0.13]
    import numpy as np
    yp = np.arange(len(names)); hh = 0.38
    fig, ax = plt.subplots(figsize=(6.4, 3.9))
    ax.axvline(0, color=BASE, lw=1.0)
    ax.barh(yp + hh/2, sim, height=hh, color=BLUE, label="Simulated (touch-fill)")
    ax.barh(yp - hh/2, mid, height=hh, color=RED, label="Marked to mid")
    for y_, v in zip(yp + hh/2, sim):
        ax.text(v + (0.05 if v >= 0 else -0.05), y_, f"{v:+.2f}", va="center",
                ha="left" if v >= 0 else "right", color=SEC, fontsize=8)
    for y_, v in zip(yp - hh/2, mid):
        ax.text(v - 0.05, y_, f"{v:+.2f}", va="center", ha="right", color=SEC, fontsize=8)
    ax.set_yticks(yp); ax.set_yticklabels(names, color=SEC)
    ax.set_xlabel("P&L  (Rs lakh)"); ax.set_xlim(-3.9, 1.9)
    ax.grid(axis="y", visible=False); ax.grid(axis="x", visible=True); ax.tick_params(length=0)
    ax.legend(loc="lower right", frameon=False, fontsize=9, labelcolor=SEC)
    fig.savefig(FIG / "instruments.pdf"); plt.close(fig)

# ── Fig 4: break-even spread retention ────────────────────────────────────────
def fig_breakeven():
    import numpy as np
    mid, sc = -10.14, 12.52; fstar = -mid / sc
    f = np.linspace(0, 1, 200); y = mid + f * sc
    fig, ax = plt.subplots(figsize=(6.4, 3.5))
    ax.axvspan(0, fstar*100, color=RED, alpha=0.06)
    ax.axvspan(fstar*100, 100, color=BLUE, alpha=0.07)
    ax.axhline(0, color=BASE, lw=1.0)
    ax.plot(f*100, y, color=BLUE, lw=2.2, zorder=3)
    ax.axvline(fstar*100, color=NET, lw=1.2, ls=(0, (4, 3)))
    ax.scatter([0, 100], [mid, mid+sc], s=42, color=BLUE, ec="white", zorder=4)
    ax.annotate(f"break-even: {fstar*100:.0f}% retention", (fstar*100, 0),
                textcoords="offset points", xytext=(8, 34), color=NET, fontsize=9.5)
    ax.text(2, mid+0.5, "−10.14 L\n(0% retained)", color=SEC, fontsize=8.5, va="bottom")
    ax.text(98, mid+sc-0.3, "+2.38 L\n(100% retained)", color=SEC, fontsize=8.5,
            ha="right", va="top")
    ax.set_xlabel("Share of theoretical spread retained  (%)")
    ax.set_ylabel("Realistic net P&L  (Rs lakh)")
    ax.set_xlim(0, 100)
    _clean(ax)
    fig.savefig(FIG / "breakeven.pdf"); plt.close(fig)

for f in (fig_waterfall, fig_markout, fig_instruments, fig_breakeven):
    f()
print("wrote:", *(p.name for p in sorted(FIG.glob("*.pdf"))))
