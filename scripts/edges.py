#!/usr/bin/env python
"""Tries several classic HFT edges on LOBSTER data and reports each out of sample.

Windows per stock: calibrate 09:35-10:35, select parameters 10:35-13:15, report 13:15-15:55.

1. queue priority      penny-improve the spread and never join the back of a queue, vs the same
                       prices with back-of-queue joining (isolates the value of being first)
2. flow persistence    market-order signs come in runs (order splitting); pull the quote that the
                       run is about to hit. Placebo: pull the other side.
3. microprice          how much better than the mid is an imbalance-based fair value at predicting
                       the mid 1 s / 10 s ahead (out-of-sample R^2)
4. hidden liquidity    how much volume executes against hidden orders

    python scripts/edges.py --stocks INTC MSFT AAPL
"""
from __future__ import annotations

import argparse
import json
import os

import numpy as np

from hftmodel import backtest as bt
from hftmodel import calibration, lobster, metrics, strategy
from hftmodel.lobster import DUMMY_ASK, DUMMY_BID, EXEC_HIDDEN, EXEC_VISIBLE

M = 60_000_000_000
MS = 1_000_000


def windows(day):
    t0 = day.ts[0] + 5 * M
    return (t0, t0 + 60 * M), (t0 + 60 * M, t0 + 220 * M), (t0 + 220 * M, day.ts[-1] - 5 * M)


def summ(res, day):
    s = metrics.summarize(res, day)
    mk = s.get("markout_ticks", {})
    first = float(np.mean(res.fills["ahead0"] == 0)) if len(res.fills["t"]) else float("nan")
    return {"pnl": s["pnl_liquidated_$"], "fills": s["fills"], "per_share": s["pnl_per_share_$"],
            "mk0": mk.get("0s"), "mk1s": mk.get("1s"), "mk10s": mk.get("10s"), "pct_first_in_queue": round(100 * first, 1)}


def fmt(s):
    return (f"pnl {s['pnl']:8.2f}  fills {s['fills']:5d}  $/sh {s['per_share']:+.4f}  "
            f"mk0 {s['mk0'] if s['mk0'] is not None else float('nan'):+.2f}  "
            f"mk1s {s['mk1s'] if s['mk1s'] is not None else float('nan'):+.2f}  first {s['pct_first_in_queue']:.0f}%")


# ---------------------------------------------------------------- 1. queue priority
def queue_priority(cal, test, lat_us=50):
    p = strategy.ASParams(gamma=0.02, k=cal["k"], sigma=cal["sigma"], tau=10.0, max_pos=5)
    lut = np.full((2, 2), 0.5)
    mk = lambda **kw: bt.BacktestConfig(feed_latency_ns=int(lat_us * 500), order_latency_ns=int(lat_us * 500),  # noqa
                                        sigma0=cal["sigma"], **kw)
    out = {
        "A-S, joins queues": summ(bt.run(test, strategy.as_cst_quote, p.fp(), lut_f=lut, cfg=mk()), test),
        "penny, joins queues": summ(bt.run(test, strategy.penny_quote, p.fp(), lut_f=lut, cfg=mk()), test),
        "penny, first-in-queue only": summ(bt.run(test, strategy.penny_quote, p.fp(), lut_f=lut,
                                                  cfg=mk(join_queue=False)), test),
    }
    return out


def queue_latency(cal, test, lats):
    p = strategy.ASParams(gamma=0.02, k=cal["k"], sigma=cal["sigma"], tau=10.0, max_pos=5)
    out = {}
    for L in lats:
        cfg = bt.BacktestConfig(feed_latency_ns=int(L * 500), order_latency_ns=int(L * 500), sigma0=cal["sigma"],
                                join_queue=False)
        out[L] = summ(bt.run(test, strategy.penny_quote, p.fp(), lut_f=np.full((2, 2), 0.5), cfg=cfg), test)
    return out


# ---------------------------------------------------------------- 2. order-flow persistence
def mo_signs(day):
    first, _, side, _ = calibration.market_orders(day)
    return day.ts[first], side


def sign_autocorr(day, lags):
    _, s = mo_signs(day)
    s = s - s.mean()
    v = np.dot(s, s) / len(s)
    return {L: float(np.dot(s[:-L], s[L:]) / (len(s) - L) / v) for L in lags}


def flow_signal(day, window_ms, m):
    """+1 (pull the ask) when net buy market orders over the trailing window >= m, -1 for sells."""
    t, s = mo_signs(day)
    cb = np.r_[0, np.cumsum(s > 0)]
    cs = np.r_[0, np.cumsum(s < 0)]
    hi = np.searchsorted(t, day.ts, side="right")
    lo = np.searchsorted(t, day.ts - int(window_ms * MS), side="right")
    net = (cb[hi] - cb[lo]) - (cs[hi] - cs[lo])
    return np.where(net >= m, 1, np.where(net <= -m, -1, 0)).astype(np.int8)


def flow_run(cal, day, ext):
    p = strategy.ASParams(gamma=0.02, k=cal["k"], sigma=cal["sigma"], tau=10.0, max_pos=5)
    cfg = bt.BacktestConfig(sigma0=cal["sigma"])
    return bt.run(day, strategy.as_cst_quote, p.fp(), lut_f=np.full((2, 2), 0.5), cfg=cfg, ext=ext)


# ---------------------------------------------------------------- 3. microprice
def grid_rows(day, step_ms=100):
    ok = (day.bid_px[:, 0] != DUMMY_BID) & (day.ask_px[:, 0] != DUMMY_ASK)
    ts = day.ts[ok]
    g = np.arange(ts[0], ts[-1], step_ms * MS)
    return np.flatnonzero(ok)[np.searchsorted(ts, g, side="right") - 1]


def micro_features(day, rows):
    bq, aq = day.bid_sz[rows, 0].astype(float), day.ask_sz[rows, 0].astype(float)
    imb = bq / (bq + aq)
    spread = (day.ask_px[rows, 0] - day.bid_px[rows, 0]) // day.tick
    mid = 0.5 * (day.ask_px[rows, 0] + day.bid_px[rows, 0]) / day.tick
    return imb, np.minimum(spread, 4), mid


def future_mid(day, rows, h_s):
    ok = (day.bid_px[:, 0] != DUMMY_BID) & (day.ask_px[:, 0] != DUMMY_ASK)
    ts = day.ts[ok]
    mid = 0.5 * (day.ask_px[ok, 0] + day.bid_px[ok, 0]) / day.tick
    j = np.clip(np.searchsorted(ts, day.ts[rows] + int(h_s * 1e9), side="right") - 1, 0, len(ts) - 1)
    return mid[j]


def microprice_test(cal_day, test_day, horizons=(1.0, 10.0), nb=10):
    """Stoikov-style microprice: mid + G(imbalance bucket, spread), with G estimated in-sample as the
    average future mid change. Compared with the 'weighted mid' (imbalance-weighted bid/ask)."""
    out = {}
    rc, rt = grid_rows(cal_day), grid_rows(test_day)
    ic, sc, mc = micro_features(cal_day, rc)
    it, st, mt = micro_features(test_day, rt)
    bc = np.minimum((ic * nb).astype(int), nb - 1)
    bt_ = np.minimum((it * nb).astype(int), nb - 1)
    for h in horizons:
        dc = future_mid(cal_day, rc, h) - mc
        dt = future_mid(test_day, rt, h) - mt
        G = np.zeros((nb, 5))
        for b in range(nb):
            for s in range(5):
                m = (bc == b) & (sc == s)
                G[b, s] = dc[m].mean() if m.sum() >= 30 else 0.0
        pred_micro = G[bt_, st]
        half_spread = 0.5 * st
        pred_wmid = (it - 0.5) * 2 * half_spread          # weighted mid minus mid
        sse0 = np.sum(dt ** 2)
        out[f"{h:g}s"] = {"r2_microprice": round(1 - np.sum((dt - pred_micro) ** 2) / sse0, 4),
                          "r2_weighted_mid": round(1 - np.sum((dt - pred_wmid) ** 2) / sse0, 4),
                          "n": int(len(dt))}
    return out


# ---------------------------------------------------------------- 4. hidden liquidity
def hidden_stats(day):
    ex = (day.type == EXEC_VISIBLE) | (day.type == EXEC_HIDDEN)
    hid = day.type == EXEC_HIDDEN
    return {"pct_volume_hidden": round(100 * day.size[hid].sum() / max(day.size[ex].sum(), 1), 1),
            "pct_executions_hidden": round(100 * hid.sum() / max(ex.sum(), 1), 1)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default="data/lobster")
    ap.add_argument("--stocks", nargs="+", default=["INTC", "MSFT", "AAPL"])
    ap.add_argument("--out", default="output")
    a = ap.parse_args()
    report = {}
    lats = [1, 10, 50, 100, 500, 1000, 5000, 20000]
    for T in a.stocks:
        day = lobster.load(*lobster.find_files(a.data, T)[0])
        wc, wv, wt = windows(day)
        c, v, te = day.slice_time(*wc), day.slice_time(*wv), day.slice_time(*wt)
        cal = {"sigma": calibration.estimate_sigma(c), "k": calibration.estimate_intensity(c).k}
        r = {}
        print(f"\n################ {T}")

        r["queue_priority"] = queue_priority(cal, te)
        print("1. QUEUE PRIORITY (test window):")
        for k, s in r["queue_priority"].items():
            print(f"   {k:28s} {fmt(s)}")
        r["queue_latency"] = queue_latency(cal, te, lats)
        print("   first-in-queue vs latency: " + "  ".join(
            f"{L}us: {s['pnl']:.0f}$/{s['pct_first_in_queue']:.0f}%" for L, s in r["queue_latency"].items()))

        r["sign_autocorr"] = sign_autocorr(day, [1, 2, 5, 10, 20, 50, 100, 200, 500])
        print("2. FLOW PERSISTENCE: market-order sign autocorrelation " +
              "  ".join(f"lag{L}: {x:+.3f}" for L, x in r["sign_autocorr"].items()))
        best = None
        for W in (50, 200, 1000, 5000):
            for m in (1, 2, 3, 5):
                pnl = flow_run(cal, v, flow_signal(v, W, m)).pnl_liquidated
                if best is None or pnl > best[0]:
                    best = (pnl, W, m)
        _, W, m = best
        ext = flow_signal(te, W, m)
        base, fl, wrong = flow_run(cal, te, None), flow_run(cal, te, ext), flow_run(cal, te, -ext)
        r["flow"] = {"selected": {"window_ms": W, "net_orders": m}, "pct_time_active": round(100 * float(np.mean(ext != 0)), 1),
                     "baseline": summ(base, te), "flow_defend": summ(fl, te), "placebo_wrong_side": summ(wrong, te)}
        print(f"   selected on validation: net {m}+ same-side market orders in {W} ms -> pull the side being hit")
        for k in ("baseline", "flow_defend", "placebo_wrong_side"):
            print(f"   {k:28s} {fmt(r['flow'][k])}")

        r["microprice"] = microprice_test(c, te)
        print("3. MICROPRICE out-of-sample R^2 of future mid change: " + "  ".join(
            f"{h}: micro {x['r2_microprice']:+.3f}, weighted-mid {x['r2_weighted_mid']:+.3f}"
            for h, x in r["microprice"].items()))
        r["hidden"] = hidden_stats(day)
        print(f"4. HIDDEN LIQUIDITY: {r['hidden']}")
        report[T] = r
    os.makedirs(a.out, exist_ok=True)
    with open(os.path.join(a.out, "edges.json"), "w") as f:
        json.dump(report, f, indent=1, default=str)


if __name__ == "__main__":
    main()
