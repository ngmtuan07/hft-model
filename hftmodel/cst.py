"""Cont–Stoikov–Talreja (2010): probability that the next mid-price move is up.

With a one-tick spread, the best bid queue x and best ask queue y evolve (in units
of the average limit order size) as two independent birth–death processes:

    birth  (new limit order at the touch)        rate lambda(1)
    death  (market order + per-order cancels)    rate mu + theta(1) * n

The mid price moves up when the ask queue depletes first, down when the bid
queue depletes first. CST compute this probability with Laplace transforms; here
we solve the equivalent absorption problem of the 2-D Markov chain directly on
a truncated grid (x, y) in {1..N}^2, which is exact up to truncation:

    h(0, y) = 0,  h(x, 0) = 1
    (r_x + r_y) h(x, y) = lambda h(x+1, y) + lambda h(x, y+1)
                         + d(x) h(x-1, y) + d(y) h(x, y-1)

with d(n) = mu + theta * n and births switched off at the truncation level N.
"""
from __future__ import annotations

import numpy as np
from scipy.sparse import lil_matrix
from scipy.sparse.linalg import spsolve


def prob_up_table(lam1: float, mu: float, theta1: float, n_max: int = 30) -> np.ndarray:
    """Returns P[x, y] = P(next mid move is up | bid queue = x, ask queue = y), x, y in 0..n_max."""
    N = n_max
    idx = lambda x, y: (x - 1) * N + (y - 1)  # noqa: E731
    A = lil_matrix((N * N, N * N))
    b = np.zeros(N * N)
    for x in range(1, N + 1):
        for y in range(1, N + 1):
            r = idx(x, y)
            bx = lam1 if x < N else 0.0
            by = lam1 if y < N else 0.0
            dx = mu + theta1 * x
            dy = mu + theta1 * y
            A[r, r] = bx + by + dx + dy
            if bx:
                A[r, idx(x + 1, y)] -= bx
            if by:
                A[r, idx(x, y + 1)] -= by
            if x > 1:
                A[r, idx(x - 1, y)] -= dx      # bid queue shrinks
            # x == 1 -> bid depletes -> h = 0, nothing to add
            if y > 1:
                A[r, idx(x, y - 1)] -= dy
            else:
                b[r] += dy                     # ask depletes -> h = 1
    h = spsolve(A.tocsr(), b)
    P = np.full((N + 1, N + 1), 0.5)
    P[0, 1:] = 0.0
    P[1:, 0] = 1.0
    H = h.reshape(N, N)
    # The model is symmetric: P(x, y) = 1 - P(y, x). Enforce it exactly so that balanced
    # queues give exactly 0.5 (solver noise of 1e-16 would otherwise flip floor/ceil quotes).
    P[1:, 1:] = 0.5 * (H + 1.0 - H.T)
    np.fill_diagonal(P, 0.5)
    return P


def _up_counts(day, unit: float, n_max: int):
    """(sums of up-moves, counts) per (bid bin, ask bin) over one-tick-spread rows."""
    mid2 = day.ask_px[:, 0] + day.bid_px[:, 0]
    one_tick = (day.ask_px[:, 0] - day.bid_px[:, 0]) == day.tick
    change = np.flatnonzero(np.diff(mid2) != 0) + 1          # rows where mid differs from previous row
    nxt = np.searchsorted(change, np.arange(len(mid2)), side="right")
    has_next = nxt < len(change)
    rows = np.flatnonzero(one_tick & has_next)
    up = (mid2[change[nxt[rows]]] > mid2[rows]).astype(np.float64)
    qb = np.clip(np.round(day.bid_sz[rows, 0] / unit).astype(int), 1, n_max)
    qa = np.clip(np.round(day.ask_sz[rows, 0] / unit).astype(int), 1, n_max)
    counts = np.zeros((n_max + 1, n_max + 1))
    sums = np.zeros((n_max + 1, n_max + 1))
    np.add.at(counts, (qb, qa), 1)
    np.add.at(sums, (qb, qa), up)
    return sums, counts


def empirical_up_table(day, unit: float, n_max: int = 30, min_obs: int = 30):
    """Empirical P(next mid move up | queues) from data, for one-tick-spread states.

    Returns (P, counts). Cells with fewer than ``min_obs`` observations are NaN.
    """
    sums, counts = _up_counts(day, unit, n_max)
    with np.errstate(invalid="ignore", divide="ignore"):
        P = sums / counts
    P[counts < min_obs] = np.nan
    return P, counts


def blended_up_table(day, prior: np.ndarray, unit: float, prior_weight: float = 50.0) -> np.ndarray:
    """Empirical P_up shrunk toward a model table (Bayesian average with ``prior_weight``
    pseudo-observations per cell) and symmetrised, so that a bid-side observation at (x, y)
    also counts as an ask-side observation at (y, x). Same shape and meaning as ``prior``."""
    n_max = prior.shape[0] - 1
    sums, counts = _up_counts(day, unit, n_max)
    # pool (x, y) with the mirror state (y, x): P(up | x, y) = 1 - P(up | y, x)
    s2 = sums + (counts.T - sums.T)
    c2 = counts + counts.T
    P = (s2 + prior_weight * prior) / (c2 + prior_weight)
    P = 0.5 * (P + 1.0 - P.T)
    P[0, 1:] = 0.0
    P[1:, 0] = 1.0
    np.fill_diagonal(P, 0.5)
    return P
