"""LOBSTER data loader.

LOBSTER (https://lobsterdata.com) ships two CSV files per ticker/day, without headers:

message file  ``{TICKER}_{DATE}_{START}_{END}_message_{LEVEL}.csv``
    time       seconds after midnight, decimal precision up to nanoseconds
    type       1 new limit order
               2 partial cancellation
               3 full deletion
               4 execution of a visible limit order
               5 execution of a hidden limit order
               6 cross trade (auction)
               7 trading halt indicator
    order_id   unique order reference
    size       shares
    price      dollars * 10_000 (integer)
    direction  1 = buy limit order, -1 = sell limit order. For executions (4/5)
               this is the side of the *resting* order, so direction == 1 means
               a seller-initiated trade that hit a bid.

orderbook file ``..._orderbook_{LEVEL}.csv``
    for each level k = 1..LEVEL: ask_price_k, ask_size_k, bid_price_k, bid_size_k.
    Row i is the state of the book *after* message i. Empty levels are filled with
    the dummy prices 9999999999 (ask) and -9999999999 (bid) and size 0.

Everything here stays in integer units: time in nanoseconds since midnight,
prices in LOBSTER units (1e-4 dollars), sizes in shares.
"""
from __future__ import annotations

import glob
import os
import re
from dataclasses import dataclass

import numpy as np
import pandas as pd

PRICE_SCALE = 10_000          # LOBSTER price units per dollar
DUMMY_ASK = 9_999_999_999
DUMMY_BID = -9_999_999_999

NEW, CANCEL_PARTIAL, DELETE, EXEC_VISIBLE, EXEC_HIDDEN, CROSS, HALT = 1, 2, 3, 4, 5, 6, 7

_NAME_RE = re.compile(
    r"(?P<ticker>[A-Z.]+)_(?P<date>\d{4}-\d{2}-\d{2})_(?P<start>\d+)_(?P<end>\d+)_message_(?P<level>\d+)\.csv$"
)


@dataclass
class LobsterDay:
    ticker: str
    date: str
    levels: int
    # messages (length n)
    ts: np.ndarray        # int64 ns since midnight
    type: np.ndarray      # int8
    order_id: np.ndarray  # int64
    size: np.ndarray      # int64
    price: np.ndarray     # int64 (1e-4 $)
    direction: np.ndarray  # int8
    # book after each message (shape n x levels)
    ask_px: np.ndarray
    ask_sz: np.ndarray
    bid_px: np.ndarray
    bid_sz: np.ndarray
    tick: int = 100       # 1 cent in LOBSTER units

    def __len__(self) -> int:
        return len(self.ts)

    @property
    def mid(self) -> np.ndarray:
        """Mid price in LOBSTER units (float, NaN when one side is empty)."""
        a = self.ask_px[:, 0].astype(np.float64)
        b = self.bid_px[:, 0].astype(np.float64)
        bad = (self.ask_px[:, 0] == DUMMY_ASK) | (self.bid_px[:, 0] == DUMMY_BID)
        m = 0.5 * (a + b)
        m[bad] = np.nan
        return m

    def slice_time(self, t0_ns: int, t1_ns: int) -> "LobsterDay":
        i0, i1 = np.searchsorted(self.ts, [t0_ns, t1_ns])
        sl = slice(i0, i1)
        return LobsterDay(
            self.ticker, self.date, self.levels,
            self.ts[sl], self.type[sl], self.order_id[sl], self.size[sl], self.price[sl], self.direction[sl],
            self.ask_px[sl], self.ask_sz[sl], self.bid_px[sl], self.bid_sz[sl], self.tick,
        )


def find_files(directory: str, ticker: str | None = None) -> list[tuple[str, str]]:
    """Returns [(message_file, orderbook_file), ...] found in ``directory``."""
    pairs = []
    for msg in sorted(glob.glob(os.path.join(directory, "**", "*_message_*.csv"), recursive=True)):
        m = _NAME_RE.search(os.path.basename(msg))
        if not m or (ticker and m["ticker"] != ticker):
            continue
        ob = msg.replace("_message_", "_orderbook_")
        if os.path.exists(ob):
            pairs.append((msg, ob))
    return pairs


def load(message_file: str, orderbook_file: str | None = None, drop_halts: bool = True,
         tick: int = 100, max_levels: int | None = None, cache: bool = True) -> LobsterDay:
    """Loads a LOBSTER message/orderbook pair into integer numpy arrays.

    ``max_levels`` reads only the first levels of the book (the SPY sample has 30 levels and
    an 852 MB orderbook file). With ``cache`` the parsed arrays are kept in a .npz next to the
    message file, which makes later loads ~50x faster.
    """
    cache_path = message_file[:-4] + (f"_L{max_levels}" if max_levels else "") + ".npz"
    if cache and os.path.exists(cache_path) and os.path.getmtime(cache_path) >= os.path.getmtime(message_file):
        z = np.load(cache_path)
        return LobsterDay(str(z["ticker"]), str(z["date"]), int(z["levels"]), z["ts"], z["type"], z["order_id"],
                          z["size"], z["price"], z["direction"], z["ask_px"], z["ask_sz"], z["bid_px"], z["bid_sz"],
                          int(z["tick"]))
    day = _load_csv(message_file, orderbook_file, drop_halts, tick, max_levels)
    if cache:
        try:
            np.savez(cache_path, ticker=day.ticker, date=day.date, levels=day.levels, ts=day.ts, type=day.type,
                     order_id=day.order_id, size=day.size, price=day.price, direction=day.direction,
                     ask_px=day.ask_px, ask_sz=day.ask_sz, bid_px=day.bid_px, bid_sz=day.bid_sz, tick=day.tick)
        except OSError:
            pass
    return day


def _load_csv(message_file, orderbook_file, drop_halts, tick, max_levels) -> LobsterDay:
    if orderbook_file is None:
        orderbook_file = message_file.replace("_message_", "_orderbook_")
    m = _NAME_RE.search(os.path.basename(message_file))
    ticker, date = (m["ticker"], m["date"]) if m else ("UNKNOWN", "1970-01-01")

    msg = pd.read_csv(message_file, header=None, usecols=range(6),
                      names=["time", "type", "order_id", "size", "price", "direction"],
                      dtype={"time": np.float64, "type": np.int8, "order_id": np.int64,
                             "size": np.int64, "price": np.int64, "direction": np.int8})
    usecols = list(range(4 * max_levels)) if max_levels else None
    ob = pd.read_csv(orderbook_file, header=None, usecols=usecols, dtype=np.int64, engine="c").to_numpy()
    if len(ob) != len(msg):
        raise ValueError(f"message/orderbook length mismatch: {len(msg)} vs {len(ob)}")
    levels = ob.shape[1] // 4

    keep = np.ones(len(msg), dtype=bool)
    if drop_halts:
        keep &= msg["type"].to_numpy() != HALT
    # Rows where one side of the book is empty are useless for quoting; keep them but
    # callers can mask with np.isnan(day.mid).

    ts = np.round(msg["time"].to_numpy() * 1e9).astype(np.int64)
    return LobsterDay(
        ticker=ticker, date=date, levels=levels,
        ts=ts[keep],
        type=msg["type"].to_numpy()[keep],
        order_id=msg["order_id"].to_numpy()[keep],
        size=msg["size"].to_numpy()[keep],
        price=msg["price"].to_numpy()[keep],
        direction=msg["direction"].to_numpy()[keep],
        ask_px=np.ascontiguousarray(ob[keep, 0::4]),
        ask_sz=np.ascontiguousarray(ob[keep, 1::4]),
        bid_px=np.ascontiguousarray(ob[keep, 2::4]),
        bid_sz=np.ascontiguousarray(ob[keep, 3::4]),
        tick=tick,
    )


def save(day: LobsterDay, directory: str, start_s: int = 34200, end_s: int = 57600) -> tuple[str, str]:
    """Writes a LobsterDay back out in the original LOBSTER CSV format."""
    os.makedirs(directory, exist_ok=True)
    stem = f"{day.ticker}_{day.date}_{start_s * 1000}_{end_s * 1000}"
    msg_path = os.path.join(directory, f"{stem}_message_{day.levels}.csv")
    ob_path = os.path.join(directory, f"{stem}_orderbook_{day.levels}.csv")
    t = day.ts // 1_000_000_000
    frac = day.ts % 1_000_000_000
    with open(msg_path, "w") as f:
        for i in range(len(day)):
            f.write(f"{t[i]}.{frac[i]:09d},{day.type[i]},{day.order_id[i]},{day.size[i]},"
                    f"{day.price[i]},{day.direction[i]}\n")
    ob = np.empty((len(day), 4 * day.levels), dtype=np.int64)
    ob[:, 0::4], ob[:, 1::4], ob[:, 2::4], ob[:, 3::4] = day.ask_px, day.ask_sz, day.bid_px, day.bid_sz
    np.savetxt(ob_path, ob, fmt="%d", delimiter=",")
    return msg_path, ob_path


def summary(day: LobsterDay) -> dict:
    mid = day.mid
    spread = (day.ask_px[:, 0] - day.bid_px[:, 0]).astype(np.float64)
    valid = ~np.isnan(mid)
    types, counts = np.unique(day.type, return_counts=True)
    return {
        "ticker": day.ticker, "date": day.date, "levels": day.levels, "messages": len(day),
        "start": _fmt_ts(day.ts[0]), "end": _fmt_ts(day.ts[-1]),
        "mid_first_$": mid[valid][0] / PRICE_SCALE, "mid_last_$": mid[valid][-1] / PRICE_SCALE,
        "mean_spread_ticks": float(np.mean(spread[valid]) / day.tick),
        "pct_one_tick_spread": float(np.mean(spread[valid] == day.tick) * 100),
        "message_types": {int(t): int(c) for t, c in zip(types, counts)},
    }


def _fmt_ts(ns: int) -> str:
    s = ns / 1e9
    return f"{int(s // 3600):02d}:{int(s % 3600 // 60):02d}:{s % 60:06.3f}"
