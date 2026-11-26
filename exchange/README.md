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
make linux      # Linux build + tests in Docker (LINUX_IMAGE=any image with g++ and make)
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
               [--keep-ticks 1] [--core N] [--rt PRIO] [--mlock] [--busy-poll US] [--kernel-ts]
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

## Phase 2: the CPU contestant on Linux

### What changed

| | before | now |
|---|---|---|
| Trader sockets | separate market-data and order sockets, polled in turn | **one socket** for market data, orders and replies: one receive call per loop |
| Receive | one `recv` per datagram | `recvmmsg` batch on Linux |
| Send | one `sendto` per order | cancel + new order in **one `sendmmsg`**, on a connected socket (no per-send address lookup) |
| Hot path | build each message on the fly | decide, fill fixed 32-byte buffers, one send. No allocation, no locks, no logging |
| Arrival time at the exchange | when the exchange loop read the packet | optional **kernel receive timestamp** (`--kernel-ts`), so the exchange's own loop is not charged to the contestant |
| Fairness | send order rotated by sequence number | rotated by a **market-event counter**. The old rotation always served the same contestant first, because each market event is followed by contestant-caused updates (this showed as a 57/43 split on Linux) |
| Self-measurement | none | the trader reports its tick-to-trade (kernel receive timestamp → orders handed to the kernel), kernel → user time and send-syscall cost |

Latency switches, the same for `trader`, `exchange run` and `udp_pingpong`:

| flag | effect | notes |
|---|---|---|
| `--core N` | pin to a CPU | pair with `isolcpus`/`nohz_full` |
| `--busy-poll US` | `SO_BUSY_POLL`: the kernel spins on the NIC queue | the exchange defaults to 50 |
| `--kernel-ts` | kernel receive timestamps; the whole process switches to `CLOCK_REALTIME` | in the trader: tick-in = kernel timestamp |
| `--rt PRIO` | `SCHED_FIFO` | **only on isolated cores**: on a shared core a spinning real-time thread starves the kernel's network work (p99 went from 3 µs to 65 µs in our VM) |
| `--mlock` | lock all memory: no page faults on the hot path | needs an unlimited memlock limit |

`tools/linux_tune.sh` checks a host against everything that matters for tail latency: CPU isolation boot parameters,
irqbalance and NIC interrupt placement, frequency governor and idle states, busy-poll sysctls, NIC interrupt coalescing
and GRO/LRO, transparent hugepages, socket buffers, memlock and real-time safety. It's a dry run by default.
`sudo tools/linux_tune.sh --apply --iface eth1` applies the runtime items. It never edits boot settings; it prints the kernel command line to add instead.

`tools/udp_pingpong` measures the network floor with the same socket options (`--local`, or `--server`/`--client`
across two machines). Run it before racing: no contestant can beat it.

### Measured (Linux 7.0 in Docker on the M1, loopback, 8 vCPUs)

`make linux` builds and runs the tests in Docker. The races are `scripts/race.sh` with `BIN=build-linux` and the flags shown,
on AAPL from 10:00, 30 s at 10× speed:

| | median reaction | p99 | p99.9 | race split |
|---|---|---|---|---|
| macOS, for reference | 62–78 µs | 230–690 µs | | |
| Linux, no tuning | 7.4–7.7 µs | 57–90 µs | 1.4–2.6 ms | 55 / 45 (identical engines) |
| Linux, exchange + both contestants tuned (`--core --busy-poll 50 --kernel-ts`) | **2.4–2.8 µs** | **11–14 µs** | **20–36 µs** | 48 / 52 (identical engines) |
| tuned vs untuned contestant | 2.8 vs 2.6 µs | 23 vs 16 µs | **139 µs vs 5.4 ms** | 48 / 52 |

Inside the tuned trader: deciding takes 83–211 ns. Kernel receive → orders handed back to the kernel takes ~1.1 µs at the median.
Loopback UDP round trip: 2.6 µs median, with p99 2.9 µs once busy polling is on (27 µs without).

What this shows:

* **The median is now set by the kernel network stack, not by our code.** Reaction is ~2.5 µs against a 2.6 µs loopback
  round trip, and our own decision is ~0.1–0.2 µs of that.
* **Tuning buys the tail.** p99.9 drops from milliseconds to tens of µs. On this idle VM an untuned process
  gets the same median, because nothing competes for its core. On a loaded host, pinning and isolation keep the median too.
* **This is a VM.** The remaining p99/p99.9 is mostly hypervisor scheduling noise. Bare metal with `isolcpus` and
  the tuning script applied is the real test.

### Next steps (need Linux hardware)

1. **Two machines, real NICs.** Exchange on its own box (`--host 0.0.0.0`), contestant on another, equal cable lengths,
   both tuned with `tools/linux_tune.sh`. Measure the floor with `udp_pingpong --server/--client` first.
2. **Kernel bypass, the biggest remaining step** (~1 µs per direction → a few hundred ns):
   * **OpenOnload** on AMD/Solarflare NICs: `onload --profile=latency ./build/trader ...`. No code change; it
     intercepts the socket calls this code already uses.
   * **AF_XDP** on most modern NICs (Intel, Mellanox): a new transport behind `net.hpp`'s two functions.
   * **DPDK or ef_vi**: lowest latency, the most work.
3. **NIC hardware timestamps** at the exchange (`SO_TIMESTAMPING` RX/TX hardware) for judging with nanosecond accuracy.

## Phase 3: FPGA contestant

It implements the same 32-byte messages: send one fixed `Hello`, parse `0x10`/`0x11` `Tob`s (offsets 4–31), run the
quote logic, and send `NewOrder`/`Cancel` with `trigger_seq` copied from the `Tob`. The strategy is the same as
`src/trader.cpp`. It can use the scoreboard's latency slider without any FPGA changes.

**Known limits.** The exchange is single-threaded, and each `sendto` costs ~3 µs on macOS (~1 µs on Linux), so very high replay
speeds or many contestants can delay the feed. Use `SPEED` ≤ 30 on macOS.
