"""Performance metrics for a market-making backtest."""
from __future__ import annotations

import numpy as np

from .backtest import BacktestResult
from .lobster import DUMMY_ASK, DUMMY_BID, LobsterDay


def equity_curve(res: BacktestResult) -> np.ndarray:
    """Mark-to-mid equity in dollars at each record time (fees accrued pro rata are ignored)."""
    r = res.records
    return (r["cash"] + r["pos"] * r["mid"]) / 10_000


def markouts(res: BacktestResult, day: LobsterDay, horizons_s=(0.1, 1.0, 10.0, 60.0)) -> dict:
    """Average signed mid move after our fills, in ticks, from our side's perspective.

    markout(h) = side * (mid(t + h) - fill_price). Positive = the fill made money at h.
    The h = 0 value is the half-spread we captured relative to the mid at the fill.
    """
    f = res.fills
    if len(f["t"]) == 0:
        return {}
    valid = (day.bid_px[:, 0] != DUMMY_BID) & (day.ask_px[:, 0] != DUMMY_ASK)
    ts = day.ts[valid]
    mid = 0.5 * (day.bid_px[valid, 0] + day.ask_px[valid, 0])
    out = {"0s": float(np.average(f["side"] * (f["mid"] - f["px"]), weights=f["qty"]) / day.tick)}
    for h in horizons_s:
        j = np.searchsorted(ts, f["t"] + int(h * 1e9), side="right") - 1
        m = mid[np.clip(j, 0, len(mid) - 1)]
        out[f"{h:g}s"] = float(np.average(f["side"] * (m - f["px"]), weights=f["qty"]) / day.tick)
    return out


def summarize(res: BacktestResult, day: LobsterDay | None = None) -> dict:
    eq = equity_curve(res)
    f = res.fills
    volume = int(f["qty"].sum())
    ok = ~np.isnan(eq)
    eqv = eq[ok]
    peak = np.maximum.accumulate(eqv) if len(eqv) else eqv
    dd = float((peak - eqv).max()) if len(eqv) else 0.0
    rets = np.diff(eqv[::60]) if len(eqv) > 120 else np.diff(eqv)     # 1-minute PnL changes
    sharpe_min = float(rets.mean() / rets.std()) if len(rets) > 1 and rets.std() > 0 else float("nan")
    out = {
        "pnl_mtm_$": round(res.pnl_mtm, 2),
        "pnl_liquidated_$": round(res.pnl_liquidated, 2),
        "fees_$": round(res.fees, 2),
        "fills": int(len(f["t"])),
        "volume_shares": volume,
        "pnl_per_share_$": round(res.pnl_liquidated / volume, 5) if volume else 0.0,
        "orders_sent": res.n_orders,
        "order_to_fill": round(res.n_orders / max(len(f["t"]), 1), 2),
        "final_position": res.final_position,
        "max_abs_position": int(np.abs(res.records["pos"]).max()) if len(res.records["pos"]) else 0,
        "max_drawdown_$": round(dd, 2),
        "sharpe_per_minute": round(sharpe_min, 3),
    }
    if day is not None:
        out["markout_ticks"] = {k: round(v, 3) for k, v in markouts(res, day).items()}
    return out
