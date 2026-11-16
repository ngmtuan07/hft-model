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

## The pitch: how a high-frequency market maker makes (or loses) money

```bash
.venv/bin/python scripts/edges.py --stocks INTC MSFT AAPL     # tries every edge below
.venv/bin/python scripts/pitch_figures.py                     # output/pitch/fig1..fig4
```

Every result is out of sample. Calibration runs 09:35–10:35, parameter selection 10:35–13:15, and the report
13:15–15:55, on INTC and MSFT for 2012-06-21 with 100-share quotes.

1. **The problem: adverse selection** (`fig1`). A market maker earns half the spread at the fill (+0.5 tick).
   Within a second the price has moved against it (−0.3 tick), because it mostly gets filled just before the price moves.
   Avellaneda–Stoikov loses −$122 (INTC) and −$190 (MSFT).
2. **The edge: queue priority** (`fig2`). Quote the same way (improve the spread by a tick), but only where the
   queue is empty, so we are first in line. Adverse selection disappears: the 1 s markout goes from −0.29 to about 0. PnL goes to about
   break-even gross (−$1 / −$31), and to +$65 / +$69 with an assumed $0.002/share maker rebate, positive in both
   the validation and the test windows. Posting at the same prices *behind* others loses the most (−$226 / −$322).
3. **Why speed matters** (`fig3`). Our share of fills where we were first falls from 98% at 1 µs to 19% at 20 ms, and the
   edge turns negative at about 1 ms. Queue position is decided in microseconds, and that is what an FPGA buys.
4. **Signals tell the market maker when to step back** (`fig4`). Market-order signs are strongly persistent
   (lag-1 autocorrelation 0.59 in all three stocks, as order-splitting theory predicts), and SPY moves before its members
   (stocks catch up in about 6–15 ms). Pulling the threatened quote on either signal cuts losses: by 35–60% for order flow
   and 30–70% for SPY, more than a wrong-side placebo does in every case. Neither pays to *take*: the move is smaller than the spread.

Edges that tested poorly and stay out of the main story, but make useful side notes:
* **Imbalance/CST as a taker signal:** right about direction about 62% of the time per event, but it cannot pay the spread.
* **Microprice:** out-of-sample R² of only 0.01–0.04 for the 1–10 s mid change. The naive imbalance-weighted mid does
  *worse* than the plain mid at 1 s (R² −0.6 to −1.0) because it over-reacts.
* **AAPL (wide spread, ~19 ticks):** being first is cheap (a 1-tick improvement), so it protects nothing. Pennying
  loses heavily. Also, 35% of AAPL volume executes against hidden orders (INTC 11%, MSFT 9%).

Caveats for the audience: one day of free sample data; the rebate is an assumption (tier-dependent);
replaying history cannot model other traders reacting to our orders. "First in line at 1 µs" means
ahead of the participants recorded in 2012, not that a 1 µs firm wins today.

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

## Taker test (`scripts/experiment_taker.py`)

The question: can the queue-imbalance / CST signal pay the spread? When the spread is one tick and P_up > θ,
we buy at the ask (IOC after 50 µs); when P_up < 1 − θ, we sell at the bid. One position at a time, 100 shares,
and an assumed taker fee of $0.003/share/side. θ, the horizon and the signal (CST, empirical, raw imbalance) are chosen on
10:35–13:15 and reported on 13:15–15:55.

| test window | trades | exit at mid (gross) | exit crossing (net of fees) |
|---|---|---|---|
| INTC, CST θ=0.6, 10 s | 115 | +0.27 tick, t +3.4 | −0.83 tick, t −10.6 |
| MSFT, empirical θ=0.9, 10 s | 222 | +0.04 tick, t +0.6 | −1.06 tick, t −16.5 |

* The signal is real. Counted per book update, it calls the next mid move correctly 87–92% of the time at θ = 0.9.
  Counted per tradable event, the hit rate drops to about 62%. Latency from 0 to 50 µs barely changes this.
* It cannot pay for crossing. A one-tick round trip plus fees costs about 1.6 ticks. The best signal earns about 0.3 tick over 10 s.
* The INTC mid-exit result (+0.27 tick, t 3.4) is the only significant positive edge found so far. It suggests a
  hybrid: take on strong signals and exit passively. Whether the passive exit fills without new adverse
  selection is untested.

## SPY → stock lead-lag (`scripts/leadlag.py`)

```bash
scripts/download_lobster_sample.sh SPY 30      # free SPY sample covers 09:30-10:30 only
.venv/bin/python scripts/leadlag.py --stocks INTC MSFT
```

Data: 09:35–10:30 on 2012-06-21, NASDAQ only. In-sample is the first half and out-of-sample the second. Output is `output/leadlag.png`.

| | INTC | MSFT |
|---|---|---|
| SPY mid changes vs stock mid changes (55 min) | 7,375 vs 752 | 7,375 vs 896 |
| SPY moved in the stock's direction in the 100 ms *before* a stock move | +0.35 SPY bps | +0.33 SPY bps |
| stock moved in SPY's direction in the 100 ms before a SPY move | +0.05 bps | +0.05 bps |
| stock follow-through after a SPY mid move: 10 ms / 100 ms / 1 s | 0.06 / 0.18 / 0.25 bps | 0.08 / 0.18 / 0.34 bps |
| P(stock's next move follows a ≥2-tick SPY move within 5 ms), median wait | 0.70, 14 ms | 0.70, 15 ms |
| taking the stale quote (out of sample, ~20 trades), exit at mid | −0.30 tick | −0.17 tick |

* SPY leads. Stocks move after SPY far more than SPY moves after stocks, and the stock's quote takes
  roughly 10–15 ms to catch up with a 2-tick SPY move. That's comparable to the ~7 ms futures-vs-SPY arbitrage window
  Budish, Cramton & Shim (2015) report for 2011.
* It can't be taken profitably on these names. A 1-tick SPY move (0.74 bps) moves INTC/MSFT by about 0.25 bps,
  while crossing their 1-tick spread costs about 1.7 bps. The latency curve is flat from 0 to 1 ms because the edge is
  smaller than the spread at any speed.
* The likely uses are defensive (cancel market-making quotes when SPY moves) or small-tick names such
  as AAPL, where the spread is smaller relative to the move.

## Defensive SPY signal and AAPL (`scripts/experiment_defend.py`, `hftmodel/signals.py`)

```bash
scripts/download_lobster_sample.sh AAPL 10
.venv/bin/python scripts/experiment_defend.py --stocks INTC MSFT AAPL
.venv/bin/python scripts/leadlag.py --stocks AAPL INTC MSFT --out output/leadlag_aapl
```

When SPY moves, the market maker cancels the stock quote that is about to be run over: the bid on a SPY drop,
the ask on a rise. Decisions happen at SPY time: `signals.with_signal` inserts signal-only rows so the engine can
cancel before the stock's next message, with the usual latency applied. One shared rule was chosen on 09:35–10:02
(SPY moves ≥1 tick within 20 ms → pull the threatened side for 100 ms) and tested on 10:02–10:30:

| out of sample | baseline | **defend** | placebo: pull the *wrong* side, same schedule |
|---|---|---|---|
| INTC | −$31 (80 fills) | **−$18** (23) | −$26 (56) |
| MSFT | −$62 (152) | **−$20** (82) | −$66 (109) |
| AAPL | −$609 (326), 1 s markout −1.08 tick | **−$412** (261), markout −0.55 | −$597 (316), markout −1.00 |

* The SPY direction carries real information. Pulling the wrong side on the same schedule does about as badly as
  doing nothing. On AAPL, adverse selection per fill halves. It still doesn't turn the market maker profitable.
* The benefit is flat in latency from 1 µs to 5 ms, because the chosen signal works on a 20–100 ms scale.
* Taking AAPL's stale quote also loses: −0.8 bps at mid and −2.3 bps after crossing, on 15 out-of-sample trades. It's the
  only name where the race shows up. AAPL catches up to SPY in about 6 ms vs 14 ms for INTC, and the share of
  stale quotes still there when our order arrives falls from 65% at 0 µs to 52% at 5 ms.

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
