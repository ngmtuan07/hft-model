"""Bridge to hftbacktest (https://github.com/nkaz001/hftbacktest).

``convert`` turns a LobsterDay into hftbacktest's event array (L2 market-by-price
depth updates + trades). ``run_as_cst`` runs the same quote kernel inside
hftbacktest so results can be cross-checked against :mod:`hftmodel.backtest`
with an independent fill/queue/latency simulator.

Depth events are the set difference of consecutive LOBSTER snapshots, so only
the visible ``levels`` are reproduced. Levels that scroll out of view keep their
last quantity (they are far from the touch and do not matter for quoting).
"""
from __future__ import annotations

import numpy as np
from numba import njit

from hftbacktest import (BacktestAsset, HashMapMarketDepthBacktest, Recorder, BUY, SELL, GTX, LIMIT,
                         DEPTH_EVENT, TRADE_EVENT, EXCH_EVENT, LOCAL_EVENT, BUY_EVENT, SELL_EVENT)
from hftbacktest.binding import event_dtype

from .lobster import DUMMY_ASK, DUMMY_BID, EXEC_HIDDEN, EXEC_VISIBLE, LobsterDay, PRICE_SCALE

_BID_EV = EXCH_EVENT | LOCAL_EVENT | DEPTH_EVENT | BUY_EVENT
_ASK_EV = EXCH_EVENT | LOCAL_EVENT | DEPTH_EVENT | SELL_EVENT
_TRADE_BUY = EXCH_EVENT | LOCAL_EVENT | TRADE_EVENT | BUY_EVENT
_TRADE_SELL = EXCH_EVENT | LOCAL_EVENT | TRADE_EVENT | SELL_EVENT


@njit(cache=True)
def _in(px_row, p):
    for j in range(px_row.shape[0]):
        if px_row[j] == p:
            return j
    return -1


@njit(cache=True)
def _diff_side(prev_px, prev_sz, cur_px, cur_sz, dummy, is_bid, out_ev, out_px, out_qty, k, ev):
    L = cur_px.shape[0]
    # deepest visible price in the current snapshot (if the side has L levels)
    full = cur_px[L - 1] != dummy
    deepest = cur_px[L - 1]
    for j in range(L):
        p = cur_px[j]
        if p == dummy:
            break
        jp = _in(prev_px, p)
        if jp < 0 or prev_sz[jp] != cur_sz[j]:
            out_ev[k], out_px[k], out_qty[k] = ev, p, cur_sz[j]
            k += 1
    for j in range(L):
        p = prev_px[j]
        if p == dummy:
            break
        if _in(cur_px, p) < 0:
            out_of_view = full and ((p < deepest) if is_bid else (p > deepest))
            if not out_of_view:
                out_ev[k], out_px[k], out_qty[k] = ev, p, 0
                k += 1
    return k


@njit(cache=True)
def _convert(ts, typ, size, price, dirn, ask_px, ask_sz, bid_px, bid_sz):
    n, L = ask_px.shape
    cap = n * (4 * L + 1) + 2 * L
    ev = np.zeros(cap, np.uint64)
    t = np.zeros(cap, np.int64)
    px = np.zeros(cap, np.int64)
    qty = np.zeros(cap, np.int64)
    k = 0
    # initial book = first snapshot
    for j in range(L):
        if bid_px[0, j] != DUMMY_BID:
            ev[k], t[k], px[k], qty[k] = _BID_EV, ts[0], bid_px[0, j], bid_sz[0, j]
            k += 1
        if ask_px[0, j] != DUMMY_ASK:
            ev[k], t[k], px[k], qty[k] = _ASK_EV, ts[0], ask_px[0, j], ask_sz[0, j]
            k += 1
    for i in range(1, n):
        k0 = k
        if typ[i] == EXEC_VISIBLE or typ[i] == EXEC_HIDDEN:
            # direction is the resting order's side: a hit bid is a seller-initiated trade
            ev[k] = _TRADE_SELL if dirn[i] == 1 else _TRADE_BUY
            px[k], qty[k] = price[i], size[i]
            k += 1
        k = _diff_side(bid_px[i - 1], bid_sz[i - 1], bid_px[i], bid_sz[i], DUMMY_BID, True, ev, px, qty, k, _BID_EV)
        k = _diff_side(ask_px[i - 1], ask_sz[i - 1], ask_px[i], ask_sz[i], DUMMY_ASK, False, ev, px, qty, k, _ASK_EV)
        for j in range(k0, k):
            t[j] = ts[i]
    return ev[:k], t[:k], px[:k], qty[:k]


def convert(day: LobsterDay, feed_latency_ns: int = 50_000, output: str | None = None) -> np.ndarray:
    """LobsterDay -> hftbacktest event array (prices in dollars, quantities in shares)."""
    ev, t, px, qty = _convert(day.ts, day.type, day.size, day.price, day.direction,
                              day.ask_px, day.ask_sz, day.bid_px, day.bid_sz)
    data = np.zeros(len(ev), event_dtype)
    data["ev"] = ev
    data["exch_ts"] = t
    data["local_ts"] = t + feed_latency_ns
    data["px"] = px / PRICE_SCALE
    data["qty"] = qty
    if output:
        np.savez_compressed(output, data=data)
    return data


@njit
def _as_cst_loop(hbt, rec, kernel, fp, ip, lut_f, lut_i, interval_ns, qty, tick_px):
    asset = 0
    next_id = 1
    err = 0
    while True:
        rc = hbt.elapse(interval_ns)
        if rc != 0:
            err = rc
            break
        hbt.clear_inactive_orders(asset)
        depth = hbt.depth(asset)
        bb, ba = depth.best_bid_tick, depth.best_ask_tick
        if bb <= 0 or ba <= 0 or ba <= bb or bb > 1 << 40 or ba > 1 << 40:
            continue
        pos = hbt.position(asset)
        inv_lots = int(round(pos / qty))
        new_b, new_a = kernel(hbt.current_timestamp, bb, ba, int(depth.best_bid_qty), int(depth.best_ask_qty),
                              inv_lots, fp[2], fp, ip, lut_f, lut_i)
        have_b = False
        have_a = False
        orders = hbt.orders(asset)
        it = orders.values()
        while it.has_next():
            o = it.get()
            keep = (o.side == BUY and o.price_tick == new_b) or (o.side == SELL and o.price_tick == new_a)
            if keep:
                if o.side == BUY:
                    have_b = True
                else:
                    have_a = True
            elif o.cancellable:
                hbt.cancel(asset, o.order_id, False)
            else:
                # an in-flight request on this side; wait for it before sending another order
                if o.side == BUY:
                    have_b = True
                else:
                    have_a = True
        if new_b > 0 and not have_b:
            hbt.submit_buy_order(asset, next_id, new_b * tick_px, qty, GTX, LIMIT, False)
            next_id += 1
        if new_a > 0 and not have_a:
            hbt.submit_sell_order(asset, next_id, new_a * tick_px, qty, GTX, LIMIT, False)
            next_id += 1
        rec.record(hbt)
    return err


def run_as_cst(data: np.ndarray, kernel, fp, ip=None, lut_f=None, lut_i=None, tick_px: float = 0.01,
               qty: float = 100, interval_ns: int = 100_000_000, order_latency_ns: int = 50_000,
               maker_fee: float = 0.0, queue: str = "power3", partial_fill: bool = False):
    """Runs a quote kernel inside hftbacktest. Returns the recorder's records (numpy structured array)."""
    ip = np.zeros(1, np.int64) if ip is None else ip
    lut_f = np.full((2, 2), 0.5) if lut_f is None else lut_f
    lut_i = np.zeros((2, 2), np.int64) if lut_i is None else lut_i
    asset = (BacktestAsset()
             .data([data])
             .linear_asset(1.0)
             .constant_order_latency(order_latency_ns, order_latency_ns)
             .tick_size(tick_px)
             .lot_size(1.0)
             .trading_value_fee_model(0.0, 0.0)
             .last_trades_capacity(0))
    # no_partial_fill_exchange: our order fills fully once the queue ahead is consumed.
    # (partial_fill_exchange can abort with InvalidOrderStatus on LOBSTER-derived L2 data.)
    asset = asset.partial_fill_exchange() if partial_fill else asset.no_partial_fill_exchange()
    asset = asset.power_prob_queue_model(3.0) if queue == "power3" else asset.risk_adverse_queue_model()
    if maker_fee:
        asset = asset.trading_qty_fee_model(maker_fee, 0.0)
    hbt = HashMapMarketDepthBacktest([asset])
    span = int(data["exch_ts"][-1] - data["exch_ts"][0])
    recorder = Recorder(1, span // interval_ns + 10)
    err = _as_cst_loop(hbt, recorder.recorder, kernel, fp, ip, lut_f, lut_i, interval_ns, float(qty), tick_px)
    hbt.close()
    if err != 1:  # 1 = end of data
        raise RuntimeError(f"hftbacktest stopped with code {err}")
    return recorder.get(0)


def summarize_records(rec: np.ndarray) -> dict:
    equity = rec["balance"] + rec["position"] * rec["price"] - rec["fee"]
    return {
        "pnl_mtm_$": round(float(equity[-1]), 2),
        "num_trades": int(rec["num_trades"][-1]),
        "volume_shares": float(rec["trading_volume"][-1]),
        "final_position": float(rec["position"][-1]),
        "max_abs_position": float(np.abs(rec["position"]).max()),
    }
