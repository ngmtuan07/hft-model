// Avellaneda-Stoikov + Cont-Stoikov-Talreja quote engine, bit-exact with
// hftmodel.strategy.as_cst_quote_fx (the Python golden model).
//
// Stream protocol (one output packet per input packet), see hftmodel/fpga.py:
//   in  (256b): w0 = {seq[31:8], type[7:0]}
//       type 0 BOOK : w1 bid_tick, w2 ask_tick, w3 bid_qty, w4 ask_qty, w5 inv_lots
//       type 1 PARAM: w1 C_INV, w2 C_HALF, w3 Q_RECIP, w4 MAX_POS
//       type 2 LUT  : w1 addr = (x << 5) | y, w2 = (skew_Q16 << 2) | gate (bit0 bid ok, bit1 ask ok)
//   out (128b): w0 header echo, w1 bid_tick, w2 ask_tick (-1 = no quote), w3 = 0
#pragma once
#include "hls_compat.hpp"

constexpr int QE_FRAC = 16;         // fractional bits (hftmodel.strategy.FX_FRAC)
constexpr int QE_RECIP_SHIFT = 24;  // queue quantisation shift (RECIP_SHIFT)
constexpr int QE_LUT_BITS = 5;      // LUT is 32 x 32 (LUT_BITS)
constexpr int QE_NQ = (1 << QE_LUT_BITS) - 1;
constexpr int32_t QE_QTY_MAX = (1 << 24) - 1;

enum { QE_TYPE_BOOK = 0, QE_TYPE_PARAM = 1, QE_TYPE_LUT = 2 };

struct QuoteParams {
    int32_t c_inv;    // gamma*sigma^2*tau, Q16 ticks per lot
    int32_t c_half;   // optimal half spread, Q16 ticks
    int32_t q_recip;  // round(2^24 / queue unit in shares)
    int32_t max_pos;  // inventory limit, lots
};

struct Quote {
    int32_t bid, ask;
};

// Pure combinational core (also used by the testbench for CPU timing).
Quote quote_core(int32_t bb, int32_t ba, int32_t bq, int32_t aq, int32_t inv, const QuoteParams& p,
                 const int32_t lut[1 << (2 * QE_LUT_BITS)]);

// Synthesis top: free-running AXI-Stream engine (ap_ctrl_none), one packet per clock.
void quote_engine(hls::stream<pkt_in_t>& s_in, hls::stream<pkt_out_t>& s_out);
