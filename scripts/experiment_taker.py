#!/usr/bin/env python
"""Taker test: is the queue-imbalance / CST signal strong enough to pay the spread?

When the spread is one tick and P_up > theta we buy at the ask (IOC at the observed ask,
after latency; if the ask moved away the order misses); P_up < 1 - theta -> sell at the bid.
One position at a time, exit after a horizon. Each trade is priced two ways:

    exit at mid    side * (mid_exit - entry)          optimistic markout (no exit cost)
    exit crossing  side * (touch_exit - entry)        pure taker: crosses the spread both ways

Windows per ticker as in experiment_adverse.py: calibrate 09:35-10:35, choose the signal and
theta on 10:35-13:15 (exit crossing, net of fees), report on 13:15-15:55.

    python scripts/experiment_taker.py --data data/lobster --tickers INTC MSFT
"""
from __future__ import annotations

import argparse
import json
import os

import numpy as np

from hftmodel import calibration, cst, lobster

M = 60_000_000_000


def windows(day):
    t0 = day.ts[0] + 5 * M
    return (t0, t0 + 60 * M), (t0 + 60 * M, t0 + 220 * M), (t0 + 220 * M, day.ts[-1] - 5 * M)


def signal_prob(day, table, unit, kind):
    """P_up estimate for every row (NaN unless spread == 1 tick)."""
    bq, aq = day.bid_sz[:, 0].astype(float), day.ask_sz[:, 0].astype(float)
    one = (day.ask_px[:, 0] - day.bid_px[:, 0]) == day.tick
    if kind == "imbalance":
        p = bq / (bq + aq)
    else:
        n = table.shape[0] - 1
        x = np.clip(np.round(bq / unit).astype(int), 1, n)
        y = np.clip(np.round(aq / unit).astype(int), 1, n)
        p = table[x, y]
    return np.where(one, p, np.nan)


def simulate(day, p, theta, latency_ns, horizon, qty=100):
    """Returns per-trade arrays (side, pnl_mid_ticks, pnl_cross_ticks) and the miss count."""
    ts, tick = day.ts, day.tick
    bid, ask = day.bid_px[:, 0], day.ask_px[:, 0]
    mid2 = bid + ask
    sig = np.where(p > theta, 1, np.where(p < 1 - theta, -1, 0))
    cand = np.flatnonzero(sig != 0)
    change = np.flatnonzero(np.diff(mid2) != 0) + 1
    sides, pm, pc = [], [], []
    misses = 0
    busy_until = -1
    for i in cand:
        if ts[i] <= busy_until:
            continue
        s = sig[i]
        te = ts[i] + latency_ns
        j = np.searchsorted(ts, te, side="right") - 1
        if s > 0:
            if ask[j] > ask[i] or day.ask_sz[j, 0] < qty:
                misses += 1
                busy_until = te
                continue
            entry = ask[j]
        else:
            if bid[j] < bid[i] or day.bid_sz[j, 0] < qty:
                misses += 1
                busy_until = te
                continue
            entry = bid[j]
        if horizon == "next":
            c = np.searchsorted(change, j, side="right")
            if c >= len(change):
                break
            k = change[c]
        else:
            k = np.searchsorted(ts, te + int(horizon * 1e9), side="right") - 1
        if k >= len(ts) - 1:
            break
        exit_cross = bid[k] if s > 0 else ask[k]
        sides.append(s)
        pm.append(s * (mid2[k] / 2 - entry) / tick)
        pc.append(s * (exit_cross - entry) / tick)
        busy_until = max(ts[k], te)
    return np.array(sides), np.array(pm), np.array(pc), misses


def stats(pnl_ticks, fee_ticks):
    if len(pnl_ticks) < 2:
        return {"trades": int(len(pnl_ticks))}
    net = pnl_ticks - 2 * fee_ticks
    return {"trades": int(len(pnl_ticks)), "gross_ticks": round(float(pnl_ticks.mean()), 4),
            "net_ticks": round(float(net.mean()), 4),
            "t_net": round(float(net.mean() / net.std(ddof=1) * np.sqrt(len(net))), 2) if net.std() > 0 else None}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default="data/lobster")
    ap.add_argument("--tickers", nargs="+", default=["INTC", "MSFT"])
    ap.add_argument("--latency-us", type=float, default=50)
    ap.add_argument("--taker-fee", type=float, default=0.003, help="$ per share per side (assumed)")
    ap.add_argument("--out", default="output")
    a = ap.parse_args()
    fee_ticks = a.taker_fee / 0.01
    lat = int(a.latency_us * 1e3)
    thetas = [0.6, 0.7, 0.8, 0.9]
    horizons = ["next", 1.0, 10.0]
    report = {"assumptions": vars(a), "tickers": {}}

    for T in a.tickers:
        day = lobster.load(*lobster.find_files(a.data, T)[0])
        wc, wv, wt = windows(day)
        c = day.slice_time(*wc)
        cp = calibration.estimate_cst(c)
        p_cst = cst.prob_up_table(cp.lam[0], cp.mu, cp.theta[0], 31)
        tables = {"cst": p_cst, "emp": cst.blended_up_table(c, p_cst, cp.S_l), "imbalance": None}
        val, test = day.slice_time(*wv), day.slice_time(*wt)

        # direction check: does the signal predict the next mid move at all?
        print(f"\n===== {T}  (unit {cp.S_l:.0f} shares, latency {a.latency_us:g} us, fee {a.taker_fee}/sh/side "
              f"= {fee_ticks:.2f} tick/side)")
        res = {"validation": {}, "test": {}}
        best = None
        print("validation (exit crossing, net of fees, mean ticks per trade):")
        for kind, tb in tables.items():
            pv = signal_prob(val, tb, cp.S_l, kind)
            for h in horizons:
                line = []
                for th in thetas:
                    s, pm, pc, miss = simulate(val, pv, th, lat, h)
                    st = stats(pc, fee_ticks)
                    res["validation"][f"{kind}|{h}|{th}"] = {**st, "misses": miss}
                    line.append(f"θ{th}: {st.get('net_ticks', float('nan')):+.3f} ({st['trades']})")
                    if st["trades"] >= 30 and (best is None or st["net_ticks"] > best[0]):
                        best = (st["net_ticks"], kind, h, th)
                print(f"  {kind:9s} h={str(h):4s}  " + "  ".join(line))
        _, kind, h, th = best
        print(f"selected on validation: signal={kind}, horizon={h}, theta={th}")

        pt = signal_prob(test, tables[kind], cp.S_l, kind)
        s, pm, pc, miss = simulate(test, pt, th, lat, h)
        hit = float(np.mean(pm > 0)) if len(pm) else float("nan")
        out = {"selected": {"signal": kind, "horizon": h, "theta": th}, "misses": miss,
               "exit_at_mid": stats(pm, 0.0), "exit_crossing": stats(pc, fee_ticks),
               "pct_trades_mid_moved_our_way": round(100 * hit, 1)}
        res["test"] = out
        print(f"TEST: {out}")
        # how much predictive power is there, regardless of costs? (all signals, test window)
        print("TEST, exit at mid at the next mid move, gross ticks/trade (includes the half spread paid on entry):")
        for kind2, tb in tables.items():
            p2 = signal_prob(test, tb, cp.S_l, kind2)
            line = []
            for th2 in thetas:
                _, pm2, _, _ = simulate(test, p2, th2, lat, "next")
                line.append(f"θ{th2}: {pm2.mean() if len(pm2) else float('nan'):+.3f} ({len(pm2)})")
            print(f"  {kind2:9s} next-move  " + "  ".join(line))
        report["tickers"][T] = res
    os.makedirs(a.out, exist_ok=True)
    with open(os.path.join(a.out, "experiment_taker.json"), "w") as f:
        json.dump(report, f, indent=1, default=str)


if __name__ == "__main__":
    main()
