# hft-model

A classic market-making model, backtested on LOBSTER order book data, with a
bit-exact fixed-point kernel ready to move onto an FPGA.

* **Avellaneda & Stoikov (2008)**: reservation price and optimal spread for a market maker with inventory risk.
* **Cont, Stoikov & Talreja (2010)**: queueing model of the limit order book. It gives
  P(next mid move is up | bid queue, ask queue), which skews the A-S reservation price.
* **LOBSTER**: NASDAQ message-level data (https://lobsterdata.com).
* **hftbacktest**: an independent backtester used to cross-check results (https://github.com/nkaz001/hftbacktest).

```
LOBSTER csv ─► lobster.py ─► calibration.py ─► cst.py (P_up table)
                    │              │                   │
                    │              ▼                   ▼
                    │        strategy.py: as_cst_quote (float)  ──►  backtest.py (own engine)
                    │                     as_cst_quote_fx (Q16) ──►  fpga.py ─► vectors ─► fpga/hls (C++/HLS)
                    └──────► hbt.py ─► hftbacktest (cross-check)
synthetic.py: CST simulator that writes LOBSTER-format data (tests, demos)
```

## Setup

```bash
python3 -m venv .venv && .venv/bin/pip install -e '.[hbt,dev]'
```

## Data

```bash
scripts/download_lobster_sample.sh INTC MSFT 10
```

This saves the free 2012-06-21 samples (about 8 MB per zip) to `data/lobster/`. Other available samples are AAPL, AMZN and GOOG
at levels 1/5/10, plus AAPL/MSFT/SPY at levels 30/50. CST's queue model is written for
**large-tick** stocks, where the spread is almost always one tick. Use INTC or MSFT to
test the CST signal. AAPL/GOOG/AMZN have wide spreads in ticks, so the skew is rarely active there.

## Run

```bash
.venv/bin/python scripts/run_backtest.py --synthetic                 # no download needed
.venv/bin/python scripts/run_backtest.py --data data/lobster --ticker INTC --alphas 0 0.5 1
.venv/bin/python scripts/run_backtest.py --data data/lobster --ticker INTC --hbt            # + hftbacktest
.venv/bin/python scripts/run_backtest.py --data data/lobster --ticker INTC --fixed-point    # FPGA kernel
.venv/bin/python -m pytest -q tests
```

The runner skips the first and last 5 minutes. It calibrates on the next `--calib-min`
minutes and backtests the rest of the day out of sample. It writes `output/report_<T>.json`,
`backtest_<T>.png` (PnL, inventory and mid price) and `calibration_<T>.png` (the fill intensity fit, plus the CST
model next to the empirical P_up).

## Model

All quantities are in **ticks** and **lots**, so the kernel is integer-friendly.

| symbol | meaning | estimated by |
|---|---|---|
| σ | mid volatility, ticks/√s | std of 1-second mid changes |
| A, k | market order intensity λ(δ)=A·e^(−kδ) at depth δ from mid | log-linear fit of how deep market orders reach |
| λ(i), μ, θ(i) | CST limit, market and cancel rates at distance i from the opposite best quote | CST §3 estimators, in units of the average limit order size |
| γ, τ, α | risk aversion, horizon, CST skew strength | your choice (`--gamma --tau --alphas`) |

```
r      = mid − q·γσ²τ + α·(2·P_up(bid_q, ask_q) − 1)      (skew only when spread = 1 tick)
half   = γσ²τ/2 + ln(1 + γ/k)/γ
bid    = floor(r − half),  ask = ceil(r + half),  post-only clamped,  no quote beyond ±max_pos
```

`P_up` is the CST probability, computed by solving the absorption problem of the two
best-queue birth–death chains on a truncated grid. This is exact up to truncation; the
paper uses Laplace transforms instead. Running it on the simulator's own data recovers the
generating parameters (`tests/test_pipeline.py`).

## Backtest assumptions (`hftmodel/backtest.py`)

* Our orders have no market impact. The historical book is replayed unchanged.
* Feed latency and order latency are separate (default 50 µs each). A replaced order keeps working until its replacement arrives.
* A new order joins the back of the visible queue. Visible executions at our price consume the queue ahead.
  Cancels come from ahead with probability a³/(a³+b³), the power queue model (hftbacktest uses the same one).
* A trade or incoming order through our price fills us fully. Hidden executions at our price are ignored.
* Post-only orders that would cross on arrival are rejected. Fees are per share (`--maker-fee`, negative = rebate).
* Reported metrics: PnL marked to mid and PnL after flattening, fills, order-to-fill ratio, drawdown, and **markouts**
  (side·(mid(t+h) − fill price) in ticks). Markouts are the adverse selection check that matters most in HFT.

hftbacktest runs with `no_partial_fill_exchange`. In this setup, `partial_fill_exchange` aborted
with InvalidOrderStatus on LOBSTER-derived L2 data. Expect the two engines to agree
in direction and order of magnitude, not to the cent: they sample quotes differently and treat partial fills differently.

## Adverse selection study (`scripts/experiment_adverse.py`)

```bash
.venv/bin/python scripts/experiment_adverse.py --data data/lobster --tickers INTC MSFT
```

Calibration runs 09:35–10:35, the parameter search runs 10:35–13:15 (one config shared by both tickers),
and the reported test runs 13:15–15:55. Results on the 2012-06-21 samples, 100-share quotes, 50 µs latency:

| test window | INTC PnL (fills) | MSFT PnL (fills) |
|---|---|---|
| A-S baseline | −$122 (296), t −5.6 | −$190 (471), t −6.3 |
| A-S + CST skew α=0.5 | −$51 (83) | −$26 (52) |
| imbalance gate 0.5 (best on validation) | −$60 (34) | +$11 (16), t +1.2 |
| baseline with $0.002/share rebate | −$76 | −$119 |

Findings:
* Almost every fill is an execution at our price as the queue in front of us is wiped out. About 80% happen when
  our side's queue is the smaller one. Markouts go from +0.5 tick at the fill to −0.3 tick after 1 s.
* The imbalance gate and the CST skew cut losses mainly by trading less. The gate has a cliff: at 0.55 it barely
  changes anything, at 0.5 the strategy trades only a few dozen times, and below that it does not trade.
* The empirical P_up table did not beat the CST table, and keeping orders behind the desired price
  (`keep_behind_ticks`) made things worse.
* There is no robust edge here. Joining the back of the queue on large-tick stocks is structurally negative. The only positive
  test result (MSFT, 16 fills) is not significant, and this is a single day.

The gate stays in the kernel (`ASParams.gate`, 2 bits per LUT entry, bit-exact in hardware). It is the cheapest
risk control. Promising next steps: more days of data, a queue-position model that keeps
early priority (several resting orders per side), and short-horizon signals beyond the best level.

## FPGA path (`hftmodel/fpga.py`, `fpga/`)

`as_cst_quote_fx` is the hardware specification. It uses Q16 fixed point, constant σ and τ, queue
binning by reciprocal multiply instead of division, and one 32×32 LUT holding α·(2P_up−1). The Vitis HLS
engine in `fpga/hls/` implements it with a single AXI-Stream in and out. Parameters and the LUT are
loaded as config packets, so on the KV260 it only needs an AXI DMA. See **[fpga/README.md](fpga/README.md)**
for the full KV260 flow (HLS → Vivado → PYNQ).

```bash
.venv/bin/python scripts/export_fpga_vectors.py --data data/lobster --ticker INTC   # exports + runs C++ csim
```

The export reports two float-vs-fixed numbers. The *arithmetic* mismatch must be 0. The *quantization*
mismatch (a few %, ≤1 tick) comes from states where the float quote sits exactly on a tick boundary and a skew
below Q16 resolution tips it over. These differences are expected.
