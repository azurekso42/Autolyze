# -*- coding: utf-8 -*-
"""
Regenerate the Chapter 4 result figures.

Nothing here is typed by hand. Every value is read from the two JSON files
written by the evaluation runs:

  results.json   - full pipeline over both datasets, both engines, plus the
                   five-point engine benchmark
  bigscale.json  - the 3.8 GB / 58.3M-record run through the Spark path

Produces:
  fig_engine.png      Figure 8 - single-machine vs distributed execution time
  fig_importance.png  Figure 6 - feature importance under two problem framings

Run:  python make_figures.py
"""
from __future__ import annotations

import json
import os

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

HERE = os.path.dirname(os.path.abspath(__file__))
results = json.load(open(os.path.join(HERE, "results.json")))
bigscale = json.load(open(os.path.join(HERE, "bigscale.json")))
curve = json.load(open(os.path.join(HERE, "learning_curve.json")))

INK, TEAL, SIENNA, AMBER, GREY = "#15232E", "#2E7D6F", "#C0563F", "#B8862B", "#5F7079"
plt.rcParams.update({
    "font.family": "serif",
    "font.serif": ["Times New Roman", "DejaVu Serif"],
    "font.size": 10,
    "axes.edgecolor": "#B9C4C9",
    "axes.labelcolor": INK,
    "text.color": INK,
    "xtick.color": GREY,
    "ytick.color": GREY,
})

# ── Figure 8: engine comparison ────────────────────────────────────────────
bench = [b for b in results["benchmark"] if "error" not in b.get("pandas", {})]
rows = [b["rows"] for b in bench]
pd_t = [b["pandas"]["total"] for b in bench]
sp_t = [b["spark"]["total"] for b in bench]
ratio = [round(s / p, 1) for s, p in zip(sp_t, pd_t)]


def label_rows(n):
    return f"{n/1e6:.2f} M" if n >= 1e6 else f"{n//1000} K"


fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(9.2, 3.5))

ax1.plot(rows, sp_t, "o-", color=SIENNA, lw=1.8, ms=5, label="Spark + MLlib")
ax1.plot(rows, pd_t, "s-", color=TEAL, lw=1.8, ms=5, label="pandas + scikit-learn")
ax1.set_xscale("log"); ax1.set_yscale("log")
ax1.set_xlabel("Records (log scale)")
ax1.set_ylabel("Elapsed time, seconds (log scale)")
ax1.set_title("(a) Absolute execution time", fontsize=10.5, color=INK, pad=8)
ax1.grid(True, which="major", ls=":", lw=0.6, color="#D8E0E3")
ax1.legend(frameon=False, fontsize=9, loc="upper left")
for sp in ("top", "right"):
    ax1.spines[sp].set_visible(False)

colors = [SIENNA if r >= 30 else AMBER if r >= 15 else TEAL for r in ratio]
bars = ax2.bar(range(len(rows)), ratio, color=colors, width=0.62)
ax2.set_xticks(range(len(rows)))
ax2.set_xticklabels([label_rows(n) for n in rows])
ax2.set_ylabel("Times slower than pandas")
ax2.set_xlabel("Records")
ax2.set_title("(b) Distributed overhead, falling above 25 K records",
              fontsize=10.5, color=INK, pad=8)
ax2.set_ylim(0, max(ratio) * 1.18)
ax2.grid(True, axis="y", ls=":", lw=0.6, color="#D8E0E3")
ax2.set_axisbelow(True)
for b, v in zip(bars, ratio):
    ax2.text(b.get_x() + b.get_width() / 2, v + max(ratio) * 0.03, f"{v}×",
             ha="center", fontsize=9, color=INK)
ax2.axhline(1.0, color=GREY, lw=1, ls="--")
ax2.text(-0.35, max(ratio) * 0.055, "parity", fontsize=8, color=GREY, ha="left")
for sp in ("top", "right"):
    ax2.spines[sp].set_visible(False)

fig.tight_layout()
fig.savefig(os.path.join(HERE, "fig_engine.png"), dpi=300,
            bbox_inches="tight", facecolor="white")
print("wrote fig_engine.png    ", [f"{r}x" for r in ratio])

# ── Figure 6: feature importance under two framings ────────────────────────
PRETTY = {
    "lag_1": "Demand lag,\n1 day", "lag_7": "Demand lag,\n7 days",
    "event_impact": "Event impact", "sell_price": "Sell price",
    "store_id_idx": "Store id",
}

sound_fc = bigscale["large_file"]["forecasting"]           # Spark, 58.3M rows
leaky_fc = results["pandas"]["m5"]["forecasting"]          # sklearn, 58,230 rows


def top5(fc):
    items = list(fc["feature_importance"].items())[:5]
    return ([PRETTY.get(k, k) for k, _ in items], [v for _, v in items])


sound_lbl, sound_val = top5(sound_fc)
leaky_lbl, leaky_val = top5(leaky_fc)

# Importance is reported for Random Forest, the model the framework extracts it
# from, so each panel quotes that model's R-squared rather than the best model's.
sound_r2 = sound_fc["models"]["Random Forest"]["r2"]
leaky_r2 = leaky_fc["models"]["Random Forest"]["r2"]

fig, (axa, axb) = plt.subplots(1, 2, figsize=(9.2, 3.4))

for ax, lbl, val, colour, title in (
    (axa, sound_lbl, sound_val, TEAL,
     f"(a) Sound formulation - target: {sound_fc['target_column']}\n"
     f"Random Forest, R$^2$ = {sound_r2}"),
    (axb, leaky_lbl, leaky_val, SIENNA,
     f"(b) Leaky formulation - target: {leaky_fc['target_column']}\n"
     f"Random Forest, R$^2$ = {leaky_r2}"),
):
    y = range(len(val))
    ax.barh(list(y), val, color=colour, height=0.6)
    ax.set_yticks(list(y)); ax.set_yticklabels(lbl, fontsize=9)
    ax.invert_yaxis(); ax.set_xlim(0, 1.15)
    ax.set_xlabel("Relative importance")
    ax.set_title(title, fontsize=10.5, color=colour, pad=8)
    for i, v in enumerate(val):
        ax.text(v + 0.02, i, f"{v:.3f}", va="center", fontsize=8.5, color=INK)
    ax.grid(True, axis="x", ls=":", lw=0.6, color="#D8E0E3")
    ax.set_axisbelow(True)
    for sp in ("top", "right"):
        ax.spines[sp].set_visible(False)

fig.tight_layout()
fig.savefig(os.path.join(HERE, "fig_importance.png"), dpi=300,
            bbox_inches="tight", facecolor="white")
print("wrote fig_importance.png", f"sound R2={sound_r2}  leaky R2={leaky_r2}")


# ── Figure 13: what the training cap costs ─────────────────────────────────
# One hold-out, drawn once and reused, so the only variable is training size.
runs = [r for r in curve["runs"] if "models" in r]
n_train = [r["train_rows"] for r in runs]
r2 = {name: [r["models"][name]["r2"] for r in runs]
      for name in ("Random Forest", "Gradient Boosting", "Linear Regression")}
secs = [r["t_s"] for r in runs]
cap = next(r for r in runs if abs(r["train_rows"] - 400_000) < 150_000)
cap_r2 = cap["models"]["Random Forest"]["r2"]
best_r2 = max(r2["Random Forest"])

fig, (axa, axb) = plt.subplots(1, 2, figsize=(9.2, 3.5))

for name, colour, mark in (("Random Forest", TEAL, "o"),
                           ("Gradient Boosting", AMBER, "s"),
                           ("Linear Regression", SIENNA, "^")):
    axa.plot(n_train, r2[name], mark + "-", color=colour, lw=1.8, ms=5, label=name)
axa.axvline(cap["train_rows"], color=GREY, ls="--", lw=1)
axa.annotate("500,000-row cap", xy=(cap["train_rows"], max(r2["Random Forest"])),
             xytext=(7, -2), textcoords="offset points", fontsize=8, color=GREY)
axa.set_xscale("log")
axa.set_xlabel("Training rows (log scale)")
axa.set_ylabel("R$^2$ on the fixed hold-out")
axa.set_title(f"(a) Accuracy against training size\n"
              f"one hold-out of {curve['test_rows']:,} later rows, reused",
              fontsize=10.5, color=INK, pad=8)
axa.legend(frameon=False, fontsize=8.5, loc="center right")

axb.plot(n_train, secs, "o-", color=SIENNA, lw=1.8, ms=5)
axb.set_xscale("log"); axb.set_yscale("log")
axb.set_xlabel("Training rows (log scale)")
axb.set_ylabel("Elapsed, seconds (log scale)")
axb.set_title(f"(b) What the accuracy costs\n"
              f"{best_r2 - cap_r2:+.4f} R$^2$ for {secs[-1]/cap['t_s']:.0f}x the time",
              fontsize=10.5, color=INK, pad=8)

for ax in (axa, axb):
    ax.grid(True, ls=":", lw=0.6, color="#D8E0E3")
    ax.set_axisbelow(True)
    for sp in ("top", "right"):
        ax.spines[sp].set_visible(False)

fig.tight_layout()
fig.savefig(os.path.join(HERE, "fig_learning_curve.png"), dpi=300,
            bbox_inches="tight", facecolor="white")
print("wrote fig_learning_curve.png",
      f"cap R2={cap_r2}  best={best_r2}  cost={best_r2 - cap_r2:+.4f}")
