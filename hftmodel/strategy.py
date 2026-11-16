"""Quote kernels: Avellaneda–Stoikov market making with a Cont–Stoikov–Talreja skew.

A kernel maps the observed top of book and our inventory to a bid and ask price.
Everything is in integer ticks and inventory lots, so the same function can be
implemented in hardware.

Avellaneda & Stoikov (2008), with horizon tau = T - t (or a constant):

    reservation price  r      = s - q * gamma * sigma^2 * tau
    optimal spread     d_a+d_b = gamma * sigma^2 * tau + (2 / gamma) * ln(1 + gamma / k)

CST signal: with a one-tick spread, P_up = P(next mid move is up | bid queue,
ask queue) from :mod:`hftmodel.cst`. The reservation price is shifted by
alpha * (2 * P_up - 1) ticks, so we lean our quotes in the predicted direction.

Kernel signature (shared by every kernel so the backtester can take any of them):

    kernel(t_ns, bid_tick, ask_tick, bid_qty, ask_qty, inv_lots, sigma, fp, ip, lut_f, lut_i)
        -> (bid_tick or -1, ask_tick or -1)

``fp``/``ip`` are float/int parameter vectors, ``lut_f``/``lut_i`` float/int lookup
tables. -1 means "no quote on this side".
"""
from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
from numba import njit

# ---- float parameter vector layout --------------------------------------------------
F_GAMMA, F_K, F_SIGMA, F_TAU, F_T_END, F_ALPHA, F_UNIT, F_MAX_POS, F_ONLINE_SIGMA, F_MIN_HALF, F_GATE = range(11)
N_F = 11
# ---- int parameter vector layout (fixed point kernel) -------------------------------
I_C_INV, I_C_HALF, I_Q_RECIP, I_MAX_POS, I_NQ, I_FRAC = range(6)
N_I = 6
FX_FRAC = 16          # fractional bits of the fixed-point datapath (compile-time constant in hardware)
RECIP_SHIFT = 24      # queue quantisation: x = (q * Q_RECIP + 2^23) >> 24, Q_RECIP = round(2^24 / unit)
LUT_BITS = 5          # LUT is 2^5 x 2^5; address = (x << 5) | y; queue bins 1..31
NQ_FX = (1 << LUT_BITS) - 1
QTY_MAX = (1 << 24) - 1   # queue sizes saturate here so q * Q_RECIP fits in 48 bits
TICK_MAX = (1 << 24) - 1  # prices must be below this many ticks so (bb + ba) << 15 fits in 48 bits


@dataclass
class ASParams:
    gamma: float = 0.1          # risk aversion, 1 / (tick * lot)
    k: float = 1.5              # order arrival decay, 1 / tick
    sigma: float = 1.0          # ticks / sqrt(s)
    tau: float = 60.0           # horizon in seconds; <= 0 -> use time left to t_end
    t_end_ns: int = 57_600 * 10**9
    alpha: float = 0.0          # CST skew strength in ticks (0 = pure Avellaneda–Stoikov)
    unit: float = 100.0         # shares per CST queue unit (average limit order size)
    max_pos: int = 5            # inventory limit in lots
    online_sigma: bool = False  # use the backtester's EWMA sigma instead of a fixed one
    min_half_spread: float = 0.5  # ticks
    gate: float = 1.0           # one-tick spread: pull a side if P(adverse move) > gate (1 = never)

    def fp(self) -> np.ndarray:
        v = np.zeros(N_F)
        v[F_GAMMA], v[F_K], v[F_SIGMA], v[F_TAU] = self.gamma, self.k, self.sigma, self.tau
        v[F_T_END], v[F_ALPHA], v[F_UNIT], v[F_MAX_POS] = self.t_end_ns, self.alpha, self.unit, self.max_pos
        v[F_ONLINE_SIGMA], v[F_MIN_HALF], v[F_GATE] = float(self.online_sigma), self.min_half_spread, self.gate
        return v

    def half_spread(self, sigma: float | None = None, tau: float | None = None) -> float:
        s = self.sigma if sigma is None else sigma
        t = self.tau if tau is None else tau
        g = self.gamma
        return max(0.5 * g * s * s * t + math.log1p(g / self.k) / g, self.min_half_spread)


@njit(cache=True)
def as_cst_quote(t_ns, bb, ba, bq, aq, inv, sigma, fp, ip, lut_f, lut_i):
    """Floating-point Avellaneda–Stoikov + CST skew + imbalance gate. ``lut_f`` is the P_up table.

    Gate (one-tick spread only): the bid is pulled when P(down) = 1 - P_up > gate, the ask
    when P_up > gate. A bid filled just before a down-tick is the main source of adverse
    selection (see scripts/experiment_adverse.py).
    """
    g = fp[F_GAMMA]
    sig = sigma if fp[F_ONLINE_SIGMA] > 0.5 else fp[F_SIGMA]
    tau = fp[F_TAU]
    if tau <= 0.0:
        tau = max((fp[F_T_END] - t_ns) * 1e-9, 0.0)
    s = 0.5 * (bb + ba)
    r = s - inv * g * sig * sig * tau
    bid_ok = True
    ask_ok = True
    if ba - bb == 1 and (fp[F_ALPHA] != 0.0 or fp[F_GATE] < 1.0):
        n = lut_f.shape[0] - 1
        x = min(max(int(bq / fp[F_UNIT] + 0.5), 1), n)
        y = min(max(int(aq / fp[F_UNIT] + 0.5), 1), n)
        pu = lut_f[x, y]
        r += fp[F_ALPHA] * (2.0 * pu - 1.0)
        bid_ok = 1.0 - pu <= fp[F_GATE]
        ask_ok = pu <= fp[F_GATE]
    half = 0.5 * g * sig * sig * tau + math.log1p(g / fp[F_K]) / g
    half = max(half, fp[F_MIN_HALF])
    bid = int(math.floor(r - half))
    ask = int(math.ceil(r + half))
    bid = min(bid, ba - 1)           # post-only: never cross the book
    ask = max(ask, bb + 1)
    if not bid_ok:
        bid = -1
    if not ask_ok:
        ask = -1
    if inv >= fp[F_MAX_POS]:
        bid = -1
    if inv <= -fp[F_MAX_POS]:
        ask = -1
    return bid, ask


# ---------------------------------------------------------------------------------------
# Fixed-point kernel = FPGA golden model.
#
# All arithmetic is int64 with FRAC fractional bits (Q.FRAC). Constant sigma and tau
# make every model term a precomputed constant:
#     C_INV  = gamma * sigma^2 * tau                     (ticks per lot, Q.FRAC)
#     C_HALF = max(C_INV / 2 + ln(1 + gamma/k) / gamma, min_half)   (ticks, Q.FRAC)
#     LUT[x, y] = (alpha * (2 P_up(x, y) - 1) << 2) | gate  (skew in ticks Q.FRAC, gate bit0 = bid
#                 allowed, bit1 = ask allowed; one BRAM read gives both)
# Datapath per book update (FRAC = 16, a compile-time constant in hardware):
#     r   = (bb + ba) << (FRAC - 1)  -  inv * C_INV  +  (LUT[x, y] >> 2)   (if spread == 1)
#     bid = (r - C_HALF) >> FRAC            (arithmetic shift = floor)
#     ask = -((-(r + C_HALF)) >> FRAC)      (ceil)
# then post-only clamps and inventory gating. Queue sizes are quantised without a divider:
#     x = clip((q * Q_RECIP + 2^23) >> 24, 1, NQ),   Q_RECIP = round(2^24 / unit)
# so the hardware needs two small multipliers, one 32x32 LUT read and adders.
# ---------------------------------------------------------------------------------------
@njit(cache=True)
def as_cst_quote_fx(t_ns, bb, ba, bq, aq, inv, sigma, fp, ip, lut_f, lut_i):
    frac = ip[I_FRAC]
    r = (bb + ba) << (frac - 1)
    r -= inv * ip[I_C_INV]
    if ba - bb == 1:
        n = ip[I_NQ]
        half = 1 << (RECIP_SHIFT - 1)
        x = min(max((min(bq, QTY_MAX) * ip[I_Q_RECIP] + half) >> RECIP_SHIFT, 1), n)
        y = min(max((min(aq, QTY_MAX) * ip[I_Q_RECIP] + half) >> RECIP_SHIFT, 1), n)
        e = lut_i[x, y]
        r += e >> 2
        gate = e & 3
    else:
        gate = 3
    bid = (r - ip[I_C_HALF]) >> frac
    ask = -((-(r + ip[I_C_HALF])) >> frac)
    bid = min(bid, ba - 1)
    ask = max(ask, bb + 1)
    if (gate & 1) == 0:
        bid = -1
    if (gate & 2) == 0:
        ask = -1
    if inv >= ip[I_MAX_POS]:
        bid = -1
    if inv <= -ip[I_MAX_POS]:
        ask = -1
    return bid, ask


def fixed_point_params(p: ASParams, p_up: np.ndarray, frac: int = FX_FRAC):
    """Quantises an ASParams + P_up table into the fixed-point kernel's int vector and LUT.

    ``p_up`` must be (NQ_FX + 1) x (NQ_FX + 1) = 32 x 32 (``cst.prob_up_table(..., n_max=31)``).
    Every register value must fit in a signed 32-bit word (the hardware register width).
    """
    if p.tau <= 0 or p.online_sigma:
        raise ValueError("fixed-point kernel needs a constant tau and sigma")
    if frac != FX_FRAC:
        raise ValueError(f"hardware datapath is fixed at FRAC={FX_FRAC}")
    if p_up.shape != (NQ_FX + 1, NQ_FX + 1):
        raise ValueError(f"P_up table must be {NQ_FX + 1}x{NQ_FX + 1}, got {p_up.shape}")
    one = 1 << frac
    ip = np.zeros(N_I, np.int64)
    ip[I_C_INV] = int(round(p.gamma * p.sigma ** 2 * p.tau * one))
    ip[I_C_HALF] = int(round(p.half_spread() * one))
    ip[I_Q_RECIP] = int(round((1 << RECIP_SHIFT) / p.unit))
    ip[I_MAX_POS] = int(p.max_pos)
    ip[I_NQ] = NQ_FX
    ip[I_FRAC] = frac
    skew = np.round(p.alpha * (2.0 * p_up - 1.0) * one).astype(np.int64)
    gate = ((1.0 - p_up) <= p.gate).astype(np.int64) | (((p_up <= p.gate).astype(np.int64)) << 1)
    lut = (skew << 2) | gate
    for name, v in (("C_INV", ip[I_C_INV]), ("C_HALF", ip[I_C_HALF]), ("Q_RECIP", ip[I_Q_RECIP]),
                    ("LUT", np.abs(skew).max() << 2)):
        if not -(1 << 31) <= int(v) < (1 << 31):
            raise ValueError(f"{name}={v} does not fit in 32 bits; reduce gamma/tau/alpha")
    return ip, lut
