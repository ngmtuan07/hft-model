import numpy as np
import pytest

from hftmodel import backtest as bt
from hftmodel import calibration, cst, fpga, lobster, strategy, synthetic


@pytest.fixture(scope="module")
def day():
    return synthetic.simulate(60_000, seed=5)


def test_lobster_roundtrip(day, tmp_path):
    msg, ob = lobster.save(day.slice_time(day.ts[0], day.ts[0] + int(60e9)), str(tmp_path))
    d2 = lobster.load(msg, ob)
    assert d2.levels == day.levels
    n = len(d2)
    assert np.array_equal(d2.ts, day.ts[:n]) and np.array_equal(d2.bid_sz, day.bid_sz[:n])
    assert lobster.find_files(str(tmp_path))


def test_book_never_crossed(day):
    assert np.all(day.ask_px[:, 0] > day.bid_px[:, 0])


def test_calibration_recovers_cst_params(day):
    p = calibration.estimate_cst(day)
    lam, mu, theta = synthetic.default_params()
    assert np.allclose(p.lam[:5], lam[:5], rtol=0.1)
    assert abs(p.mu - mu) / mu < 0.1
    assert np.allclose(p.theta[:5], theta[:5], rtol=0.15)


def test_prob_up_table_properties():
    P = cst.prob_up_table(1.9, 0.94, 0.7, 20)
    assert np.allclose(np.diag(P)[1:], 0.5, atol=1e-9)              # symmetry
    assert np.allclose(P[1:, 1:] + P[1:, 1:].T, 1.0, atol=1e-9)
    assert np.all(np.diff(P[1:, 5]) > 0)                             # more bids -> more likely up
    assert np.all(np.diff(P[5, 1:]) < 0)


def test_as_quotes_inventory_skew():
    p = strategy.ASParams(gamma=0.05, k=2.0, sigma=1.0, tau=10, max_pos=5)
    fp, lut = p.fp(), np.full((2, 2), 0.5)
    z = np.zeros(1, np.int64), np.zeros((2, 2), np.int64)
    flat = strategy.as_cst_quote(0, 1000, 1002, 100, 100, 0, 1.0, fp, z[0], lut, z[1])
    long = strategy.as_cst_quote(0, 1000, 1002, 100, 100, 3, 1.0, fp, z[0], lut, z[1])
    assert long[0] <= flat[0] and long[1] <= flat[1]                 # long -> lean lower
    assert strategy.as_cst_quote(0, 1000, 1002, 100, 100, 5, 1.0, fp, z[0], lut, z[1])[0] == -1


def test_fixed_point_matches_float(day):
    P = cst.prob_up_table(1.9, 0.94, 0.7, 31)
    p = strategy.ASParams(gamma=0.03, k=2.5, sigma=1.1, tau=10, alpha=0.7, unit=100, gate=0.6)
    ip, lut = strategy.fixed_point_params(p, P)
    r = fpga.compare_float_fixed(day, p.fp(), P, ip, lut, 100, max_rows=20_000)
    assert r["arithmetic_mismatch_pct"] < 0.01 and r["max_tick_diff"] <= 1


def test_gate_pulls_adverse_side():
    P = cst.prob_up_table(1.9, 0.94, 0.7, 31)
    p = strategy.ASParams(gamma=0.02, k=2.5, sigma=1.0, tau=10, alpha=0.0, unit=100, gate=0.6)
    z = np.zeros(1, np.int64)
    # tiny bid queue, big ask queue -> down move likely -> bid pulled, ask kept
    b, a = strategy.as_cst_quote(0, 1000, 1001, 100, 2000, 0, 1.0, p.fp(), z, P, np.zeros((2, 2), np.int64))
    assert b == -1 and a > 0
    ip, lut = strategy.fixed_point_params(p, P)
    assert strategy.as_cst_quote_fx(0, 1000, 1001, 100, 2000, 0, 1.0, p.fp(), ip, P, lut) == (b, a)


def test_backtest_accounting(day):
    p = strategy.ASParams(gamma=0.02, k=2.5, sigma=1.0, tau=10, max_pos=3)
    res = bt.run(day, strategy.as_cst_quote, p.fp(), lut_f=np.full((2, 2), 0.5))
    f = res.fills
    assert len(f["t"]) > 50
    assert res.final_position == int((f["side"] * f["qty"]).sum())
    cash = float(-(f["side"] * f["qty"] * f["px"]).sum())
    assert np.isclose(cash, res.final_cash)
    assert np.abs(res.records["pos"]).max() <= 3 * 100 + 100         # limit + one partially filled order


def test_infinite_latency_never_fills(day):
    p = strategy.ASParams(gamma=0.02, k=2.5, sigma=1.0, tau=10)
    cfg = bt.BacktestConfig(order_latency_ns=10**15)
    res = bt.run(day, strategy.as_cst_quote, p.fp(), lut_f=np.full((2, 2), 0.5), cfg=cfg)
    assert len(res.fills["t"]) == 0


def test_hftbacktest_conversion(day):
    hbt = pytest.importorskip("hftmodel.hbt")
    from hftbacktest.data.validation import validate_event_order
    data = hbt.convert(day.slice_time(day.ts[0], day.ts[0] + int(120e9)))
    validate_event_order(data)
    assert len(data) > 0
