#!/usr/bin/env python
"""Calibrate on the first part of the day, backtest Avellaneda–Stoikov (+ CST skew) on the rest.

Examples
--------
    python scripts/run_backtest.py --synthetic
    python scripts/run_backtest.py --data data/lobster --ticker INTC
    python scripts/run_backtest.py --data data/lobster --ticker MSFT --gamma 0.02 --tau 30 --alphas 0 0.5 1
    python scripts/run_backtest.py --data data/lobster --ticker INTC --hbt     # cross-check in hftbacktest
"""
from __future__ import annotations

import argparse
import json
import os
import sys

import numpy as np

from hftmodel import backtest as bt
from hftmodel import calibration, cst, lobster, metrics, strategy, synthetic

BLUE, ORANGE, AQUA, GRAY = "#2a78d6", "#eb6834", "#1baf7a", "#52514e"


def parse():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--data", help="directory containing LOBSTER *_message_*.csv / *_orderbook_*.csv")
    src.add_argument("--synthetic", action="store_true", help="simulate a CST order book instead")
    ap.add_argument("--ticker", help="ticker to pick from --data")
    ap.add_argument("--events", type=int, default=400_000, help="synthetic events")
    ap.add_argument("--skip-open-min", type=float, default=5, help="ignore the first minutes (opening auction noise)")
    ap.add_argument("--skip-close-min", type=float, default=5)
    ap.add_argument("--calib-min", type=float, default=60, help="in-sample calibration window (minutes)")
    ap.add_argument("--gamma", type=float, default=0.02, help="risk aversion, 1/(tick*lot)")
    ap.add_argument("--tau", type=float, default=10.0, help="A-S horizon in seconds (<=0: time to close)")
    ap.add_argument("--k", type=float, default=None, help="override calibrated k (1/tick)")
    ap.add_argument("--alphas", type=float, nargs="+", default=[0.0, 0.5, 1.0], help="CST skew strengths (ticks)")
    ap.add_argument("--max-pos", type=int, default=5, help="inventory limit (lots)")
    ap.add_argument("--qty", type=int, default=100, help="shares per quote")
    ap.add_argument("--latency-us", type=float, default=50, help="one-way latency (feed and order), microseconds")
    ap.add_argument("--maker-fee", type=float, default=0.0, help="$ per share, negative = rebate")
    ap.add_argument("--min-requote-ms", type=float, default=0.0)
    ap.add_argument("--fixed-point", action="store_true", help="run the FPGA fixed-point kernel instead of float")
    ap.add_argument("--hbt", action="store_true", help="also run the strategy inside hftbacktest")
    ap.add_argument("--out", default="output")
    ap.add_argument("--no-plots", action="store_true")
    return ap.parse_args()


def load_day(a) -> lobster.LobsterDay:
    if a.synthetic:
        return synthetic.simulate(a.events, seed=11)
    pairs = lobster.find_files(a.data, a.ticker)
    if not pairs:
        sys.exit(f"no LOBSTER files for ticker={a.ticker!r} under {a.data}; see scripts/download_lobster_sample.sh")
    if len(pairs) > 1 and not a.ticker:
        print("multiple files found, using the first; pass --ticker to choose:", [os.path.basename(p[0]) for p in pairs])
    return lobster.load(*pairs[0])


def main():
    a = parse()
    os.makedirs(a.out, exist_ok=True)
    day = load_day(a)
    print(json.dumps(lobster.summary(day), indent=1, default=str))

    t0 = day.ts[0] + int(a.skip_open_min * 60e9)
    t_end = day.ts[-1] - int(a.skip_close_min * 60e9)
    t_cal = t0 + int(a.calib_min * 60e9)
    if t_cal >= t_end:
        sys.exit("calibration window covers the whole sample; lower --calib-min")
    cal, test = day.slice_time(t0, t_cal), day.slice_time(t_cal, t_end)
    print(f"calibration {lobster._fmt_ts(t0)}-{lobster._fmt_ts(t_cal)}, "
          f"backtest {lobster._fmt_ts(t_cal)}-{lobster._fmt_ts(t_end)} ({len(test):,} messages)")

    # ---- calibration (in-sample only) --------------------------------------------------
    sigma = calibration.estimate_sigma(cal)
    fit = calibration.estimate_intensity(cal)
    cstp = calibration.estimate_cst(cal)
    k = a.k if a.k is not None else fit.k
    p_up = cst.prob_up_table(cstp.lam[0], cstp.mu, cstp.theta[0], 31)
    emp, _ = cst.empirical_up_table(cal, cstp.S_l, 31)
    m = ~np.isnan(emp)
    corr = float(np.corrcoef(p_up[m], emp[m])[0, 1]) if m.sum() > 3 else float("nan")
    calib = {"sigma_ticks_per_sqrt_s": round(sigma, 4), "A_per_s": round(fit.A, 4), "k_per_tick": round(fit.k, 4),
             "k_used": round(k, 4), "cst": cstp.as_dict(), "cst_model_vs_empirical_corr": round(corr, 3),
             "cst_cells_compared": int(m.sum())}
    print("calibration:", json.dumps(calib, indent=1))

    # ---- backtests ------------------------------------------------------------------------
    cfg = bt.BacktestConfig(order_qty=a.qty, feed_latency_ns=int(a.latency_us * 1e3),
                            order_latency_ns=int(a.latency_us * 1e3), maker_fee=a.maker_fee,
                            min_requote_ns=int(a.min_requote_ms * 1e6), sigma0=sigma)
    report = {"ticker": day.ticker, "date": day.date, "calibration": calib, "config": vars(a), "runs": {}}
    results = {}
    for alpha in a.alphas:
        p = strategy.ASParams(gamma=a.gamma, k=k, sigma=sigma, tau=a.tau, t_end_ns=int(t_end), alpha=alpha,
                              unit=cstp.S_l, max_pos=a.max_pos)
        name = f"AS{'+CST' if alpha else ''} alpha={alpha:g}"
        if a.fixed_point:
            ip, lut = strategy.fixed_point_params(p, p_up)
            res = bt.run(test, strategy.as_cst_quote_fx, p.fp(), ip=ip, lut_i=lut, cfg=cfg)
            name += " [fixed-point]"
        else:
            res = bt.run(test, strategy.as_cst_quote, p.fp(), lut_f=p_up, cfg=cfg)
        s = metrics.summarize(res, test)
        s["half_spread_ticks"] = round(p.half_spread(), 3)
        report["runs"][name] = s
        results[name] = res
        print(f"\n== {name}\n" + json.dumps(s, indent=1))

        if a.hbt:
            from hftmodel import hbt
            data = hbt.convert(test, feed_latency_ns=cfg.feed_latency_ns)
            rec = hbt.run_as_cst(data, strategy.as_cst_quote, p.fp(), lut_f=p_up, qty=a.qty,
                                 order_latency_ns=cfg.order_latency_ns, maker_fee=a.maker_fee)
            hs = hbt.summarize_records(rec)
            report["runs"][name]["hftbacktest"] = hs
            print("hftbacktest:", hs)

    path = os.path.join(a.out, f"report_{day.ticker}.json")
    with open(path, "w") as f:
        json.dump(report, f, indent=1, default=str)
    print("\nreport ->", path)
    if not a.no_plots:
        plot(a.out, day.ticker, test, results, fit, p_up, emp)


def plot(out, ticker, test, results, fit, p_up, emp):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    plt.rcParams.update({"axes.spines.top": False, "axes.spines.right": False, "axes.grid": True,
                         "grid.color": "#e6e5e0", "grid.linewidth": 0.6, "axes.edgecolor": "#b9b8b2",
                         "axes.labelcolor": GRAY, "xtick.color": GRAY, "ytick.color": GRAY,
                         "figure.facecolor": "#fcfcfb", "axes.facecolor": "#fcfcfb", "font.size": 9})
    colors = [BLUE, ORANGE, AQUA, "#eda100", "#e87ba4", "#6250d6"]
    fig, ax = plt.subplots(3, 1, figsize=(10, 8), sharex=True)
    for c, (name, r) in zip(colors, results.items()):
        h = (r.records["t"] - r.records["t"][0]) / 3.6e12
        ax[0].plot(h, metrics.equity_curve(r), color=c, lw=1.5, label=name)
        ax[1].plot(h, r.records["pos"], color=c, lw=1.0, label=name)
    first = next(iter(results.values()))
    ax[2].plot((first.records["t"] - first.records["t"][0]) / 3.6e12, first.records["mid"] / 10_000, color=GRAY, lw=1)
    ax[0].set_ylabel("PnL, marked to mid ($)")
    ax[1].set_ylabel("inventory (shares)")
    ax[2].set_ylabel("mid price ($)")
    ax[2].set_xlabel("hours into backtest window")
    ax[0].legend(frameon=False, loc="upper left")
    ax[0].set_title(f"{ticker}: Avellaneda–Stoikov market making, out-of-sample", loc="left")
    fig.tight_layout()
    fig.savefig(os.path.join(out, f"backtest_{ticker}.png"), dpi=130)

    fig, ax = plt.subplots(1, 3, figsize=(13, 3.8))
    ok = fit.rates > 0
    ax[0].semilogy(fit.depths[ok], fit.rates[ok], "o", color=BLUE, ms=4, label="data")
    dd = fit.depths[: max(int(np.flatnonzero(ok)[-1]) + 2, 2)] if ok.any() else fit.depths
    ax[0].semilogy(dd, fit.A * np.exp(-fit.k * dd), color=ORANGE, lw=1.5,
                   label=f"A e^(-k δ): A={fit.A:.2f}, k={fit.k:.2f}")
    ax[0].set_xlabel("depth δ from mid (ticks)")
    ax[0].set_ylabel("market orders reaching δ (per s)")
    ax[0].legend(frameon=False)
    ax[0].set_title("Fill intensity λ(δ)", loc="left")
    n = 12
    for i, (M, title) in enumerate([(p_up, "CST model P(mid up)"), (emp, "Empirical P(mid up)")]):
        im = ax[i + 1].imshow(M[1:n + 1, 1:n + 1], origin="lower", cmap="RdBu_r", vmin=0, vmax=1,
                              extent=(0.5, n + 0.5, 0.5, n + 0.5))
        ax[i + 1].set_xlabel("ask queue (units)")
        ax[i + 1].set_ylabel("bid queue (units)")
        ax[i + 1].set_title(title, loc="left")
        ax[i + 1].grid(False)
    fig.colorbar(im, ax=ax[1:], shrink=0.85)
    fig.savefig(os.path.join(out, f"calibration_{ticker}.png"), dpi=130, bbox_inches="tight")
    print("plots ->", out)


if __name__ == "__main__":
    main()
