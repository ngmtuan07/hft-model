# Exchange box (phase 1)

A C++ exchange for racing trading engines against each other. It replays a real NASDAQ day
(LOBSTER) as background market flow. Contestants (the CPU engine, later the FPGA) trade against it over UDP,
and the exchange judges who reacts first.

```
 LOBSTER day ──► replayer ──► matching engine ──► Tob feed (UDP) ──► contestant A, B, ...
                              (price-time,    ◄── orders (UDP)   ◄──
                               post-only/IOC)  ──► exec reports ──►
                                   │
                                   └─► judge: exchange-clock latency, race wins, fills, fill quality
```

## Build and run

```bash
make            # build/exchange, build/trader, build/test_book   (C++17, no dependencies)
make test       # matching-engine unit tests
make verify     # replay a day with no contestants, compare against LOBSTER's own book
make bench      # matching-engine throughput and per-message latency
make race       # exchange + two CPU contestants (one with a 20 µs handicap) on this machine
./scripts/demo.sh   # live demo: two identical CPU contestants on AAPL, scoreboard at http://localhost:8080
```

`scripts/race.sh` is configured with environment variables: `MSG` (LOBSTER message file),
`START`, `SPEED` (history replay speed), `DUR` (seconds), `A_DELAY_NS` / `B_DELAY_NS` (an artificial
handicap per contestant), `OUT`, and `ORDER_LOG=1` for a per-order CSV. Outputs: `summary.json`, `stats.jsonl`
(one scoreboard line per second, ready for a live dashboard) and the logs.

```bash
./build/exchange run --messages FILE [--start 10:00:00] [--speed 1] [--duration 60] [--wait 2]
                     [--port 9000] [--stats f.jsonl] [--summary f.json] [--order-log f.csv] [--core N]
./build/trader --name cpu --exchange 127.0.0.1:9000 [--delay-ns 0] [--qty 100] [--max-pos 500]
               [--keep-ticks 1] [--core N]
```

## Live scoreboard (`ui/scoreboard.html`, `include/http.hpp`)

Start the exchange with `--http 8080` (or run `./scripts/demo.sh`) and open http://localhost:8080.
It shows:

* **Races won.** The headline is a share bar for all contested races plus a bar for the last 100 races. Each contestant keeps its color and side.
* **Per contestant:** races won, median and p99 reaction time, orders filled, fills per second (10 s rolling) and first-in-line fills.
* **Latency distribution:** each contestant's share of reactions per log-scale bin (100 ns to 10 ms), with p50 and p99 markers.
* **Latency over time:** rolling 1 s median and p99 for the last 90 s, on a log scale.
* **Extra latency slider** per contestant (0 µs to 5 ms). The exchange holds that contestant's orders for the chosen time before
  matching, like adding cable length. It works the same for a CPU or FPGA contestant, and the races and latency it records include the delay.
* **Reset stats** and a table view.

The page is one self-contained file: no external libraries or fonts, so it works offline. It follows the system light/dark
setting. Data comes over Server-Sent Events (`/events`), with `/snapshot` polling as a fallback. Control is
`/control?c=<id>&extra_us=<n>` and `/control?reset=1`. The web server runs on its own threads. The trading loop only publishes a
snapshot string every 200 ms and reads the slider values.

## Wire protocol (`include/proto.hpp`)

Every message is one UDP datagram of exactly **32 bytes**, little-endian, with fixed offsets. That
keeps the FPGA parser to a few registers and comparators. Prices are ticks ($0.01) and quantities are shares.

| type | direction | message | fields after `type:u16, len:u16` |
|---|---|---|---|
| `0x01` | → exchange | Hello | `md_port:u16, name:char[26]` |
| `0x02` | → exchange | NewOrder | `coid:u32, trigger_seq:u32, price:u32, qty:u32, side:u8 (1 buy, 2 sell), tif:u8 (0 GTC, 1 IOC), post_only:u8, pad:u8, client_ts:u64` |
| `0x03` | → exchange | Cancel | `coid:u32, trigger_seq:u32, pad[12], client_ts:u64` |
| `0x10` | → contestant | Tob, caused by market flow | `seq:u32, exch_ts:u64, bid:u32, bid_qty:u32, ask:u32, ask_qty:u32` |
| `0x11` | → contestant | Tob, caused by a contestant | same layout |
| `0x81`..`0x85` | → contestant | Ack / Reject / Fill / Cancelled / HelloAck | `coid:u32, price:u32, qty:u32, leaves:u32, reason:u32, exch_ts:u64` |

A contestant sends `Hello` with the UDP port it wants market data on. The exchange registers it by
source address and port. `trigger_seq` is the sequence number of the `Tob` the order reacts to.

**The race rule.** Contestants make decisions only on `0x10` updates (market flow) and treat `0x11` as book
updates only. Every race is then a reaction to the *same* market event, and contestants can't chase
each other into a feedback loop. Without this rule, two penny-improving engines on AAPL turned 11,000
historical messages into 340,000 book updates.

## Replay model

* History is the background participant. Contestant orders sit in the same book with real price-time
  priority, so contestants trade with the historical flow, and a contestant first at a price gets filled first.
* A historical execution trades the order history says it did, unless a contestant had priority over it in
  our book (a better price, or ahead in the queue), in which case the contestant trades first.
* **Snapshot reconciliation.** LOBSTER's message stream doesn't report reserve (iceberg) refills and similar
  changes, so after each message the background quantity at the touched price and at the historical
  best bid/ask is snapped to LOBSTER's own book snapshot. Contestant orders are never changed.
  This is needed on 0.2% of messages for INTC/MSFT and 3% for AAPL (icebergs).
* History is replayed in batches: messages that share a timestamp form one exchange event and produce at most one
  `Tob`. Order entry is polled between every batch.

## Judging and metrics

All timing uses the exchange's clock. Contestants' own clocks are never trusted.

* **Reaction latency:** time the triggering `Tob` was sent to that contestant → arrival of the contestant's first
  order or cancel for it. This is the contestant's tick-to-trade plus both network legs. Reported as p50/p99/p99.9/max.
* **Races:** for every update that two or more contestants reacted to, the earliest arrival wins.
* **Fills:** `fills`, `fills_per_s` and `fill_qty`. On large-tick names both engines often get filled, because a trade
  usually exceeds one 100-share order, so speed shows up in **`first_in_line_fills`** (fills on orders that were
  first at their price when they arrived).
* **Fill quality:** `markout_1s_ticks_per_share`, where the mid price is 1 s of market time after each fill.
* **Fairness:** each `Tob` is sent to every contestant, rotating who is sent first.

## Results (Apple M1, macOS, everything on one machine)

Correctness: replaying each full day with no contestants reproduces LOBSTER's top of book exactly.

| | messages | top of book identical |
|---|---|---|
| INTC | 624,039 | 100.0000% |
| MSFT | 668,764 | 100.0000% |
| AAPL | 400,390 | 100.0000% |

Matching engine (`make bench`, one core, including reconciliation): **26–28 M messages/s,
36–40 ns per message**. p99 is 125–167 ns and p99.9 is 251 ns; these include about 40 ns of timer overhead. Without reconciliation it reaches 41 M/s
(24 ns per message).

Races at 10× replay speed from 10:00, 30 s each. "Fast" has no handicap; "slow" waits an extra 20 µs per decision:

| | races won | fills | first-in-line fills | reaction p50 |
|---|---|---|---|---|
| INTC fast vs slow | **18 / 18** vs 0 | 37 vs 30 | **34** vs 4 | 62 µs vs 94 µs |
| AAPL fast vs slow | **217 / 227** vs 10 | **296** vs 164 | **280** vs 50 | 72 µs vs 106 µs |
| AAPL identical engines | 94 vs 85 (52.5% / 47.5%) | 225 vs 231 | 166 vs 171 | 74 µs vs 76 µs |

The ~70 µs median reaction time is mostly macOS's UDP stack: a bare loopback round trip on this Mac is 86 µs.
Deciding takes nanoseconds, and each `sendto` costs ~3 µs.

## Toward phases 2 and 3

* **Linux and real NICs.** Run the exchange on its own box with one 10G port per contestant and equal-length cables.
  Timestamp arrivals with the NIC (`SO_TIMESTAMPING`, hardware), pin the exchange (`--core`) to an isolated CPU,
  and set `SO_BUSY_POLL` (already set where available). Kernel bypass (AF_XDP, DPDK, ef_vi) is next for both the
  exchange and the CPU contestant.
* **FPGA contestant.** It implements the same 32-byte messages: send one fixed `Hello`, parse `0x10`/`0x11` `Tob`s
  (offsets 4–31), run the quote logic, and emit `NewOrder`/`Cancel` with `trigger_seq` copied from the `Tob`.
  The strategy is the same as `src/trader.cpp`.
* **Known limits.** The exchange is single-threaded, and each `sendto` costs ~3 µs on macOS, so very high replay
  speeds or many contestants can delay the feed. Use `SPEED` ≤ 30 here, or move to Linux or kernel bypass.
