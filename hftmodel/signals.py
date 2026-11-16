"""Cross-asset signals for the backtester (SPY -> member stock lead-lag).

A SPY *trigger* fires when SPY's mid price has moved by at least ``k`` SPY ticks over the
trailing ``window_ms``. The defensive signal for a stock is the direction of the latest
trigger while it is younger than ``hold_ms`` (the stock's quote typically needs 10-15 ms
to catch up), and 0 otherwise.

``with_signal`` inserts *signal-only rows* (message type 0, book copied from the previous
row) at every trigger start and expiry, so the backtester can react at SPY time instead of
waiting for the stock's next message. That is the point of a defensive cancel: the stale
quote has to be gone before the stock's next message, which is often the sniper's trade.
"""
from __future__ import annotations

import numpy as np

from .lobster import DUMMY_ASK, DUMMY_BID, LobsterDay

MS = 1_000_000


def spy_triggers(spy: LobsterDay, window_ms: float, k_ticks: float):
    """Times (ns) and directions (+1/-1) of SPY moves >= k ticks within the trailing window."""
    ok = (spy.bid_px[:, 0] != DUMMY_BID) & (spy.ask_px[:, 0] != DUMMY_ASK)
    ts, m2 = spy.ts[ok], spy.bid_px[ok, 0] + spy.ask_px[ok, 0]
    ch = np.flatnonzero(np.diff(m2) != 0) + 1
    t = ts[ch]
    j = np.clip(np.searchsorted(ts, t - int(window_ms * MS), side="right") - 1, 0, len(ts) - 1)
    mv = (m2[ch] - m2[j]) / (2 * spy.tick)
    trig = np.abs(mv) >= k_ticks
    return t[trig], np.sign(mv[trig]).astype(np.int8)


def with_signal(day: LobsterDay, trig_t: np.ndarray, trig_d: np.ndarray, hold_ms: float):
    """Returns (merged day with signal-only rows, ext array aligned with it)."""
    hold = int(hold_ms * MS)
    t0, t1 = day.ts[0], day.ts[-1]
    keep = (trig_t > t0) & (trig_t < t1)
    trig_t, trig_d = trig_t[keep], trig_d[keep]
    new_t = np.concatenate([trig_t, trig_t + hold])
    new_t = new_t[new_t < t1]
    n0, n1 = len(day), len(new_t)
    ts = np.concatenate([day.ts, new_t])
    # stable sort: a signal row at the same timestamp as a message comes after it
    order = np.argsort(np.concatenate([day.ts * 2, new_t * 2 + 1]), kind="stable")
    is_sig = order >= n0
    src = np.where(is_sig, -1, order)
    # book for a signal row = book of the last real message before it
    last_real = np.maximum.accumulate(np.where(is_sig, -1, np.arange(len(order))))
    book_src = np.where(is_sig, order[np.maximum(last_real, 0)], order)
    book_src = np.where((is_sig) & (last_real < 0), 0, book_src)

    def pick(a, fill):
        out = a[np.clip(src, 0, n0 - 1)].copy()
        out[is_sig] = fill
        return out

    merged = LobsterDay(day.ticker, day.date, day.levels, ts[order],
                        pick(day.type, 0), pick(day.order_id, 0), pick(day.size, 0), pick(day.price, 0),
                        pick(day.direction, 0),
                        day.ask_px[book_src], day.ask_sz[book_src], day.bid_px[book_src], day.bid_sz[book_src],
                        day.tick)
    # signal state at each merged row: direction of the latest trigger if younger than hold
    i = np.searchsorted(trig_t, merged.ts, side="right") - 1
    active = (i >= 0) & (merged.ts - trig_t[np.maximum(i, 0)] < hold)
    ext = np.where(active, trig_d[np.maximum(i, 0)], 0).astype(np.int8)
    return merged, ext
