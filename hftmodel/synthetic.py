"""Synthetic LOBSTER-format data from the Cont–Stoikov–Talreja (2010) queueing model.

The order book is simulated as a continuous-time Markov chain on a price grid:

* limit buy (sell) orders arrive at distance i ticks from the opposite best quote
  with rate lambda(i), i = 1..L
* market buy (sell) orders arrive with rate mu and hit the front of the best
  ask (bid) queue
* every resting order at distance i from the opposite best quote is cancelled
  with rate theta(i)

All orders have unit size (``unit`` shares), as in the paper. The output is a
:class:`LobsterDay`, so the same pipeline (calibration -> strategy -> backtest)
runs on synthetic and real data. Calibrating on this data should recover the
parameters it was generated with, which is a cheap end-to-end check.
"""
from __future__ import annotations

import numpy as np
from numba import njit

from .lobster import DUMMY_ASK, DUMMY_BID, LobsterDay

# Rates per second in units of one order, of the same order of magnitude as the
# estimates Cont, Stoikov & Talreja report for liquid large-tick stocks:
# lambda(i) = k / i^alpha, market order rate mu, cancellation rate theta(i).
CST_DEFAULT_K = 1.92
CST_DEFAULT_ALPHA = 0.52
CST_DEFAULT_MU = 0.94
CST_DEFAULT_THETA = np.array([0.71, 0.81, 0.68, 0.56, 0.47, 0.47, 0.48, 0.49, 0.50, 0.52])


def default_params(L: int = 10):
    lam = CST_DEFAULT_K / np.arange(1, L + 1) ** CST_DEFAULT_ALPHA
    theta = np.resize(CST_DEFAULT_THETA, L).astype(np.float64)
    return lam, CST_DEFAULT_MU, theta


@njit(cache=True)
def _remove_at(ids, n, lvl, pos):
    for j in range(pos, n[lvl] - 1):
        ids[lvl, j] = ids[lvl, j + 1]
    n[lvl] -= 1


@njit(cache=True)
def _count_from(n, p, step, grid):
    """Number of orders on one side starting at p and walking away from the touch (capped at 2)."""
    s = 0
    q = p
    while 0 <= q < grid:
        s += n[q]
        if s > 1:
            return s
        q += step
    return s


@njit(cache=True)
def _pick(rates, u):
    acc = 0.0
    for j in range(rates.shape[0]):
        acc += rates[j]
        if u < acc:
            return j
    return rates.shape[0] - 1


@njit(cache=True)
def _simulate(lam, mu, theta, n_events, seed, grid, cap, unit, p0_ticks, tick, t0_ns, levels_out):
    np.random.seed(seed)
    L = lam.shape[0]
    W = 4 * L                      # cancellation scan window in ticks
    bid_ids = np.zeros((grid, cap), np.int64)
    ask_ids = np.zeros((grid, cap), np.int64)
    bid_n = np.zeros(grid, np.int64)
    ask_n = np.zeros(grid, np.int64)
    c = grid // 2
    best_bid = c - 1
    best_ask = c
    next_id = 1
    for i in range(1, L + 1):      # initial book at the stationary mean queue length
        q = max(1, int(round(lam[i - 1] / theta[i - 1])))
        for _ in range(q):
            bid_ids[best_ask - i, bid_n[best_ask - i]] = next_id
            bid_n[best_ask - i] += 1
            next_id += 1
            ask_ids[best_bid + i, ask_n[best_bid + i]] = next_id
            ask_n[best_bid + i] += 1
            next_id += 1

    ts = np.zeros(n_events, np.int64)
    typ = np.zeros(n_events, np.int8)
    oid = np.zeros(n_events, np.int64)
    price = np.zeros(n_events, np.int64)
    dirn = np.zeros(n_events, np.int8)
    ap = np.full((n_events, levels_out), DUMMY_ASK, np.int64)
    asz = np.zeros((n_events, levels_out), np.int64)
    bp = np.full((n_events, levels_out), DUMMY_BID, np.int64)
    bsz = np.zeros((n_events, levels_out), np.int64)

    lam_tot = lam.sum()
    t = 0.0
    k = 0
    while k < n_events:
        cb = 0.0
        for p in range(max(best_ask - W, 0), best_ask):
            if bid_n[p] > 0:
                cb += theta[min(best_ask - p, L) - 1] * bid_n[p]
        ca = 0.0
        for p in range(best_bid + 1, min(best_bid + W + 1, grid)):
            if ask_n[p] > 0:
                ca += theta[min(p - best_bid, L) - 1] * ask_n[p]
        R = 2.0 * lam_tot + 2.0 * mu + cb + ca
        t += np.random.exponential(1.0 / R)
        u = np.random.random() * R

        lvl = -1
        if u < lam_tot:                                       # limit buy
            p = best_ask - 1 - _pick(lam, u)
            if p < 0 or bid_n[p] >= cap:
                continue
            bid_ids[p, bid_n[p]] = next_id
            bid_n[p] += 1
            best_bid = max(best_bid, p)
            typ[k], oid[k], dirn[k], lvl = 1, next_id, 1, p
            next_id += 1
        elif u < 2 * lam_tot:                                 # limit sell
            p = best_bid + 1 + _pick(lam, u - lam_tot)
            if p >= grid or ask_n[p] >= cap:
                continue
            ask_ids[p, ask_n[p]] = next_id
            ask_n[p] += 1
            best_ask = min(best_ask, p)
            typ[k], oid[k], dirn[k], lvl = 1, next_id, -1, p
            next_id += 1
        elif u < 2 * lam_tot + mu:                            # market buy hits the ask
            p = best_ask
            if _count_from(ask_n, p, 1, grid) <= 1:
                continue                                      # never empty a side
            typ[k], oid[k], dirn[k], lvl = 4, ask_ids[p, 0], -1, p
            _remove_at(ask_ids, ask_n, p, 0)
        elif u < 2 * lam_tot + 2 * mu:                        # market sell hits the bid
            p = best_bid
            if _count_from(bid_n, p, -1, grid) <= 1:
                continue
            typ[k], oid[k], dirn[k], lvl = 4, bid_ids[p, 0], 1, p
            _remove_at(bid_ids, bid_n, p, 0)
        else:                                                 # cancellation
            u -= 2 * lam_tot + 2 * mu
            if u < cb:
                acc = 0.0
                p = -1
                for q in range(max(best_ask - W, 0), best_ask):
                    if bid_n[q] > 0:
                        acc += theta[min(best_ask - q, L) - 1] * bid_n[q]
                        if u < acc:
                            p = q
                            break
                if p < 0 or _count_from(bid_n, best_bid, -1, grid) <= 1:
                    continue
                pos = np.random.randint(0, bid_n[p])
                typ[k], oid[k], dirn[k], lvl = 3, bid_ids[p, pos], 1, p
                _remove_at(bid_ids, bid_n, p, pos)
            else:
                u -= cb
                acc = 0.0
                p = -1
                for q in range(best_bid + 1, min(best_bid + W + 1, grid)):
                    if ask_n[q] > 0:
                        acc += theta[min(q - best_bid, L) - 1] * ask_n[q]
                        if u < acc:
                            p = q
                            break
                if p < 0 or _count_from(ask_n, best_ask, 1, grid) <= 1:
                    continue
                pos = np.random.randint(0, ask_n[p])
                typ[k], oid[k], dirn[k], lvl = 3, ask_ids[p, pos], -1, p
                _remove_at(ask_ids, ask_n, p, pos)

        while bid_n[best_bid] == 0 and best_bid > 0:
            best_bid -= 1
        while ask_n[best_ask] == 0 and best_ask < grid - 1:
            best_ask += 1

        ts[k] = t0_ns + np.int64(t * 1e9)
        price[k] = (p0_ticks + lvl - c) * tick
        # post-event snapshot
        j = 0
        q = best_ask
        while j < levels_out and q < grid:
            if ask_n[q] > 0:
                ap[k, j] = (p0_ticks + q - c) * tick
                asz[k, j] = ask_n[q] * unit
                j += 1
            q += 1
        j = 0
        q = best_bid
        while j < levels_out and q >= 0:
            if bid_n[q] > 0:
                bp[k, j] = (p0_ticks + q - c) * tick
                bsz[k, j] = bid_n[q] * unit
                j += 1
            q -= 1
        k += 1
    return ts, typ, oid, price, dirn, ap, asz, bp, bsz


def simulate(n_events: int = 200_000, seed: int = 7, lam=None, mu=None, theta=None,
             unit: int = 100, start_price: float = 100.0, levels: int = 10,
             t0_s: float = 34200.0, ticker: str = "SYNTH", grid: int = 4000, cap: int = 512) -> LobsterDay:
    """Simulates ``n_events`` order book events and returns them as a LobsterDay."""
    d_lam, d_mu, d_theta = default_params(10)
    lam = np.asarray(d_lam if lam is None else lam, np.float64)
    theta = np.asarray(d_theta if theta is None else theta, np.float64)
    mu = float(d_mu if mu is None else mu)
    tick = 100
    p0_ticks = int(round(start_price * 10_000 / tick))
    ts, typ, oid, price, dirn, ap, asz, bp, bsz = _simulate(
        lam, mu, theta, n_events, seed, grid, cap, unit, p0_ticks, tick, int(t0_s * 1e9), levels)
    return LobsterDay(ticker, "2012-06-21", levels, ts, typ, oid,
                      np.full(n_events, unit, np.int64), price, dirn, ap, asz, bp, bsz, tick)
