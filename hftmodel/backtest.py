"""Event-driven market-making backtester that replays LOBSTER data.

Simulation model
----------------
* The market is replayed message by message (exchange time). Our orders are small
  and do not change the historical book (no market impact).
* Latency: the strategy sees book row i at ``ts[i] + feed_latency`` and its order
  reaches the exchange ``order_latency`` later. Replacing a quote is cancel + new;
  the old order keeps working until the replacement arrives.
* Post-only: a new order that would cross the book on arrival is rejected.
* Queue position: a new order joins the back of the visible queue at its price.
  The queue ahead shrinks by visible executions at that price and by cancellations,
  of which a fraction f = a^n / (a^n + b^n) is assumed to come from ahead of us
  (a = qty ahead, b = qty behind; the power "probabilistic queue model" also used
  by hftbacktest). We are filled by the part of an execution that exceeds the
  queue ahead.
* Trade-through: an execution or a new opposite limit order at a price better
  than ours fills our remaining quantity.
* Hidden executions (type 5) at our price are ignored (conservative).
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
from numba import njit

from .lobster import DUMMY_ASK, DUMMY_BID, LobsterDay


@dataclass
class BacktestConfig:
    order_qty: int = 100              # shares per quote
    feed_latency_ns: int = 50_000     # market data latency (exchange -> strategy)
    order_latency_ns: int = 50_000    # order entry latency (strategy -> exchange)
    min_requote_ns: int = 0           # throttle between quote updates
    join_queue: bool = True           # False: only post where the visible queue is empty (we are first);
                                      # existing orders are kept instead of rejoining at the back
    keep_behind_ticks: int = 0        # keep a resting order up to this many ticks *behind* the desired
                                      # price (retains queue priority); more aggressive -> always replace
    queue_power: float = 3.0          # n in the probabilistic queue model
    maker_fee: float = 0.0            # $ per share; negative = rebate
    record_interval_ns: int = 1_000_000_000
    sigma_halflife_s: float = 300.0   # EWMA half-life for the online sigma estimate
    sigma0: float = 1.0               # initial sigma (ticks / sqrt(s))


@njit(cache=True)
def _qty_at(px_row, sz_row, p):
    for j in range(px_row.shape[0]):
        if px_row[j] == p:
            return sz_row[j]
    return 0


@njit(cache=True)
def _prob_ahead(a, b, n):
    if a <= 0.0:
        return 0.0
    if b <= 0.0:
        return 1.0
    an = a ** n
    return an / (an + b ** n)


@njit(cache=True)
def _run(ts, typ, size, price, dirn, ask_px, ask_sz, bid_px, bid_sz, tick,
         kernel, fp, ip, lut_f, lut_i,
         qty, feed_lat, order_lat, min_requote, keep_behind, join_q, qpow, fee,
         rec_interval, sigma_alpha, sigma0, ext):
    n = ts.shape[0]
    # live orders
    b_live, b_px, b_rem, b_ahead = False, 0, 0, 0.0
    a_live, a_px, a_rem, a_ahead = False, 0, 0, 0.0
    b_t0, b_ahead0, a_t0, a_ahead0 = 0, 0.0, 0, 0.0      # placement time / initial queue ahead
    # pending replacements: px == -1 means cancel
    b_pend, b_pend_px, b_pend_t = False, 0, 0
    a_pend, a_pend_px, a_pend_t = False, 0, 0

    position = 0
    cash = 0.0            # LOBSTER price units * shares
    fees = 0.0            # dollars
    n_orders = 0
    n_cancels = 0
    last_quote_t = -(1 << 62)

    max_fills = n + 16
    f_t = np.zeros(max_fills, np.int64)
    f_side = np.zeros(max_fills, np.int8)
    f_px = np.zeros(max_fills, np.int64)
    f_qty = np.zeros(max_fills, np.int64)
    f_mid = np.zeros(max_fills, np.float64)
    # diagnostics per fill: reason (0 = execution at our price, 1 = traded through, 2 = incoming
    # marketable limit order), queue ahead when the order was placed, order age, top-of-book sizes
    f_reason = np.zeros(max_fills, np.int8)
    f_ahead0 = np.zeros(max_fills, np.float64)
    f_age = np.zeros(max_fills, np.int64)
    f_bq = np.zeros(max_fills, np.int64)
    f_aq = np.zeros(max_fills, np.int64)
    f_spread = np.zeros(max_fills, np.int64)
    nf = 0

    n_rec = int((ts[n - 1] - ts[0]) // rec_interval) + 2
    r_t = np.zeros(n_rec, np.int64)
    r_mid = np.zeros(n_rec, np.float64)
    r_pos = np.zeros(n_rec, np.int64)
    r_cash = np.zeros(n_rec, np.float64)
    r_bid = np.zeros(n_rec, np.int64)
    r_ask = np.zeros(n_rec, np.int64)
    r_sigma = np.zeros(n_rec, np.float64)
    nr = 0
    next_rec = ts[0]

    var = sigma0 * sigma0
    last_mid_s = -1.0
    rec_dt = rec_interval * 1e-9

    for i in range(1, n):
        t = ts[i]
        pb = i - 1                             # book before this message
        bb0, ba0 = bid_px[pb, 0], ask_px[pb, 0]
        book_ok0 = bb0 != DUMMY_BID and ba0 != DUMMY_ASK

        # ---- recording (state as of just before this message) -----------------------
        while next_rec <= t and nr < n_rec:
            if book_ok0:
                mid = 0.5 * (bb0 + ba0)
                if last_mid_s >= 0.0:
                    d = (mid - last_mid_s) / tick
                    var = (1.0 - sigma_alpha) * var + sigma_alpha * d * d / rec_dt
                last_mid_s = mid
            r_t[nr] = next_rec
            r_mid[nr] = 0.5 * (bb0 + ba0) if book_ok0 else np.nan
            r_pos[nr] = position
            r_cash[nr] = cash
            r_bid[nr] = b_px if b_live else -1
            r_ask[nr] = a_px if a_live else -1
            r_sigma[nr] = np.sqrt(var)
            nr += 1
            next_rec += rec_interval

        # ---- pending orders reach the exchange --------------------------------------
        if b_pend and b_pend_t <= t:
            b_pend = False
            if b_pend_px < 0:
                b_live = False
            elif book_ok0 and b_pend_px >= ba0:
                b_live = False                # post-only reject
            else:
                b_live, b_px, b_rem = True, b_pend_px, qty
                b_ahead = float(_qty_at(bid_px[pb], bid_sz[pb], b_px))
                b_t0, b_ahead0 = t, b_ahead
                n_orders += 1
        if a_pend and a_pend_t <= t:
            a_pend = False
            if a_pend_px < 0:
                a_live = False
            elif book_ok0 and a_pend_px <= bb0:
                a_live = False
            else:
                a_live, a_px, a_rem = True, a_pend_px, qty
                a_ahead = float(_qty_at(ask_px[pb], ask_sz[pb], a_px))
                a_t0, a_ahead0 = t, a_ahead
                n_orders += 1

        # ---- apply message i to our orders ------------------------------------------
        ty, sz, p, dr = typ[i], size[i], price[i], dirn[i]
        mid_before = 0.5 * (bb0 + ba0)
        if ty == 4 or ty == 5:
            if dr == 1 and b_live:            # a bid was hit (seller-initiated)
                fill = 0
                if p < b_px:
                    fill = b_rem
                elif p == b_px and ty == 4:
                    if sz > b_ahead:
                        fill = min(b_rem, int(sz - b_ahead))
                    b_ahead = max(b_ahead - sz, 0.0)
                if fill > 0:
                    position += fill
                    cash -= fill * b_px
                    fees += fill * fee
                    b_rem -= fill
                    f_t[nf], f_side[nf], f_px[nf], f_qty[nf], f_mid[nf] = t, 1, b_px, fill, mid_before
                    f_reason[nf] = 1 if p < b_px else 0
                    f_ahead0[nf], f_age[nf] = b_ahead0, t - b_t0
                    f_bq[nf], f_aq[nf], f_spread[nf] = bid_sz[pb, 0], ask_sz[pb, 0], (ba0 - bb0) // tick
                    nf += 1
                    if b_rem == 0:
                        b_live = False
            elif dr == -1 and a_live:         # an ask was lifted (buyer-initiated)
                fill = 0
                if p > a_px:
                    fill = a_rem
                elif p == a_px and ty == 4:
                    if sz > a_ahead:
                        fill = min(a_rem, int(sz - a_ahead))
                    a_ahead = max(a_ahead - sz, 0.0)
                if fill > 0:
                    position -= fill
                    cash += fill * a_px
                    fees += fill * fee
                    a_rem -= fill
                    f_t[nf], f_side[nf], f_px[nf], f_qty[nf], f_mid[nf] = t, -1, a_px, fill, mid_before
                    f_reason[nf] = 1 if p > a_px else 0
                    f_ahead0[nf], f_age[nf] = a_ahead0, t - a_t0
                    f_bq[nf], f_aq[nf], f_spread[nf] = bid_sz[pb, 0], ask_sz[pb, 0], (ba0 - bb0) // tick
                    nf += 1
                    if a_rem == 0:
                        a_live = False
        elif ty == 2 or ty == 3:
            if dr == 1 and b_live and p == b_px:
                lvl = float(_qty_at(bid_px[pb], bid_sz[pb], p))
                f = _prob_ahead(b_ahead, lvl - b_ahead, qpow)
                b_ahead = max(b_ahead - sz * f, 0.0)
            elif dr == -1 and a_live and p == a_px:
                lvl = float(_qty_at(ask_px[pb], ask_sz[pb], p))
                f = _prob_ahead(a_ahead, lvl - a_ahead, qpow)
                a_ahead = max(a_ahead - sz * f, 0.0)
        elif ty == 1:
            # an incoming limit order that would be marketable against our quote
            if dr == -1 and b_live and p <= b_px:
                fill = min(b_rem, sz)
                position += fill
                cash -= fill * b_px
                fees += fill * fee
                b_rem -= fill
                f_t[nf], f_side[nf], f_px[nf], f_qty[nf], f_mid[nf] = t, 1, b_px, fill, mid_before
                f_reason[nf], f_ahead0[nf], f_age[nf] = 2, b_ahead0, t - b_t0
                f_bq[nf], f_aq[nf], f_spread[nf] = bid_sz[pb, 0], ask_sz[pb, 0], (ba0 - bb0) // tick
                nf += 1
                if b_rem == 0:
                    b_live = False
            elif dr == 1 and a_live and p >= a_px:
                fill = min(a_rem, sz)
                position -= fill
                cash += fill * a_px
                fees += fill * fee
                a_rem -= fill
                f_t[nf], f_side[nf], f_px[nf], f_qty[nf], f_mid[nf] = t, -1, a_px, fill, mid_before
                f_reason[nf], f_ahead0[nf], f_age[nf] = 2, a_ahead0, t - a_t0
                f_bq[nf], f_aq[nf], f_spread[nf] = bid_sz[pb, 0], ask_sz[pb, 0], (ba0 - bb0) // tick
                nf += 1
                if a_rem == 0:
                    a_live = False

        # queue ahead can never exceed what is visible at our price
        if b_live:
            b_ahead = min(b_ahead, float(_qty_at(bid_px[i], bid_sz[i], b_px)))
        if a_live:
            a_ahead = min(a_ahead, float(_qty_at(ask_px[i], ask_sz[i], a_px)))

        # ---- strategy decision on the post-message book ------------------------------
        bb, ba = bid_px[i, 0], ask_px[i, 0]
        if bb == DUMMY_BID or ba == DUMMY_ASK or t - last_quote_t < min_requote:
            continue
        inv_lots = int(round(position / qty))
        qb, qa = kernel(t, bb // tick, ba // tick, bid_sz[i, 0], ask_sz[i, 0], inv_lots,
                        np.sqrt(var), fp, ip, lut_f, lut_i)
        # external signal known at this row (e.g. SPY just moved): -1 pulls the bid, +1 the ask
        if ext[i] < 0:
            qb = -1
        elif ext[i] > 0:
            qa = -1
        want_b = qb * tick if qb >= 0 else -1
        want_a = qa * tick if qa >= 0 else -1
        arrive = t + feed_lat + order_lat
        work_b = b_pend_px if b_pend else (b_px if b_live else -1)
        work_a = a_pend_px if a_pend else (a_px if a_live else -1)
        if not join_q:
            # queue-priority mode: never join the back of an existing queue. If the wanted price
            # already has visible size, keep a less aggressive resting order (it keeps its
            # priority) or do nothing; a more aggressive one is cancelled for risk.
            if want_b >= 0 and want_b != work_b and _qty_at(bid_px[i], bid_sz[i], want_b) > 0:
                want_b = work_b if (work_b >= 0 and work_b < want_b) else -1
            if want_a >= 0 and want_a != work_a and _qty_at(ask_px[i], ask_sz[i], want_a) > 0:
                want_a = work_a if (work_a >= 0 and work_a > want_a) else -1
        changed = False
        if (want_b < 0) != (work_b < 0) or (want_b >= 0 and (work_b > want_b or want_b - work_b > keep_behind * tick)):
            if want_b < 0 or b_live:
                n_cancels += 1
            b_pend, b_pend_px, b_pend_t = True, want_b, arrive
            changed = True
        if (want_a < 0) != (work_a < 0) or (want_a >= 0 and (work_a < want_a or work_a - want_a > keep_behind * tick)):
            if want_a < 0 or a_live:
                n_cancels += 1
            a_pend, a_pend_px, a_pend_t = True, want_a, arrive
            changed = True
        if changed:
            last_quote_t = t

    stats = np.array([position, cash, fees, n_orders, n_cancels, nf], np.float64)
    return (stats, f_t[:nf], f_side[:nf], f_px[:nf], f_qty[:nf], f_mid[:nf],
            f_reason[:nf], f_ahead0[:nf], f_age[:nf], f_bq[:nf], f_aq[:nf], f_spread[:nf],
            r_t[:nr], r_mid[:nr], r_pos[:nr], r_cash[:nr], r_bid[:nr], r_ask[:nr], r_sigma[:nr])


@dataclass
class BacktestResult:
    ticker: str
    tick: int
    fills: dict
    records: dict
    final_position: int
    final_cash: float           # LOBSTER units * shares
    fees: float                 # dollars
    n_orders: int
    n_cancels: int
    last_bid: int
    last_ask: int
    meta: dict = field(default_factory=dict)

    @property
    def pnl_mtm(self) -> float:
        """Dollar PnL marking the final position at the last mid, net of fees."""
        mid = 0.5 * (self.last_bid + self.last_ask)
        return (self.final_cash + self.final_position * mid) / 10_000 - self.fees

    @property
    def pnl_liquidated(self) -> float:
        """Dollar PnL after crossing the spread to flatten the final position."""
        px = self.last_bid if self.final_position > 0 else self.last_ask
        return (self.final_cash + self.final_position * px) / 10_000 - self.fees


def run(day: LobsterDay, kernel, fp: np.ndarray, ip: np.ndarray | None = None,
        lut_f: np.ndarray | None = None, lut_i: np.ndarray | None = None,
        cfg: BacktestConfig | None = None, ext: np.ndarray | None = None) -> BacktestResult:
    """Replays ``day`` through a quote kernel.

    ``ext`` (optional, int8 per row): an external defensive signal observed at that row's
    time. -1 cancels/suppresses the bid, +1 the ask (see :mod:`hftmodel.signals`). Rows with
    message type 0 are signal-only rows that carry the previous book and trigger no fills.
    """
    cfg = cfg or BacktestConfig()
    ext = np.zeros(len(day), np.int8) if ext is None else ext.astype(np.int8)
    ip = np.zeros(1, np.int64) if ip is None else ip
    lut_f = np.full((2, 2), 0.5) if lut_f is None else lut_f
    lut_i = np.zeros((2, 2), np.int64) if lut_i is None else lut_i
    alpha = 1.0 - 0.5 ** ((cfg.record_interval_ns / 1e9) / cfg.sigma_halflife_s)
    out = _run(day.ts, day.type, day.size, day.price, day.direction,
               day.ask_px, day.ask_sz, day.bid_px, day.bid_sz, day.tick,
               kernel, fp.astype(np.float64), ip.astype(np.int64), lut_f.astype(np.float64), lut_i.astype(np.int64),
               cfg.order_qty, cfg.feed_latency_ns, cfg.order_latency_ns, cfg.min_requote_ns, cfg.keep_behind_ticks, cfg.join_queue,
               cfg.queue_power, cfg.maker_fee, cfg.record_interval_ns, alpha, cfg.sigma0, ext)
    stats = out[0]
    fills = dict(zip(["t", "side", "px", "qty", "mid", "reason", "ahead0", "age_ns", "bq", "aq", "spread"], out[1:12]))
    recs = dict(zip(["t", "mid", "pos", "cash", "bid", "ask", "sigma"], out[12:19]))
    valid = (day.bid_px[:, 0] != DUMMY_BID) & (day.ask_px[:, 0] != DUMMY_ASK)
    last = np.flatnonzero(valid)[-1]
    return BacktestResult(day.ticker, day.tick, fills, recs, int(stats[0]), float(stats[1]), float(stats[2]),
                          int(stats[3]), int(stats[4]), int(day.bid_px[last, 0]), int(day.ask_px[last, 0]))
