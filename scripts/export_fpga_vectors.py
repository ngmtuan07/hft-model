#!/usr/bin/env python
"""Exports bit-exact test vectors for the hardware quote kernel and runs the C++ reference against them.

    python scripts/export_fpga_vectors.py --synthetic
    python scripts/export_fpga_vectors.py --data data/lobster --ticker INTC
    make -C fpga/hls csim         # C++ testbench checks every vector
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess

from hftmodel import backtest as bt
from hftmodel import calibration, cst, fpga, lobster, strategy, synthetic


def main():
    ap = argparse.ArgumentParser()
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--data")
    src.add_argument("--synthetic", action="store_true")
    ap.add_argument("--ticker")
    ap.add_argument("--gamma", type=float, default=0.02)
    ap.add_argument("--tau", type=float, default=10.0)
    ap.add_argument("--alpha", type=float, default=0.5)
    ap.add_argument("--max-pos", type=int, default=5)
    ap.add_argument("--gate", type=float, default=0.55, help="imbalance gate threshold (1 = off)")
    ap.add_argument("--qty", type=int, default=100)
    ap.add_argument("--max-vectors", type=int, default=100_000)
    ap.add_argument("--out", default="output/fpga_vectors")
    a = ap.parse_args()

    if a.synthetic:
        day = synthetic.simulate(200_000, seed=11)
    else:
        day = lobster.load(*lobster.find_files(a.data, a.ticker)[0])
    sigma = calibration.estimate_sigma(day)
    fit = calibration.estimate_intensity(day)
    cstp = calibration.estimate_cst(day)
    p_up = cst.prob_up_table(cstp.lam[0], cstp.mu, cstp.theta[0], 31)
    p = strategy.ASParams(gamma=a.gamma, k=fit.k, sigma=sigma, tau=a.tau, alpha=a.alpha, unit=cstp.S_l,
                          max_pos=a.max_pos, gate=a.gate)
    ip, lut = strategy.fixed_point_params(p, p_up)

    # inventory path for realistic vectors: backtest the fixed-point kernel itself
    res = bt.run(day, strategy.as_cst_quote_fx, p.fp(), ip=ip, lut_i=lut, cfg=bt.BacktestConfig(order_qty=a.qty))
    info = fpga.export_vectors(day, res, ip, lut, a.qty, a.out, a.max_vectors)
    info["float_vs_fixed"] = fpga.compare_float_fixed(day, p.fp(), p_up, ip, lut, a.qty)
    info["cpu_numba_float"] = fpga.cpu_latency(day, strategy.as_cst_quote, p.fp(), ip, p_up, lut)
    info["cpu_numba_fixed"] = fpga.cpu_latency(day, strategy.as_cst_quote_fx, p.fp(), ip, p_up, lut)
    info["params"] = {"C_INV": int(ip[0]), "C_HALF": int(ip[1]), "Q_RECIP": int(ip[2]), "MAX_POS": int(ip[3]),
                      "NQ": int(ip[4]), "FRAC": int(ip[5]), "unit_shares": round(cstp.S_l, 2)}
    print(json.dumps(info, indent=1))
    with open(os.path.join(a.out, "info.json"), "w") as f:
        json.dump(info, f, indent=1)

    hls = os.path.join(os.path.dirname(__file__), "..", "fpga", "hls")
    try:
        subprocess.run(["make", "-s", "-C", hls, "csim", f"VEC={os.path.abspath(a.out)}"], check=True)
    except (OSError, subprocess.CalledProcessError) as e:
        print("C++ testbench did not pass / could not run:", e)


if __name__ == "__main__":
    main()
