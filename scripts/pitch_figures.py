#!/usr/bin/env python
"""Pitch figures: how a high-frequency market maker makes (or loses) money.

    python scripts/pitch_figures.py            # needs INTC, MSFT, AAPL (+ SPY for figure 4)

fig1_adverse_selection.png  the market maker's problem: you earn the spread, then the price moves against you
fig2_queue_priority.png     the edge: being first in the queue (same prices, different queue position)
fig3_speed.png              why speed matters: queue position and PnL vs latency
fig4_signals.png            signals that defend: order-flow runs and SPY leading its members
"""
from __future__ import annotations

import argparse
import json
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(__file__))
import edges as E  # noqa: E402

from hftmodel import backtest as bt  # noqa: E402
from hftmodel import calibration, lobster, metrics, strategy  # noqa: E402

BLUE, ORANGE, AQUA, YELLOW, GRAY, INK = "#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#52514e", "#0b0b0b"
REBATE = -0.002   # assumed maker rebate $/share (2012 NASDAQ order of magnitude; tier-dependent)


def style():
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    plt.rcParams.update({"axes.spines.top": False, "axes.spines.right": False, "axes.grid": True,
                         "grid.color": "#e6e5e0", "grid.linewidth": 0.6, "axes.edgecolor": "#b9b8b2",
                         "axes.labelcolor": GRAY, "xtick.color": GRAY, "ytick.color": GRAY, "text.color": INK,
                         "figure.facecolor": "#fcfcfb", "axes.facecolor": "#fcfcfb", "font.size": 10,
                         "axes.titlesize": 11, "legend.frameon": False})
    return plt


def setup(data_dir, T):
    day = lobster.load(*lobster.find_files(data_dir, T)[0])
    wc, _, wt = E.windows(day)
    c = day.slice_time(*wc)
    cal = {"sigma": calibration.estimate_sigma(c), "k": calibration.estimate_intensity(c).k}
    return day, cal, day.slice_time(*wt)


def run(test, cal, kernel, join=True, lat_us=100.0, fee=0.0):
    p = strategy.ASParams(gamma=0.02, k=cal["k"], sigma=cal["sigma"], tau=10.0, max_pos=5)
    cfg = bt.BacktestConfig(join_queue=join, maker_fee=fee, sigma0=cal["sigma"],
                            feed_latency_ns=int(lat_us * 500), order_latency_ns=int(lat_us * 500))
    return bt.run(test, kernel, p.fp(), lut_f=np.full((2, 2), 0.5), cfg=cfg)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default="data/lobster")
    ap.add_argument("--out", default="output/pitch")
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)
    plt = style()
    stocks = {T: setup(a.data, T) for T in ("INTC", "MSFT")}
    variants = [("Avellaneda–Stoikov,\njoins queues", strategy.as_cst_quote, True),
                ("Penny the spread,\njoins queues", strategy.penny_quote, True),
                ("Penny the spread,\nfirst in queue only", strategy.penny_quote, False)]
    summary = {}

    # ---- fig 1 + 2 -------------------------------------------------------------------------
    hz = [0.001, 0.01, 0.1, 1, 10, 60]
    fig1, ax1 = plt.subplots(1, 2, figsize=(11, 4), sharey=True)
    fig2, ax2 = plt.subplots(1, 2, figsize=(11, 4.2), sharey=True)
    for col, (T, (day, cal, test)) in enumerate(stocks.items()):
        pnl_g, pnl_r = [], []
        for (name, kern, join), c in zip(variants, [GRAY, ORANGE, BLUE]):
            r = run(test, cal, kern, join)
            rr = run(test, cal, kern, join, fee=REBATE)
            mk = metrics.markouts(r, test, hz)
            ys = [mk["0s"]] + [mk[f"{h:g}s"] for h in hz]
            xs = [0.0005] + hz
            label = name.replace("\n", " ")
            if kern is strategy.as_cst_quote or not join:
                ax1[col].semilogx(xs, ys, "-o", color=c, ms=4, lw=2, label=label)
            pnl_g.append(r.pnl_liquidated)
            pnl_r.append(rr.pnl_liquidated)
            summary[f"{T} | {label}"] = {"pnl_gross": round(r.pnl_liquidated, 1), "pnl_with_rebate": round(rr.pnl_liquidated, 1),
                                         "fills": int(len(r.fills["t"])), "markout_ticks": {k: round(v, 3) for k, v in mk.items()}}
        ax1[col].axhline(0, color=GRAY, lw=0.8)
        ax1[col].set_title(f"{T}: profit per fill vs time after the fill", loc="left")
        ax1[col].set_xlabel("seconds after our order was filled (fill itself plotted at 0.0005)")
        x = np.arange(len(variants))
        ax2[col].bar(x - 0.2, pnl_g, 0.38, color=GRAY, label="no rebate")
        ax2[col].bar(x + 0.2, pnl_r, 0.38, color=BLUE, label=f"with ${-REBATE:.3f}/share maker rebate (assumed)")
        for xi, v in zip(x - 0.2, pnl_g):
            ax2[col].text(xi, v, f"{v:,.0f}", ha="center", va="top" if v < 0 else "bottom", fontsize=8, color=INK)
        for xi, v in zip(x + 0.2, pnl_r):
            ax2[col].text(xi, v, f"{v:,.0f}", ha="center", va="top" if v < 0 else "bottom", fontsize=8, color=INK)
        ax2[col].axhline(0, color=GRAY, lw=0.8)
        ax2[col].set_xticks(x, [v[0] for v in variants], fontsize=8)
        ax2[col].set_title(f"{T}: PnL, 13:15–15:55 (out of sample)", loc="left")
    ax1[0].set_ylabel("ticks earned per share (vs mid)")
    ax1[0].legend(loc="lower left")
    fig1.suptitle("You earn half the spread at the fill — then the price moves against you", x=0.01, ha="left")
    fig1.tight_layout()
    fig1.savefig(os.path.join(a.out, "fig1_adverse_selection.png"), dpi=150)
    ax2[0].set_ylabel("PnL ($), 100-share quotes")
    ax2[0].legend(loc="lower left", fontsize=8)
    fig2.suptitle("Same prices, different place in line: being first is the edge", x=0.01, ha="left")
    fig2.tight_layout()
    fig2.savefig(os.path.join(a.out, "fig2_queue_priority.png"), dpi=150)

    # ---- fig 3: speed ------------------------------------------------------------------------
    lats = [1, 10, 100, 500, 1000, 5000, 20000]
    fig3, ax3 = plt.subplots(1, 2, figsize=(11, 4))
    for (T, (day, cal, test)), c in zip(stocks.items(), [BLUE, ORANGE]):
        first, pnl = [], []
        for L in lats:
            r = run(test, cal, strategy.penny_quote, join=False, lat_us=L, fee=REBATE)
            first.append(100 * float(np.mean(r.fills["ahead0"] == 0)) if len(r.fills["t"]) else np.nan)
            pnl.append(r.pnl_liquidated)
        summary[f"{T} | latency sweep (first-in-queue, with rebate)"] = {
            str(L): {"pct_first": round(f, 1), "pnl": round(p, 1)} for L, f, p in zip(lats, first, pnl)}
        ax3[0].semilogx(lats, first, "-o", color=c, ms=4, lw=2, label=T)
        ax3[1].semilogx(lats, pnl, "-o", color=c, ms=4, lw=2, label=T)
    ax3[0].set_ylabel("% of our fills where we were first in line")
    ax3[0].set_title("Slower → someone else gets there first", loc="left")
    ax3[1].set_ylabel(f"PnL ($, with assumed rebate)")
    ax3[1].axhline(0, color=GRAY, lw=0.8)
    ax3[1].set_title("…and the edge is gone", loc="left")
    for ax in ax3:
        ax.set_xlabel("round-trip latency (µs)")
        ax.axvspan(0.5, 10, color="#cde2fb", alpha=0.5, lw=0)
        ax.legend()
    ax3[0].text(1.2, 25, "FPGA\nterritory", color=BLUE, fontsize=9)
    fig3.suptitle("Why HFT firms buy speed: queue position is decided in microseconds", x=0.01, ha="left")
    fig3.tight_layout()
    fig3.savefig(os.path.join(a.out, "fig3_speed.png"), dpi=150)

    # ---- fig 4: signals ------------------------------------------------------------------------
    fig4, ax4 = plt.subplots(1, 2, figsize=(11, 4))
    lags = [1, 2, 3, 5, 10, 20, 50, 100]
    for T, c in zip(("INTC", "MSFT", "AAPL"), [BLUE, ORANGE, AQUA]):
        day = stocks[T][0] if T in stocks else lobster.load(*lobster.find_files(a.data, T)[0])
        ac = E.sign_autocorr(day, lags)
        ax4[0].semilogx(lags, [ac[L] for L in lags], "-o", color=c, ms=4, lw=2, label=T)
        summary[f"{T} | market-order sign autocorrelation"] = {str(L): round(v, 3) for L, v in ac.items()}
    ax4[0].axhline(0, color=GRAY, lw=0.8)
    ax4[0].set_xlabel("lag (number of market orders)")
    ax4[0].set_ylabel("correlation of buy/sell signs")
    ax4[0].set_title("Order flow comes in runs (big orders are sliced)", loc="left")
    ax4[0].legend()
    ll = "output/leadlag_aapl/leadlag.json" if os.path.exists("output/leadlag_aapl/leadlag.json") else "output/leadlag.json"
    if os.path.exists(ll):
        rep = json.load(open(ll))["stocks"]
        for T, c in zip(("INTC", "MSFT", "AAPL"), [BLUE, ORANGE, AQUA]):
            if T not in rep:
                continue
            r = rep[T]["spy_to_stock_bps"]
            hs = [float(h) for h in r if float(h) > 0]
            ax4[1].semilogx(hs, [r[k] for k in r if float(k) > 0], "-o", color=c, ms=4, lw=2, label=f"{T} after a SPY move")
        ax4[1].set_xlabel("ms after SPY's mid price moves")
        ax4[1].set_ylabel("stock's move in the same direction (bps)")
        ax4[1].set_title("SPY moves first; its members follow within milliseconds", loc="left")
        ax4[1].legend()
    fig4.suptitle("Signals that tell a market maker when to step back", x=0.01, ha="left")
    fig4.tight_layout()
    fig4.savefig(os.path.join(a.out, "fig4_signals.png"), dpi=150)

    with open(os.path.join(a.out, "pitch_numbers.json"), "w") as f:
        json.dump(summary, f, indent=1)
    print(json.dumps(summary, indent=1))
    print("figures ->", a.out)


if __name__ == "__main__":
    main()
