"""FPGA preparation: test vectors from the fixed-point golden model + a CPU latency baseline.

The fixed-point kernel :func:`hftmodel.strategy.as_cst_quote_fx` *is* the
specification of the hardware quote engine (``fpga/hls/quote_engine.cpp``).

Everything goes through ONE AXI-Stream in and ONE AXI-Stream out, so on the KV260
the engine only needs an AXI DMA. Every input packet yields exactly one output packet.

Input packet, 256 bits = 8 x 32-bit words (word 0 = bits 31:0 = lowest address):
    w0  header: bits 7:0 = type, bits 31:8 = sequence number (echoed back)
    type 0 BOOK : w1 bid_tick, w2 ask_tick, w3 bid_qty, w4 ask_qty, w5 inv_lots (signed)
    type 1 PARAM: w1 C_INV, w2 C_HALF, w3 Q_RECIP, w4 MAX_POS   (signed 32-bit, Q16 where relevant)
    type 2 LUT  : w1 address = (x << 5) | y, w2 = (skew_Q16 << 2) | gate  (gate bit0 = bid ok, bit1 = ask ok)
Output packet, 128 bits = 4 x 32-bit words:
    w0  header echo, w1 bid_tick, w2 ask_tick (-1 = no quote), w3 reserved (0)
    config packets return bid = ask = 0 as an acknowledgement.

Files written by :func:`export_vectors`:
    stream_in.hex / stream_out.hex   one packet per line, most significant word first
    stream_in.npy / stream_out.npy   uint32 arrays (N, 8) / (N, 4), for the PYNQ driver
    vectors.csv                      book packets in decimal with timestamps
    params.json                      register values
"""
from __future__ import annotations

import os
import time

import numpy as np

from .backtest import BacktestResult
from .lobster import DUMMY_ASK, DUMMY_BID, LobsterDay
from .strategy import (I_C_HALF, I_C_INV, I_MAX_POS, I_Q_RECIP, LUT_BITS, TICK_MAX, as_cst_quote,
                       as_cst_quote_fx)

TYPE_BOOK, TYPE_PARAM, TYPE_LUT = 0, 1, 2


def _h32(v: int) -> str:
    return f"{int(v) & 0xFFFFFFFF:08X}"


def tob_updates(day: LobsterDay) -> np.ndarray:
    """Row indices where the top of book (prices or sizes) changes and both sides exist."""
    ok = (day.bid_px[:, 0] != DUMMY_BID) & (day.ask_px[:, 0] != DUMMY_ASK)
    tob = np.stack([day.bid_px[:, 0], day.ask_px[:, 0], day.bid_sz[:, 0], day.ask_sz[:, 0]], axis=1)
    changed = np.ones(len(day), bool)
    changed[1:] = np.any(tob[1:] != tob[:-1], axis=1)
    return np.flatnonzero(ok & changed)


def inventory_path(day: LobsterDay, res: BacktestResult, rows: np.ndarray, lot: int) -> np.ndarray:
    """Inventory in lots at each row, reconstructed from the backtest fills."""
    f = res.fills
    cum = np.cumsum(f["side"].astype(np.int64) * f["qty"])
    j = np.searchsorted(f["t"], day.ts[rows], side="right") - 1
    pos = np.where(j >= 0, cum[np.maximum(j, 0)], 0)
    return np.round(pos / lot).astype(np.int64)


def build_stream(bb, ba, bq, aq, inv, ip, lut):
    """Builds the (input, expected output) packet arrays: config packets first, then book updates."""
    n_lut = 1 << (2 * LUT_BITS)
    n = 1 + n_lut + len(bb)
    pin = np.zeros((n, 8), np.int64)
    pout = np.zeros((n, 4), np.int64)
    seq = np.arange(n) & 0xFFFFFF
    pin[:, 0] = seq << 8
    pin[0, 0] |= TYPE_PARAM
    pin[0, 1:5] = ip[I_C_INV], ip[I_C_HALF], ip[I_Q_RECIP], ip[I_MAX_POS]
    side = 1 << LUT_BITS
    lut_full = np.zeros((side, side), np.int64)
    lut_full[:lut.shape[0], :lut.shape[1]] = lut
    rows = slice(1, 1 + n_lut)
    pin[rows, 0] |= TYPE_LUT
    pin[rows, 1] = np.arange(n_lut)
    pin[rows, 2] = lut_full.ravel()
    b = slice(1 + n_lut, n)
    pin[b, 0] |= TYPE_BOOK
    pin[b, 1], pin[b, 2], pin[b, 3], pin[b, 4], pin[b, 5] = bb, ba, bq, aq, inv
    pout[:, 0] = pin[:, 0]
    dummy_f, lut_f = np.zeros(1), np.zeros((2, 2))
    for k in range(len(bb)):
        pout[1 + n_lut + k, 1:3] = as_cst_quote_fx(0, bb[k], ba[k], bq[k], aq[k], inv[k], 0.0, dummy_f, ip, lut_f, lut)
    return (pin & 0xFFFFFFFF).astype(np.uint32), (pout & 0xFFFFFFFF).astype(np.uint32)


def _hexline(words) -> str:
    return "".join(f"{int(w):08X}" for w in words[::-1])


def export_vectors(day: LobsterDay, res: BacktestResult, ip: np.ndarray, lut: np.ndarray, lot: int,
                   out_dir: str, max_vectors: int | None = None) -> dict:
    os.makedirs(out_dir, exist_ok=True)
    rows = tob_updates(day)
    if max_vectors:
        rows = rows[:max_vectors]
    inv = inventory_path(day, res, rows, lot)
    bb = day.bid_px[rows, 0] // day.tick
    ba = day.ask_px[rows, 0] // day.tick
    bq = day.bid_sz[rows, 0]
    aq = day.ask_sz[rows, 0]
    if max(bb.max(), ba.max()) > TICK_MAX or max(bq.max(), aq.max()) >= 1 << 31:
        raise ValueError("price above TICK_MAX ticks or size above 32 bits")
    pin, pout = build_stream(bb, ba, bq, aq, inv, ip, lut)
    np.save(os.path.join(out_dir, "stream_in.npy"), pin)
    np.save(os.path.join(out_dir, "stream_out.npy"), pout)
    with open(os.path.join(out_dir, "stream_in.hex"), "w") as f:
        f.writelines(_hexline(w) + "\n" for w in pin)
    with open(os.path.join(out_dir, "stream_out.hex"), "w") as f:
        f.writelines(_hexline(w) + "\n" for w in pout)
    n_cfg = len(pin) - len(rows)
    with open(os.path.join(out_dir, "vectors.csv"), "w") as fc:
        fc.write("ts_ns,bid_tick,ask_tick,bid_qty,ask_qty,inv_lots,out_bid,out_ask\n")
        for k in range(len(rows)):
            o = pout[n_cfg + k].view(np.int32)
            fc.write(f"{day.ts[rows[k]]},{bb[k]},{ba[k]},{bq[k]},{aq[k]},{inv[k]},{o[1]},{o[2]}\n")
    quoted = pout[n_cfg:].view(np.int32)
    return {"packets": int(len(pin)), "config_packets": int(n_cfg), "book_packets": int(len(rows)), "dir": out_dir,
            "max_price_tick": int(max(bb.max(), ba.max())), "max_qty": int(max(bq.max(), aq.max())),
            "inv_range": [int(inv.min()), int(inv.max())],
            "pct_bid_gated": round(100 * float(np.mean(quoted[:, 1] < 0)), 3)}


def compare_float_fixed(day: LobsterDay, fp: np.ndarray, p_up: np.ndarray, ip: np.ndarray, lut: np.ndarray,
                        lot: int, max_rows: int = 200_000, seed: int = 0) -> dict:
    """Compares the fixed-point quotes with the float kernel on real top-of-book states.

    * ``quantization_mismatch_pct``: float kernel with the exact P_up table and queue unit.
      Differences come from Q16 rounding of the LUT/unit. They show up when the float
      quote sits exactly on a tick boundary and a sub-1e-5-tick skew tips it over.
    * ``arithmetic_mismatch_pct``: float kernel fed the *dequantized* LUT and unit, i.e. the
      same model the hardware implements. This must be ~0; anything else is a datapath bug.
    """
    from .strategy import F_ALPHA, F_UNIT, FX_FRAC, RECIP_SHIFT
    rows = tob_updates(day)[:max_rows]
    rng = np.random.default_rng(seed)
    inv = rng.integers(-int(ip[I_MAX_POS]), int(ip[I_MAX_POS]) + 1, len(rows))
    fp_q = fp.copy()
    fp_q[F_UNIT] = (1 << RECIP_SHIFT) / ip[I_Q_RECIP]
    alpha = fp[F_ALPHA]
    p_q = ((lut >> 2) / (1 << FX_FRAC) / alpha + 1.0) / 2.0 if alpha else p_up
    raw = arith = max_d = 0
    for n, r in enumerate(rows):
        args = (0, day.bid_px[r, 0] // day.tick, day.ask_px[r, 0] // day.tick,
                day.bid_sz[r, 0], day.ask_sz[r, 0], int(inv[n]), float(fp[2]))
        fx = as_cst_quote_fx(*args, fp, ip, p_up, lut)
        fl = as_cst_quote(*args, fp, ip, p_up, lut)
        fq = as_cst_quote(*args, fp_q, ip, p_q, lut)
        if fl != fx:
            raw += 1
            max_d = max(max_d, abs(fl[0] - fx[0]), abs(fl[1] - fx[1]))
        if fq != fx:
            arith += 1
    n = max(len(rows), 1)
    return {"states": int(len(rows)), "quantization_mismatch_pct": round(100 * raw / n, 4),
            "max_tick_diff": int(max_d), "arithmetic_mismatch_pct": round(100 * arith / n, 4)}


def cpu_latency(day: LobsterDay, kernel, fp, ip, lut_f, lut_i, n: int = 200_000) -> dict:
    """Per-call latency of a kernel on this CPU (numba), the software baseline for the FPGA."""
    from numba import njit

    @njit
    def bench(kernel, bb, ba, bq, aq, fp, ip, lut_f, lut_i):
        acc = 0
        for i in range(bb.shape[0]):
            b, a = kernel(0, bb[i], ba[i], bq[i], aq[i], i % 3 - 1, 1.0, fp, ip, lut_f, lut_i)
            acc += b + a
        return acc

    rows = tob_updates(day)[:n]
    bb = day.bid_px[rows, 0] // day.tick
    ba = day.ask_px[rows, 0] // day.tick
    bq, aq = day.bid_sz[rows, 0].copy(), day.ask_sz[rows, 0].copy()
    bench(kernel, bb[:10], ba[:10], bq[:10], aq[:10], fp, ip, lut_f, lut_i)   # compile
    t = time.perf_counter_ns()
    bench(kernel, bb, ba, bq, aq, fp, ip, lut_f, lut_i)
    dt = time.perf_counter_ns() - t
    return {"calls": int(len(rows)), "ns_per_call": round(dt / max(len(rows), 1), 2)}
