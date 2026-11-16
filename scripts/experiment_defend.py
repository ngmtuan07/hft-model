#!/usr/bin/env python
"""Defensive use of the SPY lead: cancel a stock's market-making quotes when SPY moves.

When SPY moves >= k ticks within window_ms, the market maker pulls its stock quote on the side
that is about to be run over (bid on a SPY drop, ask on a SPY rise) for hold_ms. Decisions happen
at SPY time (signal-only rows, see hftmodel/signals.py), subject to the same feed + order latency
as everything else.

Window 09:35-10:30 (SPY sample coverage). First half: calibration and selection of one shared
(window, k, hold) for all stocks. Second half: out-of-sample report and a latency sweep.

    python scripts/experiment_defend.py --stocks INTC MSFT AAPL
"""
from __future__ import annotations

import argparse
import itertools
import json
import os

import numpy as np

from hftmodel import backtest as bt
from hftmodel import calibration, lobster, metrics, signals, strategy

BLUE, ORANGE, AQUA, GRAY = "#2a78d6", "#eb6834", "#1baf7a", "#52514e"


def run(day, cal, ext, lat_us=50.0, fee=0.0):
    p = strategy.ASParams(gamma=0.02, k=cal["k"], sigma=cal["sigma"], tau=10.0, max_pos=5)
    cfg = bt.BacktestConfig(feed_latency_ns=int(lat_us * 500), order_latency_ns=int(lat_us * 500),
                            sigma0=cal["sigma"], maker_fee=fee)
    return bt.run(day, strategy.as_cst_quote, p.fp(), lut_f=np.full((2, 2), 0.5), cfg=cfg, ext=ext)


def summary(res, day):
    s = metrics.summarize(res, day)
    return {"pnl": s["pnl_liquidated_$"], "fills": s["fills"], "per_share": s["pnl_per_share_$"],
            "mk1s": s["markout_ticks"].get("1s"), "mk0": s["markout_ticks"].get("0s")}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default="data/lobster")
    ap.add_argument("--stocks", nargs="+", default=["INTC", "MSFT", "AAPL"])
    ap.add_argument("--out", default="output")
    a = ap.parse_args()

    spy = lobster.load(*lobster.find_files(a.data, "SPY")[0], max_levels=1)
    t0 = spy.ts[0] + 300_000_000_000
    t1 = spy.ts[-1] - 5_000_000_000
    tm = t0 + (t1 - t0) // 2
    data = {}
    for s in a.stocks:
        d = lobster.load(*lobster.find_files(a.data, s)[0])
        ins, oos = d.slice_time(t0, tm), d.slice_time(tm, t1)
        cal = {"sigma": calibration.estimate_sigma(ins), "k": calibration.estimate_intensity(ins).k}
        data[s] = (cal, ins, oos)
        print(f"{s}: sigma {cal['sigma']:.2f} ticks/sqrt(s), k {cal['k']:.3f}/tick, "
              f"half-spread {strategy.ASParams(gamma=0.02, k=cal['k'], sigma=cal['sigma'], tau=10).half_spread():.2f} ticks")

    # ---- in-sample selection: one shared (window, k, hold) --------------------------------
    grid = list(itertools.product([1, 5, 20], [1, 2, 3], [5, 20, 100]))
    base_ins = {s: run(ins, cal, None).pnl_liquidated for s, (cal, ins, _) in data.items()}
    print(f"\nin-sample baseline: {({s: round(v, 1) for s, v in base_ins.items()})}  total {sum(base_ins.values()):.1f}")
    scores = []
    for W, k, H in grid:
        tt, dd = signals.spy_triggers(spy, W, k)
        pnl = {}
        for s, (cal, ins, _) in data.items():
            m, ext = signals.with_signal(ins, tt, dd, H)
            pnl[s] = run(m, cal, ext).pnl_liquidated
        scores.append((sum(pnl.values()), (W, k, H), pnl))
    scores.sort(key=lambda x: -x[0])
    for tot, cfg, pnl in scores[:5]:
        print(f"  window {cfg[0]:>2} ms, k {cfg[1]}, hold {cfg[2]:>3} ms: total {tot:8.1f}  "
              f"{({s: round(v, 1) for s, v in pnl.items()})}")
    W, k, H = scores[0][1]
    print(f"selected: SPY move >= {k} ticks within {W} ms -> pull the threatened quote for {H} ms")

    # ---- out of sample ------------------------------------------------------------------------
    tt, dd = signals.spy_triggers(spy, W, k)
    report = {"selected": {"window_ms": W, "k": k, "hold_ms": H}, "in_sample_top": scores[:5], "oos": {}}
    lats = [1, 10, 50, 100, 500, 1000, 5000]
    print("\nOUT OF SAMPLE (second half):")
    for s, (cal, _, oos) in data.items():
        m, ext = signals.with_signal(oos, tt, dd, H)
        rb, rd = run(oos, cal, None), run(m, cal, ext)
        sb, sd = summary(rb, oos), summary(rd, m)
        sweep = {}
        for L in lats:
            b = run(oos, cal, None, L).pnl_liquidated
            dfd = run(m, cal, ext, L).pnl_liquidated
            sweep[L] = {"baseline": round(b, 2), "defend": round(dfd, 2)}
        report["oos"][s] = {"baseline": sb, "defend": sd, "latency_sweep": sweep,
                            "pct_time_signal_active": round(100 * float(np.mean(ext != 0)), 2)}
        print(f"  {s:5s} baseline: pnl {sb['pnl']:8.2f} fills {sb['fills']:4d} mk1s {sb['mk1s']:+.3f}  |  "
              f"defend: pnl {sd['pnl']:8.2f} fills {sd['fills']:4d} mk1s {sd['mk1s']:+.3f}")
        print("        latency sweep (round trip us -> baseline / defend $): " +
              "  ".join(f"{L}: {v['baseline']:.0f}/{v['defend']:.0f}" for L, v in sweep.items()))

    os.makedirs(a.out, exist_ok=True)
    with open(os.path.join(a.out, "experiment_defend.json"), "w") as f:
        json.dump(report, f, indent=1, default=str)
    plot(report, lats, a.out)


def plot(report, lats, out):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    plt.rcParams.update({"axes.spines.top": False, "axes.spines.right": False, "axes.grid": True,
                         "grid.color": "#e6e5e0", "grid.linewidth": 0.6, "axes.edgecolor": "#b9b8b2",
                         "axes.labelcolor": GRAY, "xtick.color": GRAY, "ytick.color": GRAY,
                         "figure.facecolor": "#fcfcfb", "axes.facecolor": "#fcfcfb", "font.size": 9})
    fig, ax = plt.subplots(figsize=(7, 4))
    for c, (s, r) in zip([BLUE, ORANGE, AQUA], report["oos"].items()):
        sw = r["latency_sweep"]
        gain = [sw[L]["defend"] - sw[L]["baseline"] for L in lats]
        ax.semilogx(lats, gain, "-o", color=c, ms=4, lw=1.5, label=s)
    ax.axhline(0, color=GRAY, lw=0.8)
    ax.set_xlabel("round-trip latency (µs)")
    ax.set_ylabel("PnL gain from SPY-defence ($, out of sample)")
    ax.set_title("Value of cancelling on SPY moves, by latency", loc="left")
    ax.legend(frameon=False)
    fig.tight_layout()
    fig.savefig(os.path.join(out, "defend_latency.png"), dpi=130)
    print("plot ->", os.path.join(out, "defend_latency.png"))


if __name__ == "__main__":
    main()
