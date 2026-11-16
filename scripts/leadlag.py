#!/usr/bin/env python
"""SPY -> member stock lead-lag on LOBSTER data (2012-06-21, 09:35-10:30, NASDAQ).

1. response functions  : stock mid move after a SPY mid move (and the reverse), 0.1 ms - 1 s
2. cross-correlation    : 1 ms returns, lags -20..+20 ms
3. signal strength      : P(stock's next mid move follows SPY) vs size of the SPY move
4. staleness window     : time until the stock's quote catches up after a SPY move
5. latency sweep        : take the stale stock quote after a SPY move; PnL per trade vs latency.
                          Parameters chosen on 09:35-10:02, reported on 10:02-10:29:55.

    python scripts/leadlag.py --stocks INTC MSFT
"""
from __future__ import annotations

import argparse
import json
import os

import numpy as np

from hftmodel import lobster
from hftmodel.lobster import DUMMY_ASK, DUMMY_BID

MS = 1_000_000
BLUE, ORANGE, AQUA, YELLOW, GRAY = "#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#52514e"


class Quotes:
    """Top of book as a step function of time (exchange timestamps, ns)."""

    def __init__(self, day, t0, t1):
        d = day.slice_time(t0, t1)
        ok = (d.bid_px[:, 0] != DUMMY_BID) & (d.ask_px[:, 0] != DUMMY_ASK)
        self.ts = d.ts[ok]
        self.bid = d.bid_px[ok, 0]
        self.ask = d.ask_px[ok, 0]
        self.bsz = d.bid_sz[ok, 0]
        self.asz = d.ask_sz[ok, 0]
        self.mid2 = self.bid + self.ask            # 2 x mid, integer
        self.tick = day.tick
        self.name = day.ticker
        ch = np.flatnonzero(np.diff(self.mid2) != 0) + 1
        self.ev_t = self.ts[ch]                    # mid-change events
        self.ev_d = np.sign(self.mid2[ch] - self.mid2[ch - 1])
        self.ev_i = ch

    def at(self, t):
        """Row index of the book in force at time(s) t."""
        return np.clip(np.searchsorted(self.ts, t, side="right") - 1, 0, len(self.ts) - 1)

    def mid_bps(self, t):
        i = self.at(t)
        return 1e4 * self.mid2[i] / self.mid2[0]


def response(lead: Quotes, lag: Quotes, horizons_ms):
    """E[ sign(lead move) * lag mid change over (t, t+h] ] in bps of the lag asset (h<0: before)."""
    t, d = lead.ev_t, lead.ev_d
    m0 = lag.mid_bps(t - 1)                         # just before the lead event
    out = {}
    for h in horizons_ms:
        if h >= 0:
            out[h] = float(np.mean(d * (lag.mid_bps(t + int(h * MS)) - m0)))
        else:
            out[h] = float(np.mean(d * (m0 - lag.mid_bps(t + int(h * MS)))))
    return out


def xcorr(a: Quotes, b: Quotes, t0, t1, lags_ms, dt_ms=1):
    grid = np.arange(t0, t1, dt_ms * MS)
    ra, rb = np.diff(a.mid_bps(grid)), np.diff(b.mid_bps(grid))
    out = {}
    for L in lags_ms:                               # corr(r_a(t), r_b(t + L)): L > 0 means a leads
        if L >= 0:
            x, y = ra[: len(ra) - L], rb[L:]
        else:
            x, y = ra[-L:], rb[: len(rb) + L]
        out[L] = float(np.corrcoef(x, y)[0, 1])
    return out


def spy_moves(spy: Quotes, window_ms, k_ticks):
    """SPY trigger events: times where SPY's mid moved >= k ticks over the trailing window.
    Returns (times, direction). Consecutive triggers in the same direction are collapsed."""
    t = spy.ev_t
    m = spy.mid2[spy.ev_i]
    j = spy.at(t - int(window_ms * MS))
    mv = (m - spy.mid2[j]) / (2 * spy.tick)        # SPY ticks
    trig = np.abs(mv) >= k_ticks
    tt, dd = t[trig], np.sign(mv[trig]).astype(int)
    keep = np.ones(len(tt), bool)
    keep[1:] = (dd[1:] != dd[:-1]) | (np.diff(tt) > window_ms * MS)
    return tt[keep], dd[keep]


def follow_prob(stock: Quotes, tt, dd, max_wait_ms=1000):
    """P(stock's next mid move is in SPY's direction) and time until it happens."""
    c = np.searchsorted(stock.ev_t, tt, side="right")
    ok = c < len(stock.ev_t)
    c, d, tt = c[ok], dd[ok], tt[ok]
    wait = (stock.ev_t[c] - tt) / MS
    same = stock.ev_d[c] == d
    within = wait <= max_wait_ms
    return float(np.mean(same[within])), np.median(wait[within & same]) if (within & same).any() else np.nan, int(within.sum())


def take_stale(stock: Quotes, tt, dd, latency_ns, hold_ms, qty=100, fee_ticks=0.3, max_spread=None):
    """After a SPY trigger at tt, send an IOC at the stock's touch as seen at tt + latency/2;
    it fills if that price is still there at tt + latency. Returns per-trade ticks and stats."""
    t_see = tt + latency_ns // 2
    t_arr = tt + latency_ns
    i_see, i_arr = stock.at(t_see), stock.at(t_arr)
    spread = (stock.ask[i_see] - stock.bid[i_see]) // stock.tick
    # large-tick names: only trade a one-tick spread; small-tick names (AAPL): any spread
    one_tick = spread <= (max_spread if max_spread is not None else 10**9)
    limit = np.where(dd > 0, stock.ask[i_see], stock.bid[i_see])
    avail = np.where(dd > 0, stock.ask[i_arr] <= limit, stock.bid[i_arr] >= limit)
    size_ok = np.where(dd > 0, stock.asz[i_arr], stock.bsz[i_arr]) >= qty
    # don't count a trade if the stock had already moved our way before we even saw SPY
    # (then there is nothing stale to take)
    fill = one_tick & avail & size_ok
    entry = np.where(dd > 0, np.minimum(stock.ask[i_arr], limit), np.maximum(stock.bid[i_arr], limit))
    i_exit = stock.at(t_arr + int(hold_ms * MS))
    mid_exit = stock.mid2[i_exit] / 2
    cross_exit = np.where(dd > 0, stock.bid[i_exit], stock.ask[i_exit])
    pm = (dd * (mid_exit - entry) / stock.tick)[fill]
    pc = (dd * (cross_exit - entry) / stock.tick)[fill] - 2 * fee_ticks
    # one position at a time: drop triggers that arrive while holding
    keep = np.ones(fill.sum(), bool)
    tf = t_arr[fill]
    last = -1
    for n, t in enumerate(tf):
        if t < last:
            keep[n] = False
        else:
            last = t + int(hold_ms * MS)
    pm, pc = pm[keep], pc[keep]
    bps = 1e4 * stock.tick / (stock.mid2[0] / 2)        # one tick in bps of price
    return {"mid_bps": round(float(pm.mean() * bps), 3) if len(pm) else None,
            "cross_net_bps": round(float(pc.mean() * bps), 3) if len(pc) else None,"triggers": int(len(tt)), "eligible": int(one_tick.sum()), "trades": int(len(pm)),
            "fill_rate": round(float(fill.sum() / max(one_tick.sum(), 1)), 3),
            "mid_ticks": round(float(pm.mean()), 4) if len(pm) else None,
            "cross_net_ticks": round(float(pc.mean()), 4) if len(pc) else None,
            "t_mid": round(float(pm.mean() / pm.std(ddof=1) * np.sqrt(len(pm))), 2) if len(pm) > 2 and pm.std() > 0 else None}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default="data/lobster")
    ap.add_argument("--stocks", nargs="+", default=["INTC", "MSFT"])
    ap.add_argument("--out", default="output")
    ap.add_argument("--taker-fee", type=float, default=0.003)
    a = ap.parse_args()
    fee_ticks = a.taker_fee / 0.01

    spy_day = lobster.load(*lobster.find_files(a.data, "SPY")[0], max_levels=1)
    t0 = spy_day.ts[0] + 5 * 60_000 * MS
    t1 = spy_day.ts[-1] - 5_000 * MS
    tmid = t0 + (t1 - t0) // 2
    spy = Quotes(spy_day, t0, t1)
    stocks = {s: Quotes(lobster.load(*lobster.find_files(a.data, s)[0]), t0, t1) for s in a.stocks}
    max_spread = {s: (1 if np.mean((q.ask - q.bid) == q.tick) > 0.5 else None) for s, q in stocks.items()}
    print(f"window {lobster._fmt_ts(t0)}-{lobster._fmt_ts(t1)}; in-sample until {lobster._fmt_ts(tmid)}")
    print(f"SPY: {len(spy.ts):,} quote updates, {len(spy.ev_t):,} mid changes, "
          f"price ${spy.mid2[0] / 2e4:.2f}, 1 tick = {1e4 * spy.tick * 2 / spy.mid2[0]:.2f} bps")
    for s, q in stocks.items():
        print(f"{s}: {len(q.ev_t):,} mid changes, price ${q.mid2[0] / 2e4:.2f}, "
              f"1 tick = {1e4 * q.tick * 2 / q.mid2[0]:.2f} bps")

    horizons = [-100, -10, -1, 0.1, 0.5, 1, 2, 5, 10, 20, 50, 100, 500, 1000]
    lags = list(range(-20, 21))
    report = {"window": [int(t0), int(t1)], "stocks": {}}
    for s, q in stocks.items():
        r = {"spy_to_stock_bps": response(spy, q, horizons), "stock_to_spy_bps": response(q, spy, horizons),
             "xcorr_spy_leads": xcorr(spy, q, t0, t1, lags)}
        # signal strength by SPY move size (all data; descriptive)
        r["follow"] = {}
        for W in (1, 5, 20):
            for k in (1, 2, 3, 5):
                tt, dd = spy_moves(spy, W, k)
                p, wait, n = follow_prob(q, tt, dd)
                r["follow"][f"W{W}ms_k{k}"] = {"n": n, "p_follow": round(p, 3), "median_wait_ms": round(float(wait), 2)}
        # latency sweep: choose (W, k, hold) in-sample on the mid-exit edge at 50 us, then test
        best = None
        for W in (1, 5, 20):
            for k in (1, 2, 3, 5):
                tt, dd = spy_moves(spy, W, k)
                ins = tt < tmid
                for hold in (10, 100, 1000):
                    st = take_stale(q, tt[ins], dd[ins], 50_000, hold, fee_ticks=fee_ticks, max_spread=max_spread[s])
                    if st["trades"] >= 30 and st["mid_ticks"] is not None and (best is None or st["mid_ticks"] > best[0]):
                        best = (st["mid_ticks"], W, k, hold)
        _, W, k, hold = best
        tt, dd = spy_moves(spy, W, k)
        oos = tt >= tmid
        sweep = {}
        for lat_us in (0, 1, 10, 50, 100, 250, 500, 1000, 2000, 5000, 10000):
            sweep[lat_us] = take_stale(q, tt[oos], dd[oos], int(lat_us * 1e3), hold, fee_ticks=fee_ticks,
                                       max_spread=max_spread[s])
        r["selected"] = {"window_ms": W, "k_spy_ticks": k, "hold_ms": hold}
        r["latency_sweep_oos"] = sweep
        report["stocks"][s] = r

        print(f"\n===== {s}")
        print("response to a SPY mid move, stock bps (negative h = move *before* the SPY event):")
        print("   " + "  ".join(f"{h:>6g}ms" for h in horizons))
        print("   " + "  ".join(f"{v:+8.3f}" for v in r["spy_to_stock_bps"].values()))
        print(f"reverse: SPY response to a {s} move, SPY bps:")
        print("   " + "  ".join(f"{v:+8.3f}" for v in r["stock_to_spy_bps"].values()))
        xc = r["xcorr_spy_leads"]
        print(f"1 ms return xcorr: lag 0 {xc[0]:+.3f}, SPY leads by 1/2/5 ms {xc[1]:+.3f}/{xc[2]:+.3f}/{xc[5]:+.3f}, "
              f"stock leads by 1/2/5 ms {xc[-1]:+.3f}/{xc[-2]:+.3f}/{xc[-5]:+.3f}")
        print("P(stock's next mid move follows SPY), median wait when it does:")
        for key, v in r["follow"].items():
            print(f"   {key:10s} n {v['n']:5d}  p {v['p_follow']:.3f}  wait {v['median_wait_ms']:7.2f} ms")
        print(f"take-the-stale-quote, selected in-sample: SPY move >= {k} ticks in {W} ms, hold {hold} ms. OUT OF SAMPLE:")
        for lat, st in sweep.items():
            print(f"   latency {lat:>6} us  trades {st['trades']:4d}  fill {st['fill_rate']:.2f}  "
                  f"mid {st['mid_ticks'] if st['mid_ticks'] is not None else float('nan'):+.3f} (t {st['t_mid']})  "
                  f"cross-net {st['cross_net_ticks'] if st['cross_net_ticks'] is not None else float('nan'):+.3f} ticks  "
                  f"[mid {st['mid_bps'] if st['mid_bps'] is not None else float('nan'):+.2f} bps, "
                  f"cross-net {st['cross_net_bps'] if st['cross_net_bps'] is not None else float('nan'):+.2f} bps]")

    os.makedirs(a.out, exist_ok=True)
    with open(os.path.join(a.out, "leadlag.json"), "w") as f:
        json.dump(report, f, indent=1, default=str)
    plot(report, a.out, horizons, lags)


def plot(report, out, horizons, lags):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    plt.rcParams.update({"axes.spines.top": False, "axes.spines.right": False, "axes.grid": True,
                         "grid.color": "#e6e5e0", "grid.linewidth": 0.6, "axes.edgecolor": "#b9b8b2",
                         "axes.labelcolor": GRAY, "xtick.color": GRAY, "ytick.color": GRAY,
                         "figure.facecolor": "#fcfcfb", "axes.facecolor": "#fcfcfb", "font.size": 9})
    names = list(report["stocks"])
    colors = [BLUE, ORANGE, AQUA, YELLOW]
    fig, ax = plt.subplots(1, 3, figsize=(15, 4))
    hp = [h for h in horizons if h > 0]
    for c, s in zip(colors, names):
        r = report["stocks"][s]
        ax[0].semilogx(hp, [r["spy_to_stock_bps"][h] for h in hp], "-o", color=c, ms=4, lw=1.5, label=f"{s} after SPY move")
        ax[0].semilogx(hp, [r["stock_to_spy_bps"][h] for h in hp], "--", color=c, lw=1.2, label=f"SPY after {s} move")
        ax[1].plot(lags, [r["xcorr_spy_leads"][L] for L in lags], "-o", color=c, ms=3, lw=1.2, label=s)
        sw = r["latency_sweep_oos"]
        lat = [L for L in sw if sw[L]["mid_ticks"] is not None]
        ax[2].semilogx([max(L, 0.5) for L in lat], [sw[L]["mid_ticks"] for L in lat], "-o", color=c, ms=4, lw=1.5,
                       label=f"{s}: exit at mid")
        ax[2].semilogx([max(L, 0.5) for L in lat], [sw[L]["cross_net_ticks"] for L in lat], "--", color=c, lw=1.2,
                       label=f"{s}: cross out, net of fees")
    ax[0].set_xlabel("horizon after the event (ms)")
    ax[0].set_ylabel("signed follow-through (bps)")
    ax[0].set_title("Who follows whom", loc="left")
    ax[0].legend(frameon=False, fontsize=8)
    ax[1].axvline(0, color=GRAY, lw=0.8)
    ax[1].set_xlabel("lag (ms), > 0 = SPY leads")
    ax[1].set_ylabel("corr of 1 ms returns")
    ax[1].set_title("Cross-correlation", loc="left")
    ax[1].legend(frameon=False)
    ax[2].axhline(0, color=GRAY, lw=0.8)
    ax[2].set_xlabel("round-trip latency (µs; 0 plotted at 0.5)")
    ax[2].set_ylabel("PnL per trade (ticks)")
    ax[2].set_title("Taking stale quotes after SPY moves (out of sample)", loc="left")
    ax[2].legend(frameon=False, fontsize=8)
    fig.tight_layout()
    fig.savefig(os.path.join(out, "leadlag.png"), dpi=130)
    print("\nplot ->", os.path.join(out, "leadlag.png"))


if __name__ == "__main__":
    main()
