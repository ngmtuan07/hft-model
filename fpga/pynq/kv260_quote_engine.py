#!/usr/bin/env python3
"""Runs the quote engine on a KV260 under Kria-PYNQ and checks it against the golden model.

Copy to the board, next to:
    overlay/quote_engine.bit, overlay/quote_engine.hwh   (from fpga/vivado/kv260_bd.tcl)
    vectors/stream_in.npy, vectors/stream_out.npy         (from scripts/export_fpga_vectors.py)
then, on the board (as root, inside the PYNQ venv):
    python3 kv260_quote_engine.py --overlay overlay/quote_engine.bit --vectors vectors

What it measures:
  1. correctness: every output packet == golden model, bit for bit
  2. throughput:  book updates per second for bulk DMA transfers (PS DDR -> PL -> PS DDR)
  3. round trip:  single-packet DMA latency seen from Linux. This is dominated by driver
                  and Python overhead (tens of microseconds), not by the engine. The engine
                  itself takes a few clock cycles (see the csynth latency report); measure
                  that in hardware with an ILA or a PL cycle counter.
"""
import argparse
import time

import numpy as np
from pynq import Overlay, allocate


def run_stream(dma, pin: np.ndarray, chunk: int = 65536) -> tuple[np.ndarray, float]:
    """Sends packets through the engine in DMA chunks; returns (output packets, seconds in DMA)."""
    out = np.empty((len(pin), 4), np.uint32)
    ibuf = allocate(shape=(chunk, 8), dtype=np.uint32)
    obuf = allocate(shape=(chunk, 4), dtype=np.uint32)
    busy = 0.0
    try:
        for s in range(0, len(pin), chunk):
            n = min(chunk, len(pin) - s)
            ibuf[:n] = pin[s:s + n]
            ibuf.flush()
            t = time.perf_counter()
            # one input packet -> one output packet; TLAST at the end of each transfer is echoed
            dma.recvchannel.transfer(obuf, nbytes=n * 16)
            dma.sendchannel.transfer(ibuf, nbytes=n * 32)
            dma.sendchannel.wait()
            dma.recvchannel.wait()
            busy += time.perf_counter() - t
            obuf.invalidate()
            out[s:s + n] = obuf[:n]
    finally:
        ibuf.freebuffer()
        obuf.freebuffer()
    return out, busy


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--overlay", default="overlay/quote_engine.bit")
    ap.add_argument("--vectors", default="vectors")
    ap.add_argument("--roundtrips", type=int, default=2000)
    a = ap.parse_args()

    ol = Overlay(a.overlay)
    dma = ol.axi_dma_0
    pin = np.load(f"{a.vectors}/stream_in.npy")
    exp = np.load(f"{a.vectors}/stream_out.npy")
    is_book = (pin[:, 0] & 0xFF) == 0
    n_cfg = int(np.argmax(is_book))

    # 1 + 2: configure (params + LUT) and replay every book update
    out, busy = run_stream(dma, pin)
    bad = np.flatnonzero(np.any(out != exp, axis=1))
    print(f"{len(pin)} packets, {len(bad)} mismatches")
    for i in bad[:10]:
        print(f"  pkt {i}: got {out[i].view(np.int32)} expected {exp[i].view(np.int32)}")
    n_book = int(is_book.sum())
    print(f"bulk throughput: {n_book / busy / 1e6:.2f} M updates/s "
          f"({busy / max(n_book, 1) * 1e9:.1f} ns/update incl. DMA)")

    # 3: single-packet round trips (engine already configured above)
    one_i = allocate(shape=(1, 8), dtype=np.uint32)
    one_o = allocate(shape=(1, 4), dtype=np.uint32)
    lat = []
    books = pin[n_cfg:]
    for k in range(min(a.roundtrips, len(books))):
        one_i[0] = books[k]
        one_i.flush()
        t = time.perf_counter_ns()
        dma.recvchannel.transfer(one_o)
        dma.sendchannel.transfer(one_i)
        dma.sendchannel.wait()
        dma.recvchannel.wait()
        lat.append(time.perf_counter_ns() - t)
    lat = np.array(lat) / 1e3
    print(f"single-packet round trip (Linux + DMA + engine): median {np.median(lat):.1f} us, "
          f"p99 {np.percentile(lat, 99):.1f} us")
    one_i.freebuffer()
    one_o.freebuffer()
    return 1 if len(bad) else 0


if __name__ == "__main__":
    raise SystemExit(main())
