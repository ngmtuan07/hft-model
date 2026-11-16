"""Parameter estimation from LOBSTER data.

All outputs are in *tick* units so the strategy kernel can work on integers:
sigma in ticks / sqrt(second), k in 1 / tick, rates per second.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict

import numpy as np

from .lobster import DUMMY_ASK, DUMMY_BID, EXEC_HIDDEN, EXEC_VISIBLE, NEW, CANCEL_PARTIAL, DELETE, LobsterDay


def _valid(day: LobsterDay) -> np.ndarray:
    return (day.ask_px[:, 0] != DUMMY_ASK) & (day.bid_px[:, 0] != DUMMY_BID)


def sample_mid(day: LobsterDay, dt_s: float = 1.0):
    """Mid price (ticks) sampled on a regular grid, last-value-carried-forward."""
    v = _valid(day)
    ts, mid = day.ts[v], 0.5 * (day.ask_px[v, 0] + day.bid_px[v, 0]) / day.tick
    grid = np.arange(ts[0], ts[-1], int(dt_s * 1e9))
    i = np.searchsorted(ts, grid, side="right") - 1
    return grid, mid[np.maximum(i, 0)]


def estimate_sigma(day: LobsterDay, dt_s: float = 1.0) -> float:
    """Mid-price volatility in ticks / sqrt(second)."""
    _, m = sample_mid(day, dt_s)
    return float(np.std(np.diff(m)) / np.sqrt(dt_s))


def market_orders(day: LobsterDay):
    """Groups consecutive executions with identical timestamp and side into market orders.

    Returns (first_row, last_row, side, total_size) where side = +1 for a buyer-initiated
    order (it hit asks) and -1 for seller-initiated.
    """
    ex = np.flatnonzero((day.type == EXEC_VISIBLE) | (day.type == EXEC_HIDDEN))
    if len(ex) == 0:
        return (np.zeros(0, int),) * 3 + (np.zeros(0),)
    new_grp = np.ones(len(ex), bool)
    new_grp[1:] = (np.diff(ex) != 1) | (np.diff(day.ts[ex]) != 0) | (np.diff(day.direction[ex]) != 0)
    g = np.cumsum(new_grp) - 1
    first = ex[new_grp]
    last = ex[np.r_[np.flatnonzero(new_grp)[1:] - 1, len(ex) - 1]]
    side = -day.direction[first].astype(int)        # resting sell executed -> buyer initiated
    size = np.bincount(g, weights=day.size[ex])
    return first, last, side, size


@dataclass
class IntensityFit:
    A: float          # arrivals / second reaching depth 0
    k: float          # decay per tick
    depths: np.ndarray
    rates: np.ndarray


def estimate_intensity(day: LobsterDay, max_depth_ticks: int = 20) -> IntensityFit:
    """Fits lambda(delta) = A exp(-k delta), the rate at which market orders reach
    depth delta (ticks from the mid price), per side, as in Avellaneda–Stoikov."""
    first, last, side, _ = market_orders(day)
    prev = np.maximum(first - 1, 0)
    mid = 0.5 * (day.ask_px[prev, 0] + day.bid_px[prev, 0])
    deepest = np.where(side > 0, day.price[last] - mid, mid - day.price[last]) / day.tick
    ok = _valid(day)[prev] & (deepest > 0)
    deepest = deepest[ok]
    T = (day.ts[-1] - day.ts[0]) / 1e9
    depths = np.arange(0.5, max_depth_ticks + 0.5, 0.5)
    rates = np.array([(deepest >= d).sum() for d in depths]) / T / 2.0   # per side
    use = rates * T * 2 >= 10                                           # >= 10 observations
    if use.sum() < 2:
        # Large-tick names: market orders almost never walk the book. Fall back to the
        # rate at the touch and a steep decay so the model prefers quoting at the touch.
        return IntensityFit(A=float(rates[0] * np.exp(depths[0] * 2.0)), k=2.0, depths=depths, rates=rates)
    slope, intercept = np.polyfit(depths[use], np.log(rates[use]), 1)
    return IntensityFit(A=float(np.exp(intercept)), k=float(max(-slope, 1e-3)), depths=depths, rates=rates)


@dataclass
class CSTParams:
    lam: np.ndarray       # limit order arrival rate at distance i = 1..L (per side, per second)
    mu: float             # market order rate (per side, per second)
    theta: np.ndarray     # cancellation rate per resting order at distance i
    S_l: float            # average limit order size (shares) = queue unit
    S_m: float
    S_c: float

    def as_dict(self):
        d = asdict(self)
        return {k: (v.round(4).tolist() if isinstance(v, np.ndarray) else round(v, 4)) for k, v in d.items()}


def _qty_at(px: np.ndarray, sz: np.ndarray, target: np.ndarray) -> np.ndarray:
    return np.where(px == target[:, None], sz, 0).sum(axis=1)


def estimate_cst(day: LobsterDay, L: int = 10) -> CSTParams:
    """Estimators of Cont, Stoikov & Talreja (2010), Section 3.

    lambda(i) = N_l(i) / T
    mu        = N_m / T * S_m / S_l
    theta(i)  = N_c(i) / (T * Q_i) * S_c / S_l
    where i is the distance in ticks from the *opposite* best quote and Q_i the
    time-averaged queue size at that distance in units of S_l. Rates are per side.
    """
    T = (day.ts[-1] - day.ts[0]) / 1e9
    n = len(day)
    prev = np.r_[0, np.arange(n - 1)]            # book before message i is row i-1
    ok = _valid(day)[prev]
    best_ask_b = day.ask_px[prev, 0]
    best_bid_b = day.bid_px[prev, 0]
    dist = np.where(day.direction == 1, best_ask_b - day.price, day.price - best_bid_b) // day.tick

    is_lim = (day.type == NEW) & ok
    is_can = ((day.type == CANCEL_PARTIAL) | (day.type == DELETE)) & ok
    S_l = float(day.size[is_lim].mean())
    S_c = float(day.size[is_can].mean())
    _, _, _, mo_size = market_orders(day)
    S_m = float(mo_size.mean()) if len(mo_size) else S_l

    lam = np.array([(is_lim & (dist == i)).sum() for i in range(1, L + 1)]) / T / 2.0
    mu = len(mo_size) / T / 2.0 * S_m / S_l

    # time-averaged queue size at distance i (both sides averaged), in units of S_l
    dt = np.diff(day.ts, append=day.ts[-1]) / 1e9
    v = _valid(day)
    Q = np.zeros(L)
    for i in range(1, L + 1):
        qb = _qty_at(day.bid_px, day.bid_sz, day.ask_px[:, 0] - i * day.tick)
        qa = _qty_at(day.ask_px, day.ask_sz, day.bid_px[:, 0] + i * day.tick)
        Q[i - 1] = np.sum(((qb + qa) / 2.0 * dt)[v]) / np.sum(dt[v]) / S_l
    Nc = np.array([(is_can & (dist == i)).sum() for i in range(1, L + 1)]) / 2.0
    with np.errstate(divide="ignore", invalid="ignore"):
        theta = np.where(Q > 0, Nc / (T * Q) * S_c / S_l, 0.0)
    return CSTParams(lam=lam, mu=mu, theta=theta, S_l=S_l, S_m=S_m, S_c=S_c)
