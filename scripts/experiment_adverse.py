#!/usr/bin/env python
"""Adverse-selection experiment: imbalance gate, empirical P_up table, queue-priority retention.

Per ticker the day is split into three windows (no look-ahead):
    calibration  09:35-10:35  sigma, k, CST rates, empirical P_up table
    validation   10:35-13:15  parameter grid search (one config chosen jointly for all tickers)
    test         13:15-15:55  reported, untouched by the search

    python scripts/experiment_adverse.py --data data/lobster --tickers INTC MSFT
"""
from __future__ import annotations

import argparse
import itertools
import json
import os

import numpy as np

from hftmodel import backtest as bt
from hftmodel import calibration, cst, lobster, metrics, strategy

H = 3_600_000_000_000
M = 60_000_000_000


def windows(day):
    t0 = day.ts[0] + 5 * M
    return (t0, t0 + 60 * M), (t0 + 60 * M, t0 + 220 * M), (t0 + 220 * M, day.ts[-1] - 5 * M)


def block_stats(res, block_s=600):
    eq = metrics.equity_curve(res)
    eq = eq[~np.isnan(eq)]
    step = int(block_s * 1e9 // (res.records["t"][1] - res.records["t"][0])) if len(res.records["t"]) > 1 else 1
    d = np.diff(eq[::step])
    t = float(d.mean() / d.std() * np.sqrt(len(d))) if len(d) > 2 and d.std() > 0 else float("nan")
    return {"blocks": int(len(d)), "pnl_per_10min": round(float(d.mean()), 2), "t_stat": round(t, 2)}


def run_one(day, cal, table, cfgv, fee=0.0):
    p = strategy.ASParams(gamma=cfgv["gamma"], k=cal["k"], sigma=cal["sigma"], tau=cfgv["tau"],
                          alpha=cfgv["alpha"], unit=cal["unit"], max_pos=5, gate=cfgv["gate"])
    cfg = bt.BacktestConfig(keep_behind_ticks=cfgv["keep"], maker_fee=fee, sigma0=cal["sigma"])
    return bt.run(day, strategy.as_cst_quote, p.fp(), lut_f=cal[table], cfg=cfg), p


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default="data/lobster")
    ap.add_argument("--tickers", nargs="+", default=["INTC", "MSFT"])
    ap.add_argument("--rebate", type=float, default=0.002, help="maker rebate $/share for the sensitivity run")
    ap.add_argument("--out", default="output")
    a = ap.parse_args()

    data = {}
    for T in a.tickers:
        day = lobster.load(*lobster.find_files(a.data, T)[0])
        wc, wv, wt = windows(day)
        c = day.slice_time(*wc)
        cp = calibration.estimate_cst(c)
        p_cst = cst.prob_up_table(cp.lam[0], cp.mu, cp.theta[0], 31)
        cal = {"sigma": calibration.estimate_sigma(c), "k": calibration.estimate_intensity(c).k, "unit": cp.S_l,
               "cst": p_cst, "emp": cst.blended_up_table(c, p_cst, cp.S_l)}
        data[T] = (cal, day.slice_time(*wv), day.slice_time(*wt))
        print(f"{T}: sigma {cal['sigma']:.3f} k {cal['k']:.2f} unit {cal['unit']:.0f}")

    grid = [dict(gamma=0.02, tau=10.0, table=tb, alpha=al, gate=g, keep=kb)
            for tb, al, g, kb in itertools.product(["cst", "emp"], [0.0, 0.5], [1.0, 0.65, 0.6, 0.55, 0.5], [0, 1, 2])]
    rows = []
    for cfgv in grid:
        pnl = {}
        for T, (cal, val, _) in data.items():
            res, _ = run_one(val, cal, cfgv["table"], cfgv)
            pnl[T] = res.pnl_liquidated
        rows.append((sum(pnl.values()), cfgv, pnl))
    rows.sort(key=lambda r: -r[0])
    print("\nvalidation top 8 (sum over tickers):")
    for tot, cfgv, pnl in rows[:8]:
        print(f"  {tot:8.2f}  {cfgv}  {({k: round(v, 1) for k, v in pnl.items()})}")
    best = rows[0][1]

    named = {
        "A-S baseline": dict(gamma=0.02, tau=10.0, table="cst", alpha=0.0, gate=1.0, keep=0),
        "A-S + CST skew (previous)": dict(gamma=0.02, tau=10.0, table="cst", alpha=0.5, gate=1.0, keep=0),
        "selected on validation": best,
    }
    report = {"selected": best, "validation_top": [(round(t, 2), c, p) for t, c, p in rows[:8]], "test": {}}
    print("\nTEST window (out of sample):")
    for T, (cal, _, test) in data.items():
        for name, cfgv in named.items():
            for fee in (0.0, -a.rebate):
                res, p = run_one(test, cal, cfgv["table"], cfgv, fee)
                s = metrics.summarize(res, test)
                s.update(block_stats(res))
                key = f"{T} | {name}" + (f" | rebate {a.rebate}" if fee else "")
                report["test"][key] = s
                print(f"  {key:60s} pnl {s['pnl_liquidated_$']:8.2f}  fills {s['fills']:4d}  "
                      f"$/sh {s['pnl_per_share_$']:+.4f}  mk1s {s['markout_ticks'].get('1s', float('nan')):+.3f}  "
                      f"t {s['t_stat']:+.2f}")
    os.makedirs(a.out, exist_ok=True)
    with open(os.path.join(a.out, "experiment_adverse.json"), "w") as f:
        json.dump(report, f, indent=1, default=str)


if __name__ == "__main__":
    main()
