# Quote engine on the Kria KV260

```
PS (Linux/PYNQ, DDR4) ──AXI HP0──► AXI DMA ──MM2S 256b AXI-Stream──► quote_engine ──128b AXI-Stream──► AXI DMA ──► DDR4
                                                                     (HLS, ap_ctrl_none, II=1)
```

The engine is the Vitis HLS version of `hftmodel.strategy.as_cst_quote_fx`. Parameters and the
CST skew table are loaded as config packets on the same stream as the book updates, so the
design needs no AXI-Lite register map (packet format: `hftmodel/fpga.py`).

| file | what |
|---|---|
| `hls/quote_engine.{hpp,cpp}` | synthesizable engine: `quote_core` (datapath) + `quote_engine` (AXI-Stream top) |
| `hls/hls_compat.hpp` | uses `ap_int`/`hls::stream`/`ap_axiu` when Vitis headers exist, plain-C++ shims otherwise |
| `hls/tb_quote_engine.cpp` | bit-exact testbench over `stream_in.hex` → `stream_out.hex` |
| `hls/run_hls.tcl` | Vitis HLS: csim → csynth → (cosim with `COSIM=1`) → IP export, part `xck26-sfvc784-2LV-c`, 300 MHz |
| `vivado/kv260_bd.tcl` | Vivado block design: Zynq MPSoC + AXI DMA + engine → `overlay/quote_engine.{bit,hwh}` |
| `pynq/kv260_quote_engine.py` | on-board check, throughput and round-trip timing under Kria-PYNQ |

## Datapath (all integer, verified bit-exact)

```
x, y   = clip((min(q, 2^24-1) * Q_RECIP + 2^23) >> 24, 1, 31)        queue bins, no divider
r      = (bb + ba) << 15  -  inv * C_INV  +  LUT[(x << 5) | y]       48-bit, Q16 ticks (skew only if spread = 1)
bid    = (r - C_HALF) >> 16        ask = -((-(r + C_HALF)) >> 16)    floor / ceil
         post-only clamps, inventory gating
```

Resources: three small multipliers (DSP48E2), one 32×32×32-bit table (1 BRAM), and adders and compares.
Prices must stay below 2²⁴ ticks. The export script checks this.

## Flow

```bash
# 1. vectors (on the Mac/PC)
.venv/bin/python scripts/export_fpga_vectors.py --data data/lobster --ticker INTC
make -C fpga/hls csim                                   # plain C++ csim, no Vitis needed

# 2. HLS (machine with Vitis HLS 2022.2+)
make -C fpga/hls hls VEC=$PWD/output/fpga_vectors       # read the csynth report for latency / II / resources
#    for cosim, export ~2k vectors first: --max-vectors 2000, then COSIM=1 make -C fpga/hls hls ...

# 3. bitstream (Vivado with Kria board files)
vivado -mode batch -source fpga/vivado/kv260_bd.tcl

# 4. on the KV260 (Kria-PYNQ): copy fpga/pynq/, fpga/vivado/overlay/, output/fpga_vectors/ to the board
sudo -E python3 kv260_quote_engine.py --overlay overlay/quote_engine.bit --vectors fpga_vectors
```

Status: the Python golden model, the vector export and the C++ csim are verified here
(0 mismatches on 101,025 INTC packets). Steps 2–4 need the AMD tools and the board. They have
not been run yet. The Tcl may need IP or board-file version tweaks for your Vivado release.

## What the KV260 can and cannot measure

* **Can:** cycles per update and initiation interval (csynth report, ILA), throughput through DMA,
  and bit-exact behaviour on a full LOBSTER day.
* **Cannot:** wire-to-wire tick-to-trade. The board's only Ethernet port is wired to the PS, not the PL.
  For that, port this design to the KR260 (same K26 SOM, SFP+ to the PL) and add a 10G MAC,
  an ITCH parser and a book builder in front of `quote_engine`.
